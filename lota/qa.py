"""标注质检（找标注错误）：借鉴 FiftyOne Brain 的 mistakenness —— 用模型预测和人工标注对比，
挑出疑似漏标（模型很有把握、标注里没有）、疑似多标/标错（标了但模型完全不认）、框不准（重叠不够）的图，按可疑程度排序。
"""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from pydantic import Field

from .store import Spec
from .tools import TOOLS, Tool


class Mistakes(Spec):
    selector: str = "production"            # 用哪个模型来“复查”
    dataset_version: str = ""               # 空 = 最新数据集
    split: str = Field(default="", pattern="^(|train|val|test)$")
    limit: int = Field(default=500, ge=1, le=5000)
    conf_missing: float = Field(default=0.5, ge=0.05, le=0.99)   # 模型置信度 ≥ 它、标注里却没有 → 疑似漏标
    conf_match: float = Field(default=0.25, ge=0.01, le=0.9)     # 模型框置信度 ≥ 它才算“认得”
    iou_match: float = Field(default=0.3, ge=0.05, le=0.9)       # 重叠 ≥ 它算同一个目标
    iou_good: float = Field(default=0.6, ge=0.1, le=0.95)        # 重叠 < 它算“框不准”
    device: str = "0"


def _iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def _gt_boxes(labels, w, h):
    out = []
    for lab in labels or []:
        name, nums = lab[0], [float(x) for x in lab[1:]]
        if len(nums) == 4:                     # 检测：cx cy w h（归一化）
            cx, cy, bw, bh = nums
            box = [(cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h]
        else:                                  # 分割：多边形 → 外接框
            xs, ys = nums[0::2], nums[1::2]
            box = [min(xs) * w, min(ys) * h, max(xs) * w, max(ys) * h]
        out.append({"label": name, "xyxy": [round(v, 1) for v in box]})
    return out


def find_mistakes(c, selector="production", dataset_version="", split="", limit=500, conf_missing=0.5, conf_match=0.25,
                  iou_match=0.3, iou_good=0.6, device="0"):
    from .models import predict
    root = Path(c.directory)
    if not dataset_version:
        mans = sorted((root / "datasets").glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mans:
            raise ValueError("还没有数据集：先打包一个数据集再来查标注错误")
        dataset_version = mans[0].parent.name
    man = json.loads((root / "datasets" / Path(dataset_version).name / "manifest.json").read_text(encoding="utf-8"))
    if man.get("task") == "classify":
        raise ValueError("分类任务暂不支持找标注错误（检测、分割支持）")
    imgs = [i for i in man["images"] if not split or i["split"] == split][:limit]
    paths = [root / "datasets" / dataset_version / i["path"] for i in imgs]
    preds = []
    for k in range(0, len(paths), 64):        # 分批推理
        r = predict(root, selector, paths[k:k + 64], conf=min(conf_match, 0.1), device=device)
        preds += r["predictions"]
    model_version = r.get("model_version", selector) if paths else selector
    items, totals = [], {"missing": 0, "spurious": 0, "loc": 0}
    for img, p in zip(imgs, preds):
        w, h = p["width"], p["height"]
        gts = _gt_boxes(img.get("labels"), w, h)
        dets = [d for d in p.get("detections", []) if d["confidence"] >= min(conf_match, 0.1)]
        issues = []
        used = set()
        for g in gts:
            cands = [(i, _iou(g["xyxy"], d["xyxy"])) for i, d in enumerate(dets) if d["label"] == g["label"] and d["confidence"] >= conf_match]
            best = max(cands, key=lambda x: x[1], default=(None, 0.0))
            if best[1] < iou_match:
                issues.append({"type": "spurious", "label": g["label"], "xyxy": g["xyxy"], "score": 0.5,
                               "text": f"标了 {g['label']}，模型不认（可能多标、标错类别或特别难）"})
            else:
                used.add(best[0])
                if best[1] < iou_good:
                    issues.append({"type": "loc", "label": g["label"], "xyxy": g["xyxy"], "score": 0.3, "iou": round(best[1], 2),
                                   "text": f"{g['label']} 的框不太准（和模型重叠 {best[1]:.0%}）"})
        for i, d in enumerate(dets):
            if i in used or d["confidence"] < conf_missing:
                continue
            if max((_iou(d["xyxy"], g["xyxy"]) for g in gts if g["label"] == d["label"]), default=0.0) < iou_match:
                issues.append({"type": "missing", "label": d["label"], "xyxy": [round(v, 1) for v in d["xyxy"]], "score": d["confidence"],
                               "text": f"模型 {d['confidence']:.0%} 确定有 {d['label']}，标注里没有（可能漏标）"})
        for t in issues:
            totals[t["type"]] += 1
        if issues:
            items.append({"image_id": img["image_id"], "split": img["split"], "width": w, "height": h, "gt": gts,
                          "detections": [{"label": d["label"], "confidence": round(d["confidence"], 3), "xyxy": [round(v, 1) for v in d["xyxy"]]}
                                         for d in dets if d["confidence"] >= conf_match],
                          "issues": issues, "score": round(sum(t["score"] for t in issues), 3)})
    items.sort(key=lambda x: -x["score"])
    qa_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{dataset_version}"[:80]
    folder = root / "qa"
    folder.mkdir(exist_ok=True)
    report = {"qa_id": qa_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "dataset_version": dataset_version, "model": model_version,
              "params": {"conf_missing": conf_missing, "conf_match": conf_match, "iou_match": iou_match, "iou_good": iou_good, "split": split},
              "checked": len(imgs), "flagged": len(items), "totals": totals, "items": items}
    (folder / f"{qa_id}.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return {"qa_id": qa_id, "checked": len(imgs), "flagged": len(items), **totals, "image_ids": [x["image_id"] for x in items]}


TOOLS["quality.mistakes"] = Tool("找标注错误", Mistakes, find_mistakes)


def _ls_links(root, public):
    db = Path(root) / "annotations" / "index.sqlite3"
    if not db.is_file():
        return {}
    with closing(sqlite3.connect(db, timeout=10)) as c:
        try:
            rows = c.execute("SELECT image_id, project_id, remote_id FROM label_studio_links").fetchall()
        except sqlite3.Error:
            return {}
    return {i: f"{public.rstrip('/')}/projects/{p}/data?task={r}" for i, p, r in rows}


def register_routes(router, engine, directory):
    from fastapi import Body

    @router.get("/projects/{project_id}/qa", tags=["qa"])
    def qa_list(project_id: str):
        folder = directory(project_id) / "qa"
        out = []
        for f in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []:
            if f.name.endswith(".decisions.json"):
                continue
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({k: d[k] for k in ("qa_id", "created", "dataset_version", "model", "checked", "flagged", "totals")})
        return out

    @router.get("/projects/{project_id}/qa/{qa_id}", tags=["qa"])
    def qa_get(project_id: str, qa_id: str):
        from .hub import load_settings
        root = directory(project_id)
        f = root / "qa" / f"{Path(qa_id).name}.json"
        d = json.loads(f.read_text(encoding="utf-8"))
        links = _ls_links(root, load_settings(root)["label_studio"]["public_url"])
        dec = root / "qa" / f"{Path(qa_id).name}.decisions.json"
        d["decisions"] = json.loads(dec.read_text(encoding="utf-8")) if dec.is_file() else {}
        for it in d["items"]:
            it["ls_url"] = links.get(it["image_id"])
        return d

    @router.post("/projects/{project_id}/qa/{qa_id}/decisions", tags=["qa"])
    def qa_decide(project_id: str, qa_id: str, payload: dict = Body(...)):
        f = directory(project_id) / "qa" / f"{Path(qa_id).name}.decisions.json"
        d = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        for k, v in (payload.get("decisions") or {}).items():
            if v:
                d[k] = v
            else:
                d.pop(k, None)
        f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return {"decisions": d}
