import tempfile
import unittest
from pathlib import Path

from PIL import Image

from lota import catalog, sorting


class HideRejectedTest(unittest.TestCase):
    def test_rejected_photos_are_hidden_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, project = root / "src", root / "project"
            source.mkdir()
            project.mkdir()
            for i in range(3):
                Image.new("RGB", (8, 8), (i * 60, 0, 0)).save(source / f"{i}.png")
            catalog.scan(project, [{"name": "src", "path": str(source)}], stability_seconds=0)
            ids = [x["image_id"] for x in catalog.list_images(project)]
            sorting.store_results(project, [{"image_id": k, "category": "demo"} for k in ids])
            catalog.review_image(project, ids[0], decision="rejected")
            active = {x["image_id"] for x in catalog.list_images(project, status="active")}
            self.assertEqual(active, set(ids[1:]))
            self.assertEqual(len(catalog.list_images(project)), 3)                     # 全部（含已排除）
            hidden = sorting.results(project, exclude_rejected=True)
            self.assertEqual(hidden["categories"], {"demo": 2})
            self.assertNotIn(ids[0], {x["image_id"] for x in hidden["items"]})
            self.assertEqual(len(sorting.results(project, category="demo", exclude_rejected=True)["items"]), 2)
            self.assertEqual(sorting.results(project)["categories"], {"demo": 3})

    def test_category_filter_combines_with_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, project = root / "src", root / "project"
            source.mkdir()
            project.mkdir()
            for i in range(4):
                Image.new("RGB", (8, 8), (i * 60, 0, 0)).save(source / f"{i}.png")
            catalog.scan(project, [{"name": "src", "path": str(source)}], stability_seconds=0)
            ids = sorted(x["image_id"] for x in catalog.list_images(project))
            sorting.store_results(project, [{"image_id": k, "category": "rust" if n < 2 else "crack"} for n, k in enumerate(ids)])
            catalog.review_image(project, ids[0], decision="rejected")
            got = lambda **kw: {x["image_id"] for x in catalog.list_images(project, **kw)}
            self.assertEqual(got(category="rust"), set(ids[:2]))
            self.assertEqual(got(category="rust", status="rejected"), {ids[0]})
            self.assertEqual(got(category="crack", status="rejected"), set())
            self.assertEqual(got(category="rust", status="active"), {ids[1]})


if __name__ == "__main__":
    unittest.main()
