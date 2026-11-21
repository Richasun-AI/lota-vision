"""按整张图判断“有没有”（站点有没有杂草）；标注规范按标注任务存"""
import tempfile
import unittest
from pathlib import Path

from lota import models
from lota.hub import guideline_text


class PresenceTest(unittest.TestCase):
    def test_recall_false_alarm_and_best_threshold(self):
        truth = [{"weed"}, {"weed"}, {"weed", "camera"}, set(), set()]
        scores = [{"weed": 0.9}, {"weed": 0.3}, {"weed": 0.1, "camera": 0.8}, {"weed": 0.4}, {}]
        out = models.presence_metrics(["weed", "camera", "bar"], truth, scores)
        self.assertNotIn("bar", out)                                  # 没有正例的类别不算
        w = out["weed"]
        self.assertEqual((w["images_with"], w["images_without"]), (3, 2))
        at25 = w["at"]["0.25"]
        self.assertEqual((at25["recall"], at25["false_alarm"], at25["missed"], at25["false"]), (round(2 / 3, 4), 0.5, 1, 1))
        self.assertEqual(w["at"]["0.5"]["recall"], round(1 / 3, 4))
        self.assertGreaterEqual(w["best"]["f1"], at25["f1"])
        self.assertEqual(out["camera"]["at"]["0.5"]["recall"], 1.0)

    def test_guideline_prefers_task_then_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "annotation-tasks" / "weed"
            task.mkdir(parents=True)
            self.assertEqual(guideline_text(task), "")
            (root / "guideline.md").write_text("project rule", encoding="utf-8")
            self.assertEqual(guideline_text(task), "project rule")
            (task / "guideline.md").write_text("task rule", encoding="utf-8")
            self.assertEqual(guideline_text(task), "task rule")
            self.assertEqual(guideline_text(root), "project rule")


if __name__ == "__main__":
    unittest.main()
