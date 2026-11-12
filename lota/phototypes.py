"""照片类型（⑤类型归类）移植到新平台：沿用旧照片归类流水线（pipeline）的数据（SigLIP2 特征索引 + 人工确认种子 +
逻辑回归归类 + 临时仓库），界面换成新平台风格。代码和数据在 LOTA_LEGACY_PIPELINE 目录（emb_index / hq_filter / work/），
旧 7870 页面已于 2026-11-12 停用，这个目录不能删。
"""
import hashlib
import sys
import threading
import time
from pathlib import Path

from . import config as deploy

LEGACY = deploy.LEGACY_PIPELINE
THUMBS = Path.home() / ".cache" / "ailabel" / "type_thumbs"
_TRAIN = {"running": False, "started": None, "finished": None, "result": None, "error": None}


def _legacy():
    if str(LEGACY) not in sys.path:
        sys.path.insert(0, str(LEGACY))
    import emb_index as ei
    import hq_filter as hf
    return ei, hf


def summary():
    ei, hf = _legacy()
    eff = ei.effective_types()
    conf = ei.confirmed_labels()
    pred = ei.load_pred()
    from .typesuggest import load_suggestions
    suggested = {}
    for d in load_suggestions(ei, hf).values():
        suggested[d["photo_type"]] = suggested.get(d["photo_type"], 0) + 1
    counts, confirmed, low = {}, {}, {}
    for p, t in eff.items():
        counts[t] = counts.get(t, 0) + 1
        if conf.get(p) == t:
            confirmed[t] = confirmed.get(t, 0) + 1
        elif (pred.get(p) or [None, 1.0])[1] < 0.5:
            low[t] = low.get(t, 0) + 1
    types = []
    for k, v in hf.TAXONOMY.items():
        types.append({"key": k, "name": v.get("name", k), "name_en": v.get("name_en", ""), "count": counts.get(k, 0),
                      "confirmed": confirmed.get(k, 0), "low": low.get(k, 0), "suggested": suggested.get(k, 0), "bin": k == ei.BIN})
    types.sort(key=lambda x: (x["bin"], -x["count"]))
    rep = {}
    if ei.REPORT.exists():
        import json
        r = json.loads(ei.REPORT.read_text(encoding="utf-8"))
        rep = {k: r.get(k) for k in ("ts", "n_index", "n_human", "n_weak", "cv_acc", "bin_total", "bin_rescued",
                                     "per_type", "dist", "bin_rescued_to")}
    return {"types": types, "total": len(eff), "confirmed_total": len(conf), "report": rep, "train": _TRAIN}


def add_type(name, name_en, key="", must_have=None, anomalies=None, anomalies_en=None):
    """新增照片类型（中英双语），写进 pipeline 的 自定义类型.yaml，和总表合并使用（从旧 7870 页面迁移）"""
    _, hf = _legacy()
    ok, msg, key = hf.add_photo_type(name, key, must_have or [], anomalies or [], name_en, anomalies_en or [])
    if not ok:
        raise ValueError(msg)
    return {"key": key, "message": msg}


def images(ptype, view="all", offset=0, limit=60):
    ei, hf = _legacy()
    if view == "suggested":   # 按权重归类给出的建议（待确认）：置信度高的在前
        from .typesuggest import load_suggestions
        eff = ei.effective_types()
        rows = [{"path": p, "confirmed": False, "conf": d.get("confidence", 0), "second": "", "second_conf": 0,
                 "suggested": {"label": d.get("label"), "confidence": d.get("confidence"), "weights": d.get("weights"), "boxes": d.get("boxes")},
                 "current": eff.get(p)}
                for p, d in load_suggestions(ei, hf).items() if d["photo_type"] == ptype]
        rows.sort(key=lambda r: -r["conf"])
        return {"total": len(rows), "items": rows[offset:offset + min(limit, 300)]}
    eff = ei.effective_types()
    conf = ei.confirmed_labels()
    pred = ei.load_pred()
    rows = []
    for p, t in eff.items():
        if t != ptype:
            continue
        pr = pred.get(p) or [t, 1.0, "", 0.0]
        c = conf.get(p) == t
        if view == "confirmed" and not c or view == "predicted" and c or view == "low" and (c or pr[1] >= 0.5):
            continue
        rows.append({"path": p, "confirmed": c, "conf": pr[1], "second": pr[2], "second_conf": pr[3]})
    rows.sort(key=lambda r: (r["confirmed"], r["conf"]) if view != "confirmed" else (0, -r["conf"]))
    return {"total": len(rows), "items": rows[offset:offset + min(limit, 300)]}


