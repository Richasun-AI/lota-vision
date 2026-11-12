"""Optional local vision tools for curation and text recognition."""

import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import ExitStack, closing
from itertools import islice
from pathlib import Path

from PIL import Image, ImageOps


_PROMPT = "This is a photo of {}."
_WEIGHT_TYPES = {".safetensors", ".bin", ".pt", ".pth"}


def _fingerprint(model_dir):
    """Identify a local model revision without rereading its weights per batch."""
    state = []
    for file in sorted(model_dir.rglob("*")):
        if not file.is_file():
            continue
        stat = file.stat()
        detail = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if file.suffix.lower() not in _WEIGHT_TYPES and stat.st_size <= 16 * 1024 * 1024:
            with file.open("rb") as stream:
                detail = hashlib.file_digest(stream, "sha256").hexdigest()
        state.append((file.relative_to(model_dir).as_posix(), detail))
    if not state or not (model_dir / "config.json").is_file():
        raise ValueError(f"Expected a local SigLIP2 model directory: {model_dir}")
    return hashlib.sha256(json.dumps(state).encode()).hexdigest()


def _read_content_id(path):
    with path.open("rb") as image:
        return hashlib.file_digest(image, "sha256").hexdigest()


def _batches(items, size):
    remaining = iter(items)
    while chunk := list(islice(remaining, size)):
        yield chunk


def _pooled(features):
    # Transformers 4.x returns a tensor, while 5.x returns a model output.
    return features.pooler_output if hasattr(features, "pooler_output") else features


def _score_batches(paths, classes, model_dir, batch_size, device):
    try:
        import torch
        from transformers import AutoModel, AutoProcessor
    except ImportError as exc:
        raise RuntimeError("Install the 'curation' extra to use SigLIP2 suggestions") from exc
    if device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA is unavailable for the SigLIP2 worker")
    processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(model_dir, local_files_only=True, trust_remote_code=False)
    if model.config.model_type not in {"siglip", "siglip2"}:
        raise ValueError("The configured curation model is not SigLIP2")
    model = model.to(device).eval()
    prompts = [_PROMPT.format(label.replace("_", " ").replace("-", " ")) for label in classes]
    max_length = model.config.text_config.max_position_embeddings
    text_inputs = processor(
        text=prompts, return_tensors="pt", padding="max_length",
        truncation=True, max_length=max_length,
    ).to(device)
    with torch.inference_mode():
        text_features = torch.nn.functional.normalize(_pooled(model.get_text_features(**text_inputs)), dim=-1)
        scale = model.logit_scale.exp()
    for chunk in _batches(paths, batch_size):
        with ExitStack() as stack:
            images = []
            for path in chunk:
                original = stack.enter_context(Image.open(path))
                oriented = ImageOps.exif_transpose(original)
                stack.callback(oriented.close)
                rgb = oriented.convert("RGB")
                stack.callback(rgb.close)
                images.append(rgb)
            inputs = processor(images=images, return_tensors="pt").to(device)
            with torch.inference_mode():
                image_features = torch.nn.functional.normalize(_pooled(model.get_image_features(**inputs)), dim=-1)
                logits = (image_features @ text_features.T) * scale + model.logit_bias
                scores = logits.sigmoid().float().cpu().tolist()
        if len(scores) != len(chunk) or any(len(row) != len(classes) for row in scores):
            raise RuntimeError("SigLIP2 returned an unexpected score matrix")
        yield from zip(chunk, scores, strict=True)


