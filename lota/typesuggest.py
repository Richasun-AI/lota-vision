"""按权重给“照片类型”归类：用一个 YOLO 模型（检测或分类）扫“照片类型”页的全部照片，按规则
“权重里的这些类别 → 这个照片类型”给出**模型建议**。建议单独记着，不算人工确认，不影响照片类型分类器的种子；
在“照片类型”页的“权重建议”里看图、整批确认（确认后才算人工确认）或忽略。

- 照片是全平台共用的（不属于哪个子项目）；模型可以是当前子项目的模型（如 latest），也可以是一个权重文件。
- 已经人工确认过类型的照片不扫、不改。
- 扫过的记下来，下次接着扫（断点续扫）；换了权重 / 规则 / 范围就重新扫。
"""
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

from pydantic import Field

from . import config as deploy
from .store import Spec

LEGACY = deploy.LEGACY_PIPELINE


def _legacy():
    if str(LEGACY) not in sys.path:
        sys.path.insert(0, str(LEGACY))
    import emb_index as ei
    import hq_filter as hf
    return ei, hf


def _store(hf):
    return Path(hf.WORK) / "type_suggestions.jsonl"


def parse_rules(rules):
    """规则：“grounding_bar>grounding_lightning; rust,crack>site_ground” 或 [{"labels": [...], "photo_type": "..."}]
    → [(类别集合, 照片类型)]"""
    out = []
    if isinstance(rules, str):
        for part in rules.replace("；", ";").split(";"):
            if not part.strip():
                continue
            if ">" not in part:
                raise ValueError(f"规则“{part.strip()}”要写成“权重类别>照片类型”，多个类别用逗号分开")
            left, right = part.split(">", 1)
            labels = {x.strip() for x in left.replace("，", ",").split(",") if x.strip()}
            out.append((labels, right.strip()))
    else:
        for r in rules or []:
            labels = r.get("labels") or []
            labels = {x.strip() for x in (labels.split(",") if isinstance(labels, str) else labels) if str(x).strip()}
            out.append((labels, str(r.get("photo_type") or "").strip()))
    return [(labels, t) for labels, t in out if labels or t]


def _split(value):
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").replace("，", ",").split(",") if x.strip()]


def load_suggestions(ei=None, hf=None, *, raw=False):
    """当前有效的建议：{原图路径: 记录}。同一张图以最后一条为准；被忽略的、已经人工确认的、已删除的不算"""
    if ei is None:
        ei, hf = _legacy()
    f = _store(hf)
    recs, dismissed = {}, set()
    lines = f.read_text(encoding="utf-8").splitlines() if f.is_file() else []
    if lines:
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("dismissed"):
                dismissed.add((d["path"], d["photo_type"]))
                if recs.get(d["path"], {}).get("photo_type") == d["photo_type"]:
                    recs.pop(d["path"], None)
            elif d.get("path") and d.get("photo_type"):
                recs[d["path"]] = d
    if raw:
        return recs, dismissed
    confirmed, deleted = ei.confirmed_labels(), hf.load_deleted()
    return {p: d for p, d in recs.items()
            if p not in confirmed and p not in deleted and (p, d["photo_type"]) not in dismissed and d["photo_type"] in hf.TAXONOMY}


def dismiss(paths, ptype):
    """忽略建议：这张图不再建议成这个类型（以后重新扫也不会再建议）"""
    ei, hf = _legacy()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(_store(hf), "a", encoding="utf-8") as f:
        for p in paths:
            f.write(json.dumps({"path": p, "photo_type": ptype, "dismissed": True, "ts": ts}, ensure_ascii=False) + "\n")
    return {"dismissed": len(paths), "type": ptype}


class ClassifyTypes(Spec):
    rules: str | list = Field(min_length=1)          # 权重类别 > 照片类型，可以多条
    model: str = ""                                  # 当前子项目的模型版本或别名（如 latest）；填了就不用 weights
    weights: str = ""                                # 或者一个 YOLO 权重文件
    scope_types: str | list = ""                     # 只扫现在归在这些照片类型里的照片；空 = 全部
    min_confidence: float = Field(default=0.5, ge=0.01, le=1)
    min_boxes: int = Field(default=1, ge=1, le=100)
    scan_limit: int = Field(default=20000, ge=1, le=500000)
    imgsz: int = Field(default=640, ge=64, le=4096)
    batch: int = Field(default=16, ge=1, le=256)
    device: str = "0"
    restart: bool = False


