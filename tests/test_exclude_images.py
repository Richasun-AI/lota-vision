"""排除质量不行的图：记在标注任务里（不改图库）、删 Label Studio 任务；排除的不送去标注、可恢复"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lota import flow, labels


class FakeLS:
    def __init__(self, missing=()):
        self.deleted, self.missing = [], set(missing)

    def _request(self, method, path, payload=None, query=None):
        assert method == "DELETE"
        tid = int(path.split("/")[3])
        if tid in self.missing:
            raise RuntimeError(f"Label Studio DELETE {path} returned HTTP 404")
        self.deleted.append(tid)


class ExcludeTest(unittest.TestCase):
    def _links(self, root, rows):
        (root / "annotations").mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(root / "annotations" / "index.sqlite3")) as db:
            db.execute("CREATE TABLE label_studio_links (base_url TEXT, project_id INTEGER, task TEXT, image_id TEXT, remote_id INTEGER)")
            db.executemany("INSERT INTO label_studio_links VALUES ('u', ?, 'detect', ?, ?)", rows)
            db.commit()

    def test_exclude_and_delete_tasks_in_current_project_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._links(root, [(9, "a", 101), (9, "b", 102), (8, "a", 555), (9, "keep", 103)])
            client = FakeLS(missing={102})
            c = SimpleNamespace(directory=root)
            with mock.patch("lota.tasks.live_label_studio", lambda c: {"project_id": 9}), \
                    mock.patch.object(flow, "_ls_client", lambda ls: client):
                out = flow.exclude_images(c, ["a", "b", "a"])
            self.assertEqual(sorted(labels.excluded_images(root)), ["a", "b"])   # 记在任务目录里，图库不动
            self.assertEqual(client.deleted, [101])              # 102 已经不在 LS 里（404）也算删掉；项目 #8 的不碰
            self.assertEqual(out["label_studio_deleted"], 1)
            with closing(sqlite3.connect(root / "annotations" / "index.sqlite3")) as db:
                left = sorted(db.execute("SELECT project_id, image_id FROM label_studio_links").fetchall())
            self.assertEqual(left, [(8, "a"), (9, "keep")])

    def test_keep_tasks_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = SimpleNamespace(directory=Path(tmp))
            with mock.patch("lota.tasks.live_label_studio", lambda c: self.fail("不该连 Label Studio")):
                out = flow.exclude_images(c, ["a", "b"], delete_in_label_studio=False)
                self.assertEqual((out["excluded"], out["label_studio_deleted"]), (2, 0))
                back = flow.exclude_images(c, ["a"], restore=True)
            self.assertEqual((back["restored"], back["excluded_total"]), (1, 1))
            self.assertEqual(list(labels.excluded_images(Path(tmp))), ["b"])


class ExcludedFilteredTest(unittest.TestCase):
    def test_push_skips_excluded(self):
        with mock.patch.object(labels, "excluded_images", lambda d: {"bad": {}}), \
                mock.patch.object(labels, "_remote_project", lambda ls: 9), \
                mock.patch.object(labels.LabelStudioClient, "from_config", lambda ls: object()):
            out = labels.push_tasks("/p", {"project_id": 9}, ["bad"], ["weed"])
        self.assertEqual((out["pushed"], out["skipped_rejected"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
