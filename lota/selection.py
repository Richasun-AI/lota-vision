"""Composable selection actions with previewed, recorded file routing."""

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


def find_objects(context, image_ids, selector, labels, confidence=0.4):
    from .catalog import get_image
    from .models import predict
    from .models import resolve_model
    metadata, _ = resolve_model(context.directory, selector)
    names = metadata.get("classes", [])
    known = set(names.values() if isinstance(names, dict) else names)
    if not set(labels).issubset(known):
        raise ValueError("Selected targets are unsupported by this model: " + ", ".join(sorted(set(labels)-known)))
    ids = list(dict.fromkeys(image_ids))
    paths = [context.engine.image_path(context.directory, get_image(context.directory, key), False) for key in ids]
    result = predict(context.directory, selector, paths, confidence=confidence)
    wanted = set(labels)
    matched, remaining = [], []
    for key, row in zip(ids, result["predictions"]):
        hits = [x for x in row.get("detections", []) if
                (x.get("label") or x.get("class_name")) in wanted and x["confidence"] >= confidence]
        (matched if hits else remaining).append(key)
    return {"image_ids": matched, "remaining_ids": remaining, "predictions": result["predictions"]}


def route_files(context, image_ids, destination, execute=False):
    from .catalog import get_image
    base = (context.directory / "sorting").resolve()
    target = (base / destination).resolve()
    if not target.is_relative_to(base) or target == base:
        raise ValueError("Destination must be a folder within this scenario's sorting directory")
    plans = []
    for key in dict.fromkeys(image_ids):
        row = get_image(context.directory, key)
        source = context.engine.image_path(context.directory, row, False)
        plans.append((key, source, target / (key + source.suffix.lower())))
    report = {"execute": execute, "destination": str(target), "items": [], "image_ids": list(dict.fromkeys(image_ids))}
    for key, source, output in plans:
        item = {"image_id": key, "source": str(source), "destination": str(output), "operation": "stage_and_move"}
        if execute:
            target.mkdir(parents=True, exist_ok=True)
            if output.exists():
                if hashlib.sha256(output.read_bytes()).hexdigest() != key:
                    raise ValueError("Destination content conflict")
            else:
                handle, temporary = tempfile.mkstemp(dir=target, prefix=".stage-")
                os.close(handle)
                temporary = Path(temporary)
                try:
                    shutil.copyfile(source, temporary)
                    if hashlib.sha256(temporary.read_bytes()).hexdigest() != key:
                        raise ValueError("Source changed during routing")
                    os.replace(temporary, output)
                finally:
                    temporary.unlink(missing_ok=True)
        report["items"].append(item)
    if execute:
        records = context.directory / "sorting" / "_records"
        records.mkdir(parents=True, exist_ok=True)
        (records / f"{context.job_id}-{context.step_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
