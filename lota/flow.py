"""节点式工作流：节点目录（中文说明、类型化输入输出、参数默认值和提示）+ 新工具 + 结果复核接口。

节点 = 引擎工具。前端（static/flow.js，基于 LiteGraph.js）把连好的节点图转成引擎 Workflow：
按连线拓扑排序成 steps，连线变成 {"$ref": "上游步骤.字段"}；执行、断点续跑都由引擎 worker 负责。

插槽类型：IMAGES（图片 ID 列表）、DATASET（数据集版本）、MODEL（模型版本）、
PREDICTIONS（检测结果）、METRICS（评估指标）、REVIEW（复核单）。只有同类型的插槽能连。
"""
import hashlib
import json
import time
from pathlib import Path

from pydantic import Field

from . import config as deploy
from .store import Spec
from .tools import TOOLS, Selection, Tool
from . import cleanup  # noqa: F401  注册：移除类别 / 排除 / 删除照片
from . import vlmcheck  # noqa: F401  注册：AI 预检（qwen 看图，从旧 7870 巡检图片筛选迁移）
from . import hub  # noqa: F401  注册：模型导出、评估记录（模型中心）
from . import media  # noqa: F401  注册：图像增强、视频抽帧、视频标注
from . import qa  # noqa: F401  注册：找标注错误
from . import backends_tv  # noqa: F401  注册：非 YOLO 经典分类网络、语义分割

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fresh_dir(path):
    """新建一个不存在的文件夹；同名（同一秒内连续运行）就加 -2、-3…"""
    path = Path(path)
    cand, i = path, 1
    while True:
        try:
            cand.mkdir(parents=True, exist_ok=False)
            return cand
        except FileExistsError:
            i += 1
            cand = path.with_name(f"{path.name}-{i}")


def _images(folder: Path, recursive: bool):
    it = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in IMAGE_EXT)


# ── 新工具 ────────────────────────────────────────────────────────────────
class ImportFolder(Spec):
    folder: str = Field(min_length=1)
    recursive: bool = True


def import_folder(c, folder, recursive=True):
    """扫描一个文件夹（必须在引擎允许的目录下），按内容去重后复制进项目（源文件不动）。"""
    from .catalog import extract, scan
    root = c.engine.allowed_source(folder, directory=True)
    name = ("folder-" + hashlib.sha1(str(root).encode()).hexdigest()[:8])
    summary = scan(c.directory, [{"name": name, "path": str(root), "recursive": recursive}],
                   limit=1000000, stability_seconds=0)
    ids = list(dict.fromkeys(_sha256(p) for p in _images(root, recursive)))
    from .catalog import _database
    with _database(c.directory) as db:  # 用户删过的图片跳过（扫描也不会再登记它们）
        gone = {r[0] for r in db.execute("SELECT image_id FROM deleted")}
    skipped = len([i for i in ids if i in gone])
    ids = [i for i in ids if i not in gone]
    if len(ids) > 1000:
        raise ValueError(f"文件夹里有 {len(ids)} 张图，一次最多 1000 张，请分成多个文件夹")
    if ids:
        extract(c.directory, ids)
    return {"image_ids": ids, "count": len(ids), "skipped_deleted": skipped, "folder": str(root),
            "scan": {k: v for k, v in summary.items() if not isinstance(v, (list, dict))}}


class ImportYolo(Spec):
    images_folder: str = Field(min_length=1)
    labels_folder: str | None = None
    image_ids: list[str] | None = None   # 只用于连线排序（上游“导入图片文件夹”先跑完）


def _label_path(img: Path, images_root: Path, labels_root: Path | None) -> Path:
    rel = img.relative_to(images_root).with_suffix(".txt")
    if labels_root:
        return labels_root / rel
    parts = list(img.with_suffix(".txt").parts)  # 默认：路径里的 images 换成 labels（YOLO 标准目录）
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "images":
            parts[i] = "labels"
            break
    return Path(*parts)


def _yolo_results(txt: Path, classes: list[str]):
    results = []
    if txt.is_file():
        for line in txt.read_text(errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            k, nums = int(float(parts[0])), [float(x) for x in parts[1:]]
            if not 0 <= k < len(classes):
                raise ValueError(f"{txt.name} 里的类别编号 {k} 超出子项目类别 {classes}")
            if len(nums) == 4:
                cx, cy, w, h = nums
                x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
            else:  # 多边形：取外接框
                xs, ys = nums[0::2], nums[1::2]
                x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(1, x2), min(1, y2)
            if x2 - x1 > 1e-4 and y2 - y1 > 1e-4:
                results.append({"type": "rectanglelabels", "from_name": "label", "to_name": "image",
                                "value": {"x": x1 * 100, "y": y1 * 100, "width": (x2 - x1) * 100,
                                          "height": (y2 - y1) * 100, "rectanglelabels": [classes[k]]}})
    results.append({"type": "choices", "from_name": "coverage", "to_name": "image",
                    "value": {"choices": ["Targets reviewed" if results else "No target"]}})
    return results


def import_yolo(c, images_folder, labels_folder=None, image_ids=None):
    """把现成的 YOLO 标签（txt）导入为已复核标注：有框 = 已复核全部目标，没框 = 已复核无目标。"""
    from .labels import import_annotations
    if c.scenario.task != "detect":
        raise ValueError("YOLO 检测标签只能导入到检测类子项目")
    images_root = c.engine.allowed_source(images_folder, directory=True)
    labels_root = c.engine.allowed_source(labels_folder, directory=True) if labels_folder else None
    classes = list(c.scenario.classes)
    tasks, boxes = [], 0
    for img in _images(images_root, True):
        res = _yolo_results(_label_path(img, images_root, labels_root), classes)
        boxes += len(res) - 1
        tasks.append({"data": {"image_id": _sha256(img), "source": "YOLO 标签导入"},
                      "annotations": [{"result": res, "was_cancelled": False}]})
    if not tasks:
        raise ValueError("文件夹里没有图片")
    out = import_annotations(c.directory, tasks, classes, task="detect")
    out["boxes"] = boxes
    out["image_ids"] = [t["data"]["image_id"] for t in tasks]
    return out


class Coverage(Spec):
    predictions: list[dict] = Field(min_length=1, max_length=1000)
    label: str = Field(default="", max_length=64)   # 空 = 所有目标
    region: str = Field(default="lower_half", pattern="^(full|lower_half|lower_third)$")
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    threshold: float = Field(default=0.05, ge=0, le=1)


REGION_FRAC = {"full": 1.0, "lower_half": 0.5, "lower_third": 1 / 3}


def coverage_of(pred: dict, label: str, region: str, min_conf: float) -> float:
    w, h = pred.get("width") or 1, pred.get("height") or 1
    frac = REGION_FRAC[region]
    y0 = h * (1 - frac)
    area = 0.0  # 简化：框面积相加（重叠会略高估），够用于阈值判定
    for d in pred.get("detections") or []:
        if (label and d.get("label") != label) or d.get("confidence", 0) < min_conf:
            continue
        x1, y1, x2, y2 = d["xyxy"]
        y1 = max(y1, y0)
        if y2 > y1 and x2 > x1:
            area += (x2 - x1) * (y2 - y1)
    return min(1.0, area / (w * h * frac))


def judge_coverage(c, predictions, label="", region="lower_half", min_confidence=0.5, threshold=0.05):
    """按目标框覆盖率判定（如“杂草多”）：覆盖率 > 阈值 = 超标；生成复核单，网页上看图、调阈值、人工确认。"""
    items = []
    for p in predictions:
        cov = coverage_of(p, label, region, min_confidence)
        items.append({"image_id": p.get("image_id"), "width": p.get("width"), "height": p.get("height"),
                      "detections": [d for d in p.get("detections") or [] if not label or d.get("label") == label],
                      "coverage": round(cov, 4), "flagged": cov > threshold})
    review_id = f"{c.job_id[:8]}-{c.step_id}"
    folder = Path(c.directory) / "reviews"
    folder.mkdir(exist_ok=True)
    (folder / f"{review_id}.json").write_text(json.dumps({
        "review_id": review_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "label": label,
        "params": {"region": region, "min_confidence": min_confidence, "threshold": threshold},
        "items": items}, ensure_ascii=False), encoding="utf-8")
    flagged = [i["image_id"] for i in items if i["flagged"] and i["image_id"]]
    return {"review_id": review_id, "total": len(items), "flagged": len(flagged), "flagged_ids": flagged,
            "threshold": threshold}


# ── 抽图：从⑤类型归类（旧照片归类流水线的分拣结果）按照片类型取原图 ─────────────────
LEGACY_PIPELINE = deploy.LEGACY_PIPELINE   # ⑤页的归类结果（emb_index / hq_filter）
IMPORTS = deploy.IMPORTS_DIR


class PickType(Spec):
    photo_type: str = Field(min_length=1, max_length=64)
    only_confirmed: bool = False
    limit: int = Field(default=100, ge=1, le=1000)
    seed: int = 42


def pick_type(c, photo_type, only_confirmed=False, limit=100, seed=42):
    """按⑤页的照片类型（人工确认优先、否则模型预测）随机抽 N 张原图，复制进项目。抽过的不会重复抽。"""
    import random, shutil, sys
    if str(LEGACY_PIPELINE) not in sys.path:
        sys.path.insert(0, str(LEGACY_PIPELINE))
    import emb_index as ei, hq_filter as hf
    eff, human = ei.effective_types(), ei.confirmed_labels()
    if photo_type not in hf.TAXONOMY:
        raise ValueError(f"照片类型 {photo_type} 不在清单里")
    pool = [p for p, t in eff.items() if t == photo_type and (not only_confirmed or human.get(p) == photo_type)]
    project = Path(c.directory).name
    log = IMPORTS / project / "picked.json"   # 记录抽过的，下次不重复
    picked = set(json.loads(log.read_text())) if log.is_file() else set()
    pool = sorted(p for p in pool if p not in picked)
    random.Random(seed).shuffle(pool)
    chosen = pool[:limit]
    if not chosen:
        raise ValueError(f"“{hf.TAXONOMY[photo_type]['name']}”没有可抽的新图片（已抽过 {len(picked)} 张）")
    folder = IMPORTS / project / f"{photo_type}-{time.strftime('%Y%m%d-%H%M%S')}"
    folder = _fresh_dir(folder)
    for p in chosen:  # 复制，源图不动
        src = Path(hf.locate(p))
        shutil.copy2(src, folder / src.name)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(sorted(picked | set(chosen)), ensure_ascii=False))
    out = import_folder(c, str(folder), recursive=False)
    out.update({"photo_type": photo_type, "pool_left": len(pool) - len(chosen), "picked_before": len(picked)})
    return out


def _ls_client(cfg):
    """连接 Label Studio；令牌没配好时给出中文提示。"""
    from .labels import LabelStudioClient
    try:
        return LabelStudioClient.from_config(cfg)
    except ValueError as e:
        if "unset" in str(e) or "token" in str(e).lower():
            raise ValueError("还没配置 Label Studio 令牌：在 Label Studio 右上角头像 → Account & Settings 复制访问令牌，"
                             "写进引擎目录的 .env（一行 LABEL_STUDIO_TOKEN=令牌），"
                             "然后 bash engine.sh restart") from e
        raise


# ── Label Studio：建项目 + 带模型预框推送 ─────────────────────────────────────
class LsSetup(Spec):
    base_url: str = "http://127.0.0.1:8081"
    title: str | None = None
    token_env: str = "LABEL_STUDIO_TOKEN"
    local_files_root: str = str(deploy.DATA_ROOT)