def similar(ptype, limit=60):
    ei, hf = _legacy()
    eff = ei.effective_types()
    conf = ei.confirmed_labels()
    exclude = {p for p, t in eff.items() if t == ptype} | set(conf)
    paths, scores, seeds = ei.similar_to_type(ptype, exclude)
    return {"seeds": seeds, "items": [{"path": p, "score": round(s, 3), "current": eff.get(p)} for p, s in zip(paths[:limit], scores[:limit])]}


def confirm(paths, ptype, source="LOTA 照片类型页"):
    ei, hf = _legacy()
    if ptype not in hf.TAXONOMY:
        raise ValueError(f"没有这个照片类型：{ptype}")
    ei.save_type_labels([(p, ptype) for p in paths], source=source)
    return {"confirmed": len(paths), "type": ptype}


def start_train():
    if _TRAIN["running"]:
        return _TRAIN

    def run():
        ei, hf = _legacy()
        try:
            r = ei.train_predict()
            _TRAIN.update(result={k: r.get(k) for k in ("ts", "n_human", "n_weak", "cv_acc", "bin_total", "bin_rescued", "error")},
                          error=r.get("error"))
        except Exception as e:
            _TRAIN["error"] = str(e)[:300]
        finally:
            try:
                ei.free_model()
            except Exception:
                pass
            _TRAIN.update(running=False, finished=time.strftime("%H:%M:%S"))

    _TRAIN.update(running=True, started=time.strftime("%H:%M:%S"), finished=None, result=None, error=None)
    threading.Thread(target=run, daemon=True).start()
    return _TRAIN


def _source(path):
    """照片现在实际在哪（源目录 / 数据集 / 回收站）；只给共享图片目录里的图"""
    ei, hf = _legacy()
    src = Path(hf.locate(path)).resolve()
    if not src.is_file():   # 已删除的：在回收站里
        rec = hf.load_deleted().get(path)
        if rec and rec.get("trash"):
            src = Path(rec["trash"]).resolve()
    if not src.is_relative_to(deploy.DATA_ROOT.resolve()) or not src.is_file():
        raise FileNotFoundError("找不到这张图")
    return src


def thumb(path, size=320):
    """缩略图（缓存在服务器用户目录，不放进共享盘）"""
    from PIL import Image, ImageOps
    src = _source(path)
    size = max(96, min(int(size), 2000))
    key = hashlib.sha1(f"{path}|{size}".encode()).hexdigest()
    out = THUMBS / key[:2] / f"{key}.jpg"
    if not out.is_file() or out.stat().st_mtime < src.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((size, size))
            im.save(out, "JPEG", quality=85)
    return out


