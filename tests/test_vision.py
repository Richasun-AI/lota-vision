import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lota import vision


class VisionTests(unittest.TestCase):
    def test_suggestions_cache_by_content_and_model_revision(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            model = root / "weights"
            model.mkdir()
            (model / "config.json").write_text("{}", encoding="utf-8")
            original = root / "photo.png"
            original.write_bytes(b"photo content")
            duplicate = root / "copy.png"
            duplicate.write_bytes(original.read_bytes())

            def scorer(paths, *_):
                return [(path, [0.1, 0.8]) for path in paths]

            with patch.object(vision, "_score_batches", side_effect=scorer) as score:
                first = vision.suggest_types(root, [original, duplicate], ["room", "tower"], model, 8, "cpu")
                second = vision.suggest_types(root, [duplicate], ["room", "tower"], model, 8, "cpu")
                self.assertEqual(score.call_count, 1)
                self.assertEqual(first["cache_hits"], 1)
                self.assertEqual(second["cache_hits"], 1)
                self.assertEqual(first["items"][0]["candidates"][0], {"label": "tower", "score": 0.8})
                self.assertEqual(first["items"][0]["status"], "needs_review")
                self.assertEqual(first["items"][0]["sha256"], first["items"][1]["sha256"])
                (model / "config.json").write_text('{"revision":2}', encoding="utf-8")
                revised = vision.suggest_types(root, [original], ["room", "tower"], model, 8, "cpu")
                self.assertNotEqual(revised["model_fingerprint"], first["model_fingerprint"])
                self.assertEqual(score.call_count, 2)

    def test_ocr_returns_local_predictions_and_preserves_geometry(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / "photo.png"
            polygon = [[1, 2], [9, 2], [9, 5], [1, 5]]
            recognizer = Mock()
            recognizer.predict.return_value = [{"rec_texts": ["123.4"], "rec_scores": [0.96],
                                                 "rec_polys": [polygon]}]
            module = SimpleNamespace(PaddleOCR=Mock(return_value=recognizer))
            env = {"LOTA_OCR_DET_MODEL_DIR": str(root), "LOTA_OCR_REC_MODEL_DIR": str(root),
                   "LOTA_OCR_LANGUAGE": "en"}
            with patch.dict(os.environ, env), patch.dict("sys.modules", {"paddleocr": module}):
                output = vision.read_text([source], "en")
                with self.assertRaisesRegex(ValueError, "configured PaddleOCR recognition language"):
                    vision.read_text([source], "ch")
            self.assertEqual(output["items"][0]["lines"], [{"text": "123.4", "confidence": 0.96,
                                                            "polygon": polygon}])
            self.assertEqual(module.PaddleOCR.call_args.kwargs["text_detection_model_dir"], str(root.resolve()))
