import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from lota import typesuggest


class _Box:
    def __init__(self, cls, conf):
        self.cls, self.conf = cls, conf


class _Result:
    names = {0: "rust", 1: "grounding_bar"}

    def __init__(self, boxes):
        self.boxes = [_Box(c, s) for c, s in boxes]


# 假模型：按文件名决定检出什么
DETECTIONS = {"a.jpg": [(1, 0.9)], "b.jpg": [(0, 0.7), (1, 0.6)], "c.jpg": [(0, 0.3)], "d.jpg": [(1, 0.95)], "e.jpg": []}


class _YOLO:
    def __init__(self, path):
        pass

    def predict(self, paths, **kw):
        paths = [paths] if isinstance(paths, str) else paths
        return [_Result(DETECTIONS[Path(p).name]) for p in paths]


class TypeSuggestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / "work"
        self.work.mkdir()
        self.photos = {n: str(root / n) for n in DETECTIONS}
        for p in self.photos.values():
            Path(p).write_bytes(b"x")
        weights = root / "m.pt"
        weights.write_bytes(b"w")
        eff = {self.photos[n]: "other" for n in DETECTIONS}
        confirmed = {self.photos["d.jpg"]: "site_ground"}           # d 已人工确认：不扫
        ei, hf = ModuleType("emb_index"), ModuleType("hq_filter")
        ei.BIN = "other"
        ei.effective_types = lambda: dict(eff)
        ei.confirmed_labels = lambda: dict(confirmed)
        hf.WORK = self.work
        hf.TAXONOMY = {"other": {"name": "其他"}, "grounding_lightning": {"name": "接地/防雷"}, "site_ground": {"name": "站内地面"}}
        hf.locate = lambda p: p
        hf.load_deleted = lambda: {}
        ul = ModuleType("ultralytics")
        ul.YOLO = _YOLO
        patcher = patch.dict(sys.modules, {"emb_index": ei, "hq_filter": hf, "ultralytics": ul})
        patcher.start()
        self.addCleanup(patcher.stop)
        names = patch("lota.flow._weights_names", return_value={"names": ["rust", "grounding_bar"], "task": "detect"})
        names.start()
        self.addCleanup(names.stop)
        self.c = SimpleNamespace(directory=root / "proj", engine=SimpleNamespace(allowed_weights=lambda w: weights))

    def run_tool(self, **kw):
        args = dict(rules="grounding_bar>grounding_lightning; rust>site_ground", weights="m.pt", min_confidence=0.5)
        args.update(kw)
        return typesuggest.classify_types(self.c, **args)

    def test_parse_rules(self):
        self.assertEqual(typesuggest.parse_rules("a,b>x；c>y"), [({"a", "b"}, "x"), ({"c"}, "y")])
        self.assertEqual(typesuggest.parse_rules([{"labels": ["a"], "photo_type": "x"}]), [({"a"}, "x")])
        with self.assertRaises(ValueError):
            typesuggest.parse_rules("a")

    def test_rules_pick_highest_and_skip_confirmed(self):
        out = self.run_tool()
        self.assertEqual(out["scanned"], 4)                   # d 已确认，不扫
        sug = typesuggest.load_suggestions()
        p = self.photos
        self.assertEqual(sug[p["a.jpg"]]["photo_type"], "grounding_lightning")
        self.assertEqual(sug[p["b.jpg"]]["photo_type"], "site_ground")     # rust 0.7 > grounding_bar 0.6
        self.assertNotIn(p["c.jpg"], sug)                                  # 0.3 低于门槛
        self.assertNotIn(p["e.jpg"], sug)
        self.assertNotIn(p["d.jpg"], sug)
        self.assertEqual(out["remaining"], 0)
        self.assertEqual(self.run_tool()["scanned"], 0)       # 接着扫：都扫过了

    def test_dismissed_suggestion_does_not_come_back(self):
        self.run_tool()
        typesuggest.dismiss([self.photos["a.jpg"]], "grounding_lightning")
        self.assertNotIn(self.photos["a.jpg"], typesuggest.load_suggestions())
        self.run_tool(restart=True)
        self.assertNotIn(self.photos["a.jpg"], typesuggest.load_suggestions())

    def test_unknown_label_or_type_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_tool(rules="meter>grounding_lightning")
        with self.assertRaises(ValueError):
            self.run_tool(rules="rust>nope")
        with self.assertRaises(ValueError):
            self.run_tool(rules="rust>other")


if __name__ == "__main__":
    unittest.main()
