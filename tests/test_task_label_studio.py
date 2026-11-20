"""标注任务里送 / 回收 Label Studio：按子项目代码查配置（不是任务目录名），只用任务自己的连接，任务标签表不被换掉"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lota import flow  # noqa: F401  注册 _with_live_ls 包装
from lota.store import Scenario, Store
from lota.tasks import live_label_studio, project_id_of, task_context, task_id_of
from lota.tools import TOOLS, Context

LS = {"base_url": "http://127.0.0.1:8081", "project_id": 7, "token_env": "LABEL_STUDIO_TOKEN", "local_files_root": "/data"}


def _config(task_ls=None, project_ls=None):
    task = {"name": "Telecom Site inspection", "task": "detect", "classes": ["cctv"]}
    if task_ls:
        task["label_studio"] = task_ls
    return Scenario.model_validate({"name": "Telecom inspection", "classes": ["mcb"], "label_studio": project_ls,
                                    "annotation_tasks": {"telecom-site-inspection": task}})


class TaskLabelStudioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "runtime")
        self.engine = SimpleNamespace(store=self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _task_ctx(self, config):
        self.store.save_project("telecom-inspection", config)
        base = Context(self.store.project_dir("telecom-inspection"), config, self.engine, "job", "step")
        return task_context(base, "telecom-site-inspection")

    def _push(self, ctx):
        seen = {}
        orig = TOOLS["labels.push"]
        # 包装里调用的是注册时的原函数；换成记录参数的假函数
        for cell in orig.function.__closure__ or ():
            if isinstance(cell.cell_contents, type(orig)):
                real = cell.cell_contents
                fake = type(orig)(real.title, real.model, lambda c, **kw: seen.setdefault("c", c))
                cell.cell_contents = fake
                try:
                    orig.function(ctx, image_ids=["a"])
                finally:
                    cell.cell_contents = real
                return seen.get("c")
        raise AssertionError("labels.push 没有被包装")

    def test_ids(self):
        ctx = self._task_ctx(_config())
        self.assertEqual(project_id_of(ctx), "telecom-inspection")
        self.assertEqual(task_id_of(ctx), "telecom-site-inspection")

    def test_first_push_creates_project_for_the_task(self):
        ctx = self._task_ctx(_config(project_ls=LS))   # 子项目有连接，任务没有：不借子项目的，自动给任务建一个
        self.assertIsNone(live_label_studio(ctx))
        calls = []

        class FakeClient:
            def _request(self, method, path, payload=None, query=None):
                calls.append((method, path, payload))
                return {"id": 42}

        self.engine.save = self.store.save_project
        with mock.patch.object(flow, "_ls_client", lambda cfg: FakeClient()):
            c = self._push(ctx)
        self.assertEqual(c.scenario.label_studio.project_id, 42)
        self.assertEqual(c.scenario.classes, ["cctv"])                       # 任务标签没被子项目的换掉
        self.assertIn('value="cctv"', calls[0][2]["label_config"])          # LS 项目按任务标签建
        self.assertEqual(Path(calls[1][2]["path"]).parts[-2:], ("telecom-inspection", "raw"))   # 原图读子项目目录
        saved = self.store.project("telecom-inspection")["config"]
        self.assertEqual(saved["annotation_tasks"]["telecom-site-inspection"]["label_studio"]["project_id"], 42)
        self.assertEqual(saved["label_studio"]["project_id"], 7)            # 子项目默认连接不动
        self.assertEqual(saved["annotation_tasks"]["telecom-site-inspection"]["label_studio"]["base_url"], LS["base_url"])

    def test_pull_without_connection_explains(self):
        ctx = self._task_ctx(_config(project_ls=LS))
        orig = TOOLS["labels.pull"]
        with self.assertRaisesRegex(ValueError, "标注任务“telecom-site-inspection”还没绑定 Label Studio"):
            orig.function(ctx)

    def _fake_ls(self, existing):
        calls = []

        class FakeClient:
            def _request(self, method, path, payload=None, query=None):
                calls.append((method, path, payload))
                if method == "GET" and path.startswith("/api/projects/"):
                    if int(path.rstrip("/").split("/")[-1]) not in existing:
                        raise RuntimeError(f"Label Studio GET {path} returned HTTP 404")
                    return {"id": int(path.rstrip("/").split("/")[-1])}
                return {"id": 43}

        self.engine.save = self.store.save_project
        return calls, mock.patch.object(flow, "_ls_client", lambda cfg: FakeClient())

    def test_task_connection_is_used_and_classes_kept(self):
        ctx = self._task_ctx(_config(task_ls={**LS, "project_id": 9}, project_ls=LS))
        ctx.scenario = ctx.scenario.model_copy(update={"label_studio": None})   # 模拟任务开始时快照里还没有连接
        calls, patch = self._fake_ls(existing={9})
        with patch:
            c = self._push(ctx)
        self.assertEqual(c.scenario.label_studio.project_id, 9)
        self.assertEqual(c.scenario.classes, ["cctv"])
        self.assertEqual(c.scenario.task, "detect")
        self.assertEqual([m for m, *_ in calls], ["GET"])                       # 只核对，不新建

    def test_deleted_project_is_recreated_and_rebound(self):
        ctx = self._task_ctx(_config(task_ls={**LS, "project_id": 11}, project_ls=LS))
        calls, patch = self._fake_ls(existing=set())                           # #11 在 Label Studio 里被删了
        with patch:
            c = self._push(ctx)
        self.assertEqual(c.scenario.label_studio.project_id, 43)
        self.assertEqual(c.scenario.classes, ["cctv"])
        saved = self.store.project("telecom-inspection")["config"]
        self.assertEqual(saved["annotation_tasks"]["telecom-site-inspection"]["label_studio"]["project_id"], 43)
        self.assertEqual(saved["label_studio"]["project_id"], 7)

    def test_other_label_studio_errors_are_not_hidden(self):
        ctx = self._task_ctx(_config(task_ls={**LS, "project_id": 9}))

        class Down:
            def _request(self, *a, **k):
                raise RuntimeError("Label Studio GET /api/projects/9 returned HTTP 401")

        with mock.patch.object(flow, "_ls_client", lambda cfg: Down()), self.assertRaisesRegex(RuntimeError, "401"):
            self._push(ctx)


class AutoExtractTest(unittest.TestCase):
    def test_push_extracts_unextracted_images_into_project_raw(self):
        from PIL import Image
        from lota import catalog
        from lota.tools import auto_extract
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, project = root / "photos", root / "projects" / "telecom-inspection"
            source.mkdir()
            Image.new("RGB", (64, 48), "red").save(source / "S1_a.png")
            catalog.scan(project, [{"name": "s", "path": str(source)}], stability_seconds=0)
            image_id = catalog.list_images(project)[0]["image_id"]
            self.assertFalse(catalog.get_image(project, image_id).get("raw_path"))
            engine = SimpleNamespace(image_path=lambda d, record, raw_only: Path(record["path"]))
            ctx = Context(project / "annotation-tasks" / "telecom-site-inspection", None, engine, "job", "step")
            self.assertEqual(auto_extract(ctx, [image_id, image_id]), 1)
            self.assertTrue(catalog.get_image(project, image_id).get("raw_path"))   # 抽到子项目，不是任务目录
            self.assertEqual(auto_extract(ctx, [image_id]), 0)                       # 已抽取的不再抽


if __name__ == "__main__":
    unittest.main()