def ls_setup(c, base_url="http://127.0.0.1:8081", title=None, token_env="LABEL_STUDIO_TOKEN",
             local_files_root=str(deploy.DATA_ROOT)):
    """给当前子项目建一个 Label Studio 项目（标注模板由引擎生成），并登记本地图片目录；已连接过就直接复用。"""
    from .labels import LabelStudioClient, label_config
    project = Path(c.directory).name
    if c.scenario.label_studio:
        return {"project_id": c.scenario.label_studio.project_id, "reused": True,
                "url": f"{c.scenario.label_studio.base_url}/projects/{c.scenario.label_studio.project_id}"}
    cfg = {"base_url": base_url, "token_env": token_env}
    client = _ls_client(cfg)
    created = client._request("POST", "/api/projects/", payload={
        "title": title or f"LOTA · {c.scenario.name}", "label_config": _pose_cfg(c) or label_config(list(c.scenario.classes), c.scenario.task)})
    pid = created["id"]
    raw = (Path(c.directory) / "raw").resolve()
    raw.mkdir(exist_ok=True)
    client._request("POST", "/api/storages/localfiles", payload={  # 允许 Label Studio 读引擎复制进来的原图
        "project": pid, "path": str(raw), "use_blob_urls": True, "title": "LOTA raw"})
    from .store import Scenario
    current = c.engine.store.project(project)
    config = dict(current["config"])
    config["label_studio"] = {"base_url": base_url, "project_id": pid, "token_env": token_env,
                              "local_files_root": local_files_root}
    c.engine.save(project, Scenario.model_validate(config), current["revision"])  # 走引擎校验，记一个新配置版本
    return {"project_id": pid, "reused": False, "url": f"{base_url}/projects/{pid}"}


class PushPrelabel(Selection):
    selector: str = "production"
    confidence: float = Field(default=0.25, ge=0, le=1)
    device: str = "0"


def push_prelabel(c, image_ids, selector="production", confidence=0.25, device="0"):
    """把图片送进 Label Studio，并带上模型预框（打开就有框，只需删错框、补漏框、调边界，再选“Targets reviewed”）。"""
    from .labels import LabelStudioClient, _make_tasks, _project_name, _remote_tasks_by_image
    from .models import predict
    ls = c.engine.store.project(Path(c.directory).name)["config"].get("label_studio") or (
        c.scenario.label_studio.model_dump() if c.scenario.label_studio else None)
    if not ls:
        raise ValueError("还没连接 Label Studio：先运行“连接 Label Studio”节点")
    client = _ls_client(ls)
    task = c.scenario.task
    existing = _remote_tasks_by_image(client, ls["project_id"], _project_name(c.directory), task)
    todo = [i for i in dict.fromkeys(image_ids) if i not in existing]
    if not todo:
        return {"pushed": 0, "skipped": len(image_ids), "url": f"{ls['base_url']}/projects/{ls['project_id']}"}
    paths = c.image_paths(todo)
    result = predict(c.directory, selector, paths, output=f"{c.job_id}-{c.step_id}-prelabel.json",
                     confidence=confidence, device=device)
    by_path = {str(Path(p["path"]).resolve()): p for p in result.get("predictions", [])}
    tasks = _make_tasks(c.directory, todo, local_files_root=ls.get("local_files_root"), media_base=ls.get("media_base"), task=task)
    n_boxes = 0
    for t, path in zip(tasks, paths):
        p = by_path.get(str(Path(path).resolve()), {})
        w, h = p.get("width") or 1, p.get("height") or 1
        res = []
        for d in p.get("detections") or []:
            x1, y1, x2, y2 = d["xyxy"]
            if task == "obb" and d.get("obb"):   # 旋转框：四角点 → Label Studio 可旋转矩形（左上角 + 宽高 + 角度）
                import math
                (ax, ay), (bx, by), (cx_, cy_), _ = d["obb"]
                bw, bh = math.hypot(bx - ax, by - ay), math.hypot(cx_ - bx, cy_ - by)
                res.append({"from_name": "objects", "to_name": "image", "type": "rectanglelabels",
                            "original_width": w, "original_height": h, "image_rotation": 0,
                            "value": {"x": ax / w * 100, "y": ay / h * 100, "width": bw / w * 100, "height": bh / h * 100,
                                      "rotation": math.degrees(math.atan2(by - ay, bx - ax)) % 360, "rectanglelabels": [d["label"]]},
                            "score": d.get("confidence")})
                continue
            if task == "segment" and d.get("polygon") and len(d["polygon"]) >= 3:   # 分割：多边形预标注
                res.append({"from_name": "objects", "to_name": "image", "type": "polygonlabels",
                            "original_width": w, "original_height": h, "image_rotation": 0,
                            "value": {"points": [[x / w * 100, y / h * 100] for x, y in d["polygon"]], "polygonlabels": [d["label"]]},
                            "score": d.get("confidence")})
                continue
            if task == "pose" and d.get("keypoints"):   # 姿态：每个关键点作为 KeyPointLabels 预标注
                from .labels import pose_keypoints
                names = pose_keypoints(c.directory)
                for (kx, ky, kc), kname in zip(d["keypoints"], names):
                    if kc >= 0.3:
                        res.append({"from_name": "kp", "to_name": "image", "type": "keypointlabels", "original_width": w, "original_height": h,
                                    "value": {"x": kx / w * 100, "y": ky / h * 100, "width": 0.6, "keypointlabels": [kname]}})
            res.append({"from_name": "objects", "to_name": "image", "type": "rectanglelabels",
                        "original_width": w, "original_height": h,
                        "value": {"x": x1 / w * 100, "y": y1 / h * 100, "width": (x2 - x1) / w * 100,
                                  "height": (y2 - y1) / h * 100, "rectanglelabels": [d["label"]]},
                        "score": d.get("confidence")})
        n_boxes += len(res)
        t["data"]["ailabel_label_schema"] = {"task": task, "classes": list(c.scenario.classes)}
        t["predictions"] = [{"model_version": result.get("model_version", selector), "result": res}]
    for i in range(0, len(tasks), 100):
        client.import_tasks(ls["project_id"], tasks[i:i + 100])
    return {"pushed": len(tasks), "skipped": len(image_ids) - len(tasks), "prelabel_boxes": n_boxes,
            "model_version": result.get("model_version"), "url": f"{ls['base_url']}/projects/{ls['project_id']}"}


TOOLS.update({
    "curation.pick_type": Tool("按照片类型抽图", PickType, pick_type),
    "labels.ls_setup": Tool("连接 Label Studio", LsSetup, ls_setup),
    "labels.push_prelabel": Tool("送 Label Studio（带模型预框）", PushPrelabel, push_prelabel),
})


# ── 用权重找图：拿现成的 YOLO 权重扫海量原图，挑出含目标的照片（断点续扫）──────────────
class FindByModel(Spec):
    weights: str = ""                      # .pt 权重文件；和 model 二选一
    model: str = ""                        # 本场景已注册的模型版本或别名（如 latest）；填了就优先用它
    folder: str = str(deploy.IMAGES_DIR)
    photo_type: str = ""                   # 只在⑤归类为这个照片类型的原图里找（如 ac_distribution）；空 = 扫 folder
    order: str = Field(default="sorted", pattern="^(sorted|random)$")   # random = 打乱顺序扫，找到的图更分散
    seed: int = 1
    skip_known: bool = True                # 已经在本场景里的图（含删过的）不算命中
    strategy: str = Field(default="confident", pattern="^(confident|uncertain|mixed)$")  # 挑图策略
    per_folder: int = Field(default=0, ge=0, le=1000)   # 每个文件夹（站点）最多挑几张；0 = 不限
    dedupe: bool = True                    # 几乎一样的照片只留一张
    labels: str | list[str] = ""           # "weeds,xxx" 或 ["weeds"]；空 = 模型的任何目标都算
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    min_boxes: int = Field(default=1, ge=1, le=100)
    region: str = Field(default="full", pattern="^(full|lower_half|lower_third)$")
    min_coverage: float = Field(default=0.0, ge=0, le=1)
    scan_limit: int = Field(default=5000, ge=1, le=200000)
    max_hits: int = Field(default=200, ge=1, le=1000)
    imgsz: int = Field(default=640, ge=160, le=2048)
    batch: int = Field(default=16, ge=1, le=128)
    device: str = "0"
    restart: bool = False                  # 勾选 = 忘掉之前扫过的记录，从头扫


_NAMES_CACHE = {}


