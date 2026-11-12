"""Label Studio annotations and immutable, group-aware YOLO datasets."""

from collections import Counter, defaultdict
from contextlib import closing
import hashlib
from html import escape
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalog import get_image as _catalog_image
from .tasks import source_project

def get_image(project_dir,image_id):
    return _catalog_image(source_project(project_dir),image_id)



def _connect(project_dir):
    directory = Path(project_dir) / "annotations"
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "index.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS revisions (
            task TEXT NOT NULL,
            image_id TEXT NOT NULL,
            revision TEXT NOT NULL,
            status TEXT NOT NULL,
            labels TEXT NOT NULL,
            reason TEXT NOT NULL,
            original TEXT NOT NULL,
            PRIMARY KEY (task, image_id, revision)
        );
        CREATE TABLE IF NOT EXISTS current_annotations (
            task TEXT NOT NULL,
            image_id TEXT NOT NULL,
            revision TEXT NOT NULL,
            PRIMARY KEY (task, image_id)
        );
        CREATE TABLE IF NOT EXISTS label_studio_links (
            base_url TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            image_id TEXT NOT NULL,
            remote_id INTEGER NOT NULL,
            PRIMARY KEY (base_url, project_id, task, image_id)
        );
    """)
    return connection


def _task(task):
    if task not in {"detect", "classify", "segment", "obb", "pose"}:
        raise ValueError("Task must be detect, segment, obb, pose or classify")
    return task


def _classes(classes, task):
    _task(task)
    if not isinstance(classes, (list, tuple)) or not classes:
        raise ValueError("Provide a nonempty list of class names")
    if any(not isinstance(name, str) or not name.strip() or len(name) > 100 for name in classes):
        raise ValueError("Class names must be nonempty strings of at most 100 characters")
    if len({name.casefold() for name in classes}) != len(classes):
        raise ValueError("Class names must be unique")
    if task in ("detect", "segment", "obb", "pose"):
        return list(classes)
    for name in classes:
        stem = name.split(".", 1)[0].upper()
        if (name != name.strip() or name.endswith(".") or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                or name in {".", ".."}
                or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", stem)):
            raise ValueError(f"Classification name is not a portable folder name: {name!r}")
    return sorted(classes)


def label_config(classes, task="detect"):
    """Create the controls recognized by this module's Label Studio importer."""
    classes = _classes(classes, task)
    image = '<Image name="image" value="$image"/>'
    if task == "detect":
        names = "".join(f'<Label value="{escape(name, quote=True)}"/>' for name in classes)
        controls = (
            f'<RectangleLabels name="objects" toName="image">{names}</RectangleLabels>'
            '<Choices name="coverage" toName="image" choice="single" required="true">'
            '<Choice value="Targets reviewed"/><Choice value="No target"/>'
            '<Choice value="Unsure"/></Choices>'
        )
    elif task == "pose":   # 姿态：目标框 + 关键点（关键点名称在项目设置 pose.keypoints）
        names = "".join(f'<Label value="{escape(name, quote=True)}"/>' for name in classes)
        kps = "".join(f'<Label value="{escape(k, quote=True)}"/>' for k in (_KP.get() or []))
        controls = (
            f'<RectangleLabels name="objects" toName="image">{names}</RectangleLabels>'
            f'<KeyPointLabels name="kp" toName="image" smart="false">{kps}</KeyPointLabels>'
            '<Choices name="coverage" toName="image" choice="single" required="true">'
            '<Choice value="Targets reviewed"/><Choice value="No target"/>'
            '<Choice value="Unsure"/></Choices>'
        )
    elif task == "obb":   # 旋转框：可旋转的矩形
        names = "".join(f'<Label value="{escape(name, quote=True)}"/>' for name in classes)
        controls = (
            f'<RectangleLabels name="objects" toName="image" canRotate="true">{names}</RectangleLabels>'
            '<Choices name="coverage" toName="image" choice="single" required="true">'
            '<Choice value="Targets reviewed"/><Choice value="No target"/>'
            '<Choice value="Unsure"/></Choices>'
        )
    elif task == "segment":   # 分割：多边形（也接受矩形，自动转成 4 点多边形）
        names = "".join(f'<Label value="{escape(name, quote=True)}"/>' for name in classes)
        controls = (
            f'<PolygonLabels name="objects" toName="image" strokeWidth="2" pointSize="small" opacity="0.4">{names}</PolygonLabels>'
            '<Choices name="coverage" toName="image" choice="single" required="true">'
            '<Choice value="Targets reviewed"/><Choice value="No target"/>'
            '<Choice value="Unsure"/></Choices>'
        )
    else:
        names = "".join(f'<Choice value="{escape(name, quote=True)}"/>' for name in classes)
        controls = (
            f'<Choices name="category" toName="image" choice="single">{names}</Choices>'
            '<Choices name="coverage" toName="image" choice="single" required="true">'
            '<Choice value="Category reviewed"/><Choice value="Unsure"/></Choices>'
        )
    return f"<View>{image}{controls}</View>"


