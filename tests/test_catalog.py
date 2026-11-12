from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from lota import catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "scenario"
        self.source = self.root / "downloads" / "all"
        self.source.mkdir(parents=True)
        self.manifest = self.source.parent / "manifest.json"
        self.config = [{
            "name": "source",
            "path": str(self.source),
            "manifest_path": str(self.manifest),
            "site_pattern": r"^(?P<site>S\d+)_",
            "checkpoint": "equipment",
        }]
        self.manifest.write_text("{}", encoding="utf-8")

    def photo(self, name, color):
        path = self.source / name
        with Image.new("RGB", (64, 48), color) as image:
            image.save(path, format="PNG")
        return path

    def test_exact_aliases_manual_metadata_and_verified_copy(self):
        original = self.photo("S1_first.png", "red")
        alias = self.source / "S2_copy.jpg"
        shutil.copyfile(original, alias)
        self.manifest.write_text('{"100":"all/S1_first.png"}', encoding="utf-8")
        summary = catalog.scan(self.project, self.config, stability_seconds=0)
        self.assertEqual((summary["indexed"], summary["duplicates"]), (1, 1))
        self.assertFalse((self.project / "raw").exists())
        item, = catalog.list_images(self.project, checkpoint="equipment")
        self.assertIsNone(item["site_id"])
        self.assertEqual(item["site_conflict"], ["S1", "S2"])
        self.assertEqual(item["aliases"][0]["file_ids"], ["100"])
        self.assertEqual((item["width"], item["height"]), (64, 48))
        self.assertIn("small_resolution", item["quality"]["flags"])
        image_id = item["image_id"]
        item = catalog.review_image(
            self.project, image_id, decision="accept", site_id="S1", checkpoint="confirmed", note="reviewed",
        )
        self.assertEqual((item["status"], item["site_id"], item["checkpoint"]), ("accepted", "S1", "confirmed"))
        extracted = catalog.extract(self.project, [image_id, image_id])
        self.assertEqual(extracted["copied"], 1)
        destination = Path(extracted["items"][0]["raw_path"])
        self.assertEqual(destination.suffix, ".png")
        self.assertEqual(destination.read_bytes(), original.read_bytes())
        self.assertEqual(destination.parent.parent, self.project / "raw")
        self.assertEqual(catalog.extract(self.project, [image_id])["reused"], 1)
        self.assertEqual(catalog.stats(self.project)["extracted"], 1)
        self.assertEqual(catalog.list_images(self.project, status="extracted")[0]["path"], str(destination))

    def test_incremental_scan_keeps_manifest_cache_and_retries_stable_arrivals(self):
        path = self.photo("S1_first.png", "red")
        self.manifest.write_text('{"100":"all/S1_first.png"}', encoding="utf-8")
        now = time.time()
        os.utime(path, (now, now))
        first = catalog.scan(self.project, self.config, stability_seconds=10)
        self.assertEqual(first["unstable"], 1)
        self.assertEqual(catalog.stats(self.project)["total"], 0)
        os.utime(path, (now - 20, now - 20))
        second = catalog.scan(self.project, self.config, stability_seconds=10)
        self.assertEqual(second["indexed"], 1)
        self.manifest.write_text("{", encoding="utf-8")
        unchanged = catalog.scan(self.project, self.config, stability_seconds=10)
        self.assertEqual((unchanged["unchanged"], unchanged["inspected"]), (1, 0))
        self.assertEqual(unchanged["error_count"], 1)
        item, = catalog.list_images(self.project)
        self.assertEqual(item["aliases"][0]["file_ids"], ["100"])

    def test_extraction_falls_back_to_alias_and_rejects_changed_source(self):
        original = self.photo("S1_first.png", "red")
        alias = self.source / "S1_duplicate.png"
        shutil.copyfile(original, alias)
        catalog.scan(self.project, self.config, stability_seconds=0)
        item, = catalog.list_images(self.project)
        Path(item["source_path"]).unlink()
        extracted = catalog.extract(self.project, [item["image_id"]])
        self.assertEqual(extracted["copied"], 1)
        self.assertEqual(catalog.scan(self.project, self.config, stability_seconds=0)["missing"], 1)
        new = self.photo("S1_new.png", "blue")
        catalog.scan(self.project, self.config, stability_seconds=0)
        item = catalog.list_images(self.project)[0]
        self.assertEqual(item["source_path"], str(new))
        new.write_bytes(b"partially overwritten")
        self.assertEqual(catalog.extract(self.project, [item["image_id"]])["failed"], 1)
        self.assertIsNone(catalog.get_image(self.project, item["image_id"])["raw_path"])

    def test_limited_scans_advance_and_dhash_rechecks_full_distance(self):
        for n, color in enumerate(("red", "green", "blue")):
            self.photo(f"S1_{n}.png", color)
        for expected in (1, 2, 3):
            self.assertEqual(catalog.scan(self.project, self.config, limit=1, stability_seconds=0)["indexed"], 1)
            self.assertEqual(catalog.stats(self.project)["total"], expected)
        first, second, third = [image["image_id"] for image in catalog.list_images(self.project)]
        with catalog._database(self.project) as db, db:
            db.executemany(
                "UPDATE images SET dhash=? WHERE image_id=?",
                [("0000000000000000", first), ("0000010101010101", second), ("0101010101010101", third)],
            )
        near = catalog.near_duplicates(self.project, first, max_distance=6)
        self.assertEqual([(item["image_id"], item["distance"]) for item in near], [(second, 6)])
        all_neighbors = catalog.near_duplicates(self.project, first, max_distance=8)
        self.assertEqual([item["distance"] for item in all_neighbors], [6, 8])


if __name__ == "__main__":
    unittest.main()
