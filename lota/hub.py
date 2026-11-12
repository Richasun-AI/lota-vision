"""产品级接口：模型中心、数据集、项目设置（统一配置）、模型导出（手机端）、试一试。

页面只用大白话；专业参数都集中在“项目设置”（settings.json），训练向导、工作流默认值都从这里取。
"""
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import Field

from . import config as deploy
from .store import Spec
from .tools import TOOLS, Tool

# 导出等操作不许 Ultralytics 自动装依赖（曾经把引擎环境装坏过）
os.environ.setdefault("YOLO_AUTOINSTALL", "False")

# ── 项目设置（统一配置页）────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "train": {
        "family": "yolo26",        # yolo26 / yolov8
        "size": "s",               # n 极速 / s 均衡 / m 精准
        "preset": "standard",      # quick 30 轮 / standard 80 轮 / thorough 150 轮
        "imgsz": 640, "batch": 8, "device": "0", "workers": 2, "patience": 25,
        "lr0": None, "continue_from_latest": True,
    },
    "presets": {"quick": 30, "standard": 80, "thorough": 150},
    "augment": {"hsv_h": 0.015, "hsv_s": 0.7, "hsv_v": 0.4, "degrees": 0.0, "translate": 0.1, "scale": 0.5,
                "fliplr": 0.5, "flipud": 0.0, "mosaic": 1.0, "mixup": 0.0},
    "split": {"val": 0.2, "test": 0.1, "seed": 42},
    "naming": {"dataset": "{project}-r{n}", "model": "{dataset}-{size}-v{n}"},
    "predict": {"confidence": 0.25, "imgsz": 640},
    "find": {"strategy": "mixed", "per_folder": 3, "max_hits": 30, "min_confidence": 0.25},
    "label_studio": {"public_url": deploy.LS_PUBLIC_URL},
    "export": {"format": "tflite_int8", "imgsz": 640},
    "class_names": {},          # 类别的中英文显示名：{代码: {zh, en}}
    "guide": {"text": ""},      # 标注规范（同步到 Label Studio 的“标注说明”）
    "pose": {"keypoints": []},
    "meta": {"kind": "", "dataset": ""},   # kind=official：官方模型库（不需要标注、训练）  # 姿态任务的关键点名称（顺序固定；含 left/right 或 左/右 的会自动配对做翻转增强）
}
AUGMENT_KEYS = ("hsv_h", "hsv_s", "hsv_v", "degrees", "translate", "scale", "shear", "perspective",
                "flipud", "fliplr", "mosaic", "mixup", "copy_paste", "erasing")


def load_settings(directory):
    f = Path(directory) / "settings.json"
    saved = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    out = {}
    for k, v in DEFAULT_SETTINGS.items():
        out[k] = {**v, **(saved.get(k) or {})} if isinstance(v, dict) else saved.get(k, v)
    return out


def save_settings(directory, value):
    clean = {}
    for k, v in DEFAULT_SETTINGS.items():
        if k in value and isinstance(v, dict) and isinstance(value[k], dict):
            clean[k] = {kk: value[k][kk] for kk in value[k] if kk in v or k in ("presets", "class_names", "pose")}
    f = Path(directory) / "settings.json"
    f.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
    return load_settings(directory)


def base_weights(engine, family, size, task="detect"):
    """标准底座权重：yolo26s.pt / yolov8m.pt / yolo26n-seg.pt…（在 runtime/weights 里）"""
    suffix = {"detect": "", "segment": "-seg", "classify": "-cls", "pose": "-pose", "obb": "-obb"}.get(task, "")
    name = f"{'yolo26' if family == 'yolo26' else 'yolov8'}{size}{suffix}.pt"
    path = Path(engine.weights_roots[0]) / name
    return str(path) if path.is_file() else None


# ── 模型卡片 / 数据集摘要 ─────────────────────────────────────────────────
def model_cards(directory):
    from .models import _read_aliases
    root = Path(directory) / "models"
    aliases = _read_aliases(root) if root.is_dir() else {}
    by_version = {}
    for a, v in aliases.items():
        by_version.setdefault(v, []).append(a)
    cards = []
    for meta in sorted(root.glob("*/model.json")) if root.is_dir() else []:
        d = json.loads(meta.read_text(encoding="utf-8"))
        card = meta.parent / "card.json"
        extra = json.loads(card.read_text(encoding="utf-8")) if card.is_file() else {}
        w = meta.parent / d.get("weights", "weights.pt")
        exports = []
        ex = meta.parent / "exports"
        for f in sorted(ex.iterdir()) if ex.is_dir() else []:
            exports.append({"name": f.name, "size": f.stat().st_size})
        evals = extra.get("evaluations") or []
        cards.append({**{k: d.get(k) for k in ("version", "task", "classes", "dataset_version", "metrics", "created_at", "format")},
                      "training": d.get("training"), "aliases": sorted(by_version.get(d["version"], [])),
                      "size": w.stat().st_size if w.is_file() else None, "exports": exports,
                      "continued_from": extra.get("continued_from"), "base": extra.get("base"),
                      "evaluations": evals, "note": extra.get("note", "")})
    cards.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    return {"models": cards, "aliases": aliases}


def _card_update(directory, version, **kv):
    f = Path(directory) / "models" / version / "card.json"
    d = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    for k, v in kv.items():
        if k == "evaluation":
            d.setdefault("evaluations", []).append(v)
        else:
            d[k] = v
    f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def accepted_count(directory):
    db = Path(directory) / "annotations" / "index.sqlite3"
    if not db.is_file():
        return 0
    with closing(sqlite3.connect(db, timeout=10)) as c:
        try:
            return c.execute("SELECT COUNT(*) FROM current_annotations AS c JOIN revisions AS r USING (task, image_id, revision) "
                             "WHERE r.status='accepted'").fetchone()[0]
        except sqlite3.Error:
            return 0


def dataset_cards(directory):
    out = []
    for f in sorted((Path(directory) / "datasets").glob("*/manifest.json")):
        if f.parent.is_symlink():
            continue
        m = json.loads(f.read_text(encoding="utf-8"))
        imgs = m.get("images") or []
        boxes = sum(len(i.get("labels") or []) for i in imgs)
        out.append({**{k: v for k, v in m.items() if k != "images"}, "image_total": len(imgs), "box_total": boxes,
                    "empty_images": sum(1 for i in imgs if not i.get("labels")),
                    "created": f.stat().st_mtime, "sample_ids": [i["image_id"] for i in imgs[:8]]})
    out.sort(key=lambda d: d["created"], reverse=True)
    return out


# ── 模型导出（手机 / 边缘设备）──────────────────────────────────────────
class Export(Spec):
    version: str = Field(min_length=1)          # 模型版本或别名
    format: Literal["tflite_int8", "tflite", "onnx", "ncnn"] = "tflite_int8"
    imgsz: int = Field(default=640, ge=160, le=1600)


