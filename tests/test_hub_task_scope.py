"""标注系统页的 Label Studio 状态按“当前标注任务”取：任务自己的 LS 项目、标签、目录，不是子项目默认的"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import fastapi  # noqa: F401
except ImportError:   # 本机没装 fastapi：只要注册路由用到的两个名字
    import sys
    import types

    class HTTPException(Exception):
        def __init__(self, status_code, detail=""):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    sys.modules["fastapi"] = types.SimpleNamespace(Body=lambda *a, **k: None, HTTPException=HTTPException)

from lota import hub
from lota.store import Scenario, Store

LS = {"base_url": "http://127.0.0.1:8081", "token_env": "LABEL_STUDIO_TOKEN", "local_files_root": "/data"}


class FakeRouter:
    def __init__(self):
        self.routes = {}

    def _add(self, method, path, **_):
        def deco(fn):
            self.routes[(method, path)] = fn
            return fn
        return deco

    def get(self, path, **kw):
        return self._add("GET", path, **kw)

    def post(self, path, **kw):
        return self._add("POST", path, **kw)

    def put(self, path, **kw):
        return self._add("PUT", path, **kw)

    def delete(self, path, **kw):
        return self._add("DELETE", path, **kw)


class HubTaskScopeTest(unittest.TestCase):
    def test_ls_status_follows_annotation_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "runtime")
            store.save_project("site", Scenario.model_validate({
                "name": "Site", "classes": ["mcb"], "label_studio": {**LS, "project_id": 13},
                "annotation_tasks": {"weeds": {"name": "Weeds", "task": "detect", "classes": ["weed"],
                                               "label_studio": {**LS, "project_id": 9}}}}))
            router, engine = FakeRouter(), SimpleNamespace(store=store)
            hub.register_overview(router, engine, lambda pid: store.project_dir(pid))
            status = router.routes[("GET", "/projects/{project_id}/hub/ls-status")]
            seen = []
            fake = lambda d, cfg, classes=None, task="detect": seen.append((Path(d), cfg["project_id"], classes)) or {"project_id": cfg["project_id"]}
            with mock.patch.object(hub, "ls_status", fake):
                default = status("site")
                task = status("site", annotation_task="weeds")
            self.assertEqual(default["project_id"], 13)
            self.assertEqual(task["project_id"], 9)
            self.assertEqual(task["classes"], ["weed"])
            self.assertEqual(seen[1][0].parts[-2:], ("annotation-tasks", "weeds"))
            with self.assertRaises(Exception):
                status("site", annotation_task="nope")


if __name__ == "__main__":
    unittest.main()