def register_routes(router, engine, directory):
    from fastapi import Body, HTTPException
    from fastapi.responses import FileResponse

    @router.get("/types/summary", tags=["types"])
    def types_summary():
        return summary()

    @router.post("/types/new", tags=["types"])
    def type_new(payload: dict = Body(...)):
        split = lambda v: [x.strip() for x in (v if isinstance(v, list) else str(v or "").replace("，", ",").split(",")) if x.strip()]
        try:
            return add_type(payload.get("name", ""), payload.get("name_en", ""), payload.get("key", ""),
                            split(payload.get("must_have")), split(payload.get("anomalies")), split(payload.get("anomalies_en")))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/types/{ptype}/images", tags=["types"])
    def type_images(ptype: str, view: str = "all", offset: int = 0, limit: int = 60):
        return images(ptype, view, offset, limit)

    @router.get("/types/{ptype}/similar", tags=["types"])
    def type_similar(ptype: str, limit: int = 60):
        return similar(ptype, limit)

    @router.post("/types/confirm", tags=["types"])
    def type_confirm(payload: dict = Body(...)):
        return confirm(payload.get("paths") or [], payload.get("type", ""))

    @router.post("/types/suggestions/dismiss", tags=["types"])
    def type_suggestions_dismiss(payload: dict = Body(...)):
        """忽略按权重归类给出的建议：这些图不再建议成这个类型"""
        from .typesuggest import dismiss
        return dismiss(payload.get("paths") or [], payload.get("type", ""))

    @router.post("/types/train", tags=["types"])
    def type_train():
        return start_train()

    @router.get("/types/train", tags=["types"])
    def type_train_status():
        return _TRAIN

    @router.get("/types/original", tags=["types"])
    def type_original(path: str):
        """原图（看细节用）"""
        import mimetypes
        src = _source(path)
        return FileResponse(src, media_type=mimetypes.guess_type(src.name)[0] or "image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/types/thumb", tags=["types"])
    def type_thumb(path: str, size: int = 320):
        return FileResponse(thumb(path, size), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


# ── 删除（物理删除）：原图移出原目录进回收站 → 可恢复；“永久删除”才真正抹掉 ─────────────────────
# 沿用 pipeline 的删除机制（hq_filter.delete_image / deleted.jsonl / _deleted 回收站）。
# 同时把 LOTA 各子项目目录里登记过的同一张图标记为“已删除”（以后扫描也不会再登记回来）；恢复时撤销。
CATALOG_LOG = Path.home() / ".cache" / "ailabel" / "type_deleted_catalog.jsonl"


def _catalog_rows(engine, paths):
    """各子项目目录里，来源路径是这些文件的照片：[(项目, 目录, image_id, path, status)]"""
    import sqlite3
    from contextlib import closing
    out, want = [], list(dict.fromkeys(paths))
    for p in engine.store.projects():
        d = engine.store.project_dir(p["id"])
        db_file = Path(d) / "catalog" / "catalog.sqlite3"
        if not db_file.is_file() or not want:
            continue
        with closing(sqlite3.connect(db_file, timeout=30)) as db:
            for i in range(0, len(want), 500):
                part = want[i:i + 500]
                q = ",".join("?" * len(part))
                out += [(p["id"], d, r[0], r[1], r[2]) for r in db.execute(
                    f"""SELECT DISTINCT s.image_id, s.path, i.status FROM source_files s JOIN images i ON i.image_id = s.image_id
                        WHERE s.path IN ({q})""", part)]
    return out


def _catalog_forget(engine, paths, permanent=False):
    """进回收站：子项目里只把这张图标为“排除”（不进数据集；标注、类别都保留），记下原来的状态，恢复时还原。
    永久删除：用引擎自己的删除（删登记、派生文件、标注；记“已删除”，以后扫描不会再登记回来；不碰原图）。"""
    import json
    from types import SimpleNamespace
    from .catalog import review_image
    from .cleanup import delete_images
    rows, out = _catalog_rows(engine, paths), {}
    CATALOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CATALOG_LOG, "a", encoding="utf-8") as log:
        for pid, d, iid, path, status in rows:
            if permanent:
                delete_images(SimpleNamespace(directory=d, engine=engine), [iid], delete_source=False)
            else:
                review_image(d, iid, decision="rejected", note="照片类型页删除（原图在回收站）")
            log.write(json.dumps({"path": path, "project": pid, "image_id": iid, "prev_status": status,
                                  "mode": "deleted" if permanent else "excluded", "ts": time.time()}, ensure_ascii=False) + "\n")
            out[pid] = out.get(pid, 0) + 1
    return out