def export_model(c, version, format="tflite_int8", imgsz=640):
    """导出：tflite_int8 = 手机首选（体积约 1/4，安卓 LiteRT）；tflite = 不量化；onnx = 通用；ncnn = 安卓/ARM 备选"""
    from ultralytics import YOLO
    from .models import resolve_model
    meta, weights = resolve_model(Path(c.directory), version)
    v = meta["version"]
    out_dir = Path(c.directory) / "models" / v / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"{v}.pt"
        shutil.copy2(weights, src)
        kw = {"format": "tflite" if format.startswith("tflite") else format, "imgsz": imgsz, "device": "cpu"}
        if format == "tflite_int8":
            kw["quantize"] = "w8a32"   # 动态 INT8：不需要校准数据，体积约 1/4
        produced = Path(YOLO(str(src)).export(**kw))
        name = f"{v}_{format}_{imgsz}"
        if produced.is_dir():
            target = out_dir / (name + ".zip")
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                for f in produced.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(produced.parent))
        else:
            target = out_dir / (name + produced.suffix)
            shutil.copy2(produced, target)
    _card_update(c.directory, v, last_export=target.name)
    return {"version": v, "format": format, "file": target.name, "size": target.stat().st_size}


TOOLS["model.export"] = Tool("导出模型（手机 / 边缘设备）", Export, export_model)


# ── 评估结果记到模型卡片 ─────────────────────────────────────────────────
_orig_eval = TOOLS["model.evaluate"]


def _evaluate_card(c, **kw):
    from .models import resolve_model as _rm
    from .backends_tv import is_tv, evaluate_tv, is_semseg, evaluate_seg
    _meta, _w = _rm(Path(c.directory), kw.get("selector", "production"))
    out = evaluate_tv(c.directory, _meta, _w, kw["dataset_version"], kw.get("split", "val"), kw.get("device", "0")) if is_tv(_meta) \
        else evaluate_seg(c.directory, _meta, _w, kw["dataset_version"], kw.get("split", "val"), kw.get("device", "0")) if is_semseg(_meta) \
        else _orig_eval.function(c, **kw)
    try:
        from .models import resolve_model
        meta, _ = resolve_model(Path(c.directory), kw.get("selector", "production"))
        _card_update(c.directory, meta["version"], evaluation={
            "dataset_version": kw.get("dataset_version"), "split": kw.get("split", "val"),
            "metrics": out.get("metrics") if isinstance(out, dict) else None, "at": time.strftime("%Y-%m-%d %H:%M")})
    except Exception:
        pass
    return out


TOOLS["model.evaluate"] = Tool(_orig_eval.title, _orig_eval.model, _evaluate_card)


# ── 试一试（上传一张图，CPU 推理，不占训练用的显卡）───────────────────────
_TRY_CACHE = {}


def try_model(directory, version, data: bytes, conf=0.25, tile=False):
    from ultralytics import YOLO
    from .models import resolve_model
    meta, weights = resolve_model(Path(directory), version)
    from .backends_tv import is_tv, predict_paths, is_semseg, predict_seg
    if is_semseg(meta):   # 非 YOLO 语义分割：掩膜转成轮廓
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            t0 = time.time()
            p = predict_seg(weights, [tmp], "cpu")[0]
            return {"version": meta["version"], "task": meta["task"], "width": p["width"], "height": p["height"],
                    "ms": round((time.time() - t0) * 1000), "detections": p["detections"], "top": []}
        finally:
            os.unlink(tmp)
    if is_tv(meta):   # 非 YOLO 分类网络
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            t0 = time.time()
            p = predict_paths(weights, [tmp], "cpu")[0]
            return {"version": meta["version"], "task": meta["task"], "width": p["width"], "height": p["height"],
                    "ms": round((time.time() - t0) * 1000), "detections": [], "top": p["classifications"]}
        finally:
            os.unlink(tmp)
    key = (str(weights), weights.stat().st_mtime)
    if key not in _TRY_CACHE:
        _TRY_CACHE.clear()
        _TRY_CACHE[key] = YOLO(str(weights))
    model = _TRY_CACHE[key]
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        t0 = time.time()
        r = model.predict(tmp, conf=conf, device="cpu", verbose=False)[0]
        ms = round((time.time() - t0) * 1000)
        h, w = r.orig_shape
        out = {"version": meta["version"], "task": meta["task"], "width": w, "height": h, "ms": ms, "detections": [], "top": []}
        boxes = r.boxes if r.boxes is not None else getattr(r, "obb", None)
        if boxes is not None:
            polys = r.masks.xy if getattr(r, "masks", None) is not None else \
                [[(float(a), float(b)) for a, b in q] for q in r.obb.xyxyxyxy.cpu().tolist()] if r.boxes is None and getattr(r, "obb", None) is not None else [None] * len(boxes)
            for b, poly in zip(boxes, polys):
                det = {"label": r.names[int(b.cls)], "confidence": round(float(b.conf), 4), "xyxy": [round(float(v), 1) for v in b.xyxy[0]]}
                if poly is not None and len(poly) > 2:
                    det["polygon"] = [[round(float(x), 1), round(float(y), 1)] for x, y in poly]
                out["detections"].append(det)
            kp = getattr(r, "keypoints", None)
            if kp is not None and kp.xy is not None and r.boxes is not None:
                for det, pts in zip(out["detections"], kp.xy.cpu().tolist()):
                    det["keypoints"] = [[round(float(x), 1), round(float(y), 1)] for x, y in pts]
        if tile and r.boxes is not None and getattr(r, "masks", None) is None:   # 切片推理（高清大图里的小目标）
            from .agent import tiled_detect
            out["detections"] = tiled_detect(model, tmp, conf, 640, 1024, out["detections"], "cpu")
            out["tiled"] = True
            out["ms"] = round((time.time() - t0) * 1000)
        if getattr(r, "probs", None) is not None:
            p = r.probs
            out["top"] = [{"label": r.names[int(i)], "confidence": round(float(p.data[int(i)]), 4)} for i in p.top5]
        return out
    finally:
        os.unlink(tmp)