def suggest_types(project_dir, paths, classes, model_dir, batch_size, device):
    """Score selected images against the scene's classes for human review."""
    if not classes or not paths or batch_size < 1:
        raise ValueError("Curation requires classes, images and a positive batch size")
    paths = [Path(path) for path in paths]
    fingerprint = _fingerprint(Path(model_dir))
    profile = hashlib.sha256(json.dumps([fingerprint, _PROMPT, classes, "rgb-exif-v2"]).encode()).hexdigest()
    content_ids = [_read_content_id(path) for path in paths]
    catalog = Path(project_dir) / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(catalog / "curation.sqlite3", timeout=15)) as database:
        database.execute("""CREATE TABLE IF NOT EXISTS scores (
            profile TEXT NOT NULL, content_id TEXT NOT NULL, score_json TEXT NOT NULL,
            PRIMARY KEY (profile, content_id))""")
        cached = {
            content_id: json.loads(row[0])
            for content_id in dict.fromkeys(content_ids)
            if (row := database.execute("SELECT score_json FROM scores WHERE profile=? AND content_id=?",
                                        (profile, content_id)).fetchone())
        }
        misses = {}
        for path, content_id in zip(paths, content_ids, strict=True):
            if content_id not in cached:
                misses.setdefault(content_id, path)
        by_path = {path: content_id for content_id, path in misses.items()}
        uncached = _score_batches(by_path, classes, model_dir, batch_size, device) if misses else ()
        for path, row in uncached:
            content_id = by_path[path]
            if _read_content_id(path) != content_id:
                raise ValueError(f"Image changed during curation: {path.name}")
            with database:
                database.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?)",
                                 (profile, content_id, json.dumps(row, allow_nan=False)))
            cached[content_id] = row
    results = []
    for path, content_id in zip(paths, content_ids, strict=True):
        ranked = sorted(zip(classes, cached[content_id], strict=True), key=lambda item: item[1], reverse=True)
        results.append({"path": str(path), "image_id": content_id, "sha256": content_id, "status": "needs_review",
                        "candidates": [{"label": label, "score": score} for label, score in ranked[:5]]})
    return {"model_fingerprint": fingerprint, "cache_hits": len(paths) - len(misses), "items": results}


def _ocr_model_dirs(language):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,24}", language):
        raise ValueError("Invalid PaddleOCR language code")
    configured = os.environ.get("LOTA_OCR_LANGUAGE", "en")
    if configured != language:
        raise ValueError(f"The configured PaddleOCR recognition language is {configured!r}")
    directories = []
    for name in ("LOTA_OCR_DET_MODEL_DIR", "LOTA_OCR_REC_MODEL_DIR"):
        value = os.environ.get(name)
        if not value or not Path(value).expanduser().is_dir():
            raise ValueError(f"Set {name} to an installed local PaddleOCR model directory")
        directories.append(str(Path(value).expanduser().resolve()))
    return directories


def _plain(value):
    return value.tolist() if hasattr(value, "tolist") else value


def _ocr_lines(result):
    if "rec_texts" not in result:
        raise RuntimeError("Expected a PaddleOCR 3.x recognition result")
    texts = result["rec_texts"]
    scores = result["rec_scores"]
    boxes = result.get("rec_polys")
    if boxes is None:
        boxes = result.get("rec_boxes")
    if len(texts) != len(scores) or (boxes is not None and len(texts) != len(boxes)):
        raise RuntimeError("PaddleOCR returned unaligned recognition results")
    lines = []
    for index, text in enumerate(texts):
        score = float(scores[index])
        if not math.isfinite(score):
            raise RuntimeError("PaddleOCR returned an invalid confidence score")
        lines.append({"text": str(text), "confidence": score,
                      "polygon" if result.get("rec_polys") is not None else "xyxy":
                      _plain(boxes[index]) if boxes is not None else None})
    return lines


def read_text(paths, language):
    """Read text from images using preinstalled PaddleOCR 3.x models."""
    det_dir, rec_dir = _ocr_model_dirs(language)
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError("Install the 'ocr' extra to use PaddleOCR") from exc
    engine = PaddleOCR(
        text_detection_model_dir=det_dir, text_recognition_model_dir=rec_dir,
        use_doc_orientation_classify=False, use_doc_unwarping=False,
        use_textline_orientation=False, device="cpu",
    )
    items = []
    for path in paths:
        lines = [line for result in engine.predict(input=str(path)) for line in _ocr_lines(result)]
        items.append({"path": str(path), "lines": lines})
    return {"language": language, "items": items}
