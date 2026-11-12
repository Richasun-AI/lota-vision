import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lota.flow import rereview_candidates
from lota.hub import ls_config_check
from lota.labels import label_config


def _ls_entry(schema_classes, labels):
    return {"_ailabel_schema": {"task": "detect", "classes": schema_classes},
            "annotations": [{"was_cancelled": False, "result": [
                {"type": "rectanglelabels", "from_name": "label", "to_name": "image",
                 "value": {"x": 10, "y": 20, "width": 30, "height": 40, "rectanglelabels": [name]}} for name in labels]}]}


class RereviewTest(unittest.TestCase):
    def test_candidates_after_adding_a_class(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "annotations").mkdir()
            db = sqlite3.connect(root / "annotations" / "index.sqlite3")
            db.execute("CREATE TABLE revisions(task, image_id, revision, status, labels, reason, original)")
            db.execute("CREATE TABLE current_annotations(task, image_id, revision)")
            now = ["rust", "grounding_bar"]
            rows = {
                "old": ("accepted", _ls_entry(["rust"], ["rust"])),                      # 加类别前通过 → stale
                "bad": ("invalid", _ls_entry(["rust"], ["rust", "grounding bar"])),      # 类别名对不上 → rejected，框带过去
                "fresh": ("accepted", _ls_entry(now, ["rust", "grounding_bar"])),         # 按现在的类别通过 → 不用复核
                "empty": ("unlabeled", _ls_entry(["rust"], [])),                         # 没框 → 不送
            }
            for key, (status, original) in rows.items():
                db.execute("INSERT INTO revisions VALUES ('detect', ?, 1, ?, '[]', '', ?)", (key, status, json.dumps(original)))
                db.execute("INSERT INTO current_annotations VALUES ('detect', ?, 1)", (key,))
            db.commit()
            db.close()
            out = rereview_candidates(root, "detect", now)
            self.assertEqual(sorted(out), ["bad", "old"])
            self.assertEqual(out["old"][0], "stale")
            self.assertEqual(out["bad"][0], "rejected")
            self.assertEqual([b["rectanglelabels"] for b in out["bad"][1]], [["rust"], ["grounding_bar"]])
            self.assertEqual(out["old"][2], ["rust"])

    def test_label_studio_config_check(self):
        manual = ('<View><Image name="image" value="$image"/><RectangleLabels name="label" toName="image">'
                  '<Label value="rust"/><Label value="grounding bar"/></RectangleLabels></View>')
        self.assertEqual(ls_config_check(manual, ["rust", "grounding_bar"]), {"missing_classes": [], "has_review_choice": False})
        self.assertEqual(ls_config_check(label_config(["rust"]), ["rust", "grounding_bar"]),
                         {"missing_classes": ["grounding_bar"], "has_review_choice": True})
        self.assertEqual(ls_config_check(label_config(["rust", "grounding_bar"]), ["rust", "grounding_bar"]),
                         {"missing_classes": [], "has_review_choice": True})


if __name__ == "__main__":
    unittest.main()