def _dhash(path):
    """感知哈希（64 位）：几乎一样的照片哈希距离很小"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            g = im.convert("L").resize((9, 8))
            px = list(g.getdata())
        return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])
    except Exception:
        return None


def _weights_names(path):
    """读 YOLO 权重里的类别名和任务类型（带缓存）"""
    p = Path(path)
    key = (str(p), p.stat().st_mtime)
    if key not in _NAMES_CACHE:
        from ultralytics import YOLO
        m = YOLO(str(p))
        names = m.names
        _NAMES_CACHE[key] = {"names": [names[i] for i in sorted(names)] if isinstance(names, dict) else list(names),
                             "task": m.task}
    return _NAMES_CACHE[key]


def find_by_model(c, weights="", model="", folder=str(deploy.IMAGES_DIR), photo_type="", order="sorted", seed=1,
                  skip_known=True, strategy="confident", per_folder=0, dedupe=True, labels="", min_confidence=0.5, min_boxes=1,
                  region="full", min_coverage=0.0, scan_limit=5000, max_hits=200, imgsz=640, batch=16, device="0",
                  restart=False):
    """用一个 YOLO 模型（.pt 权重，或本场景已注册的模型/别名）去扫原图，把含目标的照片复制进项目。
    扫过的文件记下来，下次接着扫（断点续扫）；源文件不动。"""
    import random
    import shutil
    from ultralytics import YOLO
    if model:
        from .models import resolve_model
        try:
            _, w = resolve_model(Path(c.directory), model)
        except (FileNotFoundError, ValueError, KeyError) as e:
            raise ValueError(f"本场景还没有模型“{model}”（先训练一轮）：{e}") from e
        w = Path(w)
    elif weights:
        w = c.engine.allowed_weights(weights)
    else:
        raise ValueError("请选择权重文件，或填本场景已注册的模型（如 latest）")
    if photo_type:  # 只在⑤归类为这个类型的原图里找
        import sys
        if str(LEGACY_PIPELINE) not in sys.path:
            sys.path.insert(0, str(LEGACY_PIPELINE))
        import emb_index as ei, hq_filter as hf
        pool = sorted(p for p, t in ei.effective_types().items() if t == photo_type)
        if not pool:
            raise ValueError(f"⑤类型归类里没有“{photo_type}”类型的照片")
        all_files = [Path(hf.locate(p)) for p in pool]
        all_files = [p for p in all_files if p.is_file()]
        root = "⑤:" + photo_type
    else:
        root = c.engine.allowed_source(folder, directory=True)
        all_files = _images(root, True)
    known = set()
    if skip_known:
        from .catalog import _database
        with _database(c.directory) as db:
            known = {r[0] for r in db.execute("SELECT image_id FROM images")} | {r[0] for r in db.execute("SELECT image_id FROM deleted")}
    info = _weights_names(w)
    if isinstance(labels, (list, tuple)):  # 旧工作台编辑器传列表，新编辑器传逗号分隔的文字
        labels = ",".join(str(x) for x in labels)
    wanted = [x.strip() for x in labels.replace("，", ",").split(",") if x.strip()]
    unknown = [x for x in wanted if x not in info["names"]]
    if unknown:
        raise ValueError(f"权重 {w.name} 认不出 {unknown}；它能识别的类别：{info['names']}")
    want = set(wanted or info["names"])
    rule = dict(weights=str(w), folder=str(root), labels=sorted(want), conf=min_confidence, boxes=min_boxes,
                region=region, cov=min_coverage)
    key = hashlib.sha1(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:10]
    state = Path(c.directory) / "find" / key
    if restart and state.exists():
        shutil.rmtree(state)
    state.mkdir(parents=True, exist_ok=True)
    (state / "rule.json").write_text(json.dumps(rule, ensure_ascii=False, indent=1), encoding="utf-8")
    done_file = state / "scanned.txt"
    done = set(done_file.read_text(encoding="utf-8").splitlines()) if done_file.is_file() else set()
    files = [p for p in all_files if str(p) not in done]
    if order == "random":
        random.Random(seed).shuffle(files)
    total = len(files) + len(done)
    todo = files[:scan_limit]
    progress = state / "progress.json"
    model = YOLO(str(w))
    # 上次扫到了、但没来得及复制进项目（任务中途失败）的命中，这次一起补上
    pending = state / "pending_hits.jsonl"
    hits = [json.loads(x) for x in pending.read_text(encoding="utf-8").splitlines() if x.strip()] if pending.is_file() else []
    hits = [h for h in hits if Path(h["path"]).is_file()]
    scanned, t0 = 0, time.time()
    # 挑图策略（主动学习常用的“不确定性 + 多样性”）：confident = 挑模型最有把握的（够数就停）；
    # uncertain = 扫完本次范围，挑模型最拿不准的（置信度接近 0.5，标了最长本事）；mixed = 两者交替
    floor = min(min_confidence, 0.1) if strategy != "confident" else min_confidence
    pred_conf = min(floor, 0.25)

    def judge(r):
        h, wd = r.orig_shape
        if info["task"] == "classify":
            top = r.probs.top1
            name, conf = r.names[top], float(r.probs.top1conf)
            ok = {"top1": name, "confidence": round(conf, 4), "u": round(1 - abs(conf - 0.5) * 2, 4), "cmax": round(conf, 4)} \
                if name in want and conf >= floor else None
            return ok, wd, h, []
        dets = []
        for b in r.boxes:
            name, conf = r.names[int(b.cls)], float(b.conf)
            if name in want and conf >= floor:
                dets.append({"label": name, "confidence": round(conf, 4),
                             "xyxy": [round(float(v), 1) for v in b.xyxy[0]]})
        if len(dets) < min_boxes:
            return None, wd, h, dets
        pred = {"width": wd, "height": h, "detections": dets}
        cov = min(1.0, sum(coverage_of(pred, n, region, floor) for n in want))
        if cov < min_coverage:
            return None, wd, h, dets
        top3 = sorted((1 - abs(d["confidence"] - 0.5) * 2 for d in dets), reverse=True)[:3]
        return {"coverage": round(cov, 4), "u": round(sum(top3) / len(top3), 4),
                "cmax": max(d["confidence"] for d in dets)}, wd, h, dets

    with open(done_file, "a", encoding="utf-8") as log, open(pending, "a", encoding="utf-8") as keep:
        for i in range(0, len(todo), batch):
            if strategy == "confident" and len(hits) >= max_hits:
                break
            chunk = todo[i:i + batch]
            try:
                pairs = list(zip(chunk, model.predict([str(p) for p in chunk], imgsz=imgsz, conf=pred_conf,
                                                      device=device, verbose=False)))
            except Exception:  # 这一批里有坏图：逐张跑，坏的跳过
                pairs = []
                for p in chunk:
                    try:
                        pairs.append((p, model.predict(str(p), imgsz=imgsz, conf=pred_conf, device=device, verbose=False)[0]))
                    except Exception:
                        pass
            for p, r in pairs:
                ok, wd, h, dets = judge(r)
                if ok is not None:
                    sha = _sha256(Path(p))
                    if sha in known:   # 已经在本场景里（或删过）的不算
                        continue
                    known.add(sha)
                    hit = {"path": str(p), "sha": sha, "width": wd, "height": h, "detections": dets, **ok}
                    hits.append(hit)
                    keep.write(json.dumps(hit, ensure_ascii=False) + "\n")
            keep.flush()
            log.write("".join(str(p) + "\n" for p in chunk))
            log.flush()
            scanned += len(chunk)
            progress.write_text(json.dumps({
                "key": key, "step": c.step_id, "job": c.job_id, "scanned": scanned, "todo": len(todo),
                "hits": len(hits), "total_files": total, "scanned_total": len(done) + scanned,
                "seconds": round(time.time() - t0), "updated": time.strftime("%H:%M:%S")}), encoding="utf-8")
    # 命中的复制进项目（按内容去重，同一张图在多个目录里只算一次），再登记进图库
    candidates = len(hits)
    if strategy == "uncertain":
        hits.sort(key=lambda x: -x.get("u", 0))
    elif strategy == "mixed":  # 最拿不准的和最有把握的交替挑
        a = sorted(hits, key=lambda x: -x.get("u", 0)); b = sorted(hits, key=lambda x: -x.get("cmax", 0))
        hits = [x for pair in zip(a, b) for x in pair]
    # 多样性：每个文件夹（通常是一个站点/一次巡检）最多 per_folder 张；几乎一样的照片（感知哈希）只留一张
    by_sha, per_dir, hashes = {}, {}, []
    for hit in hits:
        if len(by_sha) >= max_hits:
            break
        sha = hit.get("sha") or _sha256(Path(hit["path"]))
        if sha in by_sha:
            continue
        folder_key = str(Path(hit["path"]).parent)
        if per_folder and per_dir.get(folder_key, 0) >= per_folder:
            continue
        if dedupe:
            dh = _dhash(hit["path"])
            if dh is not None and any(bin(dh ^ x).count("1") <= 5 for x in hashes):
                continue
            if dh is not None:
                hashes.append(dh)
        by_sha[sha] = hit
        per_dir[folder_key] = per_dir.get(folder_key, 0) + 1
    out = {"image_ids": [], "predictions": [], "count": 0}
    if by_sha:
        dest = IMPORTS / Path(c.directory).name / f"find-{w.stem}-{time.strftime('%Y%m%d-%H%M%S')}"
        dest = _fresh_dir(dest)
        for sha, hit in by_sha.items():
            src = Path(hit["path"])
            shutil.copy2(src, dest / f"{sha[:8]}_{src.name}")
        out = import_folder(c, str(dest), recursive=False)
        pending.unlink(missing_ok=True)  # 已经进项目了，清掉待补记录
        out["predictions"] = [{"image_id": sha, "width": h["width"], "height": h["height"], "detections": h["detections"],
                               "source": h["path"], **{k: h[k] for k in ("coverage", "top1", "confidence") if k in h}}
                              for sha, h in by_sha.items()]
        # 直接生成复核单：网页上看找到的图和框
        review_id = f"{c.job_id[:8]}-{c.step_id}"
        rv = Path(c.directory) / "reviews"
        rv.mkdir(exist_ok=True)
        (rv / f"{review_id}.json").write_text(json.dumps({
            "review_id": review_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "label": ",".join(sorted(want)),
            "params": {"region": region, "min_confidence": min_confidence, "threshold": min_coverage},
            "items": [{"image_id": p["image_id"], "width": p["width"], "height": p["height"],
                       "detections": p["detections"], "coverage": p.get("coverage", 0), "flagged": True}
                      for p in out["predictions"]]}, ensure_ascii=False), encoding="utf-8")
        out["review_id"] = review_id
    out.update({"candidates": candidates, "strategy": strategy, "scanned": scanned, "hits": len(by_sha), "scanned_total": len(done) + scanned, "total_files": total,
                "remaining": total - len(done) - scanned, "classes": info["names"], "task": info["task"], "state": key})
    return out


TOOLS.update({
    "catalog.import_folder": Tool("导入图片文件夹", ImportFolder, import_folder),
    "labels.import_yolo": Tool("导入 YOLO 标签", ImportYolo, import_yolo),
    "judge.coverage": Tool("覆盖率判定（如杂草多）", Coverage, judge_coverage),
    "curation.find_by_model": Tool("用权重找图", FindByModel, find_by_model),
})


# ── 节点目录（给工作流编辑器）────────────────────────────────────────────────
def P(label, help, kind="text", default=None, **kw):
    return {"label": label, "help": help, "kind": kind, "default": default, **kw}


NODES = {
    "catalog.import_folder": {
        "title": "导入图片文件夹", "desc": "扫描服务器上的一个文件夹，按图片内容去重后**复制**进当前子项目（原文件不动）。一次最多 1000 张。",
        "inputs": [], "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "folder": P("文件夹（服务器路径）", "必须在引擎允许的目录下，例如 /data/imports/demo_v0/images"),
            "recursive": P("包含子文件夹", "勾选后连 train/val 等子文件夹一起导入", "toggle", True)}},
    "labels.import_yolo": {
        "title": "导入 YOLO 标签", "desc": "把现成的 YOLO 标签（每张图一个同名 .txt，类别编号对应子项目的类别顺序）导入为**已复核**标注。有框 = 已复核全部目标；没有框 = 已复核、无目标。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "已标注图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "images_folder": P("图片文件夹", "和上游“导入图片文件夹”相同的路径"),
            "labels_folder": P("标签文件夹（可不填）", "不填 = 把路径里的 images 换成 labels（YOLO 标准目录结构）")}},
    "dataset.publish": {
        "title": "发布数据集", "desc": "把已复核的标注冻结成一个数据集版本，按重复组/站点划分训练集和验证集，同一组的图不会两边都有。",
        "inputs": [{"name": "已标注图片", "type": "IMAGES", "arg": None}],
        "outputs": [{"name": "数据集", "type": "DATASET", "field": "version"}],
        "params": {
            "version": P("数据集版本名", "例如 demo-v2；写 {n} 自动编号，如 mcb-r{n} → mcb-r1、mcb-r2…；同名同内容会直接复用，同名不同内容会报错"),
            "val_fraction": P("验证集比例", "0.2 = 20% 的图做验证", "number", 0.2, min=0.05, max=0.5, step=0.05),
            "test_fraction": P("测试集比例", "0 = 不留测试集；要和原模型公平比较时建议留 0.1", "number", 0.0, min=0, max=0.4, step=0.05),
            "seed": P("随机种子", "同样的种子划分结果一样，方便复现", "number", 42, min=0, max=99999, step=1)}},
    "model.train": {
        "title": "训练模型", "desc": "用数据集训练 YOLO 模型，训练完自动注册为一个模型版本。8G 显卡建议 batch ≤ 8。",
        "inputs": [{"name": "数据集", "type": "DATASET", "arg": "dataset_version"}],
        "outputs": [{"name": "模型", "type": "MODEL", "field": "version"}],
        "params": {
            "model_version": P("模型版本名", "例如 demo-y8m-v2；可写 {dataset}（= 数据集版本名）和 {n}（自动编号），如 {dataset}-n"),
            "continue_from": P("从这个模型继续训（可不填）", "填已注册模型的版本或别名，如 latest：有就从它继续训（主动学习每轮接着上一轮）；还没有就用下面的初始权重", "text", ""),
            "weights": P("初始权重", "从哪个模型开始训：已有的杂草模型（继续训）或 yolov8n（从通用模型开始）", "weights"),
            "epochs": P("训练轮数", "数据少时 50～100；轮数多不一定好，有早停", "number", 60, min=1, max=500, step=1),
            "imgsz": P("图片尺寸", "640 常用；小目标多可以 960，但更慢、更占显存", "combo", 640, values=[320, 480, 640, 800, 960, 1280]),
            "batch": P("批大小", "8G 显卡：yolov8m 用 8，yolov8n 可以 16", "number", 8, min=1, max=64, step=1),
            "lr0": P("初始学习率", "从已训好的模型继续训建议 0.001（小）；从通用模型开始可用 0.01（默认）", "number", 0.001, min=0.0001, max=0.1, step=0.0005),
            "patience": P("早停轮数", "验证指标连续多少轮不提升就提前停止", "number", 20, min=0, max=200, step=1),
            "device": P("设备", "0 = GPU；cpu 会非常慢", "combo", "0", values=["0", "cpu"]),
            "workers": P("读图线程", "2 即可", "number", 2, min=0, max=8, step=1)}},
    "model.evaluate": {
        "title": "评估模型", "desc": "在数据集的验证集（或测试集）上算指标：precision 准确率、recall 召回率、mAP50。要比较两个模型，用同一个数据集评估。",
        "inputs": [{"name": "模型", "type": "MODEL", "arg": "selector"}, {"name": "数据集", "type": "DATASET", "arg": "dataset_version"}],
        "outputs": [{"name": "指标", "type": "METRICS", "field": "metrics"}],
        "params": {
            "split": P("评估集", "val 验证集；发布数据集时留了测试集才能选 test", "combo", "val", values=["val", "test"]),
            "device": P("设备", "0 = GPU", "combo", "0", values=["0", "cpu"])}},
    "model.alias": {
        "title": "发布模型", "desc": "把模型版本设为某个别名（默认 production）。批量检测默认用 production；可以随时切回旧版本（回滚）。",
        "inputs": [{"name": "模型", "type": "MODEL", "arg": "version"}],
        "outputs": [{"name": "模型", "type": "MODEL", "field": "version"}],
        "params": {"alias": P("别名", "production = 正式使用的模型", "text", "production")}},
    "model.predict": {
        "title": "批量检测", "desc": "用模型检测一批图片。不连“模型”就用 production。一次最多 1000 张。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}, {"name": "模型", "type": "MODEL", "arg": "selector", "optional": True}],
        "outputs": [{"name": "检测结果", "type": "PREDICTIONS", "field": "predictions"}],
        "params": {
            "selector": P("模型（没连线时用）", "模型版本名或别名", "model", "production"),
            "confidence": P("置信度下限", "低于这个分数的框不要；0.25 召回多、0.5 更准", "number", 0.25, min=0.05, max=0.95, step=0.05),
            "device": P("设备", "0 = GPU", "combo", "0", values=["0", "cpu"])}},
    "judge.coverage": {
        "title": "覆盖率判定", "desc": "按目标框占画面的比例判定是否超标（如“杂草多”）。会生成**复核单**：网页上看带框的图、拖动阈值、人工确认。",
        "inputs": [{"name": "检测结果", "type": "PREDICTIONS", "arg": "predictions"}],
        "outputs": [{"name": "超标图片", "type": "IMAGES", "field": "flagged_ids"}, {"name": "复核单", "type": "REVIEW", "field": "review_id"}],
        "params": {
            "label": P("目标类别", "算哪一类框的覆盖率；留空 = 所有目标", "text", ""),
            "region": P("统计区域", "lower_half 下半部分（地面）；lower_third 下三分之一；full 全图", "combo", "lower_half", values=["lower_half", "lower_third", "full"]),
            "min_confidence": P("框的置信度下限", "低于这个分数的框不计入覆盖率", "number", 0.5, min=0.05, max=0.95, step=0.05),
            "threshold": P("超标阈值", "覆盖率超过它就判为超标；0.05 = 5%", "number", 0.05, min=0.005, max=0.5, step=0.005)}},
    "curation.unconfirm": {
        "title": "移除照片类别", "desc": "撤销这些照片的人工确认类别：分类器不再从它们学习，分拣结果回到 unknown（待复核）。照片本身不动。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}], "params": {}},
    "catalog.reject": {
        "title": "排除或恢复照片", "desc": "把照片标成“已排除”（不进数据集、不参与训练，照片还在），或者改回“待复核”/“已确认”。随时可以改回来。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "decision": P("改成", "rejected 排除 / pending 待复核 / accepted 已确认", "combo", "rejected", values=["rejected", "pending", "accepted"]),
            "note": P("备注（可不填）", "如：拍糊了、与巡检无关", "text", "")}},
    "catalog.delete": {
        "title": "删除照片", "desc": "从本场景**彻底删除**照片（登记、复制件、缩略图、类别、标注），以后扫描到同样内容也不再登记。已发布的数据集版本不受影响。勾选“删原图文件”才会删服务器上的原文件，**不可恢复**。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}], "outputs": [],
        "params": {"delete_source": P("同时删原图文件", "不勾 = 原图目录里的文件保留；勾选 = 真的删掉（不可恢复）", "toggle", False)}},
    "inspect.batch": {
        "title": "批量质检", "desc": "按“智能质检”页配置的规则逐张判定上游图片：合格 / 需注意 / 不合格，并记入质检记录。**不合格**和**需注意**的图分两路输出，可接“送 Label Studio”人工复核或“分流到目录”。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "不合格的图", "type": "IMAGES", "field": "fail_ids"}, {"name": "需注意的图", "type": "IMAGES", "field": "warn_ids"}],
        "params": {
            "photo_type": P("照片类型（可不填）", "空 = 用每张图自己的检查点去匹配规则；填了就所有图都按这个类型", "text", ""),
            "pipelines": P("只用这些规则（可不填）", "规则 ID，逗号隔开；空 = 按照片类型自动匹配", "text", "")}},
    "model.export": {
        "title": "导出到手机", "desc": "把模型导出成手机 / 边缘设备能跑的格式，文件出现在“模型中心”的模型卡片上，可下载。",
        "inputs": [{"name": "模型", "type": "MODEL", "arg": "version", "optional": True}], "outputs": [],
        "params": {
            "version": P("模型（没连线时用）", "模型版本或别名，如 production", "model", "production"),
            "format": P("格式", "tflite_int8 = 安卓首选（体积约 1/4）；tflite = 不量化；ncnn = 安卓/ARM；onnx = 通用", "combo", "tflite_int8", values=["tflite_int8", "tflite", "ncnn", "onnx"]),
            "imgsz": P("尺寸", "手机上 640 常用；要更快可 480/320", "number", 640, min=160, max=1600, step=32)}},
    "image.enhance": {
        "title": "图像增强（生成增强副本）", "desc": "对上游图片（或一个文件夹）做增强，生成**副本**加进子项目（原图不动）：暗光、雾天、偏色、模糊的照片更清楚；也可用来扩充训练数据。效果可先在“工具箱”里预览。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids", "optional": True}],
        "outputs": [{"name": "增强后的图", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "folder": P("文件夹（没连线时用）", "服务器上的图片文件夹", "text", ""),
            "method": P("增强方法", "auto 自动 / dehaze 去雾 / low_light 暗光 / clahe 提升对比度 / denoise 去噪（慢）/ sharpen 锐化 / white_balance 白平衡", "combo", "auto",
                        values=["auto", "dehaze", "low_light", "clahe", "denoise", "sharpen", "white_balance"]),
            "strength": P("强度", "0–1；0.6 适中，太强会失真", "number", 0.6, min=0, max=1, step=0.05),
            "max_side": P("最长边（像素）", "0 = 保持原尺寸；2000 可明显加快、节省空间", "number", 0, min=0, max=8000, step=100),
            "limit": P("最多几张", "一次最多 1000", "number", 500, min=1, max=1000, step=10)}},
    "video.frames": {
        "title": "视频抽帧", "desc": "把视频按间隔抽成图片（文件名带时间点），跳过几乎一样的帧，加进子项目；之后就和照片一样标注、训练、质检。视频可在“工具箱”上传。",
        "inputs": [], "outputs": [{"name": "帧图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "video": P("视频文件（服务器路径）", "如 /data/imports/<子项目>/videos/xxx.mp4（在工具箱上传后可复制路径）", "text", ""),
            "every_sec": P("每隔几秒一帧", "固定机位 2–5 秒；走动拍摄 0.5–1 秒", "number", 1.0, min=0.1, max=600, step=0.5),
            "max_frames": P("最多几帧", "一次最多 1000", "number", 300, min=1, max=1000, step=10),
            "skip_similar": P("跳过相似帧", "和上一帧几乎一样就不要", "toggle", True),
            "start_sec": P("从第几秒开始", "", "number", 0, min=0, max=100000, step=1),
            "end_sec": P("到第几秒结束", "0 = 到结尾", "number", 0, min=0, max=100000, step=1)}},
    "labels.ls_video": {
        "title": "建视频标注项目", "desc": "在 Label Studio 建一个视频跟踪标注项目：关键帧画框，中间帧自动插值；类别 = 本子项目的类别。",
        "inputs": [], "outputs": [],
        "params": {
            "videos": P("视频文件（服务器路径）", "多个用逗号隔开", "text", "", array=True),
            "title": P("项目名（可不填）", "", "text", "")}},
    "quality.mistakes": {
        "title": "找标注错误", "desc": "用模型“复查”已标注的数据集（借鉴 FiftyOne 的 mistakenness）：挑出**疑似漏标**（模型很确定有、标注里没有）、**疑似多标/标错**（标了但模型不认）、**框不准**的图，按可疑程度排序；报告在“数据集”页查看，可一键跳到 Label Studio 修改。",
        "inputs": [{"name": "数据集", "type": "DATASET", "arg": "dataset_version", "optional": True}],
        "outputs": [{"name": "可疑的图", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "selector": P("用哪个模型复查", "一般用正式模型 production；模型越准，查得越准", "model", "production"),
            "split": P("查哪部分", "空 = 全部；val/test = 只查验证或测试部分（模型没见过，结论更可靠）", "combo", "", values=["", "train", "val", "test"]),
            "conf_missing": P("漏标门槛", "模型置信度 ≥ 它、标注里却没有，算疑似漏标；0.5 稳妥，调低查得多但误报多", "number", 0.5, min=0.05, max=0.99, step=0.05),
            "iou_good": P("框准的标准", "和模型框的重叠低于它算“框不准”；0.6 合适", "number", 0.6, min=0.1, max=0.95, step=0.05),
            "limit": P("最多查几张", "", "number", 500, min=1, max=5000, step=50)}},
    "labels.ls_webhook": {
        "title": "Label Studio 自动回收", "desc": "在 Label Studio 登记 webhook：你在 Label Studio **一提交或修改标注，引擎就自动回收**，总览、数据集页的数字实时更新，不用再手动“回收标注”。接在“连接 Label Studio”后面。",
        "inputs": [], "outputs": [],
        "params": {
            "enabled": P("开启", "勾选 = 开启自动回收；不勾 = 关闭", "toggle", True),
            "engine_url": P("引擎地址（给 Label Studio 回调用）", "Label Studio 和引擎在同一台服务器，用 http://127.0.0.1:8795", "text", "http://127.0.0.1:8795")}},
    "model.train_classic": {
        "title": "训练经典分类网络（非 YOLO）", "desc": "用 torchvision 的经典网络（ResNet / EfficientNet / ConvNeXt / MobileNet / ViT，ImageNet 预训练）训练图像分类，医学影像、工业外观分类常用。数据集必须是分类任务；训练好后和 YOLO 模型一样在模型中心管理、试一试、评估、质检。",
        "inputs": [{"name": "数据集", "type": "DATASET", "arg": "dataset_version"}],
        "outputs": [{"name": "模型", "type": "MODEL", "field": "version"}],
        "params": {
            "model_version": P("模型版本名", "可写 {dataset}、{n}，如 {dataset}-resnet50", "text", "{dataset}-resnet50"),
            "arch": P("网络", "resnet50 经典基线 / efficientnet_b0 小而准 / convnext_tiny 精度高 / resnet18 数据少 / mobilenet_v3_large 手机 / vit_b_16 Transformer", "combo", "resnet50",
                      values=["resnet50", "efficientnet_b0", "convnext_tiny", "resnet18", "mobilenet_v3_large", "vit_b_16"]),
            "epochs": P("训练轮数", "20 左右；验证准确率连续 patience 轮不涨就提前停", "number", 20, min=1, max=500, step=1),
            "imgsz": P("输入尺寸", "这些网络标准是 224；细节多的图可用 384（更慢）", "number", 224, min=64, max=1024, step=32),
            "batch": P("每批张数", "显存 8G 用 32；报显存不足就减半", "number", 32, min=1, max=512, step=1),
            "lr": P("学习率", "微调用 0.0005 左右", "number", 0.0005, min=0.00001, max=0.1, step=0.0001),
            "patience": P("提前停止（轮）", "", "number", 8, min=0, max=200, step=1),
            "device": P("设备", "0 = 显卡；cpu = 处理器", "combo", "0", values=["0", "cpu"])}},
    "catalog.import_dicom": {
        "title": "导入医学影像（DICOM）", "desc": "把 CT / MR / X 光 / 超声等 DICOM 文件按**窗宽窗位**转成普通图片加进子项目（多帧可取中间一帧或全部），之后就能标注、训练检测/分割/分类模型。**不复制病人姓名等隐私字段**，只记录模态和部位。",
        "inputs": [], "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "folder": P("DICOM 文件夹（服务器路径）", "放在引擎允许读的目录里", "text", ""),
            "window": P("窗宽窗位", "auto 自动拉伸 / dicom 用文件自带 / lung 肺窗 / mediastinum 纵隔窗 / bone 骨窗 / brain 脑窗 / abdomen 腹窗", "combo", "auto",
                        values=["auto", "dicom", "lung", "mediastinum", "bone", "brain", "abdomen"]),
            "frames": P("多帧取哪些", "middle = 每个序列取中间一帧；all = 全部帧", "combo", "middle", values=["middle", "all"]),
            "max_frames": P("最多几张", "", "number", 200, min=1, max=1000, step=10),
            "recursive": P("包含子文件夹", "", "toggle", True)}},
    "model.train_semseg": {
        "title": "训练语义分割网络（非 YOLO）", "desc": "用 torchvision 的 DeepLabV3 / LR-ASPP / FCN 做**逐像素**分割（医学器官/病灶勾画、自动驾驶可行驶区域的经典做法），复用“分割”子项目的多边形标注；预测的掩膜自动转成轮廓，质检覆盖率规则、试一试都能直接用。",
        "inputs": [{"name": "数据集", "type": "DATASET", "arg": "dataset_version"}],
        "outputs": [{"name": "模型", "type": "MODEL", "field": "version"}],
        "params": {
            "model_version": P("模型版本名", "可写 {dataset}、{n}", "text", "{dataset}-deeplab-v{n}"),
            "arch": P("网络", "deeplabv3_resnet50 精度高 / deeplabv3_mobilenet_v3_large 均衡 / lraspp_mobilenet_v3_large 最快 / fcn_resnet50 经典", "combo",
                      "deeplabv3_mobilenet_v3_large", values=["deeplabv3_mobilenet_v3_large", "deeplabv3_resnet50", "lraspp_mobilenet_v3_large", "fcn_resnet50"]),
            "epochs": P("训练轮数", "30 左右；验证 mIoU 连续 patience 轮不涨就停", "number", 30, min=1, max=500, step=1),
            "imgsz": P("输入尺寸", "512 常用；细节多用 768", "number", 512, min=128, max=1024, step=32),
            "batch": P("每批张数", "显存 8G、512 尺寸用 8", "number", 8, min=1, max=128, step=1),
            "lr": P("学习率", "0.0003 左右", "number", 0.0003, min=0.00001, max=0.1, step=0.0001),
            "device": P("设备", "0 = 显卡", "combo", "0", values=["0", "cpu"])}},
    # ── 原工作台的工具（迁移到新编辑器）────────────────────────────────────
    "catalog.scan": {
        "title": "扫描原图", "desc": "按场景配置里的“图片来源”目录建立索引：原地登记、按内容去重，**不复制**文件。新拍的照片要先扫描，才能被分拣。",
        "inputs": [], "outputs": [{"name": "扫描完成", "type": "SCAN", "field": "scanned"}],
        "params": {
            "limit": P("本次最多检查几个新文件", "已登记过的不算；数量大时分几次扫", "number", 1000, min=1, max=1000000, step=100),
            "stability_seconds": P("文件稳定等待（秒）", "文件最后修改后要过这么久才登记，避免登记正在拷贝的半截文件", "number", 2, min=0, max=3600, step=1)}},
    "sorting.candidates": {
        "title": "读取待分拣图片", "desc": "从本场景图库里取出最多 N 张图片，作为后面画质检查、去重、归类的输入。前面可以接“扫描原图”，保证先扫描再读取。",
        "inputs": [{"name": "扫描完成", "type": "SCAN", "arg": None, "optional": True}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {"limit": P("最多读取几张", "一次最多 1000 张", "number", 1000, min=1, max=1000, step=50)}},
    "sorting.quality": {
        "title": "检查画质并标记", "desc": "给每张图标出画质问题（模糊、过暗、过曝、太小等）。**只打标记，不删除**，之后可以在图库里按标记筛选。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}], "params": {}},
    "sorting.vlm_check": {
        "title": "AI 预检（qwen 看图）", "desc": "用服务器本机的视觉大模型（Ollama qwen2.5vl）逐张看图，判断能不能做训练数据：截图/文档、不是现场照片、主体不清楚、模糊过暗过曝遮挡的**不通过**；本场景定义了照片类别时，和所有类别都不相关的也不通过。通过 / 不通过分两路输出，可以只把通过的送去标注。同一张图判断过会复用结果。约 3–6 秒一张。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "通过", "type": "IMAGES", "field": "image_ids"},
                    {"name": "不通过", "type": "IMAGES", "field": "failed_ids"}],
        "params": {
            "model": P("视觉模型", "Ollama 里的视觉模型名；默认 qwen2.5vl:7b（服务器上已有）", "combo", "qwen2.5vl:7b",
                       values=["qwen2.5vl:7b", "qwen2.5vl:3b", "qwen2.5vl:32b"]),
            "min_clarity": P("最低清晰度", "主体清晰度 1–5 分（1 完全看不清，5 非常清楚），低于它不通过；4 是做训练数据的稳妥值", "number", 4, min=1, max=5, step=1),
            "reject_failed": P("不通过的标为“排除”", "勾上：不通过的照片在图库里标为“已排除”（不删除，可恢复），备注写原因；不勾：只分流，不改图库", "toggle", False),
            "recheck": P("重新判断", "勾上：忽略以前的结果重新问一遍模型（换了提问或想复核时用）", "toggle", False),
        }},
    "sorting.duplicates": {
        "title": "重复图候选归组", "desc": "按感知哈希把几乎一样的照片（同一场景连拍、重复上传）归成一组，方便复核时只看一张。**不删除**。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}], "params": {}},
    "sorting.classify": {
        "title": "自动照片类型归类", "desc": "给每张图一个照片类型候选（待人工复核）。依据的优先级：人工确认 > 照片自带的检查点 > 照片分类器或特征模型 > 历史分类文件。已经训练并启用了照片分类器时，直接用它。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "hints_path": P("历史分类文件（可不填）", "以前⑤类型归类的结果，如 /data/pipeline/work/emb/pred.json", "text", ""),
            "model_dir": P("特征模型（可不填）", "填 SigLIP2 模型目录时，按类别名零样本归类；留空就用已启用的照片分类器或上面的历史文件", "feature_model", "")}},
    "selection.find": {
        "title": "检测指定目标（已注册模型）", "desc": "用本场景**已注册**的检测模型（版本名或别名）检查图片里有没有指定目标，分成“命中”和“未命中”两路。要直接用 .pt 权重扫海量原图，请用“用权重找图”。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "命中图片", "type": "IMAGES", "field": "image_ids"},
                    {"name": "未命中图片", "type": "IMAGES", "field": "remaining_ids"},
                    {"name": "检测结果", "type": "PREDICTIONS", "field": "predictions"}],
        "params": {
            "selector": P("模型版本或别名", "本场景已注册的模型，如 production、demo-y8m-v1", "model", "production"),
            "labels": P("目标类别", "模型类别名，多个用逗号隔开，如 weeds", "text", "", array=True),
            "confidence": P("置信度阈值", "把握分数低于它的框不算", "number", 0.4, min=0, max=1, step=0.05)}},
    "selection.files": {
        "title": "分流到指定目录", "desc": "把图片复制到本场景 sorting/ 下的一个子文件夹（原文件不动）。**默认只预演**，列出会复制哪些文件；勾选“真的执行”才复制。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "destination": P("子文件夹名", "如 matched、many_targets；会建在本场景的 sorting/ 目录下", "text", "matched"),
            "execute": P("真的执行", "不勾 = 只预演；勾选 = 真的复制", "toggle", False)}},
    "workflow.route": {
        "title": "按类别和阈值分流", "desc": "按检测/分类结果里把握最大的类别，查场景配置的 routes（类别 → 去向）给每张图一个去向；把握不够的去“review”（人工复核）。只出清单，不搬文件。",
        "inputs": [{"name": "检测结果", "type": "PREDICTIONS", "arg": "predictions"}], "outputs": [],
        "params": {"min_confidence": P("最低置信度", "低于它的一律去人工复核", "number", 0.7, min=0, max=1, step=0.05)}},
    "catalog.extract": {
        "title": "复制选中原图进项目", "desc": "把图片从原图目录复制进本场景的 raw/（标注、训练前必须有这一步；“导入图片文件夹”“用权重找图”已经自带）。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}], "params": {}},
    "labels.push": {
        "title": "送 Label Studio（不带预框）", "desc": "把图片作为空白任务送进本场景连接的 Label Studio 项目，从零画框。要带模型预框请用“送 Label Studio（带模型预框）”。前面要先有“连接 Label Studio”。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}], "outputs": [], "params": {}},
    "labels.import": {
        "title": "导入标注 JSON", "desc": "导入 Label Studio 导出的 JSON 任务（含标注）。JSON 较大时，建议用工作台“标注系统 → 导入标注 JSON”。",
        "inputs": [], "outputs": [],
        "params": {"tasks": P("标注 JSON", "粘贴 Label Studio 导出的 JSON（一个数组）", "text", "", json=True)}},
    "ocr.read": {
        "title": "读取图片文字（OCR）", "desc": "识别图片里的文字，如铭牌、站点编号、标签。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}], "outputs": [],
        "params": {"language": P("语言", "en = 英文，ch = 中英文", "combo", "en", values=["en", "ch"])}},
    "curation.suggest": {
        "title": "生成照片类型候选（零样本）", "desc": "不用训练，直接用 SigLIP2 按场景类别名猜每张图是什么类型，给出前几个候选供复核。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}], "outputs": [],
        "params": {
            "model_dir": P("特征模型", "SigLIP2 模型目录，服务器上已有，选默认即可", "feature_model", ""),
            "batch_size": P("批大小", "16 即可", "number", 16, min=1, max=128, step=1),
            "device": P("设备", "cuda = 显卡，cpu = 处理器", "combo", "cuda", values=["cuda", "cpu"])}},
    "curation.confirm": {
        "title": "确认照片类别", "desc": "把输入的图片**人工确认**为某个照片类别；照片分类器只从人工确认过的照片学习。类别在工作台“类别编辑器”里定义（可一键导入 44 类）。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [{"name": "已确认", "type": "CONFIRMED", "field": "confirmed"}],
        "params": {"category": P("照片类别", "本场景已启用的类别", "category", "")}},
    "curation.train": {
        "title": "训练照片分类器", "desc": "用人工确认过的照片训练一个照片类型分类器（SigLIP2 特征 + 逻辑回归，几秒到几分钟）。至少 2 个类别、每类最好 5 张以上且来自不同站点。",
        "inputs": [{"name": "已确认", "type": "CONFIRMED", "arg": None, "optional": True}],
        "outputs": [{"name": "分类器", "type": "CLASSIFIER", "field": "version"}],
        "params": {
            "version": P("分类器版本名（可不填）", "留空自动按日期时间命名，如 cls-20261112-0930", "text", ""),
            "model_dir": P("特征模型", "SigLIP2 模型目录，服务器上已有，选默认即可", "feature_model", "")}},
    "curation.reclassify": {
        "title": "重新归类照片", "desc": "用训练好的照片分类器给图片重新归类；把握不够的记为“unknown”，留给人工。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"},
                   {"name": "分类器", "type": "CLASSIFIER", "arg": "version", "optional": True}],
        "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "version": P("分类器版本（没连线时用）", "连了“训练照片分类器”就用它刚训练的；否则从已训练的分类器里选", "classifier", ""),
            "min_confidence": P("最低置信度", "把握不到这个分数的记为 unknown；0.7 稳妥，调低归得多但错得也多", "number", 0.7, min=0, max=1, step=0.05)}},
    "catalog.review": {
        "title": "复核单张照片", "desc": "对一张照片下复核结论（一般在工作台图库里点着做，这里给自动化用）。",
        "inputs": [], "outputs": [],
        "params": {
            "image_id": P("图片 ID", "图片内容的 SHA-256", "text", ""),
            "decision": P("复核结论", "accepted 已确认 / rejected 已排除 / pending 待复核", "combo", "accepted", values=["accepted", "rejected", "pending"]),
            "checkpoint": P("检查点（可不填）", "", "text", ""), "site_id": P("站点（可不填）", "", "text", ""), "note": P("备注（可不填）", "", "text", "")}},
    "catalog.near": {
        "title": "查找近重复图片", "desc": "找和某一张图几乎一样的其他图片（感知哈希距离）。",
        "inputs": [], "outputs": [],
        "params": {
            "image_id": P("图片 ID", "图片内容的 SHA-256", "text", ""),
            "max_distance": P("最大哈希距离", "越小越严格，6 左右合适", "number", 6, min=0, max=16, step=1),
            "limit": P("最多返回几张", "", "number", 30, min=1, max=100, step=1)}},
    "curation.find_by_model": {
        "title": "用权重找图", "desc": "选一个现成的 YOLO 权重（如 yolov8n.pt），去扫服务器上的海量原图，把**含目标的照片**挑出来复制进项目（源文件不动），"
                "并生成复核单看框。扫过的会记住，**再运行就接着往下扫**（断点续扫）。",
        "inputs": [{"name": "在…之后（可选）", "type": "MODEL", "arg": None, "optional": True}],
        "outputs": [{"name": "找到的图片", "type": "IMAGES", "field": "image_ids"},
                                  {"name": "检测结果", "type": "PREDICTIONS", "field": "predictions"}],
        "params": {
            "model": P("用本场景的模型（可不填）", "填本场景已注册的模型版本或别名，如 latest（主动学习每轮训练出的最新模型）；填了就不用下面的权重文件", "model", ""),
            "weights": P("权重", "用哪个 YOLO 权重来找，检测模型、分类模型都行。选中节点后，右侧说明里会列出它能识别的类别", "weights"),
            "photo_type": P("只在这类照片里找（可不填）", "只扫“照片类型”页归为这一类的原图，又快又准，如 交流配电/空开；空 = 扫下面的文件夹", "phototype", ""),
            "order": P("扫描顺序", "random = 打乱顺序（找到的图来自更多站点，推荐）；sorted = 按文件名顺序", "combo", "sorted", values=["sorted", "random"]),
            "seed": P("打乱用的随机种子", "换个数字就换一种顺序", "number", 1, min=0, max=100000, step=1),
            "strategy": P("挑图策略", "confident = 挑模型最有把握的（找到够数就停，快）；uncertain = 扫完本次范围再挑模型最拿不准的（置信度接近 0.5，标这些模型进步最快，主动学习推荐）；mixed = 两种交替", "combo", "confident", values=["confident", "uncertain", "mixed"]),
            "per_folder": P("每个文件夹最多几张", "多样性：同一个文件夹（通常是同一站点/同一次巡检）最多挑几张，避免挑一堆相似照片；0 = 不限", "number", 0, min=0, max=1000, step=1),
            "dedupe": P("去掉几乎一样的照片", "勾选 = 感知哈希几乎相同的只留一张", "toggle", True),
            "skip_known": P("跳过已在项目里的图", "勾选 = 已经在本场景（含删除过）的图不算命中，每轮都是新图", "toggle", True),
            "folder": P("在哪个文件夹里找", "服务器上的原图目录（含子文件夹）。默认是全部原图目录", "text", str(deploy.IMAGES_DIR)),
            "labels": P("找什么目标", "从所选权重里读出的类别，可多选；都不勾 = 这个权重能认出的任何目标", "labels", ""),
            "min_confidence": P("最低置信度", "框的把握分数低于它就不算。0.5 稳妥；调低找得多，但误报也多", "number", 0.5, min=0, max=1, step=0.05),
            "min_boxes": P("至少几个框", "一张图里至少检出几个目标才算命中", "number", 1, min=1, max=100, step=1),
            "region": P("只看哪块区域", "full = 整张图，lower_half = 下半部（地面），lower_third = 下三分之一", "combo", "full", values=["full", "lower_half", "lower_third"]),
            "min_coverage": P("最小覆盖率", "目标框至少占所看区域面积的多少才算，如 0.05 = 5%（找“草多”的图）；0 = 不限", "number", 0.0, min=0, max=1, step=0.01),
            "scan_limit": P("本次扫描张数", "这次最多扫多少张原图，没扫完的下次接着扫。小模型扫 5000 张约几分钟", "number", 5000, min=1, max=200000, step=1000),
            "max_hits": P("找到多少张就停", "命中够这么多张就停（最多 1000），够标一轮就行", "number", 200, min=1, max=1000, step=10),
            "imgsz": P("推理尺寸", "把图缩放到这个边长再推理，640 是常用值", "number", 640, min=160, max=2048, step=32),
            "batch": P("批大小", "一次送几张给显卡，16 即可", "number", 16, min=1, max=128, step=1),
            "device": P("设备", "0 = 显卡，cpu = 处理器（很慢）", "combo", "0", values=["0", "cpu"]),
            "restart": P("从头扫", "勾选 = 忘掉之前扫过的记录，从第一张重新扫", "toggle", False)}},
    "curation.pick_type": {
        "title": "按照片类型抽图", "desc": "从原始图片里按照片类型（⑤类型归类的结果：人工确认优先，否则模型预测）随机抽 N 张，**复制**进当前子项目。抽过的不会重复抽。",
        "inputs": [], "outputs": [{"name": "图片", "type": "IMAGES", "field": "image_ids"}],
        "params": {
            "photo_type": P("照片类型", "从“照片类型”页的归类结果里抽（括号里是这类有多少张）；类型不准就先去“照片类型”页确认、重新训练", "phototype", "site_ground"),
            "only_confirmed": P("只抽人工确认过的", "勾选 = 只抽你在⑤页确认过类型的图（更准，但少）；不勾 = 也抽模型预测的", "toggle", False),
            "limit": P("抽多少张", "一次标注建议 30～100 张；最多 1000", "number", 50, min=1, max=1000, step=1),
            "seed": P("随机种子", "换一个数会抽到另一批", "number", 42, min=0, max=99999, step=1)}},
    "labels.ls_setup": {
        "title": "连接 Label Studio", "desc": "给当前子项目在 Label Studio 里建一个标注项目（标注模板由引擎生成），并允许它读取复制进来的原图。**只需第一次运行**，之后自动复用。需要服务器 .env 里配好 LABEL_STUDIO_TOKEN。",
        "inputs": [], "outputs": [],
        "params": {
            "base_url": P("Label Studio 地址", "引擎在服务器上访问的地址", "text", "http://127.0.0.1:8081"),
            "title": P("项目名（可不填）", "不填 = “LOTA · 子项目名”"),
            "token_env": P("令牌环境变量名", "引擎从这个环境变量读 Label Studio 令牌（放在服务器 LOTA/.env）", "text", "LABEL_STUDIO_TOKEN")}},
    "labels.push_prelabel": {
        "title": "送 Label Studio（带模型预框）", "desc": "用模型先把框画好，再把图片送进 Label Studio。打开图片就有框：删错框、补漏框、调边界，最后选 **Targets reviewed**（有目标）或 **No target**（没有目标）再提交。已经送过的图不会重复送。",
        "inputs": [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}],
        "outputs": [],
        "params": {
            "selector": P("预标注用的模型", "模型版本名或别名；production = 当前正式模型", "model", "production"),
            "confidence": P("预框置信度下限", "0.25 预框多（多删）；0.5 预框少（多补）", "number", 0.25, min=0.05, max=0.95, step=0.05),
            "device": P("设备", "0 = GPU", "combo", "0", values=["0", "cpu"])}},
    "labels.pull": {
        "title": "回收 Label Studio 标注", "desc": "把你在 Label Studio 里提交的标注拉回引擎。只有选了 Targets reviewed / No target 的才算已复核，才会进数据集。",
        "inputs": [], "outputs": [{"name": "已标注", "type": "IMAGES", "field": "accepted"}], "params": {}},
    "model.register": {
        "title": "注册模型", "desc": "把一个现成的权重文件登记为模型版本（不训练），比如已有的杂草模型。登记后可以评估、发布、检测。",
        "inputs": [], "outputs": [{"name": "模型", "type": "MODEL", "field": "version"}],
        "params": {
            "version": P("模型版本名", "例如 demo-base"),
            "weights": P("权重文件", "runtime/weights 里的 .pt", "weights"),
            "dataset_version": P("关联数据集（可不填）", "这个权重是用哪个数据集训的")}},
}

# 现成模板
TEMPLATES = {
    "sorting-auto": {
        "name": "自动分拣：扫描原图 → 读取 → 画质检查 → 重复归组 → 照片类型归类",
        "desc": "原工作台“自动分拣”的新版。把场景“图片来源”里的新照片登记进来，标出画质问题、归组重复图、给出照片类型候选，然后到工作台图库里按类型复核。",
        "simple": [["scan", "limit"], ["candidates", "limit"], ["classify", "hints_path"]],
        "nodes": [
            {"id": "scan", "tool": "catalog.scan", "pos": [40, 40], "args": {"limit": 1000, "stability_seconds": 2}},
            {"id": "candidates", "tool": "sorting.candidates", "pos": [560, 40], "args": {"limit": 1000}},
            {"id": "quality", "tool": "sorting.quality", "pos": [1080, 40], "args": {}},
            {"id": "duplicates", "tool": "sorting.duplicates", "pos": [40, 300], "args": {}},
            {"id": "classify", "tool": "sorting.classify", "pos": [560, 300], "args": {"hints_path": str(deploy.LEGACY_PIPELINE / "work/emb/pred.json")}},
        ],
        "links": [["scan", 0, "candidates", 0], ["candidates", 0, "quality", 0], ["quality", 0, "duplicates", 0], ["duplicates", 0, "classify", 0]],
    },
    "find-route": {
        "name": "按目标分流：读取图库 → 检测指定目标（已注册模型）→ 命中的分流到目录",
        "desc": "用本场景已注册的模型检查图库里的图片，含指定目标的复制到 sorting/ 下的子文件夹（默认只预演）。",
        "simple": [["find", "selector"], ["find", "labels"], ["find", "confidence"], ["route", "destination"], ["route", "execute"]],
        "nodes": [
            {"id": "candidates", "tool": "sorting.candidates", "pos": [40, 40], "args": {"limit": 1000}},
            {"id": "find", "tool": "selection.find", "pos": [560, 40], "args": {"selector": "production", "labels": "", "confidence": 0.4}},
            {"id": "route", "tool": "selection.files", "pos": [1080, 40], "args": {"destination": "matched", "execute": False}},
        ],
        "links": [["candidates", 0, "find", 0], ["find", 0, "route", 0]],
    },
    "mcb-start": {
        "name": "空开·开始：从“交流配电/空开”照片里抽 20 张 → 送 Label Studio（从零画框）",
        "project": "mcb",
        "desc": "主动学习的第一步：还没有空开模型，先从⑤归类为“交流配电/空开”的照片里（优先人工确认过的）抽 20 张，送进 Label Studio。"
                "标注时只画一个类别 mcb（空开本体），没有空开的图选 No target。标完 10–15 张后去运行“空开·主动学习一轮”。",
        "simple": [["pick", "limit"], ["pick", "only_confirmed"], ["pick", "seed"]],
        "nodes": [
            {"id": "ls", "tool": "labels.ls_setup", "pos": [40, 40], "args": {"base_url": "http://127.0.0.1:8081", "token_env": "LABEL_STUDIO_TOKEN"}},
            {"id": "autopull", "tool": "labels.ls_webhook", "pos": [560, 40], "args": {"enabled": True, "engine_url": "http://127.0.0.1:8795"}},
            {"id": "pick", "tool": "curation.pick_type", "pos": [40, 300], "args": {"photo_type": "ac_distribution", "only_confirmed": True, "limit": 20, "seed": 42}},
            {"id": "push", "tool": "labels.push", "pos": [560, 300], "args": {}},
        ],
        "links": [["pick", 0, "push", 0]],
    },
    "mcb-loop": {
        "name": "空开·主动学习一轮：回收标注 → 发布 → 训练（接着上一轮）→ 评估 → 用新模型找更多图 → 带预框送 Label Studio",
        "project": "mcb",
        "desc": "在 Label Studio 里标完一批（已提交的）后运行一次：自动编号发布数据集 mcb-r1、r2…，从上一轮模型继续训练（第一轮从 yolov8n 开始），"
                "设为 latest，然后用它在“交流配电/空开”照片里找 30 张新图，带预框送进同一个 Label Studio 项目。训练时你可以继续标注。",
        "simple": [["train", "epochs"], ["find", "strategy"], ["find", "max_hits"], ["find", "per_folder"], ["find", "seed"], ["push", "confidence"]],
        "nodes": [
            {"id": "pull", "tool": "labels.pull", "pos": [40, 40], "args": {}},
            {"id": "publish", "tool": "dataset.publish", "pos": [560, 40], "args": {"version": "mcb-r{n}", "val_fraction": 0.2, "test_fraction": 0.0, "seed": 42}},
            {"id": "train", "tool": "model.train", "pos": [1080, 40],
             "args": {"model_version": "{dataset}-n", "weights": str(deploy.WEIGHTS_DIR / "yolov8n.pt"),
                      "continue_from": "latest", "epochs": 60, "imgsz": 640, "batch": 8, "patience": 20, "device": "0", "workers": 2}},
            {"id": "evaluate", "tool": "model.evaluate", "pos": [40, 460], "args": {"split": "val", "device": "0"}},
            {"id": "latest", "tool": "model.alias", "pos": [560, 460], "args": {"alias": "latest"}},
            {"id": "find", "tool": "curation.find_by_model", "pos": [40, 720],
             "args": {"model": "latest", "photo_type": "ac_distribution", "order": "random", "seed": 1, "skip_known": True,
                      "strategy": "mixed", "per_folder": 3, "dedupe": True,
                      "labels": "mcb", "min_confidence": 0.25, "min_boxes": 1, "region": "full", "min_coverage": 0.0,
                      "scan_limit": 1200, "max_hits": 30, "imgsz": 640, "batch": 16, "device": "0", "restart": False}},
            {"id": "push", "tool": "labels.push_prelabel", "pos": [560, 720], "args": {"selector": "latest", "confidence": 0.25, "device": "0"}},
        ],
        "links": [["pull", 0, "publish", 0], ["publish", 0, "train", 0], ["train", 0, "evaluate", 0], ["publish", 0, "evaluate", 1],
                  ["train", 0, "latest", 0], ["latest", 0, "find", 0], ["find", 0, "push", 0]],
    },
    "inspect-batch": {
        "name": "批量质检：读取图库 → 按规则判定 → 不合格的送 Label Studio 复核",
        "desc": "用“智能质检”页的规则批量审核子项目里的图片；不合格的自动送进 Label Studio（带模型预框）给人复核，质检记录在“智能质检”页查看。",
        "simple": [["candidates", "limit"], ["check", "photo_type"], ["push", "confidence"]],
        "nodes": [
            {"id": "candidates", "tool": "sorting.candidates", "pos": [40, 40], "args": {"limit": 200}},
            {"id": "check", "tool": "inspect.batch", "pos": [560, 40], "args": {"photo_type": "", "pipelines": ""}},
            {"id": "ls", "tool": "labels.ls_setup", "pos": [40, 300], "args": {"base_url": "http://127.0.0.1:8081", "token_env": "LABEL_STUDIO_TOKEN"}},
            {"id": "push", "tool": "labels.push_prelabel", "pos": [1080, 40], "args": {"selector": "production", "confidence": 0.25, "device": "0"}},
        ],
        "links": [["candidates", 0, "check", 0], ["check", 0, "push", 0]],
    },
    "video-label": {
        "name": "视频标注：视频抽帧 → 送 Label Studio（带模型预框）",
        "desc": "把一段巡检视频抽成图片，用当前正式模型预先画框，送进 Label Studio 修正；标完后用“主动学习一轮”或模型中心训练。",
        "simple": [["frames", "video"], ["frames", "every_sec"], ["frames", "max_frames"], ["push", "confidence"]],
        "nodes": [
            {"id": "frames", "tool": "video.frames", "pos": [40, 40], "args": {"video": "", "every_sec": 1.0, "max_frames": 300, "skip_similar": True}},
            {"id": "ls", "tool": "labels.ls_setup", "pos": [40, 300], "args": {"base_url": "http://127.0.0.1:8081", "token_env": "LABEL_STUDIO_TOKEN"}},
            {"id": "push", "tool": "labels.push_prelabel", "pos": [560, 40], "args": {"selector": "production", "confidence": 0.25, "device": "0"}},
        ],
        "links": [["frames", 0, "push", 0]],
    },
    "enhance-label": {
        "name": "增强后标注：导入文件夹 → 图像增强 → 送 Label Studio",
        "desc": "暗光、雾天照片先增强再标注，人看得更清楚；增强副本作为新图加入子项目，原图不动。",
        "simple": [["import", "folder"], ["enhance", "method"], ["enhance", "strength"]],
        "nodes": [
            {"id": "import", "tool": "catalog.import_folder", "pos": [40, 40], "args": {"folder": "", "recursive": True}},
            {"id": "enhance", "tool": "image.enhance", "pos": [560, 40], "args": {"method": "auto", "strength": 0.6, "max_side": 0, "limit": 500}},
            {"id": "ls", "tool": "labels.ls_setup", "pos": [40, 300], "args": {"base_url": "http://127.0.0.1:8081", "token_env": "LABEL_STUDIO_TOKEN"}},
            {"id": "push", "tool": "labels.push", "pos": [1080, 40], "args": {}},
        ],
        "links": [["import", 0, "enhance", 0], ["enhance", 0, "push", 0]],
    },
    "export-phone": {
        "name": "导出到手机：正式模型 → TFLite INT8",
        "desc": "把当前正式使用的模型导出为安卓手机格式，文件在模型中心下载。",
        "simple": [["export", "version"], ["export", "format"], ["export", "imgsz"]],
        "nodes": [{"id": "export", "tool": "model.export", "pos": [40, 40], "args": {"version": "production", "format": "tflite_int8", "imgsz": 640}}],
        "links": [],
    },
}
# 自己业务专用的模板放在私有目录 templates.json（不进仓库），路径可写 {WEIGHTS_DIR} 等占位符
TEMPLATES.update(deploy.private_json("templates.json", {}))


# 工作流 Add Node 菜单的分组和组内顺序（按使用流程排；序号唯一）。新工具记得加进来，没列到的进“⑦ 其他工具”
NODE_GROUPS = [
    ("① 数据导入", ["catalog.scan", "catalog.import_folder", "catalog.import_dicom", "video.frames", "catalog.extract"]),
    ("② 分拣与筛图", ["sorting.candidates", "sorting.quality", "sorting.vlm_check", "sorting.duplicates", "catalog.near", "catalog.review",
                     "catalog.reject", "catalog.delete", "curation.pick_type", "curation.find_by_model", "selection.find",
                     "workflow.route", "selection.files"]),
    ("③ 照片分类", ["curation.suggest", "curation.confirm", "curation.unconfirm", "curation.train", "curation.reclassify",
                   "sorting.classify"]),
    ("④ 标注", ["labels.ls_setup", "labels.ls_guide", "labels.push", "labels.push_prelabel", "labels.pull", "labels.import",
               "labels.import_yolo", "labels.ls_video", "labels.ls_webhook", "quality.mistakes"]),
    ("⑤ 数据集与训练", ["image.enhance", "dataset.publish", "model.train", "model.train_classic", "model.train_semseg",
                      "model.register", "model.evaluate", "model.alias"]),
    ("⑥ 推理、质检与部署", ["model.predict", "judge.coverage", "inspect.batch", "ocr.read", "model.export"]),
]
OTHER_GROUP = "⑦ 其他工具"
NODE_CATEGORY = {t: g for g, ts in NODE_GROUPS for t in ts}


def node_catalog():
    """节点目录：有中文说明的用说明；其他引擎工具也列出来（参数取自 schema）。"""
    out = {}
    for tool_id, tool in TOOLS.items():
        meta = NODES.get(tool_id)
        if meta:
            out[tool_id] = {"id": tool_id, "category": NODE_CATEGORY.get(tool_id, OTHER_GROUP), **meta}
            continue
        schema = tool.model.model_json_schema()
        params = {}
        for k, s in (schema.get("properties") or {}).items():
            if k in ("image_ids", "predictions", "tasks"):
                continue
            kind = {"integer": "number", "number": "number", "boolean": "toggle"}.get(s.get("type"), "text")
            params[k] = P(s.get("title", k), s.get("description", "（暂无中文说明）"), kind, s.get("default"))
        ins = [{"name": "图片", "type": "IMAGES", "arg": "image_ids"}] if "image_ids" in (schema.get("properties") or {}) else []
        out[tool_id] = {"id": tool_id, "title": tool.title, "category": NODE_CATEGORY.get(tool_id, OTHER_GROUP),
                        "desc": "引擎原有工具（参数说明待补充）。", "inputs": ins, "outputs": [], "params": params}
    # 按分组和组内顺序输出：工作流页按这个顺序注册节点，Add Node 菜单就是这个顺序
    rank = {t: i for i, t in enumerate(t for _, ts in NODE_GROUPS for t in ts)}
    return dict(sorted(out.items(), key=lambda kv: rank.get(kv[0], len(rank))))


def find_feature_models(engine):
    """在权重根目录（含 HF 缓存）里找 SigLIP/SigLIP2 模型文件夹，供照片分类器抽特征用"""
    out = []
    for root in engine.weights_roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for cfg in list(root.glob("*/config.json")) + list(root.glob("models--*/snapshots/*/config.json")):
            try:
                kind = json.loads(cfg.read_text(encoding="utf-8")).get("model_type")
            except (OSError, ValueError):
                continue
            if kind in ("siglip", "siglip2"):
                name = cfg.parent.parent.parent.name.replace("models--", "").replace("--", "/") if "snapshots" in cfg.parts else cfg.parent.name
                out.append({"path": str(cfg.parent), "name": name, "type": kind})
    return out


def register_routes(router, engine, directory):
    from fastapi import Body

    @router.get("/nodes", tags=["flow"])
    def nodes():
        return {"nodes": node_catalog(), "templates": TEMPLATES}

    @router.get("/weights", tags=["flow"])
    def weights():
        files = []
        for root in engine.weights_roots:
            if Path(root).is_dir():
                files += [str(p) for p in sorted(Path(root).glob("*.pt"))]
        return {"weights": files}

    @router.get("/projects/{project_id}/images/{image_id}/original", tags=["flow"])
    def original(project_id: str, image_id: str):
        """原图（原始分辨率、原文件），给大图查看器用"""
        from fastapi.responses import FileResponse
        from .catalog import get_image
        scope = directory(project_id)
        path = engine.image_path(scope, get_image(scope, image_id), raw_only=False)
        kind = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                ".bmp": "image/bmp"}.get(Path(path).suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/weights/info", tags=["flow"])
    def weights_info(path: str):
        """权重能识别哪些类别、是检测还是分类"""
        return _weights_names(engine.allowed_weights(path))

    @router.get("/projects/{project_id}/find_progress", tags=["flow"])
    def find_progress(project_id: str, job: str = "", step: str = ""):
        """用权重找图的实时进度（运行中轮询）"""
        folder = directory(project_id) / "find"
        files = sorted(folder.glob("*/progress.json"), key=lambda f: f.stat().st_mtime, reverse=True) if folder.is_dir() else []
        for f in files:
            d = json.loads(f.read_text(encoding="utf-8"))
            if (not job or d.get("job") == job) and (not step or d.get("step") == step):
                return d
        return {}

    @router.get("/feature_models", tags=["flow"])
    def feature_models():
        """照片分类器用的 SigLIP 特征模型（权重目录里找 config.json 的 model_type 是 siglip/siglip2 的文件夹）"""
        return {"models": find_feature_models(engine)}

    @router.get("/projects/{project_id}/classifiers", tags=["flow"])
    def classifiers(project_id: str):
        """已训练的照片分类器版本 + 人工已确认的照片数（按类别）"""
        from collections import Counter
        from .learning import human_labels
        root = directory(project_id)
        folder = root / "classifiers"
        items = []
        for f in sorted(folder.glob("*/classifier.json"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []:
            d = json.loads(f.read_text(encoding="utf-8"))
            items.append({"version": d["version"], "classes": d["classes"],
                          "accuracy": d.get("report", {}).get("validation_accuracy"),
                          "train_count": d.get("report", {}).get("train_count")})
        active = folder / "active.json"
        return {"classifiers": items, "active": json.loads(active.read_text(encoding="utf-8")) if active.is_file() else None,
                "confirmed": dict(Counter(human_labels(root).values()))}

    @router.get("/projects/{project_id}/reviews", tags=["flow"])
    def reviews(project_id: str):
        folder = directory(project_id) / "reviews"
        out = []
        for f in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []:
            if f.name.endswith(".decisions.json"):
                continue
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"review_id": d["review_id"], "created": d["created"], "label": d["label"],
                        "params": d["params"], "total": len(d["items"]),
                        "flagged": sum(1 for i in d["items"] if i["flagged"])})
        return out

    @router.get("/projects/{project_id}/reviews/{review_id}", tags=["flow"])
    def review(project_id: str, review_id: str):
        folder = directory(project_id) / "reviews"
        f = folder / f"{Path(review_id).name}.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        dec = folder / f"{Path(review_id).name}.decisions.json"
        d["decisions"] = json.loads(dec.read_text(encoding="utf-8")) if dec.is_file() else {}
        return d

    @router.post("/projects/{project_id}/reviews/{review_id}/decisions", tags=["flow"])
    def decide(project_id: str, review_id: str, payload: dict = Body(...)):
        """人工确认：{image_id: "many"（超标）| "ok"（不超标）| "unsure" | ""（撤销）}"""
        folder = directory(project_id) / "reviews"
        if not (folder / f"{Path(review_id).name}.json").is_file():
            raise FileNotFoundError("复核单不存在")
        dec_file = folder / f"{Path(review_id).name}.decisions.json"
        dec = json.loads(dec_file.read_text(encoding="utf-8")) if dec_file.is_file() else {}
        for k, v in (payload.get("decisions") or {}).items():
            if v in ("many", "ok", "unsure"):
                dec[k] = {"decision": v, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            elif v == "":
                dec.pop(k, None)
        dec_file.write_text(json.dumps(dec, ensure_ascii=False), encoding="utf-8")
        return {"saved": len(payload.get("decisions") or {}), "total_decided": len(dec)}


# ── 主动学习：版本自动编号、从上一轮模型继续训 ──────────────────────────────────
import re as _re


def _next_name(folder: Path, pattern: str) -> str:
    """版本名里写 {n} 就自动编号：mcb-r{n} → 已有 mcb-r1、mcb-r2 时得到 mcb-r3"""
    if "{n}" not in pattern:
        return pattern
    rx = _re.compile("^" + _re.escape(pattern).replace(_re.escape("{n}"), r"(\d+)") + "$")
    nums = [int(m.group(1)) for p in (folder.iterdir() if folder.is_dir() else []) if (m := rx.match(p.name))]
    return pattern.replace("{n}", str(max(nums, default=0) + 1))


_orig_publish = TOOLS["dataset.publish"]


def _publish_auto(c, version, **kw):
    return _orig_publish.function(c, _next_name(Path(c.directory) / "datasets", version), **kw)


TOOLS["dataset.publish"] = Tool(_orig_publish.title, _orig_publish.model, _publish_auto)

from .tools import Train as _TrainSpec  # noqa: E402


class TrainLoop(_TrainSpec):
    batch: int = Field(default=8, ge=-1, le=512)   # -1 = 自动（按显存的 60% 自动挑批大小，Ultralytics AutoBatch）
    continue_from: str | None = None   # 已注册模型的版本或别名（如 latest）；有就从它继续训，没有就用 weights
    # 数据增强（不填 = Ultralytics 默认）；项目设置页统一管理
    hsv_h: float | None = Field(default=None, ge=0, le=1)
    hsv_s: float | None = Field(default=None, ge=0, le=1)
    hsv_v: float | None = Field(default=None, ge=0, le=1)
    degrees: float | None = Field(default=None, ge=0, le=180)
    translate: float | None = Field(default=None, ge=0, le=1)
    scale: float | None = Field(default=None, ge=0, le=1)
    fliplr: float | None = Field(default=None, ge=0, le=1)
    flipud: float | None = Field(default=None, ge=0, le=1)
    mosaic: float | None = Field(default=None, ge=0, le=1)
    mixup: float | None = Field(default=None, ge=0, le=1)


_orig_train = TOOLS["model.train"]


def _train_loop(c, dataset_version, model_version, weights, continue_from=None, **kw):
    if kw.get("batch") == 0:
        raise ValueError("批大小不能是 0；填正数，或 -1 表示自动")
    model_version = _next_name(Path(c.directory) / "models", model_version.replace("{dataset}", dataset_version))
    if continue_from:
        from .models import resolve_model, train
        try:
            _, path = resolve_model(Path(c.directory), continue_from)
        except (FileNotFoundError, ValueError, KeyError, OSError):
            path = None   # 第一轮还没有上一轮模型
        if path is not None:
            out = train(c.directory, dataset_version, model_version, path, **kw)
            out["continued_from"] = continue_from
            _write_card(c, out, continued_from=continue_from, base=Path(str(path)).parent.name)
            return out
    out = _orig_train.function(c, dataset_version, model_version, weights, **kw)
    out["continued_from"] = None
    _write_card(c, out, continued_from=None, base=Path(weights).name)
    return out


def _write_card(c, out, **kv):
    try:
        from .hub import _card_update
        _card_update(c.directory, out["version"], **kv)
    except Exception:
        pass


TOOLS["model.train"] = Tool(_orig_train.title, TrainLoop, _train_loop)


# 同一个任务里先“连接 Label Studio”再“送/回收”：任务开始时的场景快照里还没有连接，这里读最新配置
def _with_live_ls(tool_id):
    orig = TOOLS[tool_id]

    def run(c, **kw):
        if c.scenario.label_studio is None:
            from .store import Scenario
            cfg = c.engine.store.project(Path(c.directory).name)["config"]
            if cfg.get("label_studio"):
                c.scenario = Scenario.model_validate(cfg)
        return orig.function(c, **kw)
    TOOLS[tool_id] = Tool(orig.title, orig.model, run)


for _t in ("labels.push", "labels.pull"):
    _with_live_ls(_t)


# 确认照片类别：选的是 44 个标准照片类型之一、但场景里还没有 → 自动加进场景类别再确认（不用先去类别编辑器导入）
def _standard_categories():
    f = Path(__file__).parent / "static" / "standard_categories.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


_orig_confirm = TOOLS["curation.confirm"]


def _confirm_auto(c, image_ids, category):
    cats = c.scenario.categories
    if category not in cats or not cats[category].enabled:
        std = _standard_categories()
        if category not in std:
            raise ValueError(f"“{category}”既不是本场景的类别，也不是标准照片类型；请在类别编辑器里先添加")
        from .store import Scenario
        project = Path(c.directory).name
        current = c.engine.store.project(project)
        config = dict(current["config"])
        config["categories"] = {**(config.get("categories") or {}), category: {**std[category], "enabled": True}}
        c.engine.save(project, Scenario.model_validate(config), current["revision"])
        c.scenario = Scenario.model_validate(config)
    return _orig_confirm.function(c, image_ids=image_ids, category=category)


TOOLS["curation.confirm"] = Tool(_orig_confirm.title, _orig_confirm.model, _confirm_auto)


from . import agent  # noqa: E402,F401  注册：批量质检（需要在 TEMPLATES/NODES 定义之后导入）


def _pose_cfg(c):
    """姿态任务建 Label Studio 项目时，先把项目设置里的关键点名称交给 label_config"""
    if c.scenario.task != "pose":
        return None
    from .labels import _KP, label_config, pose_keypoints
    _KP.set(pose_keypoints(c.directory))
    return label_config(list(c.scenario.classes), "pose")
