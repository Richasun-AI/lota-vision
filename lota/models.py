"""Manage scenario-scoped YOLO model versions and their runtime artifacts."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from uuid import uuid4


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_TASKS = {"detect", "classify", "segment", "obb", "pose"}
_FORMATS = {".pt", ".onnx"}
_TRAIN_OPTIONS = {
    "amp", "batch", "cache", "close_mosaic", "cos_lr", "deterministic", "device",
    "epochs", "fraction", "freeze", "imgsz", "lr0", "lrf", "optimizer", "patience",
    "plots", "seed", "verbose", "warmup_epochs", "weight_decay", "workers",
    # 数据增强（项目设置页统一管理）
    "hsv_h", "hsv_s", "hsv_v", "degrees", "translate", "scale", "shear", "perspective",
    "flipud", "fliplr", "mosaic", "mixup", "copy_paste", "erasing",
}
_EVALUATE_OPTIONS = {
    "batch", "conf", "device", "half", "imgsz", "iou", "max_det", "plots", "verbose",
    "workers",
}
_PREDICT_OPTIONS = {"batch", "conf", "confidence", "device", "half", "imgsz", "iou", "max_det"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or value.endswith("."):
        raise ValueError(f"Invalid model, dataset or alias identifier: {value!r}")
    return value


def _inside(directory: Path, path: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError(f"Path must stay inside {directory}")
    return path


def _managed(project_dir: Path, subdir: str) -> Path:
    root = Path(project_dir).resolve()
    return _inside(root, root / subdir)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _class_names(classes: Sequence[str] | Mapping) -> list[str]:
    if isinstance(classes, Mapping):
        indexed = {int(key): value for key, value in classes.items()}
        if len(indexed) != len(classes) or set(indexed) != set(range(len(indexed))):
            raise ValueError("Class mapping must use contiguous IDs starting at zero")
        classes = [indexed[index] for index in range(len(indexed))]
    if isinstance(classes, (str, bytes)) or not isinstance(classes, Sequence):
        raise ValueError("Classes must be an ordered list of names or an ID-to-name mapping")
    names = list(classes)
    if (
        not names
        or any(not isinstance(name, str) or not name.strip() for name in names)
        or len(set(names)) != len(names)
    ):
        raise ValueError("Classes must have distinct, nonempty names")
    return names


def _weight_file(weights: str | Path, formats: set[str] = _FORMATS) -> Path:
    path = Path(weights).expanduser().resolve()
    if path.suffix.lower() not in formats or not path.is_file():
        raise ValueError(f"Expected an existing local weight file ({', '.join(sorted(formats))}): {path}")
    return path


def _dataset(project_dir: Path, version: str) -> tuple[dict, Path]:
    base = _managed(project_dir, "datasets")
    directory = _inside(base, base / _identifier(version))
    manifest = _json(directory / "manifest.json")
    if manifest.get("version") != version or manifest.get("task") not in _TASKS:
        raise ValueError(f"Dataset {version} has an invalid version or task")
    if not isinstance(manifest.get("dataset_id"), str) or not manifest["dataset_id"]:
        raise ValueError(f"Dataset {version} has no dataset identity")
    manifest["classes"] = _class_names(manifest["classes"])
    if manifest["task"] == "classify" and manifest["classes"] != sorted(manifest["classes"]):
        raise ValueError("Classification class IDs must follow the alphabetic folder order")
    if "images" in manifest:
        digest = hashlib.sha256(json.dumps(
            [manifest["task"], manifest["classes"], manifest["images"]],
            ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        if digest != manifest["dataset_id"]:
            raise ValueError(f"Dataset {version} failed manifest identity verification")
    return manifest, directory


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
    )


def _copy_verified(source: Path, destination: Path, *, durable: bool = True) -> str:
    with source.open("rb") as reader, destination.open("xb") as writer:
        before = os.fstat(reader.fileno())
        digest = hashlib.sha256()
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            digest.update(chunk)
            writer.write(chunk)
        if not _same_file(before, source.stat()):
            raise ValueError(f"File changed while being copied: {source}")
        if durable:
            writer.flush()
            os.fsync(writer.fileno())
    return digest.hexdigest()


def _register(
    project_dir: Path,
    version: str,
    weights: str | Path,
    *,
    classes: Sequence[str] | Mapping,
    task: str,
    dataset_version: str | None,
    metrics: dict | None,
    training: dict | None = None,
) -> dict:
    root = _managed(project_dir, "models")
    target = _inside(root, root / _identifier(version))
    weight_path = _weight_file(weights)
    names = _class_names(classes)
    if task not in _TASKS:
        raise ValueError(f"Unsupported model task: {task}")
    dataset = _dataset(project_dir, dataset_version)[0] if dataset_version else None
    if dataset and (dataset["task"] != task or dataset["classes"] != names):
        raise ValueError("Model task and class IDs must match the registered dataset")
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".register-", dir=root))
    try:
        name = "weights" + weight_path.suffix.lower()
        artifact = {
            "version": version,
            "task": task,
            "classes": names,
            "weights": name,
            "sha256": _copy_verified(weight_path, staging / name),
            "format": weight_path.suffix.lower()[1:],
            "dataset_version": dataset_version,
            "dataset_id": dataset["dataset_id"] if dataset else None,
            "metrics": metrics or {},
            "training": training,
        }
        metadata = {**artifact, "created_at": _now()}
        _write_json(staging / "model.json", metadata)
        try:
            staging.rename(target)
        except OSError:
            if not target.is_dir():
                raise
            existing, registered_path = _model_artifact(root, version)
            if all(existing.get(key) == value for key, value in artifact.items()):
                if _sha256(registered_path) != existing["sha256"]:
                    raise ValueError(f"Registered weights failed checksum verification: {version}")
                return existing
            raise ValueError(f"Model version already exists with different contents: {version}") from None
        return metadata
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def register(
    project_dir: Path,
    version: str,
    weights: str | Path,
    *,
    classes: Sequence[str] | Mapping,
    task: str = "detect",
    dataset_version: str | None = None,
    metrics: dict | None = None,
) -> dict:
    """Copy a trusted, local weight file into the immutable model registry."""
    return _register(
        project_dir, version, weights, classes=classes, task=task,
        dataset_version=dataset_version, metrics=metrics,
    )


def _model_artifact(root: Path, version: str) -> tuple[dict, Path]:
    directory = _inside(root, root / _identifier(version))
    metadata = _json(directory / "model.json")
    if metadata.get("version") != version or metadata.get("task") not in _TASKS:
        raise ValueError(f"Invalid model metadata: {version}")
    weights = metadata.get("weights")
    if weights not in {"weights.pt", "weights.onnx"}:
        raise ValueError(f"Invalid registered model filename: {weights!r}")
    artifact = _inside(directory, directory / weights)
    if not artifact.is_file():
        raise FileNotFoundError(f"Registered weights are missing for {version}")
    return metadata, artifact


def _alias_database(root: Path) -> Path:
    return _inside(root, root / "_aliases.sqlite3")


def _read_aliases(root: Path) -> dict[str, str]:
    path = _alias_database(root)
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path, timeout=10)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'aliases'").fetchone() is None:
            return {}
        rows = connection.execute("SELECT alias, version FROM aliases ORDER BY alias").fetchall()
    return dict(rows)


def list_models(project_dir: Path) -> dict:
    root = _managed(project_dir, "models")
    models = []
    if root.is_dir():
        for directory in sorted(root.iterdir()):
            if not directory.name.startswith(".") and (directory / "model.json").is_file():
                metadata, _ = _model_artifact(root, directory.name)
                models.append(metadata)
    return {"models": models, "aliases": _read_aliases(root)}


def resolve_model(project_dir: Path, selector: str) -> tuple[dict, Path]:
    """Resolve a version or alias and check its registered weights."""
    if not isinstance(selector, str):
        raise ValueError("Model selector must be a version or alias")
    root = _managed(project_dir, "models")
    aliases = _read_aliases(root)
    version = _identifier(selector[1:] if selector.startswith("@") else selector)
    if selector.startswith("@") or not (root / version / "model.json").is_file():
        version = aliases.get(version, version)
    metadata, path = _model_artifact(root, version)
    if _sha256(path) != metadata.get("sha256"):
        raise ValueError(f"Registered weights failed checksum verification: {version}")
    return metadata, path


def set_alias(project_dir: Path, alias: str, version: str) -> dict:
    """Promote or roll back an alias within one scenario, recording the previous version."""
    alias, version = _identifier(alias), _identifier(version)
    root = _managed(project_dir, "models")
    metadata, weights = _model_artifact(root, version)
    if _sha256(weights) != metadata.get("sha256"):
        raise ValueError(f"Registered weights failed checksum verification: {version}")
    with closing(sqlite3.connect(_alias_database(root), timeout=10, isolation_level=None)) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, version TEXT NOT NULL)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS alias_history (
                id INTEGER PRIMARY KEY, alias TEXT NOT NULL, previous_version TEXT,
                version TEXT NOT NULL, changed_at TEXT NOT NULL
            )"""
        )
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT version FROM aliases WHERE alias = ?", (alias,)).fetchone()
        previous = row[0] if row else None
        if previous != version:
            connection.execute(
                """INSERT INTO aliases(alias, version) VALUES (?, ?)
                   ON CONFLICT(alias) DO UPDATE SET version = excluded.version""",
                (alias, version),
            )
            connection.execute(
                "INSERT INTO alias_history(alias, previous_version, version, changed_at) VALUES (?, ?, ?, ?)",
                (alias, previous, version, _now()),
            )
        connection.commit()
    return {"alias": alias, "version": version, "previous_version": previous}