# ── 路由 ─────────────────────────────────────────────────────────────────
def register_routes(router, engine, directory):
    from fastapi import Body, File, Form, UploadFile
    from fastapi.responses import FileResponse

    @router.get("/projects/{project_id}/hub/summary", tags=["hub"])
    def summary(project_id: str):
        d = directory(project_id)
        from .catalog import _database
        with _database(d) as db:
            images = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            by_status = dict(db.execute("SELECT status, COUNT(*) FROM images GROUP BY status").fetchall())
        ds, ms = dataset_cards(d), model_cards(d)
        last_ds = ds[0] if ds else None
        acc = accepted_count(d)
        prod = ms["aliases"].get("production")
        prod_card = next((m for m in ms["models"] if m["version"] == prod), None)
        return {"images": images, "by_status": by_status, "accepted_annotations": acc,
                "datasets": len(ds), "models": len(ms["models"]), "aliases": ms["aliases"],
                "last_dataset": last_ds and {k: last_ds[k] for k in ("version", "image_total", "box_total", "counts", "created")},
                "new_since_last_dataset": max(0, acc - (last_ds["image_total"] if last_ds else 0)),
                "production": prod_card and {k: prod_card[k] for k in ("version", "metrics", "created_at")},
                "settings": load_settings(d)}

    @router.get("/projects/{project_id}/hub/models", tags=["hub"])
    def models(project_id: str):
        return model_cards(directory(project_id))

    @router.get("/projects/{project_id}/hub/datasets", tags=["hub"])
    def datasets(project_id: str):
        d = directory(project_id)
        return {"datasets": dataset_cards(d), "accepted_annotations": accepted_count(d)}

    @router.get("/projects/{project_id}/hub/datasets/{version}/samples", tags=["hub"])
    def samples(project_id: str, version: str, split: str = "", offset: int = 0, limit: int = 24, empty: str = ""):
        f = directory(project_id) / "datasets" / Path(version).name / "manifest.json"
        m = json.loads(f.read_text(encoding="utf-8"))
        imgs = [i for i in m.get("images") or [] if (not split or i.get("split") == split)
                and (empty != "only" or not i.get("labels")) and (empty != "no" or i.get("labels"))]
        return {"total": len(imgs), "classes": m.get("classes"),
                "items": [{k: i.get(k) for k in ("image_id", "split", "labels", "site_id")} for i in imgs[offset:offset + min(limit, 200)]]}

    @router.get("/projects/{project_id}/settings", tags=["hub"])
    def get_settings(project_id: str):
        return {"settings": load_settings(directory(project_id)), "defaults": DEFAULT_SETTINGS}

    @router.put("/projects/{project_id}/settings", tags=["hub"])
    def put_settings(project_id: str, payload: dict = Body(...)):
        return {"settings": save_settings(directory(project_id), payload.get("settings") or payload)}

    @router.get("/projects/{project_id}/hub/base-weights", tags=["hub"])
    def bases(project_id: str, task: str = "detect"):
        out = []
        for fam in ("yolo26", "yolov8"):
            for size in ("n", "s", "m"):
                p = base_weights(engine, fam, size, task)
                out.append({"family": fam, "size": size, "path": p, "ok": bool(p)})
        return out

    @router.post("/projects/{project_id}/models/{version}/try", tags=["hub"])
    async def try_it(project_id: str, version: str, file: UploadFile = File(...), conf: float = Form(0.25), tile: bool = Form(False)):
        data = await file.read()
        if len(data) > 30 * 1024 * 1024:
            raise ValueError("图片太大（超过 30MB）")
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(try_model, directory(project_id), version, data, conf, tile)

    @router.get("/projects/{project_id}/models/{version}/files/{name}", tags=["hub"])
    def model_file(project_id: str, version: str, name: str):
        from .models import resolve_model
        meta, weights = resolve_model(directory(project_id), version)
        if name in ("weights.pt", "weights"):
            return FileResponse(weights, filename=f"{meta['version']}.pt")
        f = directory(project_id) / "models" / meta["version"] / "exports" / Path(name).name
        if not f.is_file():
            raise FileNotFoundError(name)
        return FileResponse(f, filename=f.name)

    @router.put("/projects/{project_id}/models/{version}/note", tags=["hub"])
    def note(project_id: str, version: str, payload: dict = Body(...)):
        _card_update(directory(project_id), Path(version).name, note=str(payload.get("note", ""))[:500])
        return {"ok": True}


# ── 总览：所有子项目的状态 + Label Studio 协同进度 + 下一步建议 ─────────────────────
def ls_status(directory, cfg):
    """Label Studio 项目进度：任务数、已标注数；以及引擎从 LS 回收了多少"""
    if not cfg or not cfg.get("project_id"):
        return None
    out = {"project_id": cfg["project_id"]}
    try:
        from .labels import LabelStudioClient
        cli = LabelStudioClient.from_config(cfg)
        cli.timeout = 4
        p = cli._request("GET", f"/api/projects/{cfg['project_id']}")
        out.update(title=p.get("title"), tasks=p.get("task_number") or 0, annotated=p.get("num_tasks_with_annotations") or 0)
        try:
            hooks = cli._request("GET", "/api/webhooks", query={"project": cfg["project_id"]}) or []
            out["auto_pull"] = any("/api/hooks/label-studio" in str(w.get("url", "")) and w.get("is_active", True) for w in hooks)
        except Exception:
            out["auto_pull"] = None
    except Exception as e:
        out["error"] = str(e)[:160]
    db = Path(directory) / "annotations" / "index.sqlite3"
    if db.is_file():
        with closing(sqlite3.connect(db, timeout=10)) as c:
            try:
                out["pulled"] = c.execute(
                    "SELECT COUNT(DISTINCT c.image_id) FROM current_annotations c JOIN label_studio_links l ON l.image_id=c.image_id "
                    "WHERE l.project_id=?", (cfg["project_id"],)).fetchone()[0]
            except sqlite3.Error:
                out["pulled"] = 0
    return out


def suggestions(pid, cfg, s, ls, pipes, ms):
    """按规则给出“下一步该做什么”（最重要的在前）"""
    tips = []
    add = lambda level, text, href, label: tips.append({"level": level, "text": text, "href": href, "label": label})
    if not ls:
        add("info", "还没有连接 Label Studio：标注要在 Label Studio 里做", f"/flow?project={pid}", "去工作流连接")
    elif ls.get("annotated", 0) > ls.get("pulled", 0):
        add("warn", f"Label Studio 里有 {ls['annotated'] - ls.get('pulled', 0)} 张已标注、还没回收", f"/datasets?project={pid}", "回收并打包")
    elif ls.get("tasks", 0) > ls.get("annotated", 0):
        add("info", f"Label Studio 里还有 {ls['tasks'] - ls['annotated']} 张没标", cfg.get("public", "") or "", "去标注")
    if s["new_since_last_dataset"] > 0 and s["datasets"]:
        add("warn", f"比最新数据集多了 {s['new_since_last_dataset']} 张标注，可以打包新版本再训练", f"/models?project={pid}", "训练新模型")
    if s["accepted_annotations"] and not s["datasets"]:
        add("warn", f"已有 {s['accepted_annotations']} 张标注，还没打包数据集", f"/datasets?project={pid}", "打包数据集")
    if s["datasets"] and not s["models"]:
        add("warn", "有数据集了，还没训练模型", f"/models?project={pid}", "训练第一个模型")
    models = ms["models"]
    prod = next((m for m in models if "production" in m["aliases"]), None)
    score = lambda m: (m.get("metrics") or {}).get("metrics/mAP50(B)")
    best = max((m for m in models if score(m) is not None), key=score, default=None)
    if models and not prod:
        add("warn", "还没有“正式使用”的模型：预标注、质检默认用它", f"/models?project={pid}", "去设置")
    elif prod and best and best is not prod and score(prod) is not None and score(best) - score(prod) > 0.02:
        add("ok", f"{best['version']} 比正式模型高 {(score(best) - score(prod)) * 100:.0f} 分，可以考虑设为正式使用（先在同一测试集上评估确认）", f"/models?project={pid}", "去对比")
    if prod and not pipes:
        add("info", "还没有质检规则：配好后就能自动审核照片", f"/inspect?project={pid}", "配置质检规则")
    if prod and not any(m["exports"] for m in models):
        add("info", "正式模型还没导出到手机", f"/models?project={pid}", "导出")
    if not tips:
        add("ok", "一切就绪：可以继续标注、找更多图，或用智能质检审核新照片", f"/inspect?project={pid}", "智能质检")
    return tips


