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

    sys.modules["fastapi"] = types.SimpleNamespace(Body=lambda *a, **k: None, HTTPException=HTTPException,
                                                   File=lambda *a, **k: None, Form=lambda *a, **k: None, UploadFile=object)
    sys.modules["fastapi.responses"] = types.SimpleNamespace(FileResponse=object, StreamingResponse=object)

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


class TaskDirEndpointsTest(unittest.TestCase):
    """数据集 / 模型 / 总览：选了标注任务读任务目录；图库、设置仍是子项目的"""
    def test_dataset_model_endpoints_follow_task(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "runtime")
            store.save_project("site", Scenario.model_validate({
                "name": "Site", "classes": ["mcb"],
                "annotation_tasks": {"weeds": {"name": "Weeds", "task": "detect", "classes": ["weed"]}}}))
            root = store.project_dir("site")
            root.mkdir(parents=True, exist_ok=True)
            (root / "settings.json").write_text(json.dumps({"naming": {"dataset": "X-{project}"}}), encoding="utf-8")
            router, engine = FakeRouter(), SimpleNamespace(store=store)
            hub.register_routes(router, engine, lambda pid: store.project_dir(pid))
            seen = []
            with mock.patch.object(hub, "dataset_cards", lambda d: seen.append(("ds", Path(d))) or []),                     mock.patch.object(hub, "model_cards", lambda d: seen.append(("ms", Path(d))) or {"models": [], "aliases": {}}),                     mock.patch.object(hub, "accepted_count", lambda d: seen.append(("acc", Path(d))) or 7):
                r = router.routes[("GET", "/projects/{project_id}/hub/datasets")]("site", annotation_task="weeds")
                self.assertEqual(r["accepted_annotations"], 7)
                s = router.routes[("GET", "/projects/{project_id}/hub/summary")]("site", annotation_task="weeds")
                router.routes[("GET", "/projects/{project_id}/hub/datasets")]("site")
            task = root / "annotation-tasks" / "weeds"
            self.assertTrue(all(d == task for _, d in seen[:-2]))           # 选了任务：都读任务目录
            self.assertEqual(seen[-1][1], root)                              # 没选任务：照旧读子项目
            self.assertEqual(s["images"], 0)                                 # 图库按子项目统计
            self.assertEqual(s["settings"]["naming"]["dataset"], "X-{project}")
            self.assertEqual(hub.load_settings(task)["naming"]["dataset"], "X-{project}")   # 任务目录没有设置：用子项目的
            with self.assertRaises(Exception):
                router.routes[("GET", "/projects/{project_id}/hub/models")]("site", annotation_task="nope")


if __name__ == "__main__":
    unittest.main()