def classify_types(c, rules, model="", weights="", scope_types="", min_confidence=0.5, min_boxes=1,
                   scan_limit=20000, imgsz=640, batch=16, device="0", restart=False):
    import shutil
    from ultralytics import YOLO
    from .flow import _weights_names
    ei, hf = _legacy()
    if model:
        from .models import resolve_model
        try:
            _, w = resolve_model(Path(c.directory), model)
        except (FileNotFoundError, ValueError, KeyError) as e:
            raise ValueError(f"当前子项目还没有模型“{model}”：{e}") from e
        w = Path(w)
    elif weights:
        w = c.engine.allowed_weights(weights)
    else:
        raise ValueError("请选择权重文件，或填当前子项目的模型（如 latest）")
    info = _weights_names(w)
    parsed = parse_rules(rules)
    if not parsed:
        raise ValueError("至少要一条规则：权重里的哪些类别 → 归到哪个照片类型")
    for labels, t in parsed:
        if not labels:
            raise ValueError(f"规则“→ {t}”没选权重类别")
        unknown = sorted(labels - set(info["names"]))
        if unknown:
            raise ValueError(f"权重 {w.name} 认不出 {unknown}；它能识别的类别：{info['names']}")
        if t not in hf.TAXONOMY:
            raise ValueError(f"没有这个照片类型：{t or '（没选）'}")
        if t == ei.BIN:
            raise ValueError("“其他/无关”是临时仓库，不能作为归类目标")
    scope = set(_split(scope_types))
    bad = sorted(scope - set(hf.TAXONOMY))
    if bad:
        raise ValueError(f"没有这些照片类型：{bad}")

    eff, confirmed = ei.effective_types(), ei.confirmed_labels()
    pool = sorted(p for p, t in eff.items() if p not in confirmed and (not scope or t in scope))
    rule_key = {"weights": str(w), "mtime": Path(w).stat().st_mtime, "rules": sorted([sorted(l), t] for l, t in parsed),
                "scope": sorted(scope), "conf": min_confidence, "boxes": min_boxes}
    key = hashlib.sha1(json.dumps(rule_key, sort_keys=True).encode()).hexdigest()[:10]
    state = Path(hf.WORK) / "type_suggest" / key
    if restart and state.exists():
        shutil.rmtree(state)
    state.mkdir(parents=True, exist_ok=True)
    (state / "rule.json").write_text(json.dumps({**rule_key, "model": model}, ensure_ascii=False, indent=1), encoding="utf-8")
    done_file = state / "scanned.txt"
    done = set(done_file.read_text(encoding="utf-8").splitlines()) if done_file.is_file() else set()
    todo = [p for p in pool if p not in done][:scan_limit]
    _, dismissed = load_suggestions(ei, hf, raw=True)

    net = YOLO(str(w))
    conf_floor = min(min_confidence, 0.25)
    suggested, scanned, missing, errors = Counter(), 0, 0, 0
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    def decide(r):
        """→ (照片类型, 命中的权重类别, 置信度, 框数) 或 None；多条规则都中时取置信度最高的"""
        best = None
        if info["task"] == "classify":
            name, conf = r.names[r.probs.top1], float(r.probs.top1conf)
            for labels, t in parsed:
                if name in labels and conf >= min_confidence and (best is None or conf > best[2]):
                    best = (t, name, conf, 1)
            return best
        for labels, t in parsed:
            hits = [(r.names[int(b.cls)], float(b.conf)) for b in r.boxes
                    if r.names[int(b.cls)] in labels and float(b.conf) >= min_confidence]
            if len(hits) >= min_boxes:
                name, conf = max(hits, key=lambda x: x[1])
                if best is None or conf > best[2]:
                    best = (t, name, conf, len(hits))
        return best

    with open(done_file, "a", encoding="utf-8") as log, open(_store(hf), "a", encoding="utf-8") as out:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            located = []
            for p in chunk:
                f = Path(hf.locate(p))
                if f.is_file():
                    located.append((p, f))
                else:
                    missing += 1
                    log.write(p + "\n")
            try:
                pairs = list(zip(located, net.predict([str(f) for _, f in located], imgsz=imgsz, conf=conf_floor,
                                                      device=device, verbose=False))) if located else []
            except Exception:   # 这一批里有坏图：逐张跑，坏的跳过
                pairs = []
                for item in located:
                    try:
                        pairs.append((item, net.predict(str(item[1]), imgsz=imgsz, conf=conf_floor, device=device, verbose=False)[0]))
                    except Exception:
                        errors += 1
                        log.write(item[0] + "\n")
            for (p, _), r in pairs:
                best = decide(r)
                if best and (p, best[0]) not in dismissed:
                    out.write(json.dumps({"path": p, "photo_type": best[0], "label": best[1], "confidence": round(best[2], 4),
                                          "boxes": best[3], "weights": Path(w).name, "model": model, "was": eff.get(p), "ts": ts},
                                         ensure_ascii=False) + "\n")
                    suggested[best[0]] += 1
                log.write(p + "\n")
                scanned += 1
            out.flush()
            log.flush()
    done_now = set(done_file.read_text(encoding="utf-8").splitlines())
    name = lambda t: hf.TAXONOMY.get(t, {}).get("name", t)
    return {"scanned": scanned, "suggested": sum(suggested.values()),
            "by_type": {name(t): n for t, n in suggested.most_common()},
            "pool": len(pool), "remaining": sum(1 for p in pool if p not in done_now), "skipped_confirmed": len(confirmed),
            "missing_files": missing, "errors": errors, "weights": Path(w).name,
            "note": "建议在“照片类型”页各类型的“权重建议”里确认或忽略；确认后才算人工确认"}
