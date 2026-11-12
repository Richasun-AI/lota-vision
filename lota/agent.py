"""Agent / MCP 接口 + 检测管线（流式质检）。

借鉴 Ultralytics Agents 的积木式管线：图片输入 → YOLO 模型 → 条件（数量 / 置信度 / 覆盖率）→ 视觉大模型（可选，
只在条件命中时才调用）→ 输出（合格 / 不合格 + 原因 + 框，记入质检历史）。
给 agent、MCP、流式下载器、手机 App 调用；所有能力都能用 HTTP 调到。
"""
import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import config as deploy
from .flow import REGION_FRAC, TEMPLATES, NODES

OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")

# ── 检测管线（质检规则）────────────────────────────────────────────────────
EXAMPLE_PIPELINES = {
    "mcb": [{
        "id": "mcb-present", "name": "空开照片检查", "name_en": "Breaker photo check", "enabled": True,
        "match": ["ac_distribution", "mcb", "空开", "配电"],
        "model": "production", "conf": 0.25, "imgsz": 640,
        "rules": [{"label": "mcb", "metric": "count", "min_conf": 0.4, "op": "==", "value": 0,
                   "verdict": "warn", "message": "照片里没看到空开，可能拍错了", "message_en": "No breaker visible; photo may be wrong"}],
        "vlm": {"enabled": False, "model": "qwen2.5vl:7b", "prompt": "请判断照片里的空开是否有跳闸、烧蚀或破损，一句中文一句英文。"},
    }],
}
EXAMPLE_PIPELINES.update(deploy.private_json("pipelines.json", {}))   # 自己业务专用的管线（私有目录，不进仓库）
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
METRICS = {"count": "目标数量", "coverage": "覆盖率", "max_conf": "最高置信度", "top1": "分类结果"}


def pipelines_path(directory):
    return Path(directory) / "pipelines.json"


def load_pipelines(directory):
    f = pipelines_path(directory)
    if f.is_file():
        return json.loads(f.read_text(encoding="utf-8"))
    return EXAMPLE_PIPELINES.get(Path(directory).name, [])


def save_pipelines(directory, items):
    if not isinstance(items, list):
        raise ValueError("pipelines 必须是列表")
    for p in items:
        if not p.get("id") or not p.get("rules"):
            raise ValueError("每条管线都要有 id 和至少一条规则")
        for r in p["rules"]:
            if r.get("metric") not in METRICS or r.get("op") not in OPS:
                raise ValueError(f"规则写法不对：metric 只能是 {list(METRICS)}，op 只能是 {list(OPS)}")
    pipelines_path(directory).write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    return items


# ── 推理（模型缓存；显卡出错自动改用 CPU）───────────────────────────────────
_MODELS, _LOCK = {}, threading.Lock()


def _model(directory, selector):
    from ultralytics import YOLO
    from .models import resolve_model
    from .backends_tv import is_tv, is_semseg
    meta, weights = resolve_model(Path(directory), selector)
    if is_tv(meta):          # 非 YOLO 分类网络：返回权重路径，推理时分派
        return meta, ("tv", weights)
    if is_semseg(meta):      # 非 YOLO 语义分割
        return meta, ("seg", weights)
    key = (str(weights), weights.stat().st_mtime)
    with _LOCK:
        if key not in _MODELS:
            if len(_MODELS) > 6:
                _MODELS.clear()
            _MODELS[key] = YOLO(str(weights))
    return meta, _MODELS[key]


def _predict(model, path, conf, imgsz):
    for device in ("0", "cpu"):
        try:
            return model.predict(path, conf=conf, imgsz=imgsz, device=device, verbose=False)[0], device
        except Exception:
            if device == "cpu":
                raise
    raise RuntimeError("推理失败")