def register_overview(router, engine, directory):
    @router.get("/overview", tags=["hub"])
    def overview():
        from .agent import load_pipelines, recent_inspections
        out = []
        for p in engine.store.projects():
            pid, cfg = p["id"], p["config"]
            d = directory(pid)
            from .catalog import _database
            with _database(d) as db:
                images = db.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            ds, ms = dataset_cards(d), model_cards(d)
            acc = accepted_count(d)
            s = {"images": images, "accepted_annotations": acc, "datasets": len(ds), "models": len(ms["models"]),
                 "new_since_last_dataset": max(0, acc - (ds[0]["image_total"] if ds else 0))}
            ls = ls_status(d, cfg.get("label_studio"))
            st = load_settings(d)
            if ls and ls.get("project_id"):
                ls["url"] = f"{st['label_studio']['public_url'].rstrip('/')}/projects/{ls['project_id']}/data"
            pipes = load_pipelines(d)
            insp = {}
            for r in recent_inspections(d, 2000):
                insp[r["verdict"]] = insp.get(r["verdict"], 0) + 1
            prod = next((m for m in ms["models"] if "production" in m["aliases"]), None)
            out.append({"id": pid, "name": cfg.get("name"), "task": cfg.get("task"), "classes": cfg.get("classes"), "active": bool(p.get("active")),
                        **s, "ls": ls, "pipelines": len(pipes), "inspections": insp,
                        "production": prod and {"version": prod["version"], "metrics": prod.get("metrics")},
                        "latest": ms["aliases"].get("latest"),
                        "kind": st.get("meta", {}).get("kind", ""),
                        "suggestions": [{"level": "ok", "text": f"官方预训练模型（{st['meta'].get('dataset')}），共 {len(ms['models'])} 个；可直接试一试、导出到手机、用在质检规则里，或在别的子项目训练时当底座",
                                         "href": f"/models?project={pid}", "label": "看模型"}]
                        if st.get("meta", {}).get("kind") == "official" else suggestions(pid, {"public": ls.get("url") if ls else ""}, s, ls, pipes, ms)})
        return out


# ── 与 Label Studio 实时协同：提交/修改标注 → webhook → 引擎自动回收 ──────────────────
class LsWebhook(Spec):
    enabled: bool = True
    engine_url: str = "http://127.0.0.1:8795"   # Label Studio 访问引擎用的地址（同一台服务器用 127.0.0.1）


def ls_webhook(c, enabled=True, engine_url="http://127.0.0.1:8795"):
    """在 Label Studio 里给本子项目的标注项目登记（或取消）webhook：一提交/修改标注，引擎就自动回收"""
    from .labels import LabelStudioClient
    cfg = c.engine.store.project(Path(c.directory).name)["config"].get("label_studio")
    if not cfg or not cfg.get("project_id"):
        raise ValueError("这个子项目还没连接 Label Studio：先运行“连接 Label Studio”")
    cli = LabelStudioClient.from_config(cfg)
    pid = Path(c.directory).name
    url = f"{engine_url.rstrip('/')}/api/hooks/label-studio?project={pid}"
    existing = [w for w in (cli._request("GET", "/api/webhooks", query={"project": cfg["project_id"]}) or [])
                if str(w.get("url", "")).startswith(engine_url.rstrip("/") + "/api/hooks/label-studio")]
    for w in existing:
        cli._request("DELETE", f"/api/webhooks/{w['id']}")
    if not enabled:
        return {"enabled": False, "removed": len(existing)}
    token = os.environ.get("LOTA_API_TOKEN", "")
    hook = cli._request("POST", "/api/webhooks", payload={
        "project": cfg["project_id"], "url": url, "send_payload": False, "send_for_all_actions": False, "is_active": True,
        "headers": {"Authorization": f"Bearer {token}"} if token else {},
        "actions": ["ANNOTATION_CREATED", "ANNOTATION_UPDATED", "ANNOTATIONS_DELETED"]})
    return {"enabled": True, "webhook_id": hook.get("id"), "url": url, "ls_project": cfg["project_id"]}


TOOLS["labels.ls_webhook"] = Tool("Label Studio 自动回收（webhook）", LsWebhook, ls_webhook)


def register_hooks(router, engine, directory):
    from fastapi import Body

    @router.post("/hooks/label-studio", tags=["hub"])
    def ls_hook(project: str, payload: dict = Body(default_factory=dict)):
        """Label Studio 回调：标注有变化就排一个“回收标注”任务（已有排队/运行中的就不重复排）"""
        directory(project)
        busy = [j for j in engine.store.jobs(project) if j["kind"] == "labels.pull" and j["status"] in ("queued", "running")] \
            if hasattr(engine.store, "jobs") else []
        if busy:
            return {"queued": False, "reason": "已有回收任务在排队"}
        try:
            job = engine.enqueue_action(project, "labels.pull", {})
        except ValueError as e:
            if "Start the scenario" not in str(e):
                raise
            engine.store.activate(project, True)
            job = engine.enqueue_action(project, "labels.pull", {})
        return {"queued": True, "job_id": job["id"], "action": payload.get("action")}


# ── 训练实时进度：读最新一次训练的 results.csv（Ultralytics 每训完一轮写一行）────────────
def train_progress(directory, version=""):
    import csv
    runs = Path(directory) / "runs"
    cands = [p for p in runs.glob(f"{version or '*'}/*/train/results.csv") if "_evaluations" not in p.parts] if runs.is_dir() else []
    if not cands:
        return {}
    f = max(cands, key=lambda p: p.stat().st_mtime)
    rows = list(csv.DictReader(open(f, encoding="utf-8")))
    req = f.parent.parent / "request.json"
    total = None
    if req.is_file():
        try:
            r = json.loads(req.read_text(encoding="utf-8"))
            total = (r.get("parameters") or r.get("options") or r).get("epochs")
        except ValueError:
            pass
    pick = lambda row, *keys: next((float(row[k]) for k in keys if row.get(k) not in (None, "")), None)
    pts = [{"epoch": int(float(r["epoch"])), "map50": pick(r, "metrics/mAP50(B)", "metrics/accuracy_top1"),
            "precision": pick(r, "metrics/precision(B)"), "recall": pick(r, "metrics/recall(B)")} for r in rows if r.get("epoch")]
    return {"version": f.parent.parent.parent.name, "epochs_total": total, "epochs_done": len(pts), "points": pts,
            "updated": f.stat().st_mtime, "running": time.time() - f.stat().st_mtime < 600}


