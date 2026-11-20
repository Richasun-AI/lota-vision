"""批量补确认：只给“画了框、没选确认选项”的标注补 Targets reviewed，其余不动，补完回收"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lota import flow
from lota.labels import label_config
from lota.store import Scenario, Store
from lota.tools import Context

LS = {"base_url": "http://127.0.0.1:8081", "project_id": 5, "token_env": "LABEL_STUDIO_TOKEN", "local_files_root": "/data"}
BOX = {"type": "rectanglelabels", "from_name": "objects", "to_name": "image",
       "value": {"x": 1, "y": 1, "width": 5, "height": 5, "rectanglelabels": ["weed"]}}


class ConfirmReviewedTest(unittest.TestCase):
    def test_only_drawn_but_unconfirmed_get_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "runtime")
            cfg = Scenario.model_validate({"name": "S", "classes": ["weed"], "label_studio": LS})
            store.save_project("site", cfg)
            d = store.project_dir("site")
            (d / "annotations").mkdir(parents=True)
            tasks = {   # image_id → (LS 任务号, 回收时的原因, LS 里的结果)
                "a": (101, "Confirm all target classes have been reviewed", [BOX]),
                "b": (102, "No boxes and no explicit negative label", []),
                "c": (103, "Confirm all target classes have been reviewed", [BOX]),
            }
            with closing(sqlite3.connect(d / "annotations" / "index.sqlite3")) as db, db:
                db.execute("CREATE TABLE revisions(task, image_id, revision, status, labels, reason, original)")
                db.execute("CREATE TABLE current_annotations(task, image_id, revision)")
                for iid, (tid, reason, res) in tasks.items():
                    orig = {"id": tid, "project": 5, "annotations": [{"id": tid * 10, "result": res}]}
                    db.execute("INSERT INTO revisions VALUES ('detect', ?, 1, 'unlabeled', '[]', ?, ?)", (iid, reason, json.dumps(orig)))
                    db.execute("INSERT INTO current_annotations VALUES ('detect', ?, 1)", (iid,))
            remote = {101: [BOX], 102: [], 103: [BOX, {"type": "choices", "from_name": "coverage", "to_name": "image",
                                                      "value": {"choices": ["Targets reviewed"]}}]}   # 103 在 LS 里其实已经选了
            patched = {}

            class FakeClient:
                def _request(self, method, path, payload=None, query=None):
                    if path == "/api/projects/5":
                        return {"label_config": label_config(["weed"])}
                    if path.startswith("/api/tasks/"):
                        tid = int(path.split("/")[-1])
                        return {"annotations": [{"id": tid * 10, "result": remote[tid]}]}
                    if method == "PATCH":
                        patched[path] = payload["result"]
                        return {}
                    raise AssertionError(path)

            ctx = Context(d, cfg, SimpleNamespace(store=store), "job", "step")
            with mock.patch.object(flow, "_ls_client", lambda c: FakeClient()), \
                    mock.patch("lota.labels.pull_annotations", lambda *a, **k: {"pulled": 1}):
                out = flow.TOOLS["labels.confirm_reviewed"].function(ctx, image_ids=[])
            self.assertEqual(out["confirmed"], 1)
            self.assertEqual(list(patched), ["/api/annotations/1010"])
            added = patched["/api/annotations/1010"][-1]
            self.assertEqual((added["from_name"], added["value"]["choices"]), ("coverage", ["Targets reviewed"]))
            self.assertEqual(out["skipped"], {"没画框或选了 No target，要人判断": 1, "Label Studio 里已经选过了（回收一下就好）": 1})
            self.assertEqual(out["pull"], {"pulled": 1})


if __name__ == "__main__":
    unittest.main()