def _project_name(project_dir):
    directory=Path(project_dir).resolve()
    return source_project(directory).name+"/"+directory.name if directory.parent.name=="annotation-tasks" else directory.name


def _image_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Expected a lowercase SHA-256 image ID")
    return value


def _raw_image(project_dir, image_id):
    row = get_image(project_dir, _image_id(image_id))
    if not row or not row.get("raw_path"):
        raise ValueError(f"Extract image {image_id} before labeling")
    root = source_project(project_dir).resolve()
    path = (root / row["raw_path"]).resolve()
    if not path.is_relative_to(root / "raw") or not path.is_file():
        raise ValueError(f"Managed image is unavailable: {image_id}")
    if row.get("sha256") != image_id:
        raise ValueError(f"Managed image identity is inconsistent: {image_id}")
    return row, path


def make_tasks(project_dir, image_ids, *, media_base, task="detect"):
    """Build Label Studio tasks backed by extracted originals."""
    return _make_tasks(project_dir, image_ids, media_base=media_base, task=task)


def _make_tasks(project_dir, image_ids, *, media_base=None, local_files_root=None, task):
    _task(task)
    if not media_base and not local_files_root:
        raise ValueError("Provide a media base URL or Label Studio local-files root")
    if media_base and urlsplit(media_base).scheme not in {"http", "https"}:
        raise ValueError("Media base must be an HTTP(S) URL")
    if not isinstance(image_ids, (list, tuple)):
        raise ValueError("image_ids must be a list")
    tasks = []
    seen = set()
    for image_id in image_ids:
        image_id = _image_id(image_id)
        if image_id in seen:
            continue
        seen.add(image_id)
        row, path = _raw_image(project_dir, image_id)
        if local_files_root:
            try:
                relative = path.relative_to(Path(local_files_root).resolve())
            except ValueError as error:
                raise ValueError("Managed image is outside the Label Studio local-files root") from error
            url = "/data/local-files/?" + urlencode({"d": relative.as_posix()})
        elif "{image_id}" in media_base:
            url = media_base.replace("{image_id}", quote(image_id, safe=""))
        else:
            url = f"{media_base.rstrip('/')}/{quote(image_id, safe='')}"
        tasks.append({"data": {
            "image": url,
            "image_id": image_id,
            "ailabel_project": _project_name(project_dir),
            "ailabel_task": task,
            "site_id": row.get("site_id"),
            "checkpoint": row.get("checkpoint"),
        }})
    return tasks


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _choices(item):
    value = item.get("value")
    if not isinstance(value, dict):
        raise ValueError("Annotation result has no value object")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], str):
        raise ValueError("Expected exactly one choice")
    return choices[0]


def _class_name(names, classes):
    """Label Studio 里的标签名 → 子项目类别代码。完全一致直接用；否则转小写、空格/短横线换成下划线再对
    （类别代码不能有空格，Label Studio 里常写成 “grounding bar” 这样）。对不上返回 None。"""
    if not isinstance(names, list) or len(names) != 1 or not isinstance(names[0], str):
        return None
    name = names[0]
    if name in classes:
        return name
    code = re.sub(r"[\s-]+", "_", name.strip().lower())
    return code if code in classes else None


def _box(item, classes, image):
    value = item.get("value")
    if not isinstance(value, dict):
        raise ValueError("Rectangle has no value object")
    names = [_class_name(value.get("rectanglelabels"), classes)]
    if names[0] is None:
        raise ValueError("Rectangle class is unknown or ambiguous")
    try:
        x, y, width, height = (float(value[key]) for key in ("x", "y", "width", "height"))
        rotation = float(value.get("rotation", 0))
        image_rotation = float(item.get("image_rotation", 0))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Rectangle geometry is invalid") from error
    if not all(math.isfinite(number) for number in (x, y, width, height, rotation, image_rotation)):
        raise ValueError("Rectangle geometry is not finite")
    if rotation % 360 or image_rotation % 360:
        raise ValueError("Rotated annotations need review before axis-aligned YOLO export")
    if min(x, y) < -1e-6 or min(width, height) <= 0 or max(x + width, y + height) > 100.000001:
        raise ValueError("Rectangle lies outside the image or has no area")
    image_w, image_h = image.get("width"), image.get("height")
    original_w, original_h = item.get("original_width"), item.get("original_height")
    if all(isinstance(value, int) and value > 0 for value in (image_w, image_h, original_w, original_h)):
        if abs(image_w / image_h - original_w / original_h) > 0.01:
            raise ValueError("Rectangle image dimensions do not match the managed image")
    left, top = max(0, x), max(0, y)
    right, bottom = min(100, x + width), min(100, y + height)
    geometry = [round(number, 8) for number in (
        (left + right) / 200, (top + bottom) / 200, (right - left) / 100, (bottom - top) / 100
    )]
    if min(geometry[2:]) <= 0:
        raise ValueError("Rectangle is below YOLO export precision")
    return [names[0], *geometry]


