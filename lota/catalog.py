"""Read-only image discovery, review, and verified extraction for a scenario."""

from __future__ import annotations

import fnmatch
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
import warnings
from collections import defaultdict
from contextlib import closing, contextmanager
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif"}
_FORMAT_SUFFIXES = {
    "JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tiff",
    "GIF": ".gif", "AVIF": ".avif",
}
_IMAGE_ID = re.compile(r"[0-9a-f]{64}")
_DECISIONS = {
    "accept": "accepted", "accepted": "accepted", "reject": "rejected",
    "rejected": "rejected", "pending": "pending", "reset": "pending",
}
_CHUNK_SIZE = 1024 * 1024
_BAND_EXPRESSIONS = tuple(f"substr(dhash, {start}, 2)" for start in range(1, 16, 2))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    image_id TEXT PRIMARY KEY,
    ext TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    dhash TEXT NOT NULL,
    quality TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    manual_site TEXT,
    manual_checkpoint TEXT,
    raw_relpath TEXT,
    review_note TEXT,
    created_ns INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS image_status_created ON images(status, created_ns DESC);
CREATE INDEX IF NOT EXISTS image_created ON images(created_ns DESC);
CREATE TABLE IF NOT EXISTS source_files (
    source_root TEXT NOT NULL,
    path TEXT NOT NULL,
    source_name TEXT NOT NULL,
    signature TEXT,
    image_id TEXT REFERENCES images(image_id),
    state TEXT NOT NULL,
    detail TEXT,
    observed_ns INTEGER NOT NULL,
    PRIMARY KEY(source_root, path)
);
CREATE INDEX IF NOT EXISTS source_current_image ON source_files(image_id, state, observed_ns DESC);
CREATE TABLE IF NOT EXISTS observations (
    source_root TEXT NOT NULL,
    path TEXT NOT NULL,
    image_id TEXT NOT NULL REFERENCES images(image_id),
    source_name TEXT NOT NULL,
    site_id TEXT,
    checkpoint TEXT,
    file_ids TEXT NOT NULL,
    first_seen_ns INTEGER NOT NULL,
    PRIMARY KEY(source_root, path, image_id)
);
CREATE INDEX IF NOT EXISTS observations_image ON observations(image_id);
CREATE TABLE IF NOT EXISTS manifests (
    path TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_history (
    review_id INTEGER PRIMARY KEY,
    image_id TEXT NOT NULL REFERENCES images(image_id),
    status TEXT NOT NULL,
    site_id TEXT,
    checkpoint TEXT,
    note TEXT,
    reviewed_ns INTEGER NOT NULL
);
"""


@contextmanager
def _database(project_dir: Path):
    directory = Path(project_dir).resolve() / "catalog"
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / "catalog.sqlite3", timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        if db.execute("PRAGMA user_version").fetchone()[0] == 0:
            db.execute("PRAGMA journal_mode = WAL")
            with db:
                db.executescript(_SCHEMA)
                for band, expression in enumerate(_BAND_EXPRESSIONS):
                    db.execute(f"CREATE INDEX IF NOT EXISTS image_dhash_band_{band} ON images({expression})")
                db.execute("PRAGMA user_version = 1")
        # 已删除图片的墓碑：扫描时同样内容的图片不再登记回来
        db.execute("CREATE TABLE IF NOT EXISTS deleted (image_id TEXT PRIMARY KEY, deleted_ns INTEGER NOT NULL, detail TEXT)")
        yield db


def _signature(info: os.stat_result) -> str:
    return ":".join(str(value) for value in (
        info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino,
    ))


def _regular_stat(path: Path) -> os.stat_result:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Not a regular file: {path}")
    return info


def _matches_open_file(observed: os.stat_result, opened: os.stat_result) -> bool:
    # On Windows the path and file descriptor can expose different ctime fields.
    return (
        os.path.samestat(observed, opened)
        and observed.st_size == opened.st_size
        and observed.st_mtime_ns == opened.st_mtime_ns
    )


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _file_hash(stream) -> str:
    sha = hashlib.sha256()
    for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
        sha.update(chunk)
    return sha.hexdigest()


def _edge_stddev(histogram: list[int]) -> float:
    count = sum(histogram)
    mean = sum(index * n for index, n in enumerate(histogram)) / count
    return math.sqrt(sum(n * (index - mean) ** 2 for index, n in enumerate(histogram)) / count)


def _image_info(stream, suffix: str) -> tuple[str, int, int, str, str]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(stream) as opened:
            ext = _FORMAT_SUFFIXES.get(opened.format, suffix)
            opened.load()
            with ImageOps.exif_transpose(opened) as oriented:
                width, height = oriented.size
                with oriented.convert("L") as thumb:
                    thumb.thumbnail((128, 128), Image.Resampling.LANCZOS)
                    with thumb.resize((9, 8), Image.Resampling.LANCZOS) as sample:
                        pixels = sample.tobytes()
                    histogram = thumb.histogram()
                    with thumb.filter(ImageFilter.FIND_EDGES) as edges:
                        edge_histogram = (
                            edges.crop((1, 1, edges.width - 1, edges.height - 1)).histogram()
                            if min(edges.size) > 2 else edges.histogram()
                        )
    dhash = 0
    for row in range(8):
        for column in range(8):
            left = row * 9 + column
            dhash = (dhash << 1) | (pixels[left] > pixels[left + 1])
    count = sum(histogram)
    dark_fraction = sum(histogram[:35]) / count
    light_fraction = sum(histogram[245:]) / count
    edge_stddev = _edge_stddev(edge_histogram)
    flags = []
    if min(width, height) < 320:
        flags.append("small_resolution")
    if dark_fraction > 0.8:
        flags.append("dark")
    if light_fraction > 0.8:
        flags.append("overexposed")
    if edge_stddev < 7:
        flags.append("low_sharpness")
    quality = {
        "mean_brightness": round(sum(index * n for index, n in enumerate(histogram)) / count, 4),
        "dark_fraction": round(dark_fraction, 4),
        "light_fraction": round(light_fraction, 4),
        "edge_stddev": round(edge_stddev, 4),
        "flags": flags,
    }
    return ext, width, height, f"{dhash:016x}", json.dumps(quality)


def _walk(root: Path, recursive: bool, patterns: list[str], project_dir: Path, on_error):
    if root.is_file():
        if root.suffix.lower() in _IMAGE_SUFFIXES and not root.is_symlink():
            yield root
        return
    directories = [root]
    while directories:
        directory = directories.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if _within(path, project_dir):
                        continue
                    relative = path.relative_to(root).as_posix().casefold()
                    if any(
                        fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(path.name.casefold(), pattern)
                        for pattern in patterns
                    ):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if recursive:
                                directories.append(path)
                        elif path.suffix.lower() in _IMAGE_SUFFIXES and entry.is_file(follow_symlinks=False):
                            yield path
                    except OSError as exc:
                        on_error(exc)
        except OSError as exc:
            on_error(exc)


def _manifest_lookup(db, manifest_path: str | None, source_root: Path, report_error) -> dict[str, list[str]]:
    if not manifest_path:
        return {}
    root = source_root if source_root.is_dir() else source_root.parent
    manifest = Path(manifest_path)
    if not manifest.is_absolute():
        manifest = root / manifest
    manifest = manifest.resolve()
    cached = db.execute("SELECT signature, payload FROM manifests WHERE path = ?", (str(manifest),)).fetchone()
    try:
        signature = _signature(_regular_stat(manifest))
        if cached and cached["signature"] == signature:
            payload = json.loads(cached["payload"])
        else:
            payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("expected a mapping of file IDs to paths")
            if _signature(_regular_stat(manifest)) != signature:
                raise ValueError("manifest changed while reading")
            with db:
                db.execute(
                    """INSERT INTO manifests VALUES (?, ?, ?)
                       ON CONFLICT(path) DO UPDATE SET signature=excluded.signature, payload=excluded.payload""",
                    (str(manifest), signature, json.dumps(payload, ensure_ascii=False)),
                )
    except (OSError, ValueError) as exc:
        report_error(f"{manifest}: {exc}")
        payload = json.loads(cached["payload"]) if cached else {}
    result = defaultdict(list)
    for file_id, value in payload.items():
        relative = value.get("path", value.get("relative_path")) if isinstance(value, dict) else value
        if not isinstance(relative, str) or not relative.strip():
            continue
        relpath = Path(relative.replace("\\", "/"))
        if ".." in relpath.parts:
            continue
        candidates = [manifest.parent / relpath, root / relpath]
        for candidate in dict.fromkeys(candidates):
            if _within(candidate, root):
                result[str(candidate)].append(str(file_id))
                break
    return result


def _site_for(path: Path, root: Path, pattern: re.Pattern[str] | None) -> str | None:
    if pattern is None:
        return None
    text = path.relative_to(root).as_posix() if path != root else path.name
    match = pattern.search(text)
    if not match:
        return None
    site_id = match.group("site")
    return site_id.strip() if site_id and site_id.strip() else None


def _validate_sources(sources: list[dict]):
    if not isinstance(sources, list):
        raise ValueError("sources must be a list")
    result = []
    for source in sources:
        if not isinstance(source, dict) or not source.get("path"):
            raise ValueError("Every source must include a path")
        path = Path(source["path"]).expanduser()
        if not path.is_absolute():
            raise ValueError(f"Source path must be absolute: {path}")
        try:
            pattern = re.compile(source["site_pattern"]) if source.get("site_pattern") else None
        except re.error as exc:
            raise ValueError(f"Invalid site pattern for {path}: {exc}") from exc
        if pattern is not None and "site" not in pattern.groupindex:
            raise ValueError(f"Site pattern for {path} must contain a named 'site' group")
        result.append((path.resolve(), source, pattern))
    return result


def _add_error(summary, path, reason):
    summary["error_count"] += 1
    if len(summary["errors"]) < 50:
        summary["errors"].append({"path": str(path), "reason": str(reason)})


class _InvalidImage(ValueError):
    pass


def _inspect(db, path: Path, observed: os.stat_result, staged_ids: set[str]):
    signature = _signature(observed)
    if path.resolve(strict=True) != path:
        raise OSError("Source path now resolves to another location")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _matches_open_file(observed, opened):
            return None, None
        image_id = _file_hash(stream)
        details = None
        if image_id not in staged_ids and not db.execute(
            "SELECT 1 FROM images WHERE image_id=?", (image_id,),
        ).fetchone():
            stream.seek(0)
            try:
                details = _image_info(stream, path.suffix.lower())
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
                raise _InvalidImage(str(exc)) from exc
        if (
            _signature(os.fstat(stream.fileno())) != _signature(opened)
            or _signature(_regular_stat(path)) != signature
            or path.resolve(strict=True) != path
        ):
            return None, None
        return image_id, details


def _observe(db, root, path, image_id, source_name, site_id, checkpoint, file_ids, now):
    db.execute(
        """INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_root, path, image_id) DO UPDATE SET
               source_name=excluded.source_name, site_id=excluded.site_id,
               checkpoint=excluded.checkpoint, file_ids=excluded.file_ids""",
        (root, path, image_id, source_name, site_id, checkpoint, file_ids, now),
    )


def _record_batch(db, root, source_name, batch, staged_ids, summary):
    if not batch:
        return
    with db:
        for record in batch:
            path, signature, image_id, state, detail, now, site_id, checkpoint, file_ids, details = record
            if state == "ready" and image_id and db.execute("SELECT 1 FROM deleted WHERE image_id=?", (image_id,)).fetchone():
                state, detail, image_id = "invalid", "已删除，不再登记 / deleted", None  # 用户删过的图，不登记回来
                summary["duplicates"] += 1
            if state == "ready":
                if details is None:
                    summary["duplicates"] += 1
                else:
                    cursor = db.execute(
                        """INSERT OR IGNORE INTO images(image_id, ext, width, height, dhash, quality, created_ns)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (image_id, *details, now),
                    )
                    summary["indexed" if cursor.rowcount else "duplicates"] += 1
            if state != "metadata":
                db.execute(
                    """INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_root, path) DO UPDATE SET
                           source_name=excluded.source_name, signature=excluded.signature,
                           image_id=excluded.image_id, state=excluded.state, detail=excluded.detail,
                           observed_ns=excluded.observed_ns
                       WHERE source_files.observed_ns <= excluded.observed_ns""",
                    (root, path, source_name, signature, image_id, state, detail, now),
                )
            if image_id:
                _observe(db, root, path, image_id, source_name, site_id, checkpoint, file_ids, now)
    batch.clear()
    staged_ids.clear()


