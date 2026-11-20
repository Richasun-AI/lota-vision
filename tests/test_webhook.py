"""自动回收 webhook：合并改地址不能把触发动作清空；项目被删了要说清楚，不报看不懂的 400"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import fastapi  # noqa: F401
except ImportError:
    import sys
    import types
    sys.modules["fastapi"] = types.SimpleNamespace(Body=lambda *a, **k: None, HTTPException=Exception,
                                                   File=None, Form=None, UploadFile=None)

from lota import hub, merge
from lota.store import Scenario, Store
from lota.tools import Context

LS = {"base_url": "http://127.0.0.1:8081", "project_id": 9, "token_env": "LABEL_STUDIO_TOKEN", "local_files_root": "/data"}


class Fake:
    def __init__(self, hooks=(), exists=True):
        self.hooks, self.exists, self.calls = list(hooks), exists, []

    def _request(self, method, path, payload=None, query=None):
        self.calls.append((method, path, payload))
        if path.startswith("/api/projects/") and not self.exists:
            raise RuntimeError(f"Label Studio GET {path} returned HTTP 404")
        if path == "/api/webhooks" and method == "GET":
            return self.hooks
        if path == "/api/storages/localfiles" and method == "GET":
            return []
        return {"id": 1}


class WebhookTest(unittest.TestCase):
    def test_merge_rebind_keeps_actions(self):
        hook = {"id": 5, "url": "http://127.0.0.1:8795/api/hooks/label-studio?project=weeds", "is_active": True,
                "actions": ["ANNOTATION_CREATED", "ANNOTATION_UPDATED"], "send_for_all_actions": False, "send_payload": False}
        fake = Fake([hook])
        with tempfile.TemporaryDirectory() as tmp, mock.patch("lota.labels.LabelStudioClient.from_config", lambda cfg: fake):
            report = {}
            merge._ls_rebind(LS, Path(tmp) / "raw", "site", "weeds", "weeds", report)
        patch = [p for m, path, p in fake.calls if m == "PATCH"][0]
        self.assertEqual(patch["url"], "http://127.0.0.1:8795/api/hooks/label-studio?project=site&annotation_task=weeds")
        self.assertEqual(patch["actions"], ["ANNOTATION_CREATED", "ANNOTATION_UPDATED"])   # 不能被清空
        self.assertEqual(report["label_studio"]["webhooks_updated"], 1)

    def test_merge_rebind_fills_empty_actions(self):
        hook = {"id": 5, "url": "http://127.0.0.1:8795/api/hooks/label-studio?project=weeds", "actions": []}
        fake = Fake([hook])
        with tempfile.TemporaryDirectory() as tmp, mock.patch("lota.labels.LabelStudioClient.from_config", lambda cfg: fake):
            merge._ls_rebind(LS, Path(tmp) / "raw", "site", "weeds", "weeds", {})
        patch = [p for m, path, p in fake.calls if m == "PATCH"][0]
        self.assertEqual(patch["actions"], list(merge.HOOK_ACTIONS))

    def test_webhook_on_deleted_project_explains(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "runtime")
            cfg = Scenario.model_validate({"name": "S", "classes": ["weed"], "label_studio": LS})
            store.save_project("site", cfg)
            ctx = Context(store.project_dir("site"), cfg, SimpleNamespace(store=store), "j", "s")
            with mock.patch("lota.labels.LabelStudioClient.from_config", lambda c: Fake(exists=False)), \
                    self.assertRaisesRegex(ValueError, "#9 已经在 Label Studio 里删掉了"):
                hub.ls_webhook(ctx)


if __name__ == "__main__":
    unittest.main()