def register_progress(router, engine, directory):
    @router.get("/projects/{project_id}/train-progress", tags=["hub"])
    def progress(project_id: str, version: str = ""):
        return train_progress(directory(project_id), Path(version).name if version else "")


# ── 训练图表：混淆矩阵、PR 曲线、训练曲线、验证集预测（Ultralytics 训练时自动生成）────────────
PLOT_NAMES = {
    "results.png": "训练曲线（每一轮的损失和成绩）",
    "confusion_matrix_normalized.png": "混淆矩阵（哪些类别容易认错，比例）",
    "confusion_matrix.png": "混淆矩阵（数量）",
    "BoxPR_curve.png": "PR 曲线（框）", "MaskPR_curve.png": "PR 曲线（轮廓）", "PR_curve.png": "PR 曲线",
    "BoxF1_curve.png": "F1 曲线（找最合适的置信度阈值）", "F1_curve.png": "F1 曲线",
    "val_batch0_pred.jpg": "验证集：模型预测", "val_batch0_labels.jpg": "验证集：人工标注",
    "val_batch1_pred.jpg": "验证集：模型预测 2", "val_batch1_labels.jpg": "验证集：人工标注 2",
    "labels.jpg": "标注分布（框的位置、大小）",
}


def _run_dir(directory, version):
    from .models import resolve_model
    meta, _ = resolve_model(Path(directory), version)
    run = (meta.get("training") or {}).get("run")
    d = Path(directory) / run / "train" if run else None
    if not d or not d.is_dir():
        cands = sorted((Path(directory) / "runs" / meta["version"]).glob("*/train"), key=lambda p: p.stat().st_mtime, reverse=True)
        d = cands[0] if cands else None
    return meta, d


def register_plots(router, engine, directory):
    from fastapi.responses import FileResponse

    @router.get("/projects/{project_id}/models/{version}/plots", tags=["hub"])
    def plots(project_id: str, version: str):
        meta, d = _run_dir(directory(project_id), version)
        files = [{"name": n, "title": t} for n, t in PLOT_NAMES.items() if d and (d / n).is_file()]
        per = {}
        metrics = dict(meta.get("metrics") or {})
        if not any(k.startswith("class/") for k in metrics):   # 老模型：用最近一次评估的按类别成绩
            card = directory(project_id) / "models" / meta["version"] / "card.json"
            evs = json.loads(card.read_text(encoding="utf-8")).get("evaluations", []) if card.is_file() else []
            for ev in reversed(evs):
                if any(k.startswith("class/") for k in (ev.get("metrics") or {})):
                    metrics = ev["metrics"]
                    break
        for k, v in metrics.items():
            if k.startswith("class/"):
                _, name, key = k.split("/", 2)
                per.setdefault(name, {})[key] = v
        return {"version": meta["version"], "plots": files,
                "per_class": [{"class": k, **v} for k, v in sorted(per.items(), key=lambda kv: kv[1].get("ap50", 0))]}

    @router.get("/projects/{project_id}/models/{version}/plots/{name}", tags=["hub"])
    def plot_file(project_id: str, version: str, name: str):
        if name not in PLOT_NAMES:
            raise FileNotFoundError(name)
        _, d = _run_dir(directory(project_id), version)
        if not d or not (d / name).is_file():
            raise FileNotFoundError(name)
        return FileResponse(d / name)


# ── 标注规范：写在项目设置里，一键同步成 Label Studio 的“标注说明”（每次打开任务都会显示）──────────
def default_guide(name, task, classes, class_names=None):
    cn = class_names or {}
    cls = "；".join(f"{c}（{(cn.get(c) or {}).get('zh', c)}）" for c in classes)
    if task == "classify":
        body = [f"整张图判断属于哪个类别：{cls}。",
                "选好类别后，再选 Category reviewed 确认；看不清、拿不准的选 Unsure（不会进训练）。",
                "只看照片主体，不要被角落里的小东西影响判断。"]
    else:
        how = {"segment": "用多边形沿目标边缘描一圈，点不要太稀（拐角处多点几个）",
               "obb": "用可旋转的框，框的方向跟着目标走（先画框，再拖旋转手柄）"}.get(task, "画矩形框，框要贴着目标的可见边缘，不留大片空白、也不切掉目标")
        body = [f"类别：{cls}。",
                f"画法：{how}。",
                "每张图先看完整：有目标 → 全部标完后选 Targets reviewed；没有目标 → 直接选 No target；看不清 → 选 Unsure（不会进训练）。",
                "一个目标一个标注；被遮挡的只标可见部分，遮挡超过 80% 的不标。",
                "成片、没有明确边界的目标（如杂草、锈蚀、污渍）：按“连成一片的区域标一个”，不要拆成很多碎块，也不要把相隔很远的两片标在一起；同一个人、不同时候都要按同一个标准。",
                "拿不准的在评论里写原因，方便统一标准。"]
    return f"{name} · 标注规范\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(body))


def guide_html(text):
    from html import escape
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return ""
    head, rest = lines[0], lines[1:]
    items = "".join(f"<li>{escape(l.split('. ', 1)[1] if l[:1].isdigit() and '. ' in l else l)}</li>" for l in rest)
    return f"<h3>{escape(head)}</h3><ol>{items}</ol>"


class LsGuide(Spec):
    text: str = ""          # 空 = 用项目设置里的标注规范


def ls_guide(c, text=""):
    """把标注规范写进 Label Studio 项目的“标注说明”，并设为每次打开任务都显示"""
    from .labels import LabelStudioClient
    cfg = c.engine.store.project(Path(c.directory).name)["config"]
    ls = cfg.get("label_studio")
    if not ls or not ls.get("project_id"):
        raise ValueError("这个子项目还没连接 Label Studio：先运行“连接 Label Studio”")
    st = load_settings(c.directory)
    text = text or st.get("guide", {}).get("text") or default_guide(cfg.get("name", ""), cfg.get("task", "detect"), cfg.get("classes", []), st.get("class_names"))
    cli = LabelStudioClient.from_config(ls)
    cli._request("PATCH", f"/api/projects/{ls['project_id']}", payload={"expert_instruction": guide_html(text), "show_instruction": True})
    return {"ls_project": ls["project_id"], "lines": len([l for l in text.splitlines() if l.strip()])}


TOOLS["labels.ls_guide"] = Tool("同步标注规范到 Label Studio", LsGuide, ls_guide)


def register_guide(router, engine, directory):
    @router.get("/projects/{project_id}/guide-template", tags=["hub"])
    def guide_template(project_id: str):
        d = directory(project_id)
        cfg = engine.store.project(project_id)["config"]
        st = load_settings(d)
        return {"text": default_guide(cfg.get("name", ""), cfg.get("task", "detect"), cfg.get("classes", []), st.get("class_names"))}


