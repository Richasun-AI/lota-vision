"""加了标签以后：更新现在这个 Label Studio 项目的标注界面；旧标注认定“不含新类别”后能回收、打包，再加标签要重新认定"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from lota import catalog, flow
from lota.labels import _project_name, import_annotations, label_config, schema_ok
from lota.store import Scenario, Store
from lota.tools import Context

LS = {"base_url": "http://127.0.0.1:8081", "project_id": 5, "token_env": "LABEL_STUDIO_TOKEN", "local_files_root": "/data"}
BOX = {"type": "rectanglelabels", "from_name": "objects", "to_name": "image", "original_width": 64, "original_height": 48,
       "value": {"x": 10, "y": 10, "width": 20, "height": 20, "rectanglelabels": ["weed"]}}
REVIEWED = {"type": "choices", "from_name": "coverage", "to_name": "image", "value": {"choices": ["Targets reviewed"]}}


class NewClassesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.store = Store(root / "runtime")
        photos = root / "photos"
        photos.mkdir()
        Image.new("RGB", (64, 48), "green").save(photos / "S1_a.png")
        self.cfg = Scenario.model_validate({"name": "S", "classes": ["weed", "camera"], "label_studio": LS,
                                            "sources": [{"name": "s", "path": str(photos)}]})
        self.store.save_project("site", self.cfg)
        self.d = self.store.project_dir("site")
        catalog.scan(self.d, [{"name": "s", "path": str(photos)}], stability_seconds=0)
        self.iid = catalog.list_images(self.d)[0]["image_id"]
        catalog.extract(self.d, [self.iid])
        self.old_task = {"id": 1, "project": 5, "data": {
            "image_id": self.iid, "ailabel_project": _project_name(self.d), "ailabel_task": "detect",
            "ailabel_label_schema": {"task": "detect", "classes": ["weed"]}},
            "annotations": [{"id": 10, "result": [BOX, REVIEWED]}]}
        self.calls = []
        test = self

        class FakeClient:
            def list_tasks(self, pid):
                return [test.old_task]

            def export_tasks(self, pid):
                return [test.old_task]

            def _request(self, method, path, payload=None, query=None):
                test.calls.append((method, path, payload))
                return {"label_config": label_config(["weed"])}

        self.patch = mock.patch.object(flow, "_ls_client", lambda cfg: FakeClient())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.lspatch = mock.patch("lota.labels.LabelStudioClient.from_config", lambda cfg: FakeClient())
        self.lspatch.start()
        self.addCleanup(self.lspatch.stop)
        self.ctx = Context(self.d, self.cfg, SimpleNamespace(store=self.store), "job", "step")

    def test_old_annotations_need_ack_then_import(self):
        r = import_annotations(self.d, [self.old_task], ["weed", "camera"])
        self.assertEqual(r["accepted"], 0)
        self.assertIn("labels changed", r["rejected"][0]["reason"])
        preview = flow.TOOLS["labels.ack_new_classes"].function(self.ctx, dry_run=True)
        self.assertEqual(preview["old_schemas"][0]["new_classes"], ["camera"])
        self.assertEqual(preview["old_schemas"][0]["label_studio_tasks"], 1)
        self.assertFalse((self.d / "label_schema_ack.json").exists())                 # 预览不记任何东西
        out = flow.TOOLS["labels.ack_new_classes"].function(self.ctx, dry_run=False)
        self.assertEqual(out["acknowledged"], 1)
        self.assertEqual(out["pull"]["accepted"], 1)                                   # 认定后旧标注收进来了
        self.assertTrue(schema_ok({"task": "detect", "classes": ["weed"]}, "detect", ["weed", "camera"], self.d))
        # 以后再加标签：认定失效，要再判断一次
        self.assertFalse(schema_ok({"task": "detect", "classes": ["weed"]}, "detect", ["weed", "camera", "pole"], self.d))
        # 删了类别的旧标签表不能认定
        self.assertFalse(schema_ok({"task": "detect", "classes": ["weed", "gone"]}, "detect", ["weed", "camera"], self.d))

    def test_update_label_studio_config_keeps_project(self):
        out = flow.TOOLS["labels.ls_update_config"].function(self.ctx)
        self.assertEqual(out["added_classes"], ["camera"])
        method, path, payload = self.calls[-1]
        self.assertEqual((method, path), ("PATCH", "/api/projects/5"))
        self.assertIn('value="camera"', payload["label_config"])
        self.assertIn("Targets reviewed", payload["label_config"])


if __name__ == "__main__":
    unittest.main()