def scan(
    project_dir: Path, sources: list[dict], *, limit: int | None = None, stability_seconds: float = 2,
) -> dict:
    """Index sources in place. The limit counts new file inspections, not known paths."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if not math.isfinite(stability_seconds) or stability_seconds < 0:
        raise ValueError("stability_seconds must be finite and non-negative")
    configured = _validate_sources(sources)
    project_dir = Path(project_dir).resolve()
    summary = dict.fromkeys(
        ("discovered", "inspected", "indexed", "unchanged", "duplicates", "unstable", "invalid", "missing", "error_count"),
        0,
    )
    summary.update(errors=[], limited=False)
    cutoff_age = int(stability_seconds * 1_000_000_000)
    with _database(project_dir) as db:
        for root, source, pattern in configured:
            if summary["limited"]:
                break
            if not root.exists():
                _add_error(summary, root, "Source does not exist")
                continue
            if _within(root, project_dir):
                raise ValueError(f"Source is inside the managed scenario directory: {root}")
            _scan_source(db, project_dir, root, source, pattern, cutoff_age, limit, summary)
    return summary


def _scan_source(db, project_dir, root, source, pattern, cutoff_age, limit, summary):
    root_text = str(root)
    source_name = str(source.get("name") or root.name)
    checkpoint = str(source["checkpoint"]) if source.get("checkpoint") else None
    patterns = source.get("exclude") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    patterns = [str(part).replace("\\", "/").casefold() for part in patterns]
    manifest = _manifest_lookup(
        db, source.get("manifest_path"), root, lambda exc: _add_error(summary, root, exc),
    )
    known = {
        row["path"]: row for row in db.execute(
            """SELECT f.path, f.signature, f.image_id, f.state, o.source_name,
                      o.site_id, o.checkpoint, o.file_ids
               FROM source_files f
               LEFT JOIN observations o ON o.source_root=f.source_root AND o.path=f.path AND o.image_id=f.image_id
               WHERE f.source_root=?""",
            (root_text,),
        )
    }
    started = time.time_ns()
    seen: set[str] = set()
    staged_ids: set[str] = set()
    walk_errors = []
    batch = []

    with closing(_walk(root, bool(source.get("recursive", True)), patterns, project_dir, walk_errors.append)) as paths:
        for path in paths:
            path_text = str(path)
            seen.add(path_text)
            summary["discovered"] += 1
            before = known.get(path_text)
            try:
                info = _regular_stat(path)
            except OSError as exc:
                _add_error(summary, path, exc)
                continue
            signature = _signature(info)
            now = time.time_ns()
            site_id = _site_for(path, root, pattern)
            file_ids = json.dumps(manifest.get(path_text, []))
            state, image_id, detail, details = "pending", None, None, None
            if before and before["signature"] == signature and before["state"] in {"ready", "invalid"}:
                summary["unchanged"] += 1
                image_id = before["image_id"]
                if not image_id or (
                    before["source_name"], before["site_id"], before["checkpoint"], before["file_ids"],
                ) == (source_name, site_id, checkpoint, file_ids):
                    continue
                state = "metadata"
            elif now - info.st_mtime_ns < cutoff_age:
                summary["unstable"] += 1
                if before and before["signature"] == signature and before["state"] == "pending":
                    continue
            else:
                if limit is not None and summary["inspected"] >= limit:
                    summary["limited"] = True
                    break
                summary["inspected"] += 1
                try:
                    image_id, details = _inspect(db, path, info, staged_ids)
                except _InvalidImage as exc:
                    try:
                        still_current = signature == _signature(_regular_stat(path))
                    except OSError:
                        still_current = False
                    state = "invalid" if still_current else "pending"
                    detail = str(exc)
                    summary["invalid" if still_current else "unstable"] += 1
                    if still_current:
                        _add_error(summary, path, detail)
                except OSError as exc:
                    summary["unstable"] += 1
                    detail = str(exc)
                    _add_error(summary, path, detail)
                else:
                    if image_id is None:
                        summary["unstable"] += 1
                    else:
                        state = "ready"
                        staged_ids.add(image_id)
            batch.append((path_text, signature, image_id, state, detail, now, site_id, checkpoint, file_ids, details))
            if len(batch) >= 96:
                _record_batch(db, root_text, source_name, batch, staged_ids, summary)
    _record_batch(db, root_text, source_name, batch, staged_ids, summary)
    for exc in walk_errors:
        _add_error(summary, getattr(exc, "filename", None) or root, exc)
    if not summary["limited"] and not walk_errors:
        with db:
            for path_text in known.keys() - seen:
                cursor = db.execute(
                    """UPDATE source_files SET state='missing', detail='Source path not found'
                       WHERE source_root=? AND path=? AND observed_ns < ? AND state <> 'missing'""",
                    (root_text, path_text, started),
                )
                summary["missing"] += cursor.rowcount


def _id(image_id: str) -> str:
    if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
        raise ValueError("Invalid image ID")
    return image_id


def _metadata(rows, field: str) -> list[str]:
    return sorted({item[field] for item in rows if item[field]})


def _image_dict(project_dir: Path, db, row) -> dict:
    image_id = row["image_id"]
    aliases = [
        dict(item) for item in db.execute(
            """SELECT path, source_name, site_id, checkpoint, file_ids FROM observations
               WHERE image_id=? ORDER BY path, source_root""",
            (image_id,),
        )
    ]
    for alias in aliases:
        alias["file_ids"] = json.loads(alias["file_ids"])
    site_options = _metadata(aliases, "site_id")
    checkpoint_options = _metadata(aliases, "checkpoint")
    current = db.execute(
        """SELECT path FROM source_files WHERE image_id=? AND state='ready'
           ORDER BY observed_ns DESC, path LIMIT 1""",
        (image_id,),
    ).fetchone()
    source_path = current["path"] if current else (aliases[0]["path"] if aliases else None)
    raw_path = str(project_dir / row["raw_relpath"]) if row["raw_relpath"] else None
    return {
        "image_id": image_id,
        "sha256": image_id,
        "path": raw_path or source_path,
        "raw_path": raw_path,
        "source_path": source_path,
        "site_id": row["manual_site"] or (site_options[0] if len(site_options) == 1 else None),
        "site_conflict": site_options if len(site_options) > 1 else [],
        "checkpoint": row["manual_checkpoint"] or (checkpoint_options[0] if len(checkpoint_options) == 1 else None),
        "checkpoint_conflict": checkpoint_options if len(checkpoint_options) > 1 else [],
        "status": row["status"],
        "width": row["width"],
        "height": row["height"],
        "dhash": row["dhash"],
        "quality": json.loads(row["quality"]),
        "duplicate_group": image_id,
        "review_note": row["review_note"],
        "aliases": aliases,
    }


def list_images(
    project_dir: Path, *, status: str | None = None, checkpoint: str | None = None,
    site_id: str | None = None, extracted: bool | None = None,
    limit: int = 100, offset: int = 0,
) -> list[dict]:
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("limit must be 1–1000 and offset must be non-negative")
    project_dir = Path(project_dir).resolve()
    filters = []
    params = []
    if status in {"extracted", "copied"}:
        filters.append("raw_relpath IS NOT NULL")
    elif status == "active":   # 图库默认：不显示已排除的
        filters.append("status<>'rejected'")
    elif status:
        if status not in _DECISIONS:
            raise ValueError("Unknown image status")
        filters.append("status=?")
        params.append(_DECISIONS[status])
    if checkpoint:
        filters.append(
            """COALESCE(manual_checkpoint,
                (SELECT MIN(o.checkpoint) FROM observations o
                 WHERE o.image_id=images.image_id HAVING COUNT(DISTINCT o.checkpoint)=1)
                )=?"""
        )
        params.append(checkpoint)
    if site_id:
        filters.append(
            """COALESCE(manual_site,
                (SELECT MIN(o.site_id) FROM observations o
                 WHERE o.image_id=images.image_id HAVING COUNT(DISTINCT o.site_id)=1)
                )=?"""
        )
        params.append(site_id)
    if extracted is not None:
        filters.append("raw_relpath IS NOT NULL" if extracted else "raw_relpath IS NULL")
    where = " WHERE " + " AND ".join(filters) if filters else ""
    with _database(project_dir) as db:
        return [
            _image_dict(project_dir, db, row) for row in db.execute(
                f"SELECT * FROM images{where} ORDER BY created_ns DESC, image_id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
        ]


def get_image(project_dir: Path, image_id: str) -> dict:
    project_dir = Path(project_dir).resolve()
    with _database(project_dir) as db:
        row = db.execute("SELECT * FROM images WHERE image_id=?", (_id(image_id),)).fetchone()
        if row is None:
            raise FileNotFoundError(f"Unknown image: {image_id}")
        return _image_dict(project_dir, db, row)


def review_image(
    project_dir: Path, image_id: str, *, decision: str, checkpoint: str | None = None,
    site_id: str | None = None, note: str | None = None,
) -> dict:
    project_dir = Path(project_dir).resolve()
    image_id = _id(image_id)
    status = _DECISIONS.get(decision)
    if not status:
        raise ValueError("Decision must be accepted, rejected, or pending")
    with _database(project_dir) as db, db:
        cursor = db.execute(
            """UPDATE images SET status=?,
                   manual_site=CASE WHEN ? IS NULL THEN manual_site ELSE NULLIF(TRIM(?), '') END,
                   manual_checkpoint=CASE WHEN ? IS NULL THEN manual_checkpoint ELSE NULLIF(TRIM(?), '') END,
                   review_note=COALESCE(?, review_note)
               WHERE image_id=?""",
            (status, site_id, site_id, checkpoint, checkpoint, note, image_id),
        )
        if cursor.rowcount != 1:
            raise FileNotFoundError(f"Unknown image: {image_id}")
        row = db.execute("SELECT * FROM images WHERE image_id=?", (image_id,)).fetchone()
        db.execute(
            """INSERT INTO review_history(image_id, status, site_id, checkpoint, note, reviewed_ns)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (image_id, status, row["manual_site"], row["manual_checkpoint"], note, time.time_ns()),
        )
        return _image_dict(project_dir, db, row)