# ── 数据集下载（标准 YOLO / 分类目录 zip）、外部模型导入 ─────────────────────────────
def register_io(router, engine, directory):
    from fastapi import File, Form, UploadFile
    from fastapi.responses import FileResponse

    @router.get("/projects/{project_id}/datasets/{version}/download", tags=["hub"])
    def dataset_zip(project_id: str, version: str):
        """把数据集版本打包成 zip（检测/分割/旋转框/姿态 = images/ + labels/ + data.yaml；分类 = split/类别/图片），可直接给 Ultralytics 训练"""
        d = directory(project_id) / "datasets" / Path(version).name
        if not (d / "manifest.json").is_file():
            raise FileNotFoundError(version)
        cache = Path(directory(project_id)) / "exports"
        cache.mkdir(exist_ok=True)
        out = cache / f"{d.name}.zip"
        if not out.is_file() or out.stat().st_mtime < (d / "manifest.json").stat().st_mtime:
            tmp = out.with_suffix(".zip.part")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:     # 图片已压缩，不再压
                for f in sorted(d.rglob("*")):
                    if f.name == "data.yaml" and f.parent == d:   # 去掉服务器绝对路径：解压到哪都能直接 yolo train data=.../data.yaml
                        y = json.loads(f.read_text(encoding="utf-8"))
                        y.pop("path", None)
                        z.writestr(f"{d.name}/data.yaml", json.dumps(y, ensure_ascii=False, indent=2) + chr(10))
                    elif f.is_file() and f.name != "stats.json":
                        z.write(f, f"{d.name}/{f.relative_to(d)}")
            tmp.replace(out)
        return FileResponse(out, filename=out.name, media_type="application/zip")

    @router.post("/projects/{project_id}/models/import", tags=["hub"])
    async def import_model(project_id: str, file: UploadFile = File(...), version: str = Form(...), note: str = Form("")):
        """上传别处训练好的 Ultralytics .pt，登记成本子项目的一个模型版本（类别必须和子项目一致）"""
        import re
        from starlette.concurrency import run_in_threadpool
        from .models import register
        d = directory(project_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", version):
            raise ValueError("版本名只能用字母、数字、点、下划线、短横线")
        if not (file.filename or "").lower().endswith(".pt"):
            raise ValueError("只支持 Ultralytics 的 .pt 权重")
        cfg = engine.store.project(project_id)["config"]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "upload.pt"
            with open(f, "wb") as w:
                while chunk := await file.read(8 * 1024 * 1024):
                    w.write(chunk)

            def check_and_register():
                from ultralytics import YOLO
                m = YOLO(str(f))
                names = [m.names[i] for i in sorted(m.names)] if isinstance(m.names, dict) else list(m.names)
                task = {"detect": "detect", "segment": "segment", "classify": "classify", "obb": "obb", "pose": "pose"}.get(m.task, m.task)
                if task != cfg.get("task"):
                    raise ValueError(f"模型是“{task}”任务，子项目是“{cfg.get('task')}”，对不上")
                want = sorted(cfg["classes"]) if task == "classify" else list(cfg["classes"])
                if names != want:
                    raise ValueError(f"模型类别 {names} 和子项目类别 {want} 不一致（顺序也要一致）")
                meta = register(d, version, f, classes=names, task=task)
                _card_update(d, version, base=f"外部导入：{file.filename}", note=note[:300])
                return meta
            meta = await run_in_threadpool(check_and_register)
        return {"version": meta["version"], "task": meta["task"], "classes": meta["classes"]}


# ── 系统状态：显卡、磁盘、正在运行的任务 ─────────────────────────────────────────
_STARTED = time.time()


def system_status(engine):
    import shutil
    import subprocess
    gpu = []
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
                                       "--format=csv,noheader,nounits"], text=True, timeout=5)
        for line in out.strip().splitlines():
            n, mu, mt, u, t = [x.strip() for x in line.split(",")]
            gpu.append({"name": n, "mem_used": int(mu), "mem_total": int(mt), "util": int(u), "temp": int(t)})
    except Exception:
        pass
    disks = []
    for label, p in (("引擎数据", engine.store.root if hasattr(engine.store, "root") else Path(engine.weights_roots[0]).parent), ("共享图片盘", deploy.DATA_ROOT)):
        try:
            u = shutil.disk_usage(p)
            disks.append({"label": label, "path": str(p), "used": u.used, "total": u.total, "free": u.free})
        except Exception:
            pass
    jobs = []
    for p in engine.store.projects():
        for j in engine.store.jobs(p["id"], limit=20):
            if j["status"] in ("queued", "running"):
                jobs.append({"project": p["id"], "id": j["id"], "kind": j["kind"], "status": j["status"], "step": j.get("current_step")})
    return {"gpu": gpu, "disks": disks, "jobs": jobs, "uptime_min": round((time.time() - _STARTED) / 60), "time": time.strftime("%Y-%m-%d %H:%M:%S")}


def register_system(router, engine, directory):
    @router.get("/system", tags=["hub"])
    def system():
        return system_status(engine)


# ── 数据集统计图表（参考 Ultralytics Platform 数据集的“图表”页）───────────────────────
def _hist(values, bins=12, lo=None, hi=None):
    if not values:
        return {"edges": [], "counts": []}
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    if hi <= lo:
        hi = lo + 1
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, max(0, int((v - lo) / step)))] += 1
    return {"edges": [round(lo + i * step, 2) for i in range(bins + 1)], "counts": counts}


def _grid(points, n=20, xr=(0, 1), yr=(0, 1)):
    g = [[0] * n for _ in range(n)]
    for x, y in points:
        i = min(n - 1, max(0, int((x - xr[0]) / ((xr[1] - xr[0]) or 1) * n)))
        j = min(n - 1, max(0, int((y - yr[0]) / ((yr[1] - yr[0]) or 1) * n)))
        g[j][i] += 1
    return g


