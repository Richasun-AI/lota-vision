import math
import unittest

from lota.flow import REASON_ZH, _rot, _straight


class StraightenTest(unittest.TestCase):
    def test_zero_rotation_is_unchanged(self):
        v = _straight({"x": 10, "y": 20, "width": 30, "height": 40, "rotation": 0}, 1000, 500)
        for k, want in {"x": 10, "y": 20, "width": 30, "height": 40, "rotation": 0}.items():
            self.assertAlmostEqual(v[k], want, places=6)

    def test_rotated_box_becomes_enclosing_box(self):
        # 100x50 像素的框，左上角 (200,100)，顺时针转 90° → 占 x∈[150,200], y∈[100,200]
        W, H = 1000, 1000
        v = _straight({"x": 20, "y": 10, "width": 10, "height": 5, "rotation": 90}, W, H)
        self.assertAlmostEqual(v["x"], 15, places=6)
        self.assertAlmostEqual(v["y"], 10, places=6)
        self.assertAlmostEqual(v["width"], 5, places=6)
        self.assertAlmostEqual(v["height"], 10, places=6)
        self.assertEqual(v["rotation"], 0)

    def test_clipped_to_image(self):
        v = _straight({"x": 0, "y": 0, "width": 50, "height": 50, "rotation": 30}, 800, 600)
        self.assertGreaterEqual(v["x"], 0)
        self.assertLessEqual(v["x"] + v["width"], 100 + 1e-9)
        self.assertLessEqual(v["y"] + v["height"], 100 + 1e-9)

    def test_angle_is_signed(self):
        self.assertAlmostEqual(_rot({"rotation": 352.8}), -7.2, places=6)
        self.assertEqual(_rot({}), 0)

    def test_rotated_reason_is_known(self):
        self.assertEqual(REASON_ZH["Rotated annotations need review before axis-aligned YOLO export"][0], "rotated")


if __name__ == "__main__":
    unittest.main()
