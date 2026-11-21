"""给 Label Studio 里已有的任务补模型预测：只补没标过的、同一模型不重复、类别不一致报错"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lota import flow


class FakeLS:
    def __init__(self, tasks, preds):
        self.tasks, self.preds, self.posted = tasks, preds, []

    def list_tasks(self, pid):
        return list(self.tasks)

    def _request(self, method, path, payload=None, query=None):
        if method == "GET" and path == "/api/predictions/":
            return self.preds.get(query["task"], [])
        if method == "POST" and path == "/api/predictions/":
            self.posted.append(payload)
            return {"id": len(self.posted)}
        raise AssertionError((method, path))


def task(tid, image_id, annotated=False):
    return {"id": tid, "data": {"image_id": image_id, "ailabel_project": "p"}, "total_annotations": 1 if annotated else 0}


class AddPredictionsTest(unittest.TestCase):
    def _run(self, client, meta, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = SimpleNamespace(directory=Path(tmp.name), scenario=SimpleNamespace(task="detect", classes=["weed"]),
                            image_paths=lambda ids: [f"/img/{i}.jpg" for i in ids])
        pred = lambda d, sel, paths, **k: {"predictions": [
            {"path": p, "width": 100, "height": 50, "detections": [{"label": "weed", "confidence": 0.8, "xyxy": [10, 5, 60, 25]}]} for p in paths]}
        with mock.patch("lota.tasks.require_label_studio", lambda c: {"project_id": 9, "base_url": "http://ls"}), \
                mock.patch.object(flow, "_ls_client", lambda ls: client), \
                mock.patch("lota.labels._matches_task", lambda t, n, k: True), \
                mock.patch("lota.labels._project_names", lambda d: {"p"}), \
                mock.patch("lota.labels.excluded_images", lambda d: {"bad": {}}), \
                mock.patch("lota.models.resolve_model", lambda d, s: (meta, "w.pt")), \
                mock.patch("lota.models.predict", pred):
            return flow.add_predictions(c, **kw)

    def test_only_unlabeled_and_no_duplicates(self):
        client = FakeLS([task(1, "a"), task(2, "b", annotated=True), task(3, "c"), task(4, "bad")],
                        {3: [{"model_version": "m-v2"}]})
        out = self._run(client, {"version": "m-v2", "task": "detect", "classes": ["weed"]})
        self.assertEqual([p["task"] for p in client.posted], [1])        # 2 已标过、3 补过、4 已排除
        self.assertEqual((out["added"], out["already"], out["skipped_labeled"]), (1, 1, 1))
        p = client.posted[0]
        self.assertEqual(p["model_version"], "m-v2")
        self.assertEqual(p["result"][0]["value"], {"x": 10.0, "y": 10.0, "width": 50.0, "height": 40.0, "rectanglelabels": ["weed"]})
        self.assertIn("Create Annotations From Predictions", out["message"])

    def test_class_mismatch_refused(self):
        with self.assertRaises(ValueError):
            self._run(FakeLS([], {}), {"version": "old", "task": "detect", "classes": ["camera"]})


if __name__ == "__main__":
    unittest.main()
