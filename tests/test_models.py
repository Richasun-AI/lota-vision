"""Model registry and frozen dataset regression tests."""

from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from lota import models


class ModelsTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="lota-model-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "projects" / "demo"
        self.weights = self.root / "initial.pt"
        self.weights.write_bytes(b"initial weights")

    def test_model_versions_and_aliases_are_scenario_scoped(self):
        first = models.register(self.project, "v1", self.weights, classes=["weed"])
        self.assertEqual(models.register(self.project, "v1", self.weights, classes=["weed"]), first)
        self.weights.write_bytes(b"different weights")
        with self.assertRaisesRegex(ValueError, "version already exists"):
            models.register(self.project, "v1", self.weights, classes=["weed"])
        models.register(self.project, "v2", self.weights, classes=["weed"])

        self.assertIsNone(models.set_alias(self.project, "production", "v1")["previous_version"])
        self.assertEqual(models.set_alias(self.project, "production", "v2")["previous_version"], "v1")
        self.assertEqual(models.set_alias(self.project, "production", "v1")["previous_version"], "v2")
        self.assertEqual(models.list_models(self.project)["aliases"], {"production": "v1"})
        _, artifact = models.resolve_model(self.project, "@production")
        self.assertEqual(artifact.read_bytes(), b"initial weights")
        with self.assertRaises(FileNotFoundError):
            models.resolve_model(self.root / "projects" / "other", "@production")
        artifact.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "checksum"):
            models.resolve_model(self.project, "v1")

    def _dataset(self):
        directory = self.project / "datasets" / "data-v1"
        directory.mkdir(parents=True)
        directory.joinpath("data.yaml").write_text("names: [weed]\n", encoding="utf-8")
        labels = "0 0.50000000 0.50000000 0.50000000 0.50000000\n"
        items = []
        for split in ("train", "val"):
            data = split.encode()
            image_id = sha256(data).hexdigest()
            image = directory / "images" / split / f"{image_id}.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(data)
            label_file = directory / "labels" / split / f"{image_id}.txt"
            label_file.parent.mkdir(parents=True)
            label_file.write_text(labels, encoding="utf-8")
            items.append({
                "image_id": image_id, "path": image.relative_to(directory).as_posix(),
                "split": split, "labels": [["weed", 0.5, 0.5, 0.5, 0.5]],
            })
        identity = sha256(json.dumps(
            ["detect", ["weed"], items], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        directory.joinpath("manifest.json").write_text(json.dumps({
            "version": "data-v1", "dataset_id": identity, "task": "detect", "classes": ["weed"],
            "counts": {"train": 1, "val": 1, "test": 0}, "images": items,
        }), encoding="utf-8")
        return directory, items

    def test_training_uses_private_verified_dataset_and_registers_provenance(self):
        directory, items = self._dataset()
        original = directory / items[0]["path"]
        label_file = next((directory / "labels" / "train").glob("*.txt"))

        def train(data, project, **options):
            staged = Path(yaml.safe_load(Path(data).read_text(encoding="utf-8"))["path"])
            self.assertNotEqual(staged, directory)
            self.assertEqual((staged / items[0]["path"]).read_bytes(), b"train")
            (staged / items[0]["path"]).write_bytes(b"runtime JPEG repair")
            best = Path(project) / "best.pt"
            best.write_bytes(b"trained weights")
            model.trainer = SimpleNamespace(best=best, model=SimpleNamespace(names={0: "weed"}))
            return SimpleNamespace(results_dict={"metrics/mAP50(B)": 0.8})

        model = SimpleNamespace(train=train)
        with patch.object(models, "_runtime_yolo", return_value=model) as runtime:
            metadata = models.train(self.project, "data-v1", "model-v1", self.weights, epochs=1)
            self.assertEqual(models.train(self.project, "data-v1", "model-v1", self.weights, epochs=1), metadata)
            runtime.assert_called_once()
        self.assertEqual(original.read_bytes(), b"train")
        self.assertEqual(metadata["dataset_version"], "data-v1")
        self.assertEqual(metadata["metrics"], {"metrics/mAP50(B)": 0.8})

        label_file.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "labels failed identity"):
            models.train(self.project, "data-v1", "other-version", self.weights, epochs=1)

    def test_prediction_output_cannot_escape_scenario_directory(self):
        models.register(self.project, "v1", self.weights, classes=["weed"])
        image = self.root / "frame.jpg"
        image.write_bytes(b"image")
        with patch.object(models, "_predict_upstream") as runtime:
            with self.assertRaisesRegex(ValueError, "stay inside"):
                models.predict(self.project, "v1", [image], "../out.json")
            runtime.assert_not_called()