def _poly(item, classes, image):
    """Label Studio 多边形（百分比坐标）→ [类别, x1, y1, x2, y2, …]（0–1 归一化，YOLO-seg 格式）"""
    value = item.get("value")
    if not isinstance(value, dict):
        raise ValueError("Polygon has no value object")
    if item.get("type") == "rectanglelabels":       # 矩形预标注也接受：转成 4 点多边形
        name, cx, cy, w, h = _box(item, classes, image)
        x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        return [name, *[round(v, 8) for v in (x1, y1, x2, y1, x2, y2, x1, y2)]]
    names = [_class_name(value.get("polygonlabels"), classes)]
    if names[0] is None:
        raise ValueError("Polygon class is unknown or ambiguous")
    points = value.get("points")
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError("Polygon needs at least 3 points")
    flat = []
    for pt in points:
        x, y = float(pt[0]), float(pt[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("Polygon geometry is not finite")
        flat += [round(min(1.0, max(0.0, x / 100)), 8), round(min(1.0, max(0.0, y / 100)), 8)]
    return [names[0], *flat]


def _obb(item, classes, image):
    """Label Studio 可旋转矩形（左上角 x,y + 宽高 + 绕左上角顺时针旋转角，单位 %）→ [类别, 四角点 8 个数（0–1）]"""
    value = item.get("value")
    if not isinstance(value, dict):
        raise ValueError("Rectangle has no value object")
    names = [_class_name(value.get("rectanglelabels"), classes)]
    if names[0] is None:
        raise ValueError("Rectangle class is unknown or ambiguous")
    W = float(item.get("original_width") or image.get("width") or 0)
    H = float(item.get("original_height") or image.get("height") or 0)
    if W <= 0 or H <= 0:
        raise ValueError("Rotated box needs the image size")
    x, y = float(value["x"]) / 100 * W, float(value["y"]) / 100 * H
    w, h = float(value["width"]) / 100 * W, float(value["height"]) / 100 * H
    if w <= 0 or h <= 0:
        raise ValueError("Rotated box has no area")
    t = math.radians(float(value.get("rotation", 0)))
    c, s = math.cos(t), math.sin(t)
    pts = [(x, y), (x + w * c, y + w * s), (x + w * c - h * s, y + w * s + h * c), (x - h * s, y + h * c)]
    flat = []
    for px, py in pts:
        flat += [round(min(1.0, max(0.0, px / W)), 8), round(min(1.0, max(0.0, py / H)), 8)]
    return [names[0], *flat]


import contextvars as _cv
_KP = _cv.ContextVar("ailabel_pose_keypoints", default=None)


def pose_keypoints(project_dir):
    """姿态任务的关键点名称（项目设置 settings.json 的 pose.keypoints）"""
    f = Path(project_dir) / "settings.json"
    try:
        kps = (json.loads(f.read_text(encoding="utf-8")).get("pose") or {}).get("keypoints") if f.is_file() else None
    except ValueError:
        kps = None
    if not kps:
        raise ValueError("姿态任务需要先在“项目设置 → 姿态关键点”里写关键点名称")
    return list(kps)


def _pose(results, classes, kps):
    boxes = [_box(item, classes, {}) for item in results if item.get("type") == "rectanglelabels"]
    pts = []
    for item in results:
        if item.get("type") != "keypointlabels":
            continue
        v = item.get("value") or {}
        name = (v.get("keypointlabels") or [None])[0]
        if name in kps:
            pts.append((name, float(v["x"]) / 100, float(v["y"]) / 100))
    out = []
    for name, cx, cy, w, h in boxes:
        x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        vals = []
        for k in kps:   # 每个关键点取落在这个框里的那个（框里没有 = 缺失）
            hit = [(px, py) for n, px, py in pts if n == k and x1 <= px <= x2 and y1 <= py <= y2]
            vals += [round(hit[0][0], 8), round(hit[0][1], 8), 2.0] if hit else [0.0, 0.0, 0.0]
        out.append([name, cx, cy, w, h, *vals])
    return out


def _interpret(results, classes, task, image):
    if any(not isinstance(result, dict) for result in results):
        raise ValueError("Invalid result object")
    coverage = [
        _choices(item) for item in results
        if item.get("type") == "choices" and item.get("from_name") == "coverage"
    ]
    if len(set(coverage)) > 1:
        raise ValueError("Conflicting coverage choices")
    coverage = coverage[0] if coverage else None
    if coverage == "Unsure":
        return "unknown", [], "Annotator marked evidence as uncertain"
    if task in ("detect", "segment", "obb", "pose"):
        rectangles = [
            _box(item, classes, image) for item in results
            if item.get("type") == "rectanglelabels"
        ] if task == "detect" else _pose(results, classes, _KP.get() or []) if task == "pose" else [
            _obb(item, classes, image) for item in results
            if item.get("type") == "rectanglelabels"
        ] if task == "obb" else [
            _poly(item, classes, image) for item in results
            if item.get("type") in ("polygonlabels", "rectanglelabels")
        ]
        if coverage not in {None, "Targets reviewed", "No target"}:
            raise ValueError("Unknown detection coverage choice")
        if rectangles and coverage == "No target":
            raise ValueError("Boxes contradict the explicit No target choice")
        if rectangles and coverage != "Targets reviewed":
            return "unlabeled", [], "Confirm all target classes have been reviewed"
        if rectangles:
            boxes = [list(box) for box in sorted({tuple(box) for box in rectangles})]
            return "accepted", boxes, ""
        if coverage == "No target":
            return "accepted", [], ""
        return "unlabeled", [], "No boxes and no explicit negative label"
    names = [
        _choices(item) for item in results
        if item.get("type") == "choices" and item.get("from_name") == "category"
    ]
    if coverage not in {None, "Category reviewed"}:
        raise ValueError("Unknown classification coverage choice")
    if not names:
        return "unlabeled", [], "No category selected"
    if len(set(names)) != 1 or _class_name(names[:1], classes) is None:
        raise ValueError("Category is unknown or ambiguous")
    names = [_class_name(names[:1], classes)]
    if coverage != "Category reviewed":
        return "unlabeled", [], "Confirm the single image category"
    return "accepted", [names[0]], ""


def _annotation(task_data, classes, task, image):
    annotations = task_data.get("annotations", [])
    if not isinstance(annotations, list):
        return "invalid", [], "Annotations must be a list"
    completed = [
        entry for entry in annotations if isinstance(entry, dict)
        and isinstance(entry.get("result"), list)
        and not entry.get("was_cancelled") and not entry.get("skipped") and not entry.get("is_draft")
    ]
    ground_truth = [entry for entry in completed if entry.get("ground_truth") is True]
    if ground_truth:
        completed = ground_truth
    if not completed:
        return "unlabeled", [], "No completed annotation"
    try:
        results = [_interpret(entry["result"], classes, task, image) for entry in completed]
    except ValueError as error:
        return "invalid", [], str(error)
    if any(result[:2] != results[0][:2] for result in results[1:]):
        return "needs_review", [], "Completed annotations disagree"
    return results[0]


def _task_rows(tasks):
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", tasks.get("results"))
    if not isinstance(tasks, list):
        raise ValueError("Expected a Label Studio JSON task list")
    return tasks


def import_annotations(project_dir, tasks, classes, *, task="detect"):
    """Record original Label Studio JSON and normalize the current human annotation."""
    classes = _classes(classes, task)
    if task == "pose":
        _KP.set(pose_keypoints(project_dir))
    observations = defaultdict(list)
    rejected = []
    for index, value in enumerate(_task_rows(tasks)):
        data = value.get("data") if isinstance(value, dict) else None
        try:
            if not isinstance(data, dict):
                raise ValueError("Task data is missing")
            if data.get("ailabel_project") not in {None, _project_name(project_dir)}:
                raise ValueError("Task belongs to another project")
            if data.get("ailabel_task") not in {None, task}:
                raise ValueError("Task belongs to another annotation type")
            image_id = _image_id(data.get("image_id"))
            row, _ = _raw_image(project_dir, image_id)
            schema={"task":task,"classes":classes}
            if data.get("ailabel_label_schema") not in (None,schema):
                raise ValueError("Annotation task labels changed; review a new labeling batch")
            value=json.loads(_dump(value))
            value["_ailabel_schema"]=schema
        except (KeyError, FileNotFoundError, TypeError, ValueError) as error:
            rejected.append({"index": index, "reason": str(error)})
            continue
        observations[image_id].append((value, _annotation(value, classes, task, row)))

    counts = Counter()
    with closing(_connect(project_dir)) as connection, connection:
        for image_id, entries in observations.items():
            source = [entry[0] for entry in entries]
            decision = entries[0][1]
            if any(entry[1][:2] != decision[:2] for entry in entries[1:]):
                decision = "needs_review", [], "Export contains conflicting tasks for this image"
            status, labels, reason = decision
            if status == "unlabeled" and reason == "No completed annotation" and connection.execute(
                    "SELECT 1 FROM current_annotations WHERE task = ? AND image_id = ?", (task, image_id)).fetchone():
                # Label Studio 里还没人标的任务（比如刚“送回复核”的），不能覆盖这张图已有的标注记录
                counts["kept_existing"] += 1
                continue
            original = _dump(source[0] if len(source) == 1 else source)
            revision = _sha(_dump([source, decision]).encode())
            connection.execute(
                "INSERT OR IGNORE INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task, image_id, revision, status, _dump(labels), reason, original),
            )
            connection.execute(
                "INSERT INTO current_annotations VALUES (?, ?, ?) "
                "ON CONFLICT(task, image_id) DO UPDATE SET revision = excluded.revision",
                (task, image_id, revision),
            )
            counts[status] += 1
    return {"task": task, "processed": sum(counts.values()), "counts": dict(counts),
            "accepted": counts["accepted"], "rejected": rejected}


def list_annotations(project_dir, *, task=None, limit=100, offset=0):
    """List the current normalized annotation and its review state."""
    if task is not None:
        _task(task)
    if not isinstance(limit, int) or not isinstance(offset, int) or limit < 0 or offset < 0:
        raise ValueError("limit and offset must be nonnegative integers")
    sql = (
        "SELECT r.task, r.image_id, r.revision, r.status, r.labels, r.reason "
        "FROM current_annotations AS c JOIN revisions AS r USING (task, image_id, revision)"
    )
    params = []
    if task is not None:
        sql += " WHERE c.task = ?"
        params.append(task)
    sql += " ORDER BY c.task, c.image_id LIMIT ? OFFSET ?"
    with closing(_connect(project_dir)) as connection:
        return [dict(row) | {"labels": json.loads(row["labels"])}
                for row in connection.execute(sql, (*params, limit, offset))]


def _group_samples(samples):
    """Combine sites and explicit duplicate-group memberships before splitting."""
    parents = {}

    def root(node):
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    for sample in samples:
        keys = [f"image:{sample['image_id']}"]
        if sample["site_id"]:
            keys.append(f"site:{sample['site_id']}")
        if sample["duplicate_group"]:
            keys.append(f"duplicate:{sample['duplicate_group']}")
        for key in keys:
            parents.setdefault(key, key)
        first = root(keys[0])
        for key in keys[1:]:
            other = root(key)
            if other != first:
                first, other = sorted((first, other))
                parents[other] = first

    groups = defaultdict(list)
    for sample in samples:
        groups[root(f"image:{sample['image_id']}")].append(sample)
    return groups


def _split_groups(samples, classes, fractions, seed, task):
    groups = _group_samples(samples)
    if len(groups) < len(fractions):
        raise ValueError(f"Need at least {len(fractions)} independent site or duplicate groups")
    group_counts = {
        group: Counter(label for sample in rows for label in sorted(sample["categories"]))
        for group, rows in groups.items()
    }
    total_classes = Counter(label for sample in samples for label in sorted(sample["categories"]))
    if task == "classify":
        missing = [name for name in classes
                   if sum(name in counts for counts in group_counts.values()) < len(fractions)]
        if missing:
            raise ValueError("Need an independent group per split for classification classes: "
                             + ", ".join(missing))

    rng = random.Random(seed)
    names = list(fractions)
    totals = {name: max(1.0, len(samples) * fraction) for name, fraction in fractions.items()}
    best = None
    tries = max(4, min(32, 8192 // len(groups)))
    for attempt in range(tries):
        order = sorted(groups)
        rng.shuffle(order)
        if attempt == 0:
            order.sort(key=lambda group: len(groups[group]), reverse=True)
        counts = Counter()
        split_classes = {name: Counter() for name in names}
        assignment = {}
        for group in order:
            size, labels = len(groups[group]), group_counts[group]

            def cost(name):
                goal = totals[name]
                delta = (2 * (counts[name] - goal) + size) * size / goal
                for label, amount in labels.items():
                    target = max(1.0, total_classes[label] * fractions[name])
                    current = split_classes[name][label]
                    delta += (2 * (current - target) + amount) * amount / target
                    if current == 0:
                        delta -= 5
                if counts[name] == 0:
                    delta -= 5
                return delta

            split = min(names, key=cost)
            assignment[group] = split
            counts[split] += size
            split_classes[split].update(labels)

        if not all(counts[name] and split_classes[name] for name in names):
            continue
        if task == "classify" and any(set(split_classes[name]) != set(classes) for name in names):
            continue
        score = sum(abs(counts[name] - totals[name]) for name in names)
        score += 2 * sum(len(set(total_classes) - set(split_classes[name])) for name in names)
        if best is None or score < best[0]:
            best = score, assignment

    if best is None:
        raise ValueError("Cannot form independent nonempty splits with the required class coverage; "
                         "add labeled site groups or adjust the split fractions")
    return {sample["image_id"]: (best[1][group], group)
            for group, rows in groups.items() for sample in rows}


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("Dataset version must contain only ASCII letters, digits, dots, hyphens or underscores")
    if value.endswith(".") or re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value, re.I):
        raise ValueError("Dataset version is not a portable folder name")
    return value


def _verify_copied(path, expected):
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != expected:
        raise ValueError(f"Managed source bytes changed for {expected}; re-extract before publishing")


def _collect_samples(project_dir, classes, task, require_site):
    with closing(_connect(project_dir)) as connection:
        approved = connection.execute(
            "SELECT r.image_id, r.labels, r.original FROM current_annotations AS c "
            "JOIN revisions AS r USING (task, image_id, revision) "
            "WHERE c.task = ? AND r.status = 'accepted' ORDER BY r.image_id",
            (task,),
        ).fetchall()
    samples = []
    excluded = Counter()
    for row in approved:
        image_id = row["image_id"]
        try:
            image, raw_path = _raw_image(project_dir, image_id)
        except (KeyError, FileNotFoundError, ValueError):
            excluded["missing_raw"] += 1
            continue
        original=json.loads(row["original"])
        exports=original if isinstance(original,list) else [original]
        if any(not isinstance(entry,dict) or entry.get("_ailabel_schema")!={"task":task,"classes":classes} for entry in exports):
            excluded["label_schema_changed"] += 1
            continue
        checked=[_annotation(entry,classes,task,image) for entry in exports if isinstance(entry,dict)]
        if not checked or any(item[0]!="accepted" for item in checked):
            excluded["coverage_not_confirmed"] += 1
            continue
        site = image.get("site_id")
        if not site and require_site:
            excluded["missing_site"] += 1
            continue
        labels = json.loads(row["labels"])
        categories = {label[0] for label in labels} if task in ("detect", "segment", "obb", "pose") else set(labels)
        if not categories.issubset(classes):
            excluded["retired_class"] += 1
            continue
        samples.append({
            "image_id": image_id,
            "raw_path": raw_path,
            "site_id": site,
            "checkpoint": image.get("checkpoint"),
            "duplicate_group": image.get("duplicate_group"),
            "labels": labels,
            "categories": categories,
        })
    return samples, dict(excluded)


def _existing_version(target, dataset_id):
    if target.is_symlink():
        raise ValueError("Dataset version points to a symbolic link")
    path = target / "manifest.json"
    if not path.is_file():
        raise ValueError(f"Dataset version path already exists without a manifest: {target.name}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != dataset_id:
        raise ValueError(f"Dataset version already exists with different content: {target.name}")
    return manifest


def publish_dataset(project_dir, version, classes, *, task="detect", val_fraction=0.2,
                    test_fraction=0.0, seed=42, require_site=True):
    """Publish a read-only-by-convention content snapshot without crossing site groups."""
    version = _version(version)
    classes = _classes(classes, task)
    if task == "pose":
        _KP.set(pose_keypoints(project_dir))
    val_fraction, test_fraction = float(val_fraction), float(test_fraction)
    if not math.isfinite(val_fraction) or not math.isfinite(test_fraction) or not (
        0 < val_fraction < 1 and 0 <= test_fraction < 1 and val_fraction + test_fraction < 1
    ):
        raise ValueError("Validation fraction must be positive and val + test must be below 1")
    fractions = {"train": 1 - val_fraction - test_fraction, "val": val_fraction}
    if test_fraction:
        fractions["test"] = test_fraction
    samples, excluded = _collect_samples(project_dir, classes, task, require_site)
    if not samples:
        raise ValueError(f"No accepted samples are ready to publish (excluded: {excluded})")
    assignments = _split_groups(samples, classes, fractions, seed, task)

    counts = dict.fromkeys(("train", "val", "test"), 0)
    class_counts = {split: dict.fromkeys(classes, 0) for split in fractions}
    image_rows = []
    for sample in samples:
        split, group = assignments[sample["image_id"]]
        suffix = sample["raw_path"].suffix.lower()
        filename = sample["image_id"] + suffix
        labels = sample["labels"]
        if task in ("detect", "segment", "obb", "pose"):
            relative = f"images/{split}/{filename}"
            for label in labels:
                class_counts[split][label[0]] += 1
        else:
            relative = f"{split}/{labels[0]}/{filename}"
            class_counts[split][labels[0]] += 1
        counts[split] += 1
        image_rows.append({
            "image_id": sample["image_id"], "path": relative, "split": split,
            "group": group, "site_id": sample["site_id"], "checkpoint": sample["checkpoint"],
            "labels": labels,
        })

    dataset_id = _sha(_dump([task, classes, image_rows]).encode())
    base = Path(project_dir).resolve() / "datasets"
    base.mkdir(parents=True, exist_ok=True)
    target = base / version
    if target.exists() or target.is_symlink():
        return _existing_version(target, dataset_id)

    manifest = {
        "version": version, "dataset_id": dataset_id, "task": task, "classes": classes,
        "counts": counts, "class_counts": class_counts, "excluded": excluded, "seed": seed,
        "fractions": fractions, "images": image_rows,
        "train_path": "data.yaml" if task in ("detect", "segment", "obb", "pose") else ".",
    }
    if task == "pose":   # 关键点形状和左右翻转对应关系（数据增强左右翻转时，左手、右手要对调）
        kps = pose_keypoints(project_dir)
        manifest["keypoints"] = kps
        manifest["kpt_shape"] = [len(kps), 3]
        swap = lambda n: (n.replace("left", "\0").replace("right", "left").replace("\0", "right")
                          .replace("左", "\0").replace("右", "左").replace("\0", "右"))
        manifest["flip_idx"] = [kps.index(swap(k)) if swap(k) in kps else i for i, k in enumerate(kps)]
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=base) as temporary:
        stage = Path(temporary) / version
        stage.mkdir()
        class_ids = {name: index for index, name in enumerate(classes)}
        for sample, image in zip(samples, image_rows):
            destination = stage / image["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(sample["raw_path"], destination)
            _verify_copied(destination, image["image_id"])
            if task in ("detect", "segment", "obb", "pose"):
                labels_path = stage / "labels" / image["split"] / (image["image_id"] + ".txt")
                labels_path.parent.mkdir(parents=True, exist_ok=True)
                rows = [
                    f"{class_ids[label[0]]} " + " ".join(f"{number:.8f}" for number in label[1:])
                    for label in image["labels"]
                ]
                labels_path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
        if task in ("detect", "segment", "obb", "pose"):
            yaml = {"path": str(target), **{name: f"images/{name}" for name in fractions},
                    "names": dict(enumerate(classes))}
            (stage / "data.yaml").write_text(json.dumps(yaml, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
        try:
            stage.rename(target)
        except OSError:
            if target.exists() or target.is_symlink():
                return _existing_version(target, dataset_id)
            raise
    return manifest


def list_datasets(project_dir):
    root = Path(project_dir) / "datasets"
    datasets = []
    for path in sorted(root.glob("*/manifest.json")):
        if path.parent.is_symlink():
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        datasets.append({key: value for key, value in manifest.items() if key != "images"})
    return datasets


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class LabelStudioClient:
    """Client for Label Studio's project, import, and JSON export APIs."""

    def __init__(self, base_url, token, *, auth_scheme="Token", timeout=30):
        if not isinstance(base_url, str):
            raise ValueError("Label Studio base URL is required")
        parts = urlsplit(base_url)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError("Label Studio base URL must be an HTTP(S) server URL without credentials")
        if not isinstance(token, str) or not token.strip() or any(char in token for char in "\r\n"):
            raise ValueError("A Label Studio API token is required")
        if auth_scheme not in {"Token", "Bearer"}:
            raise ValueError("Label Studio auth_scheme must be Token or Bearer")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())
        # 新版 Label Studio 的“个人访问令牌”（JWT，eyJ 开头）是刷新令牌：要先换成短期访问令牌再用 Bearer；
        # 旧的 Legacy Token 直接用 “Token xxx”
        self._refresh = token.strip() if token.strip().startswith("eyJ") else None
        self._auth = None if self._refresh else f"{auth_scheme} {token}"

    def _access(self, renew=False):
        if self._refresh and (renew or not self._auth):
            request = Request(self.base_url + "/api/token/refresh", method="POST",
                              data=_dump({"refresh": self._refresh}).encode(),
                              headers={"Content-Type": "application/json", "Accept": "application/json"})
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    self._auth = "Bearer " + json.loads(response.read())["access"]
            except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as error:
                raise RuntimeError("Label Studio personal access token could not be exchanged; check the token") from error
        return self._auth

    @classmethod
    def from_config(cls, config):
        token_env = config.get("token_env") or config.get("api_token_env")
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise ValueError("Provide a valid Label Studio token_env")
        token = os.environ.get(token_env)
        if not token:
            raise ValueError(f"Label Studio token environment variable is unset: {token_env}")
        return cls(config.get("base_url") or config.get("url"), token,
                   auth_scheme=config.get("auth_scheme", "Token"))

    def _request(self, method, path, *, query=None, payload=None, _retried=False):
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        headers = {"Authorization": self._access(), "Accept": "application/json"}
        data = None
        if payload is not None:
            data = _dump(payload).encode()
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as error:
            if error.code == 401 and self._refresh and not _retried:  # 短期访问令牌过期：换一个再试一次
                self._access(renew=True)
                return self._request(method, path, query=query, payload=payload, _retried=True)
            raise RuntimeError(f"Label Studio {method} {path} returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"Label Studio {method} {path} could not reach the configured service") from error
        if not body:
            return None
        try:
            return json.loads(body)
        except (UnicodeDecodeError, ValueError) as error:
            raise RuntimeError(f"Label Studio {method} {path} did not return JSON") from error

    def create_project(self, title, classes, *, task="detect"):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Label Studio project title is required")
        return self._request("POST", "/api/projects/", payload={
            "title": title, "label_config": label_config(classes, task),
        })

    def list_tasks(self, project_id):
        page, size, received = 1, 100, 0
        while True:
            payload = self._request("GET", "/api/tasks/", query={
                "project": project_id, "page": page, "page_size": size,
            })
            tasks = _task_rows(payload)
            yield from tasks
            received += len(tasks)
            total = payload.get("total", payload.get("count")) if isinstance(payload, dict) else None
            if not tasks or (isinstance(total, int) and received >= total) or (
                not isinstance(total, int) and len(tasks) < size
            ):
                return
            page += 1

    def import_tasks(self, project_id, tasks):
        return self._request("POST", f"/api/projects/{project_id}/import",
                             query={"return_task_ids": "true"}, payload=tasks)

    def export_tasks(self, project_id):
        return _task_rows(self._request("GET", f"/api/projects/{project_id}/export",
                                      query={"exportType": "JSON", "download_all_tasks": "true"}))


def _remote_project(config):
    try:
        project_id = int(config.get("project_id"))
    except (TypeError, ValueError) as error:
        raise ValueError("Provide a positive Label Studio project_id") from error
    if project_id <= 0:
        raise ValueError("Provide a positive Label Studio project_id")
    return project_id


def _matches_task(remote, project_name, task):
    data = remote.get("data") if isinstance(remote, dict) else None
    return (isinstance(data, dict) and data.get("ailabel_project") == project_name
            and data.get("ailabel_task") == task)


def _remote_tasks_by_image(client, project_id, project_name, task):
    by_image = {}
    for remote in client.list_tasks(project_id):
        if not _matches_task(remote, project_name, task):
            continue
        image_id, remote_id = remote["data"].get("image_id"), remote.get("id")
        if not isinstance(image_id, str) or not isinstance(remote_id, int):
            continue
        if image_id in by_image and by_image[image_id] != remote_id:
            raise ValueError(f"Label Studio has duplicate remote tasks for managed image {image_id}")
        by_image[image_id] = remote_id
    return by_image


def push_tasks(project_dir, label_studio, image_ids, classes, *, task="detect"):
    """Submit only managed tasks absent from the configured Label Studio project."""
    _classes(classes, task)
    project_id = _remote_project(label_studio)
    client = LabelStudioClient.from_config(label_studio)
    requested = _make_tasks(project_dir, image_ids, task=task,
                            media_base=label_studio.get("media_base"),
                            local_files_root=label_studio.get("local_files_root"))
    for request in requested:
        request["data"]["ailabel_label_schema"]={"task":task,"classes":_classes(classes,task)}
    project_name = _project_name(project_dir)
    pushed, matched = 0, {}
    with closing(_connect(project_dir)) as connection, connection:
        # Serializing pushes inside the scene allows reconciliation after an uncertain POST.
        connection.execute("BEGIN IMMEDIATE")
        existing = _remote_tasks_by_image(client, project_id, project_name, task)
        pending = []
        for request in requested:
            image_id = request["data"]["image_id"]
            if image_id in existing:
                matched[image_id] = existing[image_id]
            else:
                pending.append(request)
        for start in range(0, len(pending), 100):
            batch = pending[start:start + 100]
            response = client.import_tasks(project_id, batch)
            ids = response.get("task_ids") if isinstance(response, dict) else None
            if isinstance(ids, list) and len(ids) == len(batch) and all(isinstance(i, int) for i in ids):
                matched.update((request["data"]["image_id"], remote_id)
                               for request, remote_id in zip(batch, ids))
            else:
                actual = _remote_tasks_by_image(client, project_id, project_name, task)
                missing = [request["data"]["image_id"] for request in batch
                           if request["data"]["image_id"] not in actual]
                if missing:
                    raise RuntimeError("Label Studio did not yet confirm the submitted tasks; retry to reconcile")
                matched.update((request["data"]["image_id"], actual[request["data"]["image_id"]])
                               for request in batch)
            pushed += len(batch)
        connection.executemany(
            "INSERT INTO label_studio_links VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(base_url, project_id, task, image_id) DO UPDATE SET remote_id=excluded.remote_id",
            ((client.base_url, project_id, task, image_id, remote_id)
             for image_id, remote_id in matched.items()),
        )
    return {"project_id": project_id, "pushed": pushed, "existing": len(requested) - pushed,
            "tasks": matched}


def pull_annotations(project_dir, label_studio, classes, *, task="detect"):
    """Import a complete JSON export scoped to the engine's tasks for this scene."""
    _classes(classes, task)
    project_id = _remote_project(label_studio)
    client = LabelStudioClient.from_config(label_studio)
    rows = client.export_tasks(project_id)
    project_name = _project_name(project_dir)
    relevant = [row for row in rows if _matches_task(row, project_name, task)]
    summary = import_annotations(project_dir, relevant, classes, task=task)
    return summary | {"project_id": project_id, "skipped_remote": len(rows) - len(relevant)}