def _catalog_restore(engine, paths):
    """恢复：把子项目里被“排除”的同一张图还原成删除前的状态（确认 / 待复核 / 排除）"""
    import json
    from .catalog import review_image
    if not CATALOG_LOG.is_file():
        return {}
    want, last = set(paths), {}
    for line in CATALOG_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("path") in want:
            last[(r["path"], r["project"], r["image_id"])] = r     # 同一张图删过多次：以最后一次为准
    out = {}
    for (path, pid, iid), r in last.items():
        if r.get("mode") != "excluded":
            continue
        try:
            review_image(engine.store.project_dir(pid), iid, decision=r.get("prev_status") or "pending", note="从回收站恢复")
        except (FileNotFoundError, KeyError, ValueError):
            continue
        out[pid] = out.get(pid, 0) + 1
    return out


def delete(engine, paths, permanent=False, note="LOTA 照片类型页删除"):
    ei, hf = _legacy()
    ok, failed = [], []
    for p in dict.fromkeys(paths):
        msg = hf.delete_image(p, note)
        (ok if msg.startswith("已删除") else failed).append({"path": p, "msg": msg})
    done = [x["path"] for x in ok]
    purged = purge(done)["purged"] if permanent and done else 0
    catalog = _catalog_forget(engine, done, permanent) if done else {}
    return {"deleted": len(done), "purged": purged, "failed": failed, "catalog": catalog}


def trash(offset=0, limit=60):
    ei, hf = _legacy()
    rows = [{"path": p, "ts": r.get("ts", ""), "note": r.get("note", ""), "trash": r.get("trash", "")}
            for p, r in hf.load_deleted().items() if not r.get("purged")]
    rows.sort(key=lambda r: r["ts"], reverse=True)
    size = 0
    for r in rows:
        try:
            size += Path(r["trash"]).stat().st_size
        except OSError:
            pass
    return {"total": len(rows), "bytes": size, "items": rows[offset:offset + min(limit, 300)]}


def restore(engine, paths):
    ei, hf = _legacy()
    ok, failed = [], []
    for p in dict.fromkeys(paths):
        msg = hf.restore_image(p)
        (ok if msg.startswith("已恢复") else failed).append({"path": p, "msg": msg})
    done = [x["path"] for x in ok]
    return {"restored": len(done), "failed": failed, "catalog": _catalog_restore(engine, done) if done else {}}


def purge(paths=None, engine=None):
    """永久删除回收站里的文件（不可恢复）。paths=None = 清空整个回收站。给了 engine 时，子项目里的同一张图也彻底删除"""
    ei, hf = _legacy()
    rows = hf.load_deleted()
    want = None if paths is None else set(paths)
    n, done = 0, []
    for p, rec in rows.items():
        if rec.get("purged") or (want is not None and p not in want):
            continue
        t = Path(rec.get("trash") or "")
        if t.is_file() and t.resolve().is_relative_to(hf.TRASH.resolve()):
            t.unlink()
            n += 1
        hf._log_deleted({"action": "purge", "path": p})
        done.append(p)
    catalog = _catalog_forget(engine, done, permanent=True) if engine and done else {}
    return {"purged": n, "catalog": catalog}


def register_delete_routes(router, engine):
    from fastapi import Body

    @router.post("/types/delete", tags=["types"])
    def type_delete(payload: dict = Body(...)):
        """payload: {paths: [...], permanent: false}。默认移到回收站（原目录里就没有了，可恢复）；permanent=true 直接永久删除"""
        paths = payload.get("paths") or []
        if not paths:
            raise ValueError("没有选照片")
        if len(paths) > 5000:
            raise ValueError("一次最多删 5000 张")
        return delete(engine, paths, bool(payload.get("permanent")))

    @router.get("/types/trash", tags=["types"])
    def type_trash(offset: int = 0, limit: int = 60):
        return trash(offset, limit)

    @router.post("/types/trash/restore", tags=["types"])
    def type_restore(payload: dict = Body(...)):
        return restore(engine, payload.get("paths") or [])

    @router.post("/types/trash/purge", tags=["types"])
    def type_purge(payload: dict = Body(...)):
        """payload: {paths: [...]} 永久删除这些；{all: true} 清空整个回收站。不可恢复"""
        if payload.get("all"):
            return purge(None, engine)
        paths = payload.get("paths") or []
        if not paths:
            raise ValueError("没有选照片")
        return purge(paths, engine)
