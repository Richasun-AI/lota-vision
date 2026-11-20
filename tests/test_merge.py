"""子项目合并：B 变成 A 的标注任务，图片 / 类别 / 人工标签并进 A，B 停用、目录不动"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from lota import catalog
from lota.merge import merge
from lota.store import Scenario, Store, Workflow


def _scenario(name, classes, sources, **extra):
    return Scenario.model_validate({"name": name, "classes": classes, "sources": sources, **extra})


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.store = Store(root / "runtime")
        self.engine = SimpleNamespace(store=self.store, save=self.store.save_project)
        self.photos_a, self.photos_b = root / "pa", root / "pb"
        self.photos_a.mkdir(), self.photos_b.mkdir()
        Image.new("RGB", (64, 48), "red").save(self.photos_a / "shared.png")
        Image.new("RGB", (64, 48), "red").save(self.photos_b / "shared_copy.png")   # 同一张图，两边都有
        Image.new("RGB", (64, 48), "blue").save(self.photos_b / "only_b.png")
        self.store.save_project("telecom", _scenario("Telecom", ["tower"], [{"name": "src", "path": str(self.photos_a)}],
                                                     categories={"tower": {"name": "铁塔"}, "gate": {"name": "大门"}}))
        self.store.save_project("weeds", _scenario("示例 B", ["weeds", "grounding_bar"],
                                                   [{"name": "src", "path": str(self.photos_b)}],
                                                   categories={"weeds": {"name": "野草"}, "gate": {"name": "门"}},
                                                   annotation_tasks={"bars": {"name": "接地排", "task": "detect",
                                                                              "classes": ["grounding_bar"]}}))
        self.a, self.b = self.store.project_dir("telecom"), self.store.project_dir("weeds")
        catalog.scan(self.a, [{"name": "src", "path": str(self.photos_a)}], stability_seconds=0)
        catalog.scan(self.b, [{"name": "src", "path": str(self.photos_b)}], stability_seconds=0)
        self.ids = {Path(x["aliases"][0]["path"]).name: x["image_id"] for x in catalog.list_images(self.b)}
        catalog.extract(self.b, list(self.ids.values()))                       # B 两张都抽取过，A 一张都没抽
        # B 的标注、数据集（data.yaml 里有 B 的绝对路径）、模型权重
        (self.b / "annotations").mkdir()
        with closing(sqlite3.connect(self.b / "annotations" / "index.sqlite3")) as db, db:
            db.execute("CREATE TABLE current_annotations(task, image_id, revision)")
            db.execute("INSERT INTO current_annotations VALUES ('detect', ?, 1)", (self.ids["only_b.png"],))
        (self.b / "datasets" / "v1").mkdir(parents=True)
        (self.b / "datasets" / "v1" / "data.yaml").write_text(f"path: {self.b / 'datasets' / 'v1'}\nraw: {self.b / 'raw'}\n", encoding="utf-8")
        (self.b / "models" / "v1").mkdir(parents=True)
        (self.b / "models" / "v1" / "weights.pt").write_bytes(b"w" * (2 << 20))
        (self.b / "annotation-tasks" / "bars" / "annotations").mkdir(parents=True)
        (self.b / "annotation-tasks" / "bars" / "annotations" / "note.json").write_text(
            json.dumps({"dir": str(self.b / "annotation-tasks" / "bars")}), encoding="utf-8")
        # 人工类别：同一张图 A 说 tower、B 说 weeds → 以 A 为准并列出冲突
        shared = self.ids["shared_copy.png"]
        for d, cat in ((self.a, "tower"), (self.b, "weeds")):
            with closing(sqlite3.connect(d / "catalog" / "learning.sqlite3")) as db, db:
                db.execute("CREATE TABLE labels(image_id TEXT PRIMARY KEY,category TEXT NOT NULL,updated TEXT)")
                db.execute("CREATE TABLE history(image_id TEXT,category TEXT,updated TEXT)")
                db.execute("INSERT INTO labels VALUES (?,?,'t')", (shared, cat))
        with closing(sqlite3.connect(self.b / "catalog" / "learning.sqlite3")) as db, db:
            db.execute("INSERT INTO labels VALUES (?,?,'t')", (self.ids["only_b.png"], "weeds"))
        self.store.save_workflow("weeds", "train", Workflow.model_validate(
            {"name": "训练", "steps": [{"id": "pull", "tool": "labels.pull"}, {"id": "scan", "tool": "catalog.scan"}]}))

    def test_dry_run_changes_nothing(self):
        before = (self.store.project("telecom")["revision"], self.store.project("weeds")["revision"])
        report = merge(self.engine, "telecom", "weeds", dry_run=True)
        self.assertEqual(report["catalog"]["new"], 1)
        self.assertEqual(report["catalog"]["already_in_a"], 1)
        self.assertEqual(report["sorting"]["learning"]["conflicts"][0]["b"], "weeds")
        self.assertEqual(report["categories"]["conflicts_a_kept"][0]["code"], "gate")
        self.assertEqual((self.store.project("telecom")["revision"], self.store.project("weeds")["revision"]), before)
        self.assertFalse((self.a / "annotation-tasks").exists())
        self.assertEqual(len(catalog.list_images(self.a)), 1)
        self.assertEqual(sorted(p.name for p in (self.b / "catalog").iterdir()),
                         sorted(["catalog.sqlite3", "learning.sqlite3"] + [p.name for p in (self.b / "catalog").iterdir()
                                                                           if p.name.startswith("catalog.sqlite3-")]))

    def test_merge(self):
        report = merge(self.engine, "telecom", "weeds", dry_run=False)
        cfg = self.store.project("telecom")["config"]
        # B 变成标注任务，B 原有任务加前缀
        self.assertEqual(cfg["annotation_tasks"]["weeds"]["classes"], ["weeds", "grounding_bar"])
        self.assertEqual(cfg["annotation_tasks"]["weeds-bars"]["classes"], ["grounding_bar"])
        self.assertEqual(cfg["categories"]["gate"]["name"], "大门")                 # 冲突以 A 为准
        self.assertEqual(cfg["categories"]["weeds"]["name"], "野草")
        self.assertIn("grounding_bar", cfg["categories"])                          # B 的标签补了占位类别
        self.assertEqual([s["name"] for s in cfg["sources"]], ["src", "weeds-src"])
        # 图片：去重合并，原图副本硬链接过来
        self.assertEqual(len(catalog.list_images(self.a)), 2)
        for iid in self.ids.values():
            raw = catalog.get_image(self.a, iid).get("raw_path")
            self.assertTrue(raw and Path(raw).is_file() and Path(raw).is_relative_to(self.a / "raw"))
        # 文件：任务目录里有 B 的标注 / 数据集 / 模型，路径改写正确，大文件是硬链接
        task = self.a / "annotation-tasks" / "weeds"
        self.assertTrue((task / "annotations" / "index.sqlite3").is_file())
        yaml = (task / "datasets" / "v1" / "data.yaml").read_text(encoding="utf-8")
        self.assertIn(f"path: {task / 'datasets' / 'v1'}", yaml)
        self.assertIn(f"raw: {self.a / 'raw'}", yaml)
        self.assertNotIn(str(self.b), yaml)
        self.assertIn(str(self.b), (self.b / "datasets" / "v1" / "data.yaml").read_text(encoding="utf-8"))   # B 原文件不变
        self.assertTrue((task / "models" / "v1" / "weights.pt").samefile(self.b / "models" / "v1" / "weights.pt"))
        note = json.loads((self.a / "annotation-tasks" / "weeds-bars" / "annotations" / "note.json").read_text(encoding="utf-8"))
        self.assertEqual(note["dir"], str(self.a / "annotation-tasks" / "weeds-bars"))
        # 人工类别：A 的保留，B 独有的加进来
        with closing(sqlite3.connect(self.a / "catalog" / "learning.sqlite3")) as db:
            labels = dict(db.execute("SELECT image_id, category FROM labels"))
        self.assertEqual(labels[self.ids["shared_copy.png"]], "tower")
        self.assertEqual(labels[self.ids["only_b.png"]], "weeds")
        # 工作流搬过去，标注步骤绑定到任务
        wf = {w["id"]: w["definition"] for w in self.store.workflows("telecom")}
        steps = {s["id"]: s for s in wf["weeds-train"]["steps"]}
        self.assertEqual(steps["pull"]["args"]["annotation_task"], "weeds")
        self.assertNotIn("annotation_task", steps["scan"]["args"])
        # B：停用、改名、留记录，目录不动
        b = self.store.project("weeds")
        self.assertFalse(b["active"])
        self.assertTrue(b["config"]["name"].startswith("[已并入 telecom]"))
        self.assertTrue((self.b / "MERGED_INTO.json").is_file())
        self.assertTrue((self.b / "models" / "v1" / "weights.pt").is_file())
        with self.assertRaisesRegex(ValueError, "已经并入过"):
            merge(self.engine, "telecom", "weeds", dry_run=True)
        self.assertEqual(report["task_code"], "weeds")
        # Label Studio 旧任务的归属标记：任务里认 B 原来的标记，回收 / 查重才收得到合并前的任务
        from lota.labels import _matches_task, _project_names
        names = _project_names(task)
        self.assertEqual(names, {"telecom/weeds", "weeds"})
        self.assertEqual(_project_names(self.a / "annotation-tasks" / "weeds-bars"), {"telecom/weeds-bars", "weeds/bars"})
        old = {"data": {"ailabel_project": "weeds", "ailabel_task": "detect", "image_id": "x"}}
        other = {"data": {"ailabel_project": "telecom", "ailabel_task": "detect", "image_id": "y"}}
        self.assertTrue(_matches_task(old, names, "detect"))
        self.assertFalse(_matches_task(other, names, "detect"))
        self.assertTrue(_matches_task(old, "weeds", "detect"))                      # 单个标记的老用法照旧

    def test_refuses_self_and_busy(self):
        with self.assertRaisesRegex(ValueError, "并入它自己"):
            merge(self.engine, "telecom", "telecom")


if __name__ == "__main__":
    unittest.main()
