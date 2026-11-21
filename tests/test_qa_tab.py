"""找标注错误 → 把可疑图集中到 Label Studio 的“LOTA·标注质检”标签页去改"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lota import flow, qa


class FakeLS:
    def __init__(self, tasks):
        self.tasks = {t["id"]: t for t in tasks}
        self.views, self.patches = [], []

    def list_tasks(self, pid):
        return list(self.tasks.values())

    def _request(self, method, path, payload=None, query=None):
        if path.startswith("/api/tasks/"):
            tid = int(path.split("/")[3])
            self.tasks[tid]["data"] = payload["data"]
            self.patches.append(tid)
            return {}
        if path == "/api/dm/views/" and method == "GET":
            return self.views
        if path == "/api/dm/views/" and method == "POST":
            view = {"id": 7, "data": payload["data"]}
            self.views.append(view)
            return view
        if path.startswith("/api/dm/views/") and method == "PATCH":
            self.views[0]["data"] = payload["data"]
            return self.views[0]
        raise AssertionError(path)


def item(image_id, *types):
    return {"image_id": image_id, "issues": [{"type": t} for t in types], "score": 1}


class QaTabTest(unittest.TestCase):
    def test_pick_filters_and_skips_decided(self):
        report = {"items": [item("a", "missing"), item("b", "spurious"), item("c", "missing", "loc")]}
        self.assertEqual(qa.qa_pick(report, {}), ["a", "b", "c"])
        self.assertEqual(qa.qa_pick(report, {"a": "fine"}, "missing"), ["c"])
        self.assertEqual(qa.qa_pick(report, {"a": "later"}, "missing"), ["a", "c"])
        self.assertEqual(qa.qa_pick(report, {"a": "fixed"}, "missing", skip_decided=False), ["a", "c"])

    def test_tab_marks_only_flagged_tasks_in_current_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "qa").mkdir()
            (root / "qa" / "r1.json").write_text(json.dumps({"items": [item("a", "missing"), item("b", "loc"), item("z", "missing")]}), encoding="utf-8")
            (root / "annotations").mkdir()
            with closing(sqlite3.connect(root / "annotations" / "index.sqlite3")) as db:
                db.execute("CREATE TABLE label_studio_links (image_id TEXT, project_id INTEGER, remote_id INTEGER)")
                db.executemany("INSERT INTO label_studio_links VALUES (?,?,?)", [("a", 9, 101), ("b", 9, 102), ("z", 8, 555)])
                db.commit()
            client = FakeLS([{"id": 101, "data": {"image": "x"}}, {"id": 102, "data": {"image": "y", "lota_qa": "qa"}},
                             {"id": 103, "data": {"image": "w", "lota_check": "coverage"}}])
            c = SimpleNamespace(directory=root)
            with mock.patch("lota.tasks.require_label_studio", lambda c: {"project_id": 9, "base_url": "http://ls"}), \
                    mock.patch("lota.tasks.source_project", lambda d: d), \
                    mock.patch("lota.hub.load_settings", lambda d: {"label_studio": {"public_url": "http://pub"}}), \
                    mock.patch.object(flow, "_ls_client", lambda ls: client):
                out = qa.qa_tab(c, "r1", issue="missing")
            self.assertEqual(client.tasks[101]["data"], {"image": "x", "lota_qa": "qa"})   # 加标记，原有字段不动
            self.assertEqual(client.tasks[102]["data"], {"image": "y"})                  # 不在这次清单里：去掉标记
            self.assertEqual(client.tasks[103]["data"], {"image": "w", "lota_check": "coverage"})  # 别的标签页不受影响
            self.assertEqual((out["tasks"], out["images"], out["not_in_label_studio"]), (1, 2, 1))
            self.assertEqual(out["url"], "http://pub/projects/9/data?tab=7")
            self.assertEqual(client.views[0]["data"]["title"], qa.QA_TAB_TITLE)
            self.assertIn("#9", out["message"])


class ReviewTabUnchangedTest(unittest.TestCase):
    def test_review_tab_still_uses_lota_check_and_coverage_filters(self):
        client = FakeLS([{"id": 1, "data": {"image": "x"}}, {"id": 2, "data": {"image": "y", "lota_check": "coverage"}}])
        items = [{"kind": "coverage", "links": [{"project": 9, "task": 1}]}, {"kind": "class", "links": [{"project": 9, "task": 2}]}]
        with mock.patch.object(flow, "review_items", lambda *a: items), mock.patch.object(flow, "_ls_client", lambda ls: client):
            out = flow.review_tab("/d", {"project_id": 9, "base_url": "http://ls"}, "detect", ["weed"], "coverage")
        self.assertEqual(client.tasks[1]["data"]["lota_check"], "coverage")
        self.assertNotIn("lota_check", client.tasks[2]["data"])
        f = client.views[0]["data"]["filters"]["items"]
        self.assertEqual(f[0]["filter"], "filter:tasks:data.lota_check")
        self.assertEqual(len(f), 4)                                   # 外加 3 个“还没选确认选项”的条件
        self.assertEqual((out["tasks"], out["added"], out["removed"], out["url"]), (1, 1, 1, "http://ls/projects/9/data?tab=7"))


if __name__ == "__main__":
    unittest.main()