def _verified_copy(source: Path, destination: Path, image_id: str) -> None:
    observed = _regular_stat(source)
    before = _signature(observed)
    if source.resolve(strict=True) != source:
        raise OSError("Source path now resolves to another location")
    sha = hashlib.sha256()
    temp_path = None
    try:
        with source.open("rb") as incoming, tempfile.NamedTemporaryFile(
            mode="wb", suffix=".tmp", prefix=".extract-", dir=destination.parent, delete=False,
        ) as outgoing:
            temp_path = Path(outgoing.name)
            opened = os.fstat(incoming.fileno())
            if not _matches_open_file(observed, opened):
                raise OSError("Source changed before extraction")
            for chunk in iter(lambda: incoming.read(_CHUNK_SIZE), b""):
                sha.update(chunk)
                outgoing.write(chunk)
            if (
                sha.hexdigest() != image_id
                or _signature(os.fstat(incoming.fileno())) != _signature(opened)
                or _signature(_regular_stat(source)) != before
                or source.resolve(strict=True) != source
            ):
                raise OSError("Source changed or no longer matches the indexed SHA-256")
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temp_path, destination)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def extract(project_dir: Path, image_ids: list[str]) -> dict:
    """Copy selected bytes into the scenario and verify immutable content identity."""
    project_dir = Path(project_dir).resolve()
    summary = {"copied": 0, "reused": 0, "failed": 0, "items": []}
    with _database(project_dir) as db:
        for image_id in dict.fromkeys(image_ids):
            item = {"image_id": image_id}
            try:
                image_id = _id(image_id)
                row = db.execute("SELECT ext FROM images WHERE image_id=?", (image_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError(f"Unknown image: {image_id}")
                relative = Path("raw") / image_id[:2] / (image_id + row["ext"])
                destination = project_dir / relative
                if destination.resolve() != destination:
                    raise OSError("Managed destination resolves to another location")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    _regular_stat(destination)
                    with destination.open("rb") as stream:
                        if _file_hash(stream) != image_id:
                            raise OSError("Existing managed copy fails SHA-256 verification")
                    outcome = "reused"
                else:
                    aliases = db.execute(
                        """SELECT o.path FROM observations o
                           LEFT JOIN source_files f
                             ON f.source_root=o.source_root AND f.path=o.path AND f.image_id=o.image_id
                           WHERE o.image_id=?
                           ORDER BY (f.state='ready') DESC, f.observed_ns DESC, o.path""",
                        (image_id,),
                    )
                    last_error = None
                    for alias in dict.fromkeys(row["path"] for row in aliases):
                        try:
                            _verified_copy(Path(alias), destination, image_id)
                        except OSError as exc:
                            last_error = exc
                        else:
                            break
                    else:
                        raise OSError(f"No unchanged source copy is available: {last_error or 'no source aliases'}")
                    outcome = "copied"
                with db:
                    db.execute("UPDATE images SET raw_relpath=? WHERE image_id=?", (relative.as_posix(), image_id))
                summary[outcome] += 1
                item.update(status=outcome, path=str(destination), raw_path=str(destination))
            except (OSError, ValueError) as exc:
                summary["failed"] += 1
                item.update(status="failed", error=str(exc))
            summary["items"].append(item)
    return summary


def near_duplicates(
    project_dir: Path, image_id: str, *, max_distance: int = 6, limit: int = 50,
) -> list[dict]:
    """Return ranked dHash candidates, without classifying or suppressing any image."""
    if not 0 <= max_distance <= 64 or not 1 <= limit <= 200:
        raise ValueError("max_distance must be 0–64 and limit must be 1–200")
    project_dir = Path(project_dir).resolve()
    image_id = _id(image_id)
    with _database(project_dir) as db:
        original = db.execute("SELECT dhash FROM images WHERE image_id=?", (image_id,)).fetchone()
        if original is None:
            raise FileNotFoundError(f"Unknown image: {image_id}")
        target = int(original["dhash"], 16)
        if max_distance <= 7:
            # At most seven changed bits cannot affect all eight disjoint bands.
            condition = " OR ".join(expression + "=?" for expression in _BAND_EXPRESSIONS)
            params = tuple(original["dhash"][i:i + 2] for i in range(0, 16, 2))
            sql = f"SELECT image_id, dhash FROM images WHERE ({condition}) AND image_id<>?"
        else:
            params = ()
            sql = "SELECT image_id, dhash FROM images WHERE image_id<>?"
        candidates = (
            ((target ^ int(row["dhash"], 16)).bit_count(), row["image_id"])
            for row in db.execute(sql, (*params, image_id))
        )
        matches = heapq.nsmallest(limit, (candidate for candidate in candidates if candidate[0] <= max_distance))
        results = []
        for distance, match_id in matches:
            row = db.execute("SELECT * FROM images WHERE image_id=?", (match_id,)).fetchone()
            results.append({**_image_dict(project_dir, db, row), "distance": distance})
        return results


def stats(project_dir: Path) -> dict:
    with _database(Path(project_dir)) as db:
        total, extracted = db.execute("SELECT COUNT(*), COUNT(raw_relpath) FROM images").fetchone()
        return {
            "total": total,
            "extracted": extracted,
            "review": dict(db.execute("SELECT status, COUNT(*) FROM images GROUP BY status")),
            "sources": dict(db.execute("SELECT state, COUNT(*) FROM source_files GROUP BY state")),
            "source_roots": [
                dict(row) for row in db.execute(
                    """SELECT source_root AS path, state, COUNT(*) AS count FROM source_files
                       GROUP BY source_root, state ORDER BY source_root, state"""
                )
            ],
        }