def _options(params: dict, permitted: set[str], defaults: dict) -> dict:
    extra = params.keys() - permitted
    if extra:
        raise ValueError(f"Unsupported model parameters: {', '.join(sorted(extra))}")
    return {**defaults, **params}


def _runtime_yolo(project_dir: Path, weights: Path, task: str):
    runtime = _managed(project_dir, "runtime") / "ultralytics"
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("YOLO_CONFIG_DIR", str(runtime))
    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    try:
        from ultralytics import YOLO
    except ImportError as error:
        raise RuntimeError("Install the Ultralytics model runtime in the worker environment") from error
    model = YOLO(str(weights), task=task)
    if model.task != task:
        raise ValueError(f"Weight task {model.task!r} does not match {task!r}")
    return model


def _copy_tree(source: Path, destination: Path) -> None:
    def reject_links(folder, names):
        for name in names:
            if (Path(folder) / name).is_symlink():
                raise ValueError(f"Dataset must contain materialized files: {name}")
        return []

    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"Dataset split directory is unavailable: {source}")
    shutil.copytree(source, destination, ignore=reject_links)


def _stage_manifest_images(manifest: dict, directory: Path, staging: Path, expected_splits: list[str]) -> None:
    counts = Counter()
    seen = set()
    class_ids = {name: index for index, name in enumerate(manifest["classes"])}
    for item in manifest["images"]:
        relative = Path(item["path"])
        split, image_id = item["split"], item["image_id"]
        if (
            relative.is_absolute() or relative.as_posix() != item["path"]
            or split not in expected_splits or len(relative.parts) != 3
            or relative.stem != image_id or not re.fullmatch(r"[a-f0-9]{64}", image_id)
            or image_id in seen
        ):
            raise ValueError(f"Invalid or duplicate image in dataset manifest: {relative}")
        seen.add(image_id)
        counts[split] += 1
        if manifest["task"] in ("detect", "segment", "obb", "pose"):
            if relative.parts[:2] != ("images", split):
                raise ValueError(f"Detection image is outside its assigned split: {relative}")
            label_path = Path("labels") / split / f"{image_id}.txt"
            labels = "".join(
                f"{class_ids[name]} {' '.join(f'{value:.8f}' for value in box)}\n"
                for name, *box in item["labels"]
            )
            if _inside(directory, directory / label_path).read_text(encoding="utf-8") != labels:
                raise ValueError(f"Dataset labels failed identity verification: {label_path}")
            staged_label = staging / label_path
            staged_label.parent.mkdir(parents=True, exist_ok=True)
            staged_label.write_text(labels, encoding="utf-8", newline="\n")
        else:
            category = relative.parts[1]
            if relative.parts[0] != split or category not in class_ids or item["labels"] != [category]:
                raise ValueError(f"Classification image is outside its assigned class: {relative}")
        source = _inside(directory, directory / relative)
        target = _inside(staging, staging / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if _copy_verified(source, target, durable=False) != image_id:
            raise ValueError(f"Dataset image failed checksum verification: {relative}")

    for split in expected_splits:
        if not counts[split] or counts[split] != manifest["counts"].get(split):
            raise ValueError(f"Dataset {split} split differs from its manifest count")


def _stage_dataset(manifest: dict, directory: Path, staging: Path, split: str = "val") -> Path:
    """Give the trainer private image and label copies for its cache and JPEG repair."""
    expected_splits = ["train", "val"]
    test_directory = directory / ("images/test" if manifest["task"] in ("detect", "segment", "obb", "pose") else "test")
    if (
        manifest.get("counts", {}).get("test") or split == "test"
        or ("images" not in manifest and test_directory.is_dir())
    ):
        expected_splits.append("test")
    if "images" in manifest:
        _stage_manifest_images(manifest, directory, staging, expected_splits)
    if manifest["task"] == "classify":
        if "images" not in manifest:
            for folder in expected_splits:
                _copy_tree(directory / folder, staging / folder)
        return staging

    import yaml

    if not (directory / "data.yaml").is_file():
        raise FileNotFoundError(f"YOLO data.yaml is missing in {directory}")
    data = {"path": str(staging), "names": manifest["classes"]}
    if manifest.get("kpt_shape"):   # 姿态任务
        data["kpt_shape"] = manifest["kpt_shape"]
        data["flip_idx"] = manifest.get("flip_idx") or list(range(manifest["kpt_shape"][0]))
    for folder in expected_splits:
        if "images" not in manifest:
            for parent in ("images", "labels"):
                source = _inside(directory, directory / parent / folder)
                _copy_tree(source, staging / parent / folder)
        data[folder] = f"images/{folder}"
    config = staging / "data.yaml"
    config.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config


def _metric_dict(results) -> dict:
    metrics = {}
    for key, value in getattr(results, "results_dict", {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            metrics[str(key)] = number
    # 按类别的成绩（扁平键 class/<类别>/ap50 等，不影响原有读取）
    try:
        box, names = getattr(results, "box", None), getattr(results, "names", None) or {}
        idx = getattr(results, "ap_class_index", None)
        if box is not None and idx is not None and len(idx):
            for i, k in enumerate(list(idx)):
                name = names[int(k)] if isinstance(names, dict) else names[int(k)]
                for key, arr in (("ap50", getattr(box, "ap50", None)), ("ap", getattr(box, "ap", None)),
                                 ("precision", getattr(box, "p", None)), ("recall", getattr(box, "r", None))):
                    if arr is not None and len(arr) > i and math.isfinite(float(arr[i])):
                        metrics[f"class/{name}/{key}"] = float(arr[i])
    except Exception:
        pass
    return metrics


def train(project_dir: Path, dataset_version: str, model_version: str, weights: str | Path, **params) -> dict:
    """Run Ultralytics training on a released dataset and register its best checkpoint."""
    version = _identifier(model_version)
    manifest, directory = _dataset(project_dir, dataset_version)
    initial = _weight_file(weights, {".pt"})
    options = _options(params, _TRAIN_OPTIONS, {"epochs": 50, "workers": 0, "cache": False, "seed": 42})
    initial_digest = _sha256(initial)
    provenance = {"initial_weights": initial.name, "initial_sha256": initial_digest, "parameters": options}
    existing = _managed(project_dir, "models") / version / "model.json"
    if existing.is_file():
        metadata, _ = resolve_model(project_dir, version)
        record = metadata.get("training") or {}
        if (
            metadata.get("dataset_version") == dataset_version
            and metadata.get("dataset_id") == manifest["dataset_id"]
            and all(record.get(key) == value for key, value in provenance.items())
        ):
            return metadata
        raise ValueError(f"Model version already exists: {version}")

    runs = _managed(project_dir, "runs")
    run = _inside(runs, runs / version / uuid4().hex)
    run.mkdir(parents=True, exist_ok=False)
    _write_json(run / "request.json", {
        **provenance, "dataset_version": dataset_version, "dataset_id": manifest["dataset_id"],
        "model_version": version,
    })
    with tempfile.TemporaryDirectory(prefix=".data-", dir=run) as temporary:
        work = Path(temporary)
        staged_weights = work / "initial.pt"
        if _copy_verified(initial, staged_weights) != initial_digest:
            raise ValueError("Initial weights changed after provenance was recorded")
        data_root = work / "dataset"
        data_root.mkdir()
        data = _stage_dataset(manifest, directory, data_root)
        model = _runtime_yolo(project_dir, staged_weights, manifest["task"])
        results = model.train(
            data=str(data), project=str(run), name="train", exist_ok=False, **options,
        )
    best = _inside(run, Path(model.trainer.best))
    trained_names = getattr(getattr(model.trainer, "model", None), "names", None)
    if trained_names is not None and _class_names(trained_names) != manifest["classes"]:
        raise ValueError("Trained model class IDs do not match the dataset manifest")
    metrics = _metric_dict(results)
    metadata = _register(
        project_dir, version, best, classes=manifest["classes"], task=manifest["task"],
        dataset_version=dataset_version, metrics=metrics,
        training={**provenance, "run": run.relative_to(Path(project_dir).resolve()).as_posix()},
    )
    _write_json(run / "result.json", {"model_version": version, "metrics": metrics, "sha256": metadata["sha256"]})
    return metadata


def evaluate(project_dir: Path, selector: str, dataset_version: str, *, split: str = "val", **params) -> dict:
    """Evaluate a registered model against a frozen validation or test split."""
    if split not in {"val", "test"}:
        raise ValueError("Evaluation split must be val or test")
    metadata, weights = resolve_model(project_dir, selector)
    manifest, directory = _dataset(project_dir, dataset_version)
    if (metadata["task"], metadata["classes"]) != (manifest["task"], manifest["classes"]):
        raise ValueError("The model and evaluation dataset must share task and class IDs")
    if not manifest.get("counts", {}).get(split):
        raise ValueError(f"Dataset {dataset_version} has no {split} examples")
    options = _options(params, _EVALUATE_OPTIONS, {"workers": 0, "plots": False})
    runs = _managed(project_dir, "runs")
    output = _inside(runs, runs / "_evaluations" / uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    model = _runtime_yolo(project_dir, weights, metadata["task"])
    runtime_names = getattr(model, "names", None)
    if runtime_names is not None and _class_names(runtime_names) != metadata["classes"]:
        raise ValueError("Model class IDs differ from the registered class names")
    with tempfile.TemporaryDirectory(prefix=".data-", dir=output) as temporary:
        data = _stage_dataset(manifest, directory, Path(temporary), split)
        results = model.val(
            data=str(data), split=split, project=str(output), name="validation", exist_ok=False,
            **options,
        )
    report = {
        "model_version": metadata["version"], "weights_sha256": metadata["sha256"],
        "dataset_version": dataset_version, "dataset_id": manifest["dataset_id"],
        "split": split, "metrics": _metric_dict(results),
        "report": str(output / "evaluation.json"),
    }
    _write_json(output / "evaluation.json", report)
    return report


class _UnsupportedONNX(Exception):
    """Select the upstream Ultralytics backend for an unsupported ONNX export layout."""


def _literal(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        try:
            return ast.literal_eval(text)
        except (TypeError, ValueError, SyntaxError):
            return None


def _providers(ort, device) -> list:
    if device is None or str(device).lower() == "cpu":
        return ["CPUExecutionProvider"]
    index = str(device).lower().removeprefix("cuda:")
    if index.isdecimal() and "CUDAExecutionProvider" in ort.get_available_providers():
        return [("CUDAExecutionProvider", {"device_id": int(index)}), "CPUExecutionProvider"]
    raise _UnsupportedONNX(f"ONNX Runtime provider is unavailable for device {device!r}")


def _onnx_input(np, image, height: int, width: int, floating_type):
    from PIL import Image

    scale = min(width / image.width, height / image.height)
    scaled_w, scaled_h = max(1, round(image.width * scale)), max(1, round(image.height * scale))
    left, top = (width - scaled_w) // 2, (height - scaled_h) // 2
    canvas = Image.new("RGB", (width, height), (114, 114, 114))
    canvas.paste(image.resize((scaled_w, scaled_h), Image.Resampling.BILINEAR), (left, top))
    tensor = np.ascontiguousarray(np.asarray(canvas).transpose(2, 0, 1)[None], dtype=floating_type)
    tensor /= floating_type(255)
    return tensor, scale, left, top


def _nms(np, boxes, scores, labels, threshold: float, max_det: int) -> list[int]:
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    pending = np.argsort(-scores, kind="stable")
    chosen = []
    while pending.size and len(chosen) < max_det:
        top = pending[0]
        chosen.append(int(top))
        pending = pending[1:]
        lo = np.maximum(boxes[top, :2], boxes[pending, :2])
        hi = np.minimum(boxes[top, 2:], boxes[pending, 2:])
        overlap = np.maximum(hi - lo, 0).prod(axis=1)
        iou = overlap / np.maximum(areas[top] + areas[pending] - overlap, 1e-9)
        pending = pending[(labels[pending] != labels[top]) | (iou <= threshold)]
    return chosen


def _onnx_detections(np, raw, classes: list[str], dimensions, transform, options: dict) -> list[dict]:
    attributes = len(classes) + 4
    if raw.ndim != 3 or raw.shape[0] != 1:
        raise _UnsupportedONNX(f"Unexpected detection output shape {raw.shape}")
    if raw.shape[1] == attributes and raw.shape[2] != attributes:
        rows = raw[0].T
    elif raw.shape[2] == attributes and raw.shape[1] != attributes:
        rows = raw[0]
    else:
        raise _UnsupportedONNX(f"Expected {attributes} detection attributes, received {raw.shape}")

    rows = rows[np.isfinite(rows).all(axis=1)]
    scores = rows[:, 4:]
    labels = scores.argmax(axis=1)
    scores = scores[np.arange(len(scores)), labels]
    selected = (scores >= options["conf"]) & (rows[:, 2] > 0) & (rows[:, 3] > 0)
    rows, scores, labels = rows[selected], scores[selected], labels[selected]
    if not len(rows):
        return []
    boxes = np.column_stack((rows[:, :2] - rows[:, 2:4] / 2, rows[:, :2] + rows[:, 2:4] / 2))
    chosen = _nms(np, boxes, scores, labels, options["iou"], options["max_det"])
    width, height = dimensions
    scale, left, top = transform
    boxes[:, (0, 2)] = np.clip((boxes[:, (0, 2)] - left) / scale, 0, width)
    boxes[:, (1, 3)] = np.clip((boxes[:, (1, 3)] - top) / scale, 0, height)
    return [
        {
            "class_id": int(labels[index]), "label": classes[labels[index]],
            "confidence": float(scores[index]), "xyxy": boxes[index].tolist(),
        }
        for index in chosen
        if boxes[index, 2] > boxes[index, 0] and boxes[index, 3] > boxes[index, 1]
    ]


def _predict_onnx(weights: Path, model: dict, paths: list[Path], options: dict) -> list[dict]:
    try:
        import numpy as np
        import onnxruntime as ort
        from PIL import Image, ImageOps
    except ImportError as error:
        raise _UnsupportedONNX("ONNX Runtime dependencies are unavailable") from error
    if model["task"] != "detect":
        raise _UnsupportedONNX("Classification ONNX exports use the upstream Ultralytics runtime")
    session = ort.InferenceSession(str(weights), providers=_providers(ort, options.get("device")))
    inputs = session.get_inputs()
    if len(inputs) != 1 or len(session.get_outputs()) != 1:
        raise _UnsupportedONNX("Expected one image input and one detection output")
    input_spec = inputs[0]
    shape = input_spec.shape
    types = {"tensor(float)": np.float32, "tensor(float16)": np.float16}
    if len(shape) != 4 or shape[0] != 1 or shape[1] != 3 or input_spec.type not in types:
        raise _UnsupportedONNX(f"Unsupported fixed NCHW input: {shape}, {input_spec.type}")
    height, width = shape[2:]
    if not all(isinstance(value, int) and value > 0 for value in (height, width)):
        raise _UnsupportedONNX(f"ONNX image dimensions are not fixed: {shape}")
    metadata = session.get_modelmeta().custom_metadata_map
    settings = _literal(metadata.get("args", ""))
    names = _literal(metadata.get("names", ""))
    if metadata.get("task") != "detect" or names is None or not isinstance(settings, dict):
        raise _UnsupportedONNX("Expected detection task, names and export arguments in ONNX metadata")
    if settings.get("nms") or settings.get("end2end") or _literal(metadata.get("end2end", "")):
        raise _UnsupportedONNX("End-to-end and embedded-NMS ONNX exports require Ultralytics")
    if _class_names(names) != model["classes"]:
        raise ValueError("ONNX model class IDs differ from the registered class names")
    supplied_size = options.get("imgsz")
    if supplied_size is not None:
        size = [supplied_size, supplied_size] if isinstance(supplied_size, int) else list(supplied_size)
        if size != [height, width]:
            raise ValueError(f"This ONNX model requires fixed input size {height}×{width}")

    predictions = []
    for path in paths:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
        tensor, scale, left, top = _onnx_input(np, image, height, width, types[input_spec.type])
        raw = session.run(None, {input_spec.name: tensor})[0]
        detections = _onnx_detections(np, raw, model["classes"], image.size, (scale, left, top), options)
        predictions.append({
            "path": str(path), "width": image.width, "height": image.height,
            "detections": detections,
        })
    return predictions


def _upstream_result(path: Path, result, metadata: dict, options: dict) -> dict:
    names = metadata["classes"]
    if _class_names(result.names) != names:
        raise ValueError("Model class IDs differ from the registered class names")
    height, width = result.orig_shape
    entry = {"path": str(path), "width": int(width), "height": int(height)}
    if metadata["task"] == "obb":   # 旋转框：轴对齐外接框 + 四角点
        ob = result.obb
        entry["detections"] = [
            {"class_id": int(k), "label": names[int(k)], "confidence": float(sc), "xyxy": xy, "obb": [[round(float(a), 1), round(float(b), 1)] for a, b in corners]}
            for k, sc, xy, corners in zip(ob.cls.cpu().tolist(), ob.conf.cpu().tolist(), ob.xyxy.cpu().tolist(), ob.xyxyxyxy.cpu().tolist())
        ] if ob is not None else []
    elif metadata["task"] in ("detect", "segment", "obb", "pose"):
        boxes = result.boxes
        if boxes is None:
            raise ValueError("Detection runtime returned a result without detection boxes")
        entry["detections"] = [
            {"class_id": int(label), "label": names[int(label)], "confidence": float(score), "xyxy": points}
            for label, score, points in zip(
                boxes.cls.cpu().tolist(), boxes.conf.cpu().tolist(), boxes.xyxy.cpu().tolist(), strict=True,
            )
        ]
        kp = getattr(result, "keypoints", None)
        if metadata["task"] == "pose" and kp is not None:   # 姿态：每个目标附关键点（原图像素坐标 + 置信度）
            xy = kp.xy.cpu().tolist()
            cf = kp.conf.cpu().tolist() if kp.conf is not None else [[1.0] * len(p) for p in xy]
            for det, pts, cs in zip(entry["detections"], xy, cf):
                det["keypoints"] = [[round(float(x), 1), round(float(y), 1), round(float(c), 3)] for (x, y), c in zip(pts, cs)]
        masks = getattr(result, "masks", None)
        if metadata["task"] == "segment" and masks is not None:   # 分割：每个目标附多边形（原图像素坐标）
            for det, poly in zip(entry["detections"], masks.xy):
                det["polygon"] = [[round(float(x), 1), round(float(y), 1)] for x, y in poly]
    else:
        if result.probs is None:
            raise ValueError("Classification runtime returned a result without probabilities")
        scores = result.probs.data.cpu().tolist()
        entry["classifications"] = [
            {"class_id": index, "label": names[index], "confidence": float(score)}
            for index, score in sorted(enumerate(scores), key=lambda item: -item[1])
            if score >= options["conf"]
        ]
    return entry


def _predict_upstream(project_dir: Path, weights: Path, metadata: dict, paths: list[Path], options: dict) -> list[dict]:
    model = _runtime_yolo(project_dir, weights, metadata["task"])
    batch = options.get("batch", 16)
    predictions = []
    for start in range(0, len(paths), batch):
        images = paths[start:start + batch]
        stream = model.predict(
            source=[str(image) for image in images], save=False, verbose=False, stream=True,
            **options,
        )
        predictions.extend(
            _upstream_result(path, result, metadata, options)
            for path, result in zip(images, stream, strict=True)
        )
    return predictions


def predict(
    project_dir: Path,
    selector: str,
    paths: Sequence[str | Path],
    output: str | Path | None = None,
    **params,
) -> dict:
    """Predict on local images with a scenario's resolved model and optional JSON output."""
    metadata, weights = resolve_model(project_dir, selector)
    if isinstance(paths, (str, bytes, Path)):
        raise ValueError("Pass a list of local image paths")
    images = [Path(path).resolve() for path in paths]
    if not images or any(not path.is_file() for path in images):
        raise ValueError("At least one image is required, and every input must be an existing local file")
    if "confidence" in params:
        if "conf" in params:
            raise ValueError("Specify either conf or confidence")
        params["conf"] = params.pop("confidence")
    options = _options(params, _PREDICT_OPTIONS, {"conf": 0.25, "iou": 0.7, "max_det": 300})
    if not 0 <= options["conf"] <= 1 or not 0 <= options["iou"] <= 1:
        raise ValueError("Confidence and IoU thresholds must be between zero and one")
    for key in ("max_det", "batch"):
        if key in options and (type(options[key]) is not int or options[key] <= 0):
            raise ValueError(f"{key} must be a positive integer")

    target = None
    if output is not None:
        directory = _managed(project_dir, "predictions")
        target = _inside(directory, directory / Path(output))
        if target.suffix.lower() != ".json":
            raise ValueError("Prediction output must use a .json filename")

    backend = "ultralytics"
    if (metadata.get("training") or {}).get("backend") == "torchvision-seg":   # 非 YOLO：语义分割
        from .backends_tv import predict_seg
        predictions = predict_seg(weights, images, str(options.get("device", "cpu")))
        backend = "torchvision-seg"
    elif (metadata.get("training") or {}).get("backend") == "torchvision":   # 非 YOLO：torchvision 经典分类网络
        from .backends_tv import predict_paths
        predictions = predict_paths(weights, images, str(options.get("device", "cpu")))
        for p in predictions:
            p["classifications"] = [x for x in p["classifications"] if x["confidence"] >= options["conf"]] or p["classifications"][:1]
        backend = "torchvision"
    elif weights.suffix == ".onnx":
        try:
            predictions = _predict_onnx(weights, metadata, images, options)
        except _UnsupportedONNX as error:
            try:
                predictions = _predict_upstream(project_dir, weights, metadata, images, options)
            except RuntimeError as runtime_error:
                if isinstance(runtime_error.__cause__, ImportError):
                    raise RuntimeError(f"{error}. Install the Ultralytics model runtime.") from runtime_error
                raise
        else:
            backend = "onnxruntime"
    else:
        predictions = _predict_upstream(project_dir, weights, metadata, images, options)
    report = {
        "model_version": metadata["version"], "weights_sha256": metadata["sha256"],
        "task": metadata["task"], "backend": backend, "predictions": predictions,
    }
    if target is not None:
        report["output"] = str(target)
        _write_json(target, report)
    return report