def tiled_detect(model, path, conf, imgsz=640, tile=1024, base=None, device="0", overlap=0.2):
    """切片推理（SAHI 思路）：高清大图切成互相重叠的小块分别检测，坐标换回原图，加上整图结果，按类别 NMS 去重。
    远处的小目标（锈点、裂纹、铁丝网破口）在整图缩到 640 时会看不清，切片后能检出。"""
    import cv2
    import torch
    import torchvision
    img = cv2.imread(str(path))
    if img is None:
        return list(base or [])
    H, W = img.shape[:2]
    if max(H, W) <= tile:
        return list(base or [])
    step = max(1, int(tile * (1 - overlap)))
    offs = sorted({(min(x, max(0, W - tile)), min(y, max(0, H - tile)))
                   for y in range(0, max(1, H - tile + step), step) for x in range(0, max(1, W - tile + step), step)})
    dets = list(base or [])
    for i in range(0, len(offs), 8):
        chunk = offs[i:i + 8]
        crops = [img[y0:y0 + tile, x0:x0 + tile] for x0, y0 in chunk]
        rs = None
        for dev in (device, "cpu"):
            try:
                rs = model.predict(crops, conf=conf, imgsz=imgsz, device=dev, verbose=False)
                break
            except Exception:
                if dev == "cpu":
                    raise
        for r, (ox, oy) in zip(rs, chunk):
            for b in (r.boxes or []):
                x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
                dets.append({"label": r.names[int(b.cls)], "confidence": round(float(b.conf), 4),
                             "xyxy": [round(x1 + ox, 1), round(y1 + oy, 1), round(x2 + ox, 1), round(y2 + oy, 1)]})
    out = []
    for lab in {d["label"] for d in dets}:
        ds = [d for d in dets if d["label"] == lab]
        keep = torchvision.ops.nms(torch.tensor([d["xyxy"] for d in ds], dtype=torch.float32),
                                   torch.tensor([d["confidence"] for d in ds], dtype=torch.float32), 0.5)
        out += [ds[k] for k in keep.tolist()]
    return out


def _norm(x):
    return re.sub(r"[\s\-_]+", "_", str(x or "").strip().lower())


def _metric(rule, dets, top, w, h):
    lab, mc = rule.get("label"), float(rule.get("min_conf", 0))
    sel = [d for d in dets if (not lab or _norm(d["label"]) == _norm(lab)) and d["confidence"] >= mc]
    m = rule["metric"]
    if m == "count":
        return len(sel)
    if m == "max_conf":
        return max((d["confidence"] for d in dets if not lab or _norm(d["label"]) == _norm(lab)), default=0.0)
    if m == "top1":
        return top[0]["label"] if top else ""
    frac = REGION_FRAC.get(rule.get("region", "full"), 1.0)
    y0 = h * (1 - frac)
    if sel and any(d.get("polygon") for d in sel):   # 分割模型：用真实轮廓面积（并集）算覆盖率，比框准；没轮廓的用框
        import cv2
        import numpy as np
        k = 512 / max(w, h)
        canvas = np.zeros((max(1, int(h * k)), max(1, int(w * k))), np.uint8)
        for d in sel:
            if d.get("polygon"):
                cv2.fillPoly(canvas, [np.array([[x * k, y * k] for x, y in d["polygon"]], np.int32)], 1)
            else:
                x1, y1, x2, y2 = (int(v * k) for v in d["xyxy"])
                canvas[y1:y2, x1:x2] = 1
        region = canvas[int(y0 * k):, :]
        return round(float(region.mean()) if region.size else 0.0, 4)
    area = 0.0
    for d in sel:
        x1, y1, x2, y2 = d["xyxy"]
        y1 = max(y1, y0)
        if y2 > y1 and x2 > x1:
            area += (x2 - x1) * (y2 - y1)
    return round(min(1.0, area / max(1.0, w * h * frac)), 4)


