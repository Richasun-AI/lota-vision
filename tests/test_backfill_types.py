import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from PIL import Image

from lota import catalog, flow, learning, sorting


def _fake_pipeline(eff, human):
    ei, hf = ModuleType("emb_index"), ModuleType("hq_filter")
    ei.BIN = "other"
    ei.effective_types = lambda: dict(eff)
    ei.confirmed_labels = lambda: dict(human)
    hf.TAXONOMY = {"gate": {"name": "大门"}, "meter": {"name": "电表"}, "other": {"name": "其他/无关"}}
    hf.locate = lambda p: p
    return {"emb_index": ei, "hq_filter": hf}


class BackfillTypesTest(unittest.TestCase):
    def test_backfill_by_path_and_by_picked_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            originals, project = tmp / "originals", tmp / "runtime" / "demo"
            originals.mkdir()
            project.mkdir(parents=True)
            colors = {"a": (255, 0, 0), "b": (0, 255, 0), "c": (0, 0, 255), "d": (9, 9, 9), "e": (90, 90, 90)}
            paths = {}
            for name, color in colors.items():
                paths[name] = originals / f"{name}.jpg"
                Image.new("RGB", (32, 32), color).save(paths[name])
            # a、b、c、d 直接扫描原图目录收进来（按路径对）；e 是以前“按照片类型抽图”复制进来的（按内容对）
            scanned = tmp / "scanned"
            scanned.mkdir()
            imports = tmp / "imports"
            copy_dir = imports / "demo" / "gate-old"
            copy_dir.mkdir(parents=True)
            shutil.copy2(paths["e"], copy_dir / "e.jpg")
            (imports / "demo" / "picked.json").write_text(json.dumps([str(paths["e"])]))
            catalog.scan(project, [{"name": "orig", "path": str(originals)}, {"name": "old", "path": str(copy_dir)}],
                         stability_seconds=0)
            eff = {str(paths["a"]): "gate", str(paths["b"]): "meter", str(paths["c"]): "other",
                   str(paths["d"]): "gate", str(paths["e"]): "gate"}
            human = {str(paths["a"]): "gate", str(paths["d"]): "gate", str(paths["e"]): "gate"}
            ids = {n: flow._sha256(p) for n, p in paths.items()}
            c = SimpleNamespace(directory=project, scenario=SimpleNamespace(categories={}))
            added = []

            def ensure(c, key, taxonomy=None):
                added.append(key)
                c.scenario.categories[key] = SimpleNamespace(enabled=True)

            c.scenario.categories["gate"] = SimpleNamespace(enabled=True)
            learning.confirm(c, [ids["d"]], "gate")   # 本子项目已人工确认的不动
            del c.scenario.categories["gate"]
            with patch.dict(sys.modules, _fake_pipeline(eff, human)), patch.object(flow, "IMPORTS", imports), \
                    patch.object(flow, "_ensure_category", side_effect=ensure):
                dry = flow.backfill_types(c, dry_run=True)
                self.assertEqual((dry["matched"], dry["confirmed"], dry["predicted"]), (5, 2, 1))
                self.assertEqual(added, [])
                self.assertEqual(learning.human_labels(project), {ids["d"]: "gate"})
                done = flow.backfill_types(c)
            self.assertEqual(sorted(added), ["gate", "meter"])
            self.assertEqual(done["skipped"], {"其他/无关或没有类型": 1, "本子项目已人工确认": 1})
            self.assertEqual(learning.human_labels(project), {ids["a"]: "gate", ids["d"]: "gate", ids["e"]: "gate"})
            result = sorting.results(project)
            self.assertEqual(result["categories"].get("meter"), 1)
            payload = next(x["sorting"] for x in result["items"] if x["image_id"] == ids["b"])
            self.assertEqual((payload["review"], payload["provenance"]), ("pending", "photo_types"))


if __name__ == "__main__":
    unittest.main()