def dataset_stats(directory, version):
    """统计：划分、类别、图像尺寸/文件大小/格式、二维尺寸热力图、标注中心热力图、框尺寸、每图对象数、每个实例的点数。
    数据集版本是不可变的，结果缓存在数据集目录 stats.json。"""
    from PIL import Image
    d = Path(directory) / "datasets" / Path(version).name
    cache = d / "stats.json"
    if cache.is_file() and cache.stat().st_mtime >= (d / "manifest.json").stat().st_mtime:
        old = json.loads(cache.read_text(encoding="utf-8"))
        if old.get("schema") == 2:
            return old
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    task, imgs = man["task"], man.get("images") or []
    split, classes, fmts = {}, {}, {}
    ws, hs, sizes, centers, bw, bh, per_img, pts = [], [], [], [], [], [], [], []
    labeled = 0
    for it in imgs:
        split[it["split"]] = split.get(it["split"], 0) + 1
        f = d / it["path"]
        try:
            with Image.open(f) as im:
                w, h = im.size
                fmt = (im.format or f.suffix[1:]).upper()
        except Exception:
            continue
        ws.append(w); hs.append(h); sizes.append(f.stat().st_size / 1024)
        fmts[fmt] = fmts.get(fmt, 0) + 1
        labels = it.get("labels") or []
        if task == "classify":
            labels = [[x] for x in labels]
        if labels:
            labeled += 1
        per_img.append(len(labels))
        for lab in labels:
            classes[lab[0]] = classes.get(lab[0], 0) + 1
            nums = [float(x) for x in lab[1:]]
            if task == "classify" or not nums:
                continue
            if task in ("detect", "pose") or len(nums) == 4:
                cx, cy, nw, nh = nums[:4]
                if task == "pose":
                    pts.append(sum(1 for i in range(4, len(nums), 3) if i + 2 < len(nums) and nums[i + 2] > 0))
            else:   # 分割 / 旋转框：多边形 → 外接框
                xs, ys = nums[0::2], nums[1::2]
                cx, cy, nw, nh = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs), max(ys) - min(ys)
                pts.append(len(xs))
            centers.append((cx, cy)); bw.append(nw * w); bh.append(nh * h)
    total = sum(classes.values())
    top = sorted(classes.items(), key=lambda kv: -kv[1])
    lo_wh, hi_wh = (min(ws + hs), max(ws + hs)) if ws else (0, 1)
    lo_b, hi_b = (min(bw + bh), max(bw + bh)) if bw else (0, 1)
    out = {
        "schema": 2, "version": version, "task": task, "images": len(imgs), "labeled": labeled, "annotations": total,
        "split": split, "classes": [{"name": k, "count": v} for k, v in top], "class_total": len(man.get("classes") or []),
        "dims": {"width": _hist(ws, 10, lo_wh, hi_wh), "height": _hist(hs, 10, lo_wh, hi_wh),
                 "mean_w": round(sum(ws) / len(ws), 1) if ws else 0, "mean_h": round(sum(hs) / len(hs), 1) if hs else 0},
        "filesize": {**_hist(sizes, 10), "mean_kb": round(sum(sizes) / len(sizes), 1) if sizes else 0},
        "dims2d": {"grid": _grid(list(zip(ws, hs)), 20, (0, max(ws or [1]) * 1.05), (0, max(hs or [1]) * 1.05)),
                   "max_w": round(max(ws or [1]) * 1.05), "max_h": round(max(hs or [1]) * 1.05),
                   "mean_ar": round(sum(w / h for w, h in zip(ws, hs)) / len(ws), 2) if ws else 0},
        "centers": _grid(centers, 20),
        "formats": fmts,
        "bbox": {"width": _hist(bw, 10, lo_b, hi_b), "height": _hist(bh, 10, lo_b, hi_b),
                 "mean_w": round(sum(bw) / len(bw), 1) if bw else 0, "mean_h": round(sum(bh) / len(bh), 1) if bh else 0},
        "objects": {**_hist(per_img, -(-(top_n := max(per_img or [0]) + 1) // (step := -(-top_n // 12))), 0, -(-top_n // step) * step),
                    "mean": round(sum(per_img) / len(per_img), 1) if per_img else 0},
        "points": {**_hist(pts, 10), "mean": round(sum(pts) / len(pts), 1) if pts else 0} if pts else None,
    }
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def register_stats(router, engine, directory):
    @router.get("/projects/{project_id}/datasets/{version}/stats", tags=["hub"])
    def stats(project_id: str, version: str):
        return dataset_stats(directory(project_id), version)


# ── 试一试：视频（断点续传上传 → 按间隔抽帧 → 逐帧识别，结果流式返回，分析也能断点续跑）──────
_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".ts"}
_UPLOAD_MAX = 4 * 1024 ** 3
_CHUNK_MAX = 16 * 1024 * 1024


def _uploads(directory):
    d = Path(directory) / ".uploads"
    d.mkdir(exist_ok=True)
    return d


def _upload_paths(directory, uid):
    import re
    if not re.fullmatch(r"[0-9a-f]{16,40}", uid or ""):
        raise ValueError("上传编号不对")
    d = _uploads(directory)
    return d / f"{uid}.part", d / f"{uid}.json"


def _upload_cleanup(directory, keep=10, max_age_h=48):
    """只保留最近 10 个上传，超过 48 小时没动过的删掉"""
    metas = sorted(_uploads(directory).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for i, m in enumerate(metas):
        part = m.with_suffix(".part")
        last = max(m.stat().st_mtime, part.stat().st_mtime if part.is_file() else 0)
        if i >= keep or time.time() - last > max_age_h * 3600:
            part.unlink(missing_ok=True)
            m.unlink(missing_ok=True)


def upload_status(directory, uid):
    part, meta = _upload_paths(directory, uid)
    if not meta.is_file():
        raise FileNotFoundError("没有这个上传（可能已过期，请重新上传）")
    m = json.loads(meta.read_text(encoding="utf-8"))
    got = part.stat().st_size if part.is_file() else 0
    return {"id": uid, "name": m["name"], "size": m["size"], "received": got, "complete": got == m["size"]}


def _video_file(directory, uid):
    s = upload_status(directory, uid)
    if not s["complete"]:
        raise ValueError(f"视频还没传完（{s['received']} / {s['size']} 字节）")
    part, _ = _upload_paths(directory, uid)
    os.utime(part)
    return part


def try_video_stream(directory, version, video_path, conf=0.25, every_sec=1.0, max_frames=120, tile=False, max_side=1280, key=None):
    """逐帧产出 dict：meta → frame × N → done。
    帧图和结果存到 <子项目>/.try-video/<key>/：同一个视频、同一个模型、同样参数再跑时，已分析的帧直接重放，
    从断开的地方接着分析（网络断了重连不用从头来）。只保留最近 6 次。"""
    import cv2
    import hashlib
    import uuid
    from .models import resolve_model
    meta_m, weights = resolve_model(Path(directory), version)
    root = Path(directory) / ".try-video"
    root.mkdir(exist_ok=True)
    every_sec = max(0.1, float(every_sec))
    sid = hashlib.sha1(json.dumps([key, meta_m["version"], weights.stat().st_mtime, round(float(conf), 3), every_sec, bool(tile), max_side]).encode()).hexdigest()[:16] \
        if key else uuid.uuid4().hex[:16]
    out_dir = root / sid
    out_dir.mkdir(exist_ok=True)
    os.utime(out_dir)
    for old in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)[:-6]:
        shutil.rmtree(old, ignore_errors=True)
    log = out_dir / "results.ndjson"
    done_before, cached = False, []
    if log.is_file():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                break        # 最后一行可能写了一半
            if o.get("type") == "frame" and (out_dir / f"{o['i']}.jpg").is_file():
                cached.append(o)
            elif o.get("type") == "done":
                done_before = True
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            yield {"type": "error", "detail": "打不开这个视频（格式不支持或文件损坏）"}
            return
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        if not (0 < fps < 1000):
            fps = 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        duration = total / fps if total > 0 else 0
        step = max(1, round(every_sec * fps))
        planned = min(int(max_frames), (total // step + 1) if total else int(max_frames))
        cached = cached[:planned]
        yield {"type": "meta", "id": sid, "fps": round(fps, 2), "duration": round(duration, 2), "frames": total, "planned": planned,
               "every_sec": every_sec, "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
               "resumed": len(cached)}
        for o in cached:
            yield {**o, "cached": True}
        n = len(cached)
        if n >= planned or (done_before and n):
            yield {"type": "done", "analysed": n, "id": sid, "from_cache": True}
            return
        start = cached[-1]["src"] + step if cached else 0
        with open(log, "w", encoding="utf-8") as w:     # 重写一遍：去掉可能写了一半的最后一行
            for o in cached:
                w.write(json.dumps(o, ensure_ascii=False) + "\n")
            idx = 0
            while idx < start:          # 跳过已分析过的部分：只 grab 不解码，很快
                if not cap.grab():
                    break
                idx += 1
            while n < planned:
                if not cap.grab():
                    break
                if idx % step == 0:
                    ok, frame = cap.retrieve()
                    if ok:
                        hh, ww = frame.shape[:2]
                        s = min(1.0, max_side / max(hh, ww))
                        if s < 1:
                            frame = cv2.resize(frame, (round(ww * s), round(hh * s)), interpolation=cv2.INTER_AREA)
                        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
                        data = buf.tobytes()
                        (out_dir / f"{n}.jpg").write_bytes(data)
                        r = try_model(directory, version, data, conf, tile)
                        o = {"type": "frame", "i": n, "src": idx, "t": round(idx / fps, 2), "width": r["width"], "height": r["height"], "ms": r["ms"],
                             "detections": r["detections"], "top": r["top"][:3], "scale": round(s, 4)}
                        w.write(json.dumps(o, ensure_ascii=False) + "\n")
                        w.flush()
                        yield o
                        n += 1
                idx += 1
            w.write(json.dumps({"type": "done", "analysed": n}) + "\n")
        yield {"type": "done", "analysed": n, "id": sid}
    except Exception as e:   # 流已经开始，不能再改状态码，用一行 error 告诉页面
        yield {"type": "error", "detail": str(e)}
    finally:
        cap.release()


def register_try_video(router, engine, directory):
    from fastapi import Body, File, Form, HTTPException, Request, UploadFile
    from fastapi.responses import FileResponse, StreamingResponse

    # 断点续传：① 建立/查询上传（同一个文件指纹得到同一个编号）② 按偏移量一块一块 PUT ③ 传完后用编号分析
    @router.post("/projects/{project_id}/uploads", tags=["hub"])
    def upload_open(project_id: str, payload: dict = Body(...)):
        """payload: {name, size, fingerprint}。fingerprint 建议用 文件名|大小|修改时间；返回 {id, received, complete}"""
        import hashlib
        name, size = Path(str(payload.get("name") or "video.mp4")).name, int(payload.get("size") or 0)
        if Path(name).suffix.lower() not in _VIDEO_EXT:
            raise ValueError(f"不支持的视频格式 {Path(name).suffix}；支持 {', '.join(sorted(_VIDEO_EXT))}")
        if not 0 < size <= _UPLOAD_MAX:
            raise ValueError("视频大小不对（不能为空，最大 4GB）")
        d = directory(project_id)
        uid = hashlib.sha1(f"{project_id}|{payload.get('fingerprint') or ''}|{name}|{size}".encode()).hexdigest()[:20]
        part, meta = _upload_paths(d, uid)
        if not meta.is_file():
            _upload_cleanup(d)
            meta.write_text(json.dumps({"name": name, "size": size, "created": time.time()}, ensure_ascii=False), encoding="utf-8")
        os.utime(meta)
        return upload_status(d, uid)

    @router.get("/projects/{project_id}/uploads/{uid}", tags=["hub"])
    def upload_get(project_id: str, uid: str):
        return upload_status(directory(project_id), uid)

    @router.put("/projects/{project_id}/uploads/{uid}", tags=["hub"])
    async def upload_chunk(project_id: str, uid: str, request: Request, offset: int = 0):
        """原始字节放在请求体里，offset = 这一块在文件里的起点。offset 和服务器已收到的不一致时返回 409 + 已收到多少，客户端从那里接着传"""
        d = directory(project_id)
        s = upload_status(d, uid)
        part, _ = _upload_paths(d, uid)
        if offset != s["received"]:
            raise HTTPException(409, {"message": "偏移量不一致，请从 received 处继续", "received": s["received"]})
        n = 0
        with open(part, "ab") as f:
            async for chunk in request.stream():
                n += len(chunk)
                if n > _CHUNK_MAX or s["received"] + n > s["size"]:
                    f.truncate(s["received"])    # 这一块作废，已收到的部分不受影响
                    raise ValueError("这一块太大或超过了文件大小")
                f.write(chunk)
        return upload_status(d, uid)

    @router.post("/projects/{project_id}/models/{version}/try-video", tags=["hub"])
    async def try_video(project_id: str, version: str, file: UploadFile | None = File(None), upload_id: str = Form(""),
                        conf: float = Form(0.25), every_sec: float = Form(1.0), max_frames: int = Form(120), tile: bool = Form(False)):
        """视频识别，返回 NDJSON 流（每行一个 JSON：meta / frame / done / error）。
        两种给视频的方式：upload_id（先用 /uploads 断点续传，推荐，大文件、网络不稳时用）；或直接 file（小文件）。
        用 upload_id 时，同样的参数再请求一次会重放已分析的帧并从断开处继续。"""
        d = directory(project_id)
        max_frames = max(1, min(int(max_frames), 600))
        tmp_name, key = None, None
        if upload_id:
            path, key = _video_file(d, upload_id), upload_id
        elif file is not None:
            suffix = Path(file.filename or "v.mp4").suffix.lower()
            if suffix not in _VIDEO_EXT:
                raise ValueError(f"不支持的视频格式 {suffix}；支持 {', '.join(sorted(_VIDEO_EXT))}")
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            size = 0
            try:
                while chunk := await file.read(8 * 1024 * 1024):
                    size += len(chunk)
                    if size > _UPLOAD_MAX:
                        raise ValueError("视频太大（超过 4GB）")
                    tmp.write(chunk)
            except Exception:
                tmp.close()
                os.unlink(tmp.name)
                raise
            tmp.close()
            path = tmp_name = tmp.name
        else:
            raise ValueError("要么给 upload_id，要么上传 file")

        def gen():
            try:
                for item in try_video_stream(d, version, path, conf, every_sec, max_frames, tile, key=key):
                    yield json.dumps(item, ensure_ascii=False) + "\n"
            finally:
                if tmp_name:
                    os.unlink(tmp_name)
        return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    @router.get("/projects/{project_id}/try-video/{sid}/{i}.jpg", tags=["hub"])
    def try_video_frame(project_id: str, sid: str, i: int):
        f = directory(project_id) / ".try-video" / Path(sid).name / f"{int(i)}.jpg"
        if not f.is_file():
            raise FileNotFoundError("这一帧已过期（只保留最近 6 次视频试用）")
        return FileResponse(f, media_type="image/jpeg")