def _vlm(image_path, cfg):
    # 原图（如 3060×4080）直接送会让 Ollama 报 500：先缩到长边 1024
    import io
    from PIL import Image, ImageOps
    with Image.open(image_path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
    data = base64.b64encode(buf.getvalue()).decode()
    body = json.dumps({"model": cfg.get("model", "qwen2.5vl:7b"), "stream": False,
                       "messages": [{"role": "user", "content": cfg.get("prompt", "描述这张图"), "images": [data]}]}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["message"]["content"].strip()


def _matches(p, photo_type):
    m = [x.lower() for x in p.get("match") or []]
    return not m or (photo_type and any(x in photo_type.lower() for x in m))


def inspect_image(directory, image_path, photo_type="", only=None, meta=None, save=True):
    """按检测管线检查一张图；返回 {verdict: pass|warn|fail|skip, findings[], detections…}"""
    t0 = time.time()
    pipes = [p for p in load_pipelines(directory) if p.get("enabled", True) and (not only or p["id"] in only)]
    pipes = [p for p in pipes if only or _matches(p, photo_type)]
    results, rank = [], {"pass": 0, "skip": 0, "warn": 1, "fail": 2}
    for p in pipes:
        info, model = _model(directory, p.get("model", "production"))
        seg_dets = None
        if isinstance(model, tuple) and model[0] == "seg":   # 非 YOLO 语义分割：掩膜轮廓当作检测结果
            from .backends_tv import predict_seg
            pr = predict_seg(model[1], [str(image_path)], "0")[0]
            r, device, h, w, seg_dets = None, "seg", pr["height"], pr["width"], pr["detections"]
        elif isinstance(model, tuple) and model[0] == "tv":   # 非 YOLO 分类网络
            from .backends_tv import predict_paths
            pr = predict_paths(model[1], [str(image_path)], "0")[0]
            r, device, h, w = None, "tv", pr["height"], pr["width"]
        else:
            # 尺寸没写就用模型训练时的尺寸（官方旋转框模型是 1024，用 640 会漏掉小目标）
            imgsz = int(p.get("imgsz") or (getattr(model, "overrides", {}) or {}).get("imgsz") or 640)
            r, device = _predict(model, str(image_path), float(p.get("conf", 0.25)), imgsz)
            h, w = r.orig_shape
        polys = r.masks.xy if r is not None and getattr(r, "masks", None) is not None else [None] * len(r.boxes or []) if r is not None else []
        dets = []
        obb = getattr(r, "obb", None) if r is not None else None
        if r is not None and r.boxes is None and obb is not None:   # 旋转框模型
            polys = [[(float(a), float(b)) for a, b in q] for q in obb.xyxyxyxy.cpu().tolist()]
        for b, poly in zip(((r.boxes if r.boxes is not None else obb) or []) if r is not None else [], polys):
            det = {"label": r.names[int(b.cls)], "confidence": round(float(b.conf), 4), "xyxy": [round(float(v), 1) for v in b.xyxy[0]]}
            if poly is not None and len(poly) > 2:
                det["polygon"] = [[round(float(x), 1), round(float(y), 1)] for x, y in poly]
            dets.append(det)
        kp = getattr(r, "keypoints", None) if r is not None else None
        if kp is not None and kp.xy is not None:
            for det, pts in zip(dets, kp.xy.cpu().tolist()):
                det["keypoints"] = [[round(float(x), 1), round(float(y), 1)] for x, y in pts]
        top = [{"label": r.names[int(i)], "confidence": round(float(r.probs.data[int(i)]), 4)} for i in r.probs.top5] \
            if r is not None and getattr(r, "probs", None) is not None else \
            ([{"label": x["label"], "confidence": round(x["confidence"], 4)} for x in pr.get("classifications", [])] if r is None else [])
        if seg_dets is not None:
            dets = seg_dets
        if p.get("tile") and r is not None and r.boxes is not None and getattr(r, "masks", None) is None:   # 切片推理
            dets = tiled_detect(model, str(image_path), float(p.get("conf", 0.25)), int(p.get("imgsz", 640)),
                                int(p.get("tile_size", 1024)), dets, "0")
            device += "+切片"
        findings, verdict = [], "pass"
        for rule in p["rules"]:
            value = _metric(rule, dets, top, w, h)
            target = rule.get("value")
            hit = OPS[rule["op"]](value, target) if not isinstance(value, str) else OPS[rule["op"]](value, str(target))
            if hit:
                v = rule.get("verdict", "fail")
                verdict = v if rank[v] > rank[verdict] else verdict
            findings.append({"label": rule.get("label"), "metric": rule["metric"], "value": value, "op": rule["op"], "target": target,
                             "hit": hit, "verdict": rule.get("verdict", "fail") if hit else "pass",
                             "message": rule.get("message", "") if hit else "", "message_en": rule.get("message_en", "") if hit else ""})
        note = None
        vcfg = p.get("vlm") or {}
        if vcfg.get("enabled") and verdict != "pass":   # 像 Ultralytics Agents 一样：条件命中才调大模型，省时间
            try:
                note = _vlm(image_path, vcfg)
            except Exception as e:
                note = f"（视觉大模型没回应：{e}）"
        results.append({"pipeline": p["id"], "name": p.get("name"), "model": info["version"], "device": device, "verdict": verdict,
                        "findings": findings, "vlm": note, "width": w, "height": h, "detections": dets, "top": top})
    overall = max((x["verdict"] for x in results), key=lambda v: rank[v], default="skip")
    out = {"verdict": overall, "photo_type": photo_type, "results": results, "ms": round((time.time() - t0) * 1000),
           "meta": meta or {}, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if save:
        out["id"] = _record(directory, image_path, out)
    return out


def _record(directory, image_path, out):
    folder = Path(directory) / "inspections"
    (folder / "images").mkdir(parents=True, exist_ok=True)
    data = Path(image_path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    target = folder / "images" / (sha + ".jpg")
    if not target.exists():
        target.write_bytes(data)
    rid = time.strftime("%Y%m%d%H%M%S") + "-" + sha[:10]
    with open(folder / (time.strftime("%Y%m%d") + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({**out, "id": rid, "sha": sha}, ensure_ascii=False) + "\n")
    return rid


def recent_inspections(directory, limit=100, verdict=""):
    folder = Path(directory) / "inspections"
    rows = []
    for f in sorted(folder.glob("*.jsonl"), reverse=True) if folder.is_dir() else []:
        for line in reversed(f.read_text(encoding="utf-8").splitlines()):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if verdict and r.get("verdict") != verdict:
                continue
            rows.append(r)
            if len(rows) >= limit:
                return rows
    return rows


# ── 模板 → 工作流（服务器端，给 agent 直接运行模板用）─────────────────────────
def template_to_workflow(name, overrides=None):
    t = TEMPLATES.get(name)
    if not t:
        raise FileNotFoundError(f"没有模板 {name}；可用：{list(TEMPLATES)}")
    nodes = {n["id"]: n for n in t["nodes"]}
    deps = {k: [] for k in nodes}
    refs = {k: {} for k in nodes}
    for a, ao, b, bi in t["links"]:
        deps[b].append(a)
        out = NODES[nodes[a]["tool"]]["outputs"][ao]
        inp = NODES[nodes[b]["tool"]]["inputs"][bi]
        if inp.get("arg"):
            refs[b][inp["arg"]] = {"$ref": f"{a}.{out['field']}"}
    order, seen = [], set()

    def visit(k, path=()):
        if k in seen:
            return
        if k in path:
            raise ValueError("模板连线有环")
        for d in deps[k]:
            visit(d, path + (k,))
        seen.add(k)
        order.append(k)
    for k in sorted(nodes, key=lambda k: (nodes[k]["pos"][0], nodes[k]["pos"][1])):
        visit(k)
    steps = []
    for k in order:
        args = {kk: v for kk, v in nodes[k].get("args", {}).items() if v not in ("", None)}
        args.update((overrides or {}).get(k, {}))
        args.update(refs[k])
        steps.append({"id": k, "tool": nodes[k]["tool"], "args": args})
    return {"name": t["name"][:100], "steps": steps}


# ── 路由 ─────────────────────────────────────────────────────────────────
def register_routes(router, engine, directory):
    from fastapi import Body, File, Form, UploadFile
    from fastapi.responses import FileResponse
    from .store import Workflow
    from .tools import TOOLS

    def enqueue(fn, project, *a):
        try:
            return fn(project, *a)
        except ValueError as e:   # 子项目没启用：自动启用再提交
            if "Start the scenario" not in str(e):
                raise
            engine.store.activate(project, True)
            return fn(project, *a)

    def wait_job(project, job_id, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = engine.store.job(project, job_id)
            if j["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
                return j
            time.sleep(1.0)
        return engine.store.job(project, job_id)

    @router.get("/agent/manifest", tags=["agent"])
    def manifest():
        """给 agent/LLM 的能力清单：子项目、模型别名、工具（含参数说明）、模板、检测管线"""
        from .hub import model_cards
        projects = []
        for p in engine.store.projects():
            d = directory(p["id"])
            ms = model_cards(d)
            projects.append({"id": p["id"], "name": p["config"].get("name"), "task": p["config"].get("task"),
                             "classes": p["config"].get("classes"), "active": bool(p.get("active")),
                             "aliases": ms["aliases"], "models": [m["version"] for m in ms["models"]][:10],
                             "pipelines": [x["id"] for x in load_pipelines(d)]})
        tools = []
        for tid, t in TOOLS.items():
            n = NODES.get(tid, {})
            tools.append({"id": tid, "title": t.title, "description": n.get("desc", "").replace("**", ""),
                          "params": {k: {"label": v["label"], "help": v["help"], "default": v.get("default")} for k, v in (n.get("params") or {}).items()},
                          "schema": t.model.model_json_schema()})
        return {"engine": "LOTA", "projects": projects, "tools": tools,
                "templates": {k: {"name": v["name"], "desc": v.get("desc", ""), "project": v.get("project"),
                                  "nodes": [{"id": n["id"], "tool": n["tool"], "args": n.get("args", {})} for n in v["nodes"]]}
                              for k, v in TEMPLATES.items()},
                "endpoints": {"run_tool": "POST /api/agent/run", "run_template": "POST /api/agent/template",
                              "inspect": "POST /api/agent/inspect (JSON) | POST /api/agent/inspect/upload (multipart)",
                              "job": "GET /api/agent/jobs/{project}/{job_id}"}}

    @router.post("/agent/run", tags=["agent"])
    def agent_run(payload: dict = Body(...)):
        """{project, tool, args, wait=true, timeout=900} → 等待完成并返回结果（或只返回任务 ID）"""
        project, tool = payload["project"], payload["tool"]
        job = enqueue(engine.enqueue_action, project, tool, payload.get("args") or {})
        if not payload.get("wait", True):
            return {"job_id": job["id"], "status": job["status"]}
        j = wait_job(project, job["id"], float(payload.get("timeout", 900)))
        return {"job_id": j["id"], "status": j["status"], "error": j.get("error"), "result": (j.get("outputs") or {}).get("result")}

    @router.post("/agent/template", tags=["agent"])
    def agent_template(payload: dict = Body(...)):
        """{project, template, overrides: {节点ID: {参数: 值}}, wait=false} → 保存为工作流并运行"""
        project, name = payload["project"], payload["template"]
        wf = template_to_workflow(name, payload.get("overrides"))
        wid = (payload.get("workflow_id") or name + "-agent")[:64]
        engine.save_workflow(project, wid, Workflow.model_validate(wf))
        job = enqueue(engine.enqueue_workflow, project, wid, {})
        if not payload.get("wait", False):
            return {"job_id": job["id"], "workflow": wid, "status": job["status"]}
        j = wait_job(project, job["id"], float(payload.get("timeout", 3600)))
        return {"job_id": j["id"], "status": j["status"], "error": j.get("error"), "outputs": j.get("outputs")}

    @router.get("/agent/jobs/{project_id}/{job_id}", tags=["agent"])
    def agent_job(project_id: str, job_id: str):
        j = engine.store.job(project_id, job_id)
        return {k: j.get(k) for k in ("id", "kind", "status", "current_step", "error", "outputs")}

    @router.get("/projects/{project_id}/pipelines", tags=["agent"])
    def get_pipes(project_id: str):
        return {"pipelines": load_pipelines(directory(project_id)), "metrics": METRICS, "ops": list(OPS),
                "examples": EXAMPLE_PIPELINES}

    @router.put("/projects/{project_id}/pipelines", tags=["agent"])
    def put_pipes(project_id: str, payload: dict = Body(...)):
        return {"pipelines": save_pipelines(directory(project_id), payload.get("pipelines"))}

    def _inspect_bytes(project, data, photo_type, only, meta, save):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            return inspect_image(directory(project), tmp, photo_type or "", only, meta, save)
        finally:
            os.unlink(tmp)

    @router.post("/agent/inspect", tags=["agent"])
    def agent_inspect(payload: dict = Body(...)):
        """流式质检：{project, image_base64 | path | url, photo_type, pipelines?, meta?, save=true}"""
        project = payload["project"]
        if payload.get("image_base64"):
            data = base64.b64decode(payload["image_base64"].split(",")[-1])
        elif payload.get("path"):
            data = engine.allowed_source(payload["path"]).read_bytes()
        elif payload.get("url"):
            with urllib.request.urlopen(payload["url"], timeout=60) as r:
                data = r.read(40 * 1024 * 1024)
        else:
            raise ValueError("需要 image_base64、path 或 url 其中之一")
        return _inspect_bytes(project, data, payload.get("photo_type"), payload.get("pipelines"), payload.get("meta"), payload.get("save", True))

    @router.post("/agent/inspect/upload", tags=["agent"])
    async def agent_inspect_upload(project: str = Form(...), file: UploadFile = File(...), photo_type: str = Form(""),
                                   pipelines: str = Form(""), meta: str = Form("")):
        data = await file.read()
        only = [x for x in pipelines.split(",") if x] or None
        m = json.loads(meta) if meta else {"filename": file.filename}
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_inspect_bytes, project, data, photo_type, only, m, True)

    @router.get("/projects/{project_id}/inspections", tags=["agent"])
    def inspections(project_id: str, limit: int = 100, verdict: str = ""):
        rows = recent_inspections(directory(project_id), min(limit, 1000), verdict)
        stats = {}
        for r in recent_inspections(directory(project_id), 5000):
            stats[r["verdict"]] = stats.get(r["verdict"], 0) + 1
        return {"items": rows, "stats": stats}

    @router.get("/projects/{project_id}/inspections/image/{sha}", tags=["agent"])
    def inspection_image(project_id: str, sha: str):
        f = directory(project_id) / "inspections" / "images" / (Path(sha).name + ".jpg")
        if not f.is_file():
            raise FileNotFoundError(sha)
        return FileResponse(f, media_type="image/jpeg")


# ── 工作流节点：批量质检（上游图片 → 按检测管线判定 → 不合格 / 需注意的图分两路输出）────────────
from pydantic import Field as _Field  # noqa: E402
from .store import Spec as _Spec  # noqa: E402
from .tools import TOOLS as _TOOLS, Tool as _Tool  # noqa: E402


class InspectBatch(_Spec):
    image_ids: list[str] = _Field(min_length=1, max_length=1000)
    photo_type: str = ""          # 空 = 用每张图自己的检查点
    pipelines: str = ""           # 逗号分隔的规则 ID；空 = 按照片类型自动匹配


def inspect_batch(c, image_ids, photo_type="", pipelines=""):
    from .catalog import get_image
    only = [x.strip() for x in pipelines.split(",") if x.strip()] or None
    out = {"total": 0, "pass": 0, "warn": 0, "fail": 0, "skip": 0, "fail_ids": [], "warn_ids": [], "items": []}
    for key in dict.fromkeys(image_ids):
        rec = get_image(c.directory, key)
        path = c.engine.image_path(c.directory, rec, False)
        r = inspect_image(c.directory, path, photo_type or rec.get("checkpoint") or "", only,
                          {"image_id": key, "file": Path(path).name, "source": "workflow"}, True)
        out["total"] += 1
        out[r["verdict"]] = out.get(r["verdict"], 0) + 1
        if r["verdict"] in ("fail", "warn"):
            out[r["verdict"] + "_ids"].append(key)
        out["items"].append({"image_id": key, "verdict": r["verdict"],
                             "why": [f["message"] for x in r["results"] for f in x["findings"] if f["hit"]]})
    return out


_TOOLS["inspect.batch"] = _Tool("批量质检", InspectBatch, inspect_batch)


# ── 全子项目质检 + 工单 / 站点审核报告 ─────────────────────────────────────────
def inspect_all(engine, directory, image_path, photo_type="", meta=None, save=True):
    """一张照片交给所有启用的子项目里“适用这类照片”的质检规则；返回总结论 + 各子项目结果"""
    rank = {"skip": 0, "pass": 1, "warn": 2, "fail": 3}
    per, overall, t0 = [], "skip", time.time()
    for p in engine.store.projects():
        if not p.get("active"):
            continue
        d = directory(p["id"])
        # 全子项目模式只用写明了“适用照片”的规则（空 = 所有照片，在这里会误伤别的照片类型）
        pipes = [x for x in load_pipelines(d) if x.get("enabled", True) and x.get("match") and _matches(x, photo_type)]
        if not pipes:
            continue
        try:
            r = inspect_image(d, image_path, photo_type, [x["id"] for x in pipes], {**(meta or {}), "project": p["id"]}, save)
        except Exception as e:      # 某个子项目还没有模型等：记下原因，不影响别的
            r = {"verdict": "skip", "results": [], "error": str(e)[:200]}
        per.append({"project": p["id"], "name": p["config"].get("name"), **r})
        overall = r["verdict"] if rank[r["verdict"]] > rank[overall] else overall
    return {"verdict": overall, "photo_type": photo_type, "projects": per, "meta": meta or {}, "ms": round((time.time() - t0) * 1000)}


def build_report(engine, directory, order="", site="", limit=5000):
    """把各子项目的质检记录按工单 / 站点汇总：总结论、各照片类型的结果、不合格清单"""
    items = []
    for p in engine.store.projects():
        for r in recent_inspections(directory(p["id"]), limit):
            m = r.get("meta") or {}
            if order and m.get("order") != order:
                continue
            if site and m.get("site") != site:
                continue
            items.append({**r, "project": p["id"], "project_name": p["config"].get("name")})
    items.sort(key=lambda r: r.get("at", ""))
    rank = {"skip": 0, "pass": 1, "warn": 2, "fail": 3}
    overall = max((r["verdict"] for r in items), key=lambda v: rank.get(v, 0), default="skip")
    by_type = {}
    for r in items:
        k = (r.get("meta") or {}).get("photo_type") or r.get("photo_type") or "未知类型"
        b = by_type.setdefault(k, {"photo_type": k, "pass": 0, "warn": 0, "fail": 0, "skip": 0})
        b[r["verdict"]] = b.get(r["verdict"], 0) + 1
    issues = [{"id": r["id"], "sha": r["sha"], "project": r["project"], "verdict": r["verdict"],
               "photo_type": (r.get("meta") or {}).get("photo_type") or r.get("photo_type"), "file": (r.get("meta") or {}).get("file"),
               "why": [f["message"] + "（" + METRICS.get(f["metric"], f["metric"]) + " " + (f"{f['value'] * 100:.1f}%" if f["metric"] == "coverage" else str(f["value"])) + "）"
                       for x in r.get("results", []) for f in x["findings"] if f.get("hit") and f.get("message")],
               "vlm": [x.get("vlm") for x in r.get("results", []) if x.get("vlm")], "at": r.get("at")}
              for r in items if r["verdict"] in ("fail", "warn")]
    stats = {}
    for r in items:
        stats[r["verdict"]] = stats.get(r["verdict"], 0) + 1
    sites = sorted({(r.get("meta") or {}).get("site") for r in items} - {None, ""})
    orders_ = sorted({(r.get("meta") or {}).get("order") for r in items} - {None, ""})
    return {"order": order, "site": site, "sites": sites, "orders": orders_, "overall": overall, "total": len(items), "stats": stats,
            "by_type": sorted(by_type.values(), key=lambda b: (-b["fail"], -b["warn"], b["photo_type"])), "issues": issues}


def register_reports(router, engine, directory):
    from fastapi import Body, File, Form, UploadFile
    from fastapi.responses import Response

    def _bytes_to_tmp(data):
        f = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        f.write(data)
        f.close()
        return f.name

    @router.post("/agent/inspect-all", tags=["agent"])
    def agent_inspect_all(payload: dict = Body(...)):
        """全子项目质检：{image_base64 | path, photo_type, meta}（不用指定子项目，按照片类型自动找适用的规则）"""
        if payload.get("image_base64"):
            data = base64.b64decode(payload["image_base64"].split(",")[-1])
        elif payload.get("path"):
            data = engine.allowed_source(payload["path"]).read_bytes()
        else:
            raise ValueError("需要 image_base64 或 path")
        tmp = _bytes_to_tmp(data)
        try:
            return inspect_all(engine, directory, tmp, payload.get("photo_type") or "", payload.get("meta"), payload.get("save", True))
        finally:
            os.unlink(tmp)

    @router.post("/agent/inspect-all/upload", tags=["agent"])
    async def agent_inspect_all_upload(file: UploadFile = File(...), photo_type: str = Form(""), meta: str = Form("")):
        from starlette.concurrency import run_in_threadpool
        tmp = _bytes_to_tmp(await file.read())
        try:
            m = json.loads(meta) if meta else {"file": file.filename}
            return await run_in_threadpool(inspect_all, engine, directory, tmp, photo_type, m, True)
        finally:
            os.unlink(tmp)

    @router.get("/inspections/orders", tags=["agent"])
    def orders(limit: int = 200):
        """最近有质检记录的工单 / 站点（来自流式质检的 meta.order / meta.site）"""
        agg = {}
        for p in engine.store.projects():
            for r in recent_inspections(directory(p["id"]), 5000):
                m = r.get("meta") or {}
                key = (m.get("order") or "", m.get("site") or "")
                if not any(key):
                    continue
                a = agg.setdefault(key, {"order": key[0], "site": key[1], "pass": 0, "warn": 0, "fail": 0, "skip": 0, "last": ""})
                a[r["verdict"]] = a.get(r["verdict"], 0) + 1
                a["last"] = max(a["last"], r.get("at", ""))
        return sorted(agg.values(), key=lambda a: a["last"], reverse=True)[:limit]

    @router.get("/inspections/report", tags=["agent"])
    def report(order: str = "", site: str = ""):
        if not order and not site:
            raise ValueError("请指定 order（工单号）或 site（站点）")
        return build_report(engine, directory, order, site)

    @router.get("/inspections/report.csv", tags=["agent"])
    def report_csv(order: str = "", site: str = ""):
        import csv
        import io
        rep = build_report(engine, directory, order, site)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["工单", "站点", "照片类型", "文件", "子项目", "结论", "原因", "大模型说明", "时间"])
        for i in rep["issues"]:
            w.writerow([order, site, i["photo_type"], i["file"], i["project"], {"fail": "不合格", "warn": "需注意"}.get(i["verdict"], i["verdict"]),
                        "；".join(i["why"]), " ".join(i["vlm"]), i["at"]])
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="report-' + __import__("re").sub(r"[^A-Za-z0-9._-]", "_", order or site) + '.csv"'})
