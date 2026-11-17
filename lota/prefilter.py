"""初选：给一个图片目录逐张打画质分（分辨率 / 清晰度 / 曝光 / 对比度，算法和默认阈值沿用原来的图片分拣脚本），
找出重复和几乎一样的照片，在网页上按档看图、人工改判，最后把入选的照片**硬链接**到精选目录（原图不动、不占额外空间）。
精选目录再到“照片类型”页建成图库，管理成一个图像数据集。

- 每个源目录一个初选任务：{HOME}/prefilter/jobs/<代号>/（job.json 参数和状态，items.sqlite3 每张图的指标和人工改判）
- 只记原始指标，阈值改了立刻重新分档，不用重扫；再次扫描只算新增或改过的文件
- 导出的硬链接记在 {HOME}/prefilter/links.jsonl：照片类型页物理删除精选目录里的图时，原目录里的同一张也一起移走
- “不要”的照片可以物理删除：先移进本任务的回收站 {HOME}/prefilter/trash/<代号>/（同一块盘，只是挪位置），
  能恢复；“清空回收站”才真正删掉、释放空间。已经导出过的，精选目录里的同一个硬链接也一起摘掉；
  在图库里当过人工样板的照片不删（跳过并提示）
"""
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from . import config as deploy
from . import imagelib

ROOT = deploy.HOME / "prefilter" / "jobs"
TRASH = deploy.HOME / "prefilter" / "trash"
IMAGE_EXT = imagelib.IMAGE_EXT
THUMBS = Path.home() / ".cache" / "ailabel" / "prefilter_thumbs"
DEFAULTS = {
    "hard_min_dim": 32, "target_min_dim": 640,          # 分辨率：短边低于 hard 直接淘汰；达到 target 满分
    "hard_min_sharp": 8.0, "target_sharp": 150.0,        # 清晰度（拉普拉斯方差）
    "hard_min_bright": 12.0, "hard_max_bright": 245.0,   # 曝光：平均亮度过暗 / 过曝淘汰
    "hard_min_contrast": 4.0, "target_contrast": 50.0,   # 对比度（灰度标准差）
    "max_aspect": 6.0, "min_bytes": 1024,                # 长宽比、文件大小
    "w_res": 0.35, "w_sharp": 0.35, "w_bright": 0.15, "w_contrast": 0.15,
    "high": 80.0, "medium": 55.0,                        # 档位分数线
    "dedup": True, "hamming": 8,                         # 几乎一样：64 位差异哈希相差不超过几位
    "export_tiers": ["high", "medium"],                  # 导出哪几档
}
TIERS = ("high", "medium", "low", "rejected")
_jobs = {}            # 代号 → 运行状态（内存）
_cls_cache = {}       # 代号 → (key, 结果)
_lock = threading.Lock()


def _job_dir(job_id):
    d = ROOT / job_id
    if not (d / "job.json").is_file():
        raise FileNotFoundError(f"没有这个初选任务：{job_id}")
    return d


def _db(d):
    db = sqlite3.connect(d / "items.sqlite3", timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS items (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, w INTEGER, h INTEGER,
                  sharp REAL, bright REAL, contrast REAL, sha TEXT, dhash TEXT, error TEXT)""")
    db.execute("CREATE TABLE IF NOT EXISTS overrides (path TEXT PRIMARY KEY, decision TEXT, ts TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS trash (path TEXT PRIMARY KEY, trash TEXT, unlinked TEXT, row TEXT, ts TEXT)")
    return db


def _read(d):
    return json.loads((d / "job.json").read_text(encoding="utf-8"))


def _write(d, job):
    tmp = d / "job.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, d / "job.json")


# ── 任务 ────────────────────────────────────────────────────────────────
def list_jobs():
    out = []
    for f in sorted(ROOT.glob("*/job.json")) if ROOT.is_dir() else []:
        j = json.loads(f.read_text(encoding="utf-8"))
        j["run"] = _jobs.get(j["id"], {})
        out.append(j)
    out.sort(key=lambda j: j.get("created", ""), reverse=True)
    return out


def create_job(engine, source, name=""):
    src = engine.allowed_source(source, directory=True).resolve()
    job_id = hashlib.sha1(str(src).encode()).hexdigest()[:10]
    d = ROOT / job_id
    if (d / "job.json").is_file():
        return _read(d)
    d.mkdir(parents=True, exist_ok=True)
    job = {"id": job_id, "name": name.strip() or src.name, "source": str(src), "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "params": dict(DEFAULTS), "scanned": None, "exports": []}
    _write(d, job)
    return job


def get_job(job_id):
    j = _read(_job_dir(job_id))
    j["run"] = _jobs.get(job_id, {})
    return j


def set_params(job_id, params):
    d = _job_dir(job_id)
    job = _read(d)
    p = dict(job.get("params") or DEFAULTS)
    for k, v in (params or {}).items():
        if k not in DEFAULTS:
            continue
        if k == "export_tiers":
            p[k] = [t for t in v if t in TIERS]
        elif isinstance(DEFAULTS[k], bool):
            p[k] = bool(v)
        else:
            p[k] = type(DEFAULTS[k])(v)
    if p["medium"] > p["high"]:
        raise ValueError("“中”档分数线不能高于“高”档")
    job["params"] = p
    _write(d, job)
    return job


def delete_job(job_id):
    """只删初选记录（打分、改判），源目录和已经导出的精选目录都不动"""
    d = _job_dir(job_id)
    if _jobs.get(job_id, {}).get("running"):
        raise ValueError("正在扫描，先停止")
    with closing(_db(d)) as db:
        n = db.execute("SELECT COUNT(*) FROM trash").fetchone()[0]
    if n:
        raise ValueError(f"回收站里还有 {n} 张：先恢复或清空回收站，再删记录")
    shutil.rmtree(d)
    shutil.rmtree(TRASH / job_id, ignore_errors=True)
    return {"deleted": job_id}


# ── 扫描打分 ─────────────────────────────────────────────────────────────
def _dhash(gray):
    import cv2
    r = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    v = 0
    for bit in (r[:, 1:] > r[:, :-1]).flatten():
        v = (v << 1) | int(bit)
    return format(v, "016x")


def analyze(path):
    """一张图的原始指标（不抛异常：坏图记 error）"""
    import cv2
    import numpy as np
    p = Path(path)
    try:
        raw = p.read_bytes()
        st = p.stat()
    except OSError as e:
        return {"path": path, "mtime": 0, "size": 0, "error": f"读不出文件：{e}"}
    rec = {"path": path, "mtime": st.st_mtime, "size": len(raw), "sha": hashlib.sha256(raw).hexdigest()}
    try:
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    except Exception as e:
        img, rec["error"] = None, f"解码失败：{e}"
    if img is None or not img.size:
        rec.setdefault("error", "损坏或不支持的格式")
        return rec
    h, w = img.shape[:2]
    rec.update(w=w, h=h, sharp=float(cv2.Laplacian(img, cv2.CV_64F).var()), bright=float(img.mean()),
               contrast=float(img.std()), dhash=_dhash(img))
    return rec


def _list_images(root):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(x for x in dns if not x.startswith("."))
        out += [os.path.join(dp, f) for f in sorted(fns) if os.path.splitext(f)[1].lower() in IMAGE_EXT]
    return out


def start_scan(job_id, workers=12, engine=None, auto_export=False):
    """后台扫描（增量）：只算新增或改过的文件；源目录里已经没有的从记录里去掉。
    auto_export = 扫完把新入选的照片接着导出到上次的精选目录（增量更新：原图目录补了新照片时用）"""
    d = _job_dir(job_id)
    with _lock:
        if _jobs.get(job_id, {}).get("running"):
            return _jobs[job_id]
        run = _jobs[job_id] = {"running": True, "stop": False, "done": 0, "total": 0, "started": time.strftime("%H:%M:%S"),
                               "message": "列文件…", "error": None}

    def work():
        import cv2
        from concurrent.futures import ThreadPoolExecutor
        cv2.setNumThreads(1)
        job = _read(d)
        try:
            files = _list_images(job["source"])
            with closing(_db(d)) as db:
                known = {r[0]: (r[1], r[2]) for r in db.execute("SELECT path, mtime, size FROM items")}
                alive = set(files)
                gone = [p for p in known if p not in alive]
                for i in range(0, len(gone), 500):
                    part = gone[i:i + 500]
                    db.execute(f"DELETE FROM items WHERE path IN ({','.join('?' * len(part))})", part)
                db.commit()
                todo = []
                for p in files:
                    k = known.get(p)
                    if k is None:
                        todo.append(p)
                        continue
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if abs(st.st_mtime - (k[0] or 0)) > 1e-3 or st.st_size != k[1]:
                        todo.append(p)
                run.update(total=len(todo), message=f"共 {len(files)} 张，要打分 {len(todo)} 张")
                t0, batch = time.time(), []
                cols = ("path", "mtime", "size", "w", "h", "sharp", "bright", "contrast", "sha", "dhash", "error")
                with ThreadPoolExecutor(workers) as ex:
                    for i in range(0, len(todo), 256):
                        if run["stop"]:
                            break
                        for r in ex.map(analyze, todo[i:i + 256]):
                            batch.append(tuple(r.get(c) for c in cols))
                        db.executemany(f"INSERT OR REPLACE INTO items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", batch)
                        db.commit()
                        batch.clear()
                        n = min(i + 256, len(todo))
                        run.update(done=n, message=f"{n}/{len(todo)} 张 · {n / max(time.time() - t0, 1e-6):.0f} 张/秒")
            job = _read(d)
            job["scanned"] = time.strftime("%Y-%m-%d %H:%M:%S")
            job["files"] = len(files)
            _write(d, job)
            run["message"] = ("已停止（下次接着扫）" if run["stop"] else f"扫描完成：{len(files)} 张，本次打分 {len(todo)} 张，用时 {time.time() - t0:.0f} 秒")
            if auto_export and not run["stop"] and job.get("last_target") and engine is not None:
                run["message"] += "；导出新入选的…"
                r = export(engine, job_id, job["last_target"])
                run["export"] = r
                run["message"] = run["message"].replace("；导出新入选的…", f"；新链入精选目录 {r['linked'] + r['copied']} 张（已有 {r['already']} 张）")
        except Exception as e:
            run["error"] = str(e)[:300]
        finally:
            run["running"] = False
            run["finished"] = time.strftime("%H:%M:%S")

    threading.Thread(target=work, daemon=True).start()
    return run


def stop_scan(job_id):
    if job_id in _jobs:
        _jobs[job_id]["stop"] = True
    return _jobs.get(job_id, {})


# ── 分档（按当前参数，从原始指标现算） ──────────────────────────────────────────
def _grade(r, p):
    """→ (档位, 分数, 原因列表)"""
    if r["error"]:
        return "rejected", 0.0, [r["error"]]
    why = []
    w, h = r["w"] or 0, r["h"] or 0
    if (r["size"] or 0) < p["min_bytes"]:
        why.append(f"文件太小（{r['size']}B）")
    if min(w, h) < p["hard_min_dim"]:
        why.append(f"分辨率太低（{w}×{h}）")
    if r["sharp"] < p["hard_min_sharp"]:
        why.append(f"太模糊（{r['sharp']:.0f}）")
    if r["bright"] < p["hard_min_bright"]:
        why.append(f"过暗（{r['bright']:.0f}）")
    if r["bright"] > p["hard_max_bright"]:
        why.append(f"过曝（{r['bright']:.0f}）")
    if r["contrast"] < p["hard_min_contrast"]:
        why.append(f"几乎没有内容（对比度 {r['contrast']:.1f}）")
    aspect = max(w, h) / max(1, min(w, h))
    if aspect > p["max_aspect"]:
        why.append(f"长宽比太极端（{aspect:.1f}）")
    if why:
        return "rejected", 0.0, why
    ws = max(1e-9, p["w_res"] + p["w_sharp"] + p["w_bright"] + p["w_contrast"])
    s = (p["w_res"] * min(1.0, min(w, h) / p["target_min_dim"])
         + p["w_sharp"] * min(1.0, r["sharp"] / p["target_sharp"])
         + p["w_bright"] * max(0.0, 1.0 - abs(r["bright"] - 128.0) / 128.0)
         + p["w_contrast"] * min(1.0, r["contrast"] / p["target_contrast"])) / ws * 100
    s = round(s, 1)
    return ("high" if s >= p["high"] else "medium" if s >= p["medium"] else "low"), s, []


def _dup_groups(rows, scores, thr):
    """完全相同（sha256）或几乎一样（差异哈希相差 ≤ thr 位）的照片分组 → {被去掉的: 留下的}"""
    n = len(rows)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_sha = defaultdict(list)
    hashes = [int(r["dhash"], 16) if r["dhash"] else None for r in rows]
    for i, r in enumerate(rows):
        if r["sha"] and not r["error"]:
            by_sha[r["sha"]].append(i)
    for idx in by_sha.values():
        for j in idx[1:]:
            union(idx[0], j)
    if thr > 0:
        bands = defaultdict(list)     # 64 位分 4 段，任意一段相同才比较（相差 ≤ 8 位时至少有一段完全相同的概率很高）
        for i, hv in enumerate(hashes):
            if hv is not None:
                for b in range(4):
                    bands[(b, (hv >> (b * 16)) & 0xFFFF)].append(i)
        for idx in bands.values():
            if len(idx) < 2:
                continue
            if len(idx) > 400:        # 纯色图之类的大桶：只和相邻的比，避免 n² 太慢
                idx = sorted(idx, key=lambda i: hashes[i])
                pairs = ((idx[a], idx[b]) for a in range(len(idx)) for b in range(a + 1, min(a + 30, len(idx))))
            else:
                pairs = ((idx[a], idx[b]) for a in range(len(idx)) for b in range(a + 1, len(idx)))
            for i, j in pairs:
                if bin(hashes[i] ^ hashes[j]).count("1") <= thr:
                    union(i, j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    out = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        best = max(members, key=lambda i: (scores[i], rows[i]["w"] * rows[i]["h"] if rows[i]["w"] else 0))
        for i in members:
            if i != best:
                out[rows[i]["path"]] = rows[best]["path"]
    return out


def classify(job_id):
    """全部照片按当前参数分档 + 去重 + 人工改判 → {path: 记录}（带缓存：参数和数据没变就不重算）"""
    d = _job_dir(job_id)
    job = _read(d)
    p = {**DEFAULTS, **(job.get("params") or {})}
    dbf = d / "items.sqlite3"
    st = dbf.stat() if dbf.exists() else None
    key = (json.dumps(p, sort_keys=True), st.st_mtime_ns if st else 0, st.st_size if st else 0,
           (d / "items.sqlite3-wal").stat().st_mtime_ns if (d / "items.sqlite3-wal").exists() else 0)
    hit = _cls_cache.get(job_id)
    if hit and hit[0] == key:
        return job, hit[1]
    with closing(_db(d)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute("SELECT * FROM items ORDER BY path")]
        over = dict(db.execute("SELECT path, decision FROM overrides").fetchall())
    graded = [_grade(r, p) for r in rows]
    scores = [g[1] for g in graded]
    dups = _dup_groups([r for r in rows], [s if g[0] != "rejected" else -1 for g, s in zip(graded, scores)], int(p["hamming"])) if p["dedup"] else {}
    out = {}
    for r, (tier, score, why) in zip(rows, graded):
        dup = dups.get(r["path"])
        auto = tier in p["export_tiers"] and not dup
        o = over.get(r["path"])
        out[r["path"]] = {"path": r["path"], "tier": tier, "score": score, "why": why, "dup_of": dup, "override": o,
                          "keep": o == "keep" or (o != "drop" and auto), "w": r["w"], "h": r["h"], "size": r["size"],
                          "sharp": round(r["sharp"], 1) if r["sharp"] is not None else None,
                          "bright": round(r["bright"], 1) if r["bright"] is not None else None,
                          "contrast": round(r["contrast"], 1) if r["contrast"] is not None else None}
    _cls_cache[job_id] = (key, out)
    return job, out


def summary(job_id):
    job, rows = classify(job_id)
    cnt = {t: 0 for t in TIERS}
    hist = [0] * 20
    dup = keep = man_keep = man_drop = 0
    for r in rows.values():
        cnt[r["tier"]] += 1
        if r["tier"] != "rejected":
            hist[min(19, int(r["score"] // 5))] += 1
        dup += bool(r["dup_of"])
        keep += r["keep"]
        man_keep += r["override"] == "keep"
        man_drop += r["override"] == "drop"
    job["run"] = _jobs.get(job_id, {})
    with closing(_db(_job_dir(job_id))) as db:
        tr = db.execute("SELECT COUNT(*) FROM trash").fetchone()[0]
    return {"job": job, "total": len(rows), "tiers": cnt, "dup": dup, "keep": keep, "manual_keep": man_keep,
            "manual_drop": man_drop, "drop": len(rows) - keep, "trash": tr, "hist": hist}


# ── 物理删除“不要”的：回收站 → 恢复 / 清空 ───────────────────────────────────────
def _protected():
    """各图库里当过人工样板的照片，不让在初选这里删"""
    out = set()
    for lib_id in imagelib.registry()["libs"]:
        try:
            out.update(imagelib.get(lib_id).confirmed_labels())
        except Exception:
            continue
    return out


def trash_drop(job_id, paths):
    """把这些照片移进本任务的回收站（同盘挪位置，能恢复）"""
    d = _job_dir(job_id)
    if _jobs.get(job_id, {}).get("running"):
        raise ValueError("正在扫描，等扫完或先停止")
    job, rows = classify(job_id)
    src_root = Path(job["source"]).resolve()
    tdir = TRASH / job_id
    prot = _protected()
    links = imagelib.links_map()
    moved, skipped, missing, unlinked_n = 0, 0, 0, 0
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with closing(_db(d)) as db:
        db.row_factory = sqlite3.Row
        for p in dict.fromkeys(paths):
            f = Path(p)
            if p not in rows or not f.is_file() or not f.resolve().is_relative_to(src_root):
                missing += 1
                continue
            if p in prot or any(o in prot for o in links.get(p, ())):
                skipped += 1
                continue
            others = imagelib._other_links(f)
            dst = tdir / f.resolve().relative_to(src_root)
            if dst.exists():
                dst = dst.with_name(f"{dst.stem}__{int(time.time())}{dst.suffix}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dst))
            un = []
            for o in others:            # 已经导出到精选目录的同一个文件也摘掉（恢复时链回去）
                try:
                    o.unlink()
                    un.append(str(o))
                except OSError:
                    pass
            unlinked_n += len(un)
            row = db.execute("SELECT * FROM items WHERE path = ?", (p,)).fetchone()
            db.execute("INSERT OR REPLACE INTO trash (path, trash, unlinked, row, ts) VALUES (?, ?, ?, ?, ?)",
                       (p, str(dst), json.dumps(un, ensure_ascii=False), json.dumps(dict(row) if row else {}, ensure_ascii=False), ts))
            db.execute("DELETE FROM items WHERE path = ?", (p,))
            moved += 1
            if moved % 500 == 0:
                db.commit()
        db.commit()
    _cls_cache.pop(job_id, None)
    msg = f"已移进回收站 {moved} 张"
    if unlinked_n:
        msg += f"（精选目录里的同一张也摘掉了 {unlinked_n} 处）"
    if skipped:
        msg += f"；跳过 {skipped} 张：在图库里当过人工样板，要删请到照片类型页"
    if missing:
        msg += f"；{missing} 张找不到了"
    return {"moved": moved, "skipped": skipped, "missing": missing, "unlinked": unlinked_n, "message": msg}


def trash_items(job_id, offset=0, limit=60):
    with closing(_db(_job_dir(job_id))) as db:
        total = db.execute("SELECT COUNT(*) FROM trash").fetchone()[0]
        rs = db.execute("SELECT path, row, ts FROM trash ORDER BY ts DESC, path LIMIT ? OFFSET ?",
                        (max(1, min(int(limit), 500)), int(offset))).fetchall()
    out = []
    for p, row, ts in rs:
        r = json.loads(row or "{}")
        rnd = lambda k: round(r[k], 1) if r.get(k) is not None else None
        out.append({"path": p, "tier": "trash", "score": None, "why": [f"{ts} 移进回收站"], "dup_of": None, "override": None,
                    "keep": False, "w": r.get("w"), "h": r.get("h"), "size": r.get("size"),
                    "sharp": rnd("sharp"), "bright": rnd("bright"), "contrast": rnd("contrast"), "trashed": ts})
    return {"total": total, "items": out}


def _trash_rows(db, paths):
    if paths is None:
        return db.execute("SELECT path, trash, unlinked, row FROM trash").fetchall()
    out = []
    for i in range(0, len(paths), 500):
        part = paths[i:i + 500]
        out += db.execute(f"SELECT path, trash, unlinked, row FROM trash WHERE path IN ({','.join('?' * len(part))})", part).fetchall()
    return out


def trash_restore(job_id, paths=None):
    """放回原处（精选目录里摘掉的硬链接也链回去），打分记录也恢复，不用重扫"""
    d = _job_dir(job_id)
    n = relinked = fail = 0
    with closing(_db(d)) as db:
        for p, t, un, row in _trash_rows(db, paths):
            src, tf = Path(p), Path(t)
            if src.exists() or not tf.is_file():
                fail += 1
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tf), str(src))
            for o in json.loads(un or "[]"):
                o = Path(o)
                if not o.exists():
                    try:
                        o.parent.mkdir(parents=True, exist_ok=True)
                        os.link(src, o)
                        relinked += 1
                    except OSError:
                        pass
            r = json.loads(row or "{}")
            if r.get("path"):
                cols = list(r)
                db.execute(f"INSERT OR REPLACE INTO items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [r[c] for c in cols])
            db.execute("DELETE FROM trash WHERE path = ?", (p,))
            n += 1
        db.commit()
    _cls_cache.pop(job_id, None)
    msg = f"已恢复 {n} 张" + (f"（精选目录里也链回去 {relinked} 处）" if relinked else "")
    if fail:
        msg += f"；{fail} 张没恢复（原位置已有同名文件，或回收站里的文件不见了）"
    return {"restored": n, "failed": fail, "message": msg}


def trash_purge(job_id, paths=None):
    """永久删除（不能恢复）：删回收站里的文件，释放磁盘空间"""
    d = _job_dir(job_id)
    tdir = (TRASH / job_id).resolve()
    n, freed = 0, 0
    with closing(_db(d)) as db:
        for p, t, _, _ in _trash_rows(db, paths):
            tf = Path(t)
            try:
                if tf.is_file() and tf.resolve().is_relative_to(tdir):
                    st = tf.stat()
                    tf.unlink()
                    if st.st_nlink <= 1:
                        freed += st.st_size
            except OSError:
                continue
            db.execute("DELETE FROM trash WHERE path = ?", (p,))
            n += 1
        db.commit()
    if tdir.is_dir():                   # 清掉空目录
        for dp, dns, fns in sorted(os.walk(tdir), key=lambda x: -len(x[0])):
            if Path(dp) != tdir and not os.listdir(dp):
                try:
                    os.rmdir(dp)
                except OSError:
                    pass
    return {"purged": n, "freed": freed, "message": f"已永久删除 {n} 张，释放 {freed / 2**30:.2f} GB"}


def _select(rows, view):
    if view in TIERS:
        return [r for r in rows.values() if r["tier"] == view]
    if view == "keep":
        return [r for r in rows.values() if r["keep"]]
    if view == "drop":              # 不导出的（全部）
        return [r for r in rows.values() if not r["keep"]]
    if view == "manual_drop":       # 人工标了“不要”的
        return [r for r in rows.values() if r["override"] == "drop"]
    if view == "dup":
        return [r for r in rows.values() if r["dup_of"]]
    if view == "manual":
        return [r for r in rows.values() if r["override"]]
    return list(rows.values())


def resolve_paths(job_id, payload):
    """选中的照片：paths 直接给；或 all = 当前这一栏全部（view），去掉 exclude 里的"""
    if not payload.get("all"):
        return list(payload.get("paths") or [])
    _, rows = classify(job_id)
    ex = set(payload.get("exclude") or [])
    return [r["path"] for r in _select(rows, payload.get("view", "keep")) if r["path"] not in ex]


def items(job_id, view="keep", sort="score_desc", offset=0, limit=60):
    if view == "trash":
        return trash_items(job_id, offset, limit)
    _, rows = classify(job_id)
    sel = _select(rows, view)
    if sort == "score_asc":
        sel.sort(key=lambda r: (r["score"], r["path"]))
    elif sort == "path":
        sel.sort(key=lambda r: r["path"])
    elif view == "dup":
        sel.sort(key=lambda r: (r["dup_of"], r["path"]))
    else:
        sel.sort(key=lambda r: (-r["score"], r["path"]))
    limit = max(1, min(int(limit), 500))
    return {"total": len(sel), "items": sel[offset:offset + limit]}


def set_override(job_id, paths, decision):
    """人工改判：keep = 一定导出，drop = 一定不导出，clear = 回到按分数自动判断"""
    if decision not in ("keep", "drop", "clear"):
        raise ValueError("decision 只能是 keep / drop / clear")
    d = _job_dir(job_id)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with closing(_db(d)) as db:
        if decision == "clear":
            db.executemany("DELETE FROM overrides WHERE path = ?", [(p,) for p in paths])
        else:
            db.executemany("INSERT OR REPLACE INTO overrides (path, decision, ts) VALUES (?, ?, ?)", [(p, decision, ts) for p in paths])
        db.commit()
    _cls_cache.pop(job_id, None)
    return {"changed": len(paths), "decision": decision}


# ── 导出：硬链接到精选目录 ───────────────────────────────────────────────────
def export(engine, job_id, target, dry_run=False):
    job, rows = classify(job_id)
    src_root = Path(job["source"]).resolve()
    tgt = Path(target or "").expanduser()
    if not tgt.is_absolute():
        raise ValueError("精选目录要写绝对路径，如 /data/images/精选/demo")
    tgt = tgt.resolve()
    if tgt == src_root or tgt.is_relative_to(src_root):
        raise ValueError("精选目录不能放在源目录里面（下次扫描会把精选的又扫进来）")
    if src_root.is_relative_to(tgt):
        raise ValueError("精选目录不能是源目录的上级目录")
    parent = next((p for p in [tgt, *tgt.parents] if p.exists()), None)
    engine.allowed_source(parent, directory=True)       # 必须在允许的图片目录里
    keep = [r["path"] for r in rows.values() if r["keep"]]
    plan, exists = [], 0
    for p in keep:
        dst = tgt / Path(p).resolve().relative_to(src_root)
        if dst.exists():
            exists += 1
        else:
            plan.append((Path(p), dst))
    if dry_run:
        return {"dry_run": True, "keep": len(keep), "new": len(plan), "already": exists, "target": str(tgt)}
    linked = copied = failed = 0
    done = []
    for s, dst in plan:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(s, dst)
                linked += 1
            except OSError:                 # 跨文件系统不能硬链接：复制
                shutil.copy2(s, dst)
                copied += 1
            done.append((dst, s))
        except OSError:
            failed += 1
    if done:
        imagelib.record_links([(dst, s) for dst, s in done if os.path.samefile(dst, s)])
    d = _job_dir(job_id)
    job = _read(d)
    job.setdefault("exports", []).append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "target": str(tgt), "linked": linked,
                                          "copied": copied, "already": exists, "failed": failed, "keep": len(keep)})
    job["last_target"] = str(tgt)
    _write(d, job)
    return {"keep": len(keep), "linked": linked, "copied": copied, "already": exists, "failed": failed, "target": str(tgt)}


# ── 浏览目录（选源目录 / 精选目录用） ─────────────────────────────────────────
def list_dirs(engine, path=""):
    roots = [r for r in engine.source_roots if Path(r).is_dir()]
    if not path:
        return {"path": "", "parent": None, "dirs": [{"name": str(r), "path": str(r)} for r in roots]}
    p = engine.allowed_source(path, directory=True)
    dirs, n_img = [], 0
    with os.scandir(p) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                dirs.append({"name": e.name, "path": str(Path(p) / e.name)})
            elif os.path.splitext(e.name)[1].lower() in IMAGE_EXT:
                n_img += 1
    dirs.sort(key=lambda x: x["name"])
    parent = str(p.parent) if any(p.parent.is_relative_to(r) for r in roots) else ""
    return {"path": str(p), "parent": parent, "dirs": dirs[:500], "images_here": n_img}


def thumb(path, size=320):
    from PIL import Image, ImageOps
    src = Path(path)
    size = max(96, min(int(size), 2000))
    key = hashlib.sha1(f"{path}|{size}".encode()).hexdigest()
    out = THUMBS / key[:2] / f"{key}.jpg"
    if not out.is_file() or out.stat().st_mtime < src.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.draft("RGB", (size * 2, size * 2))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((size, size))
            im.save(out, "JPEG", quality=85)
    return out


def _in_job(job_id, path):
    job = get_job(job_id)
    p = Path(path).resolve()
    if p.is_file() and p.is_relative_to(Path(job["source"]).resolve()):
        return p
    with closing(_db(_job_dir(job_id))) as db:          # 回收站里的
        r = db.execute("SELECT trash FROM trash WHERE path = ?", (path,)).fetchone()
    t = Path(r[0]).resolve() if r else None
    if t and t.is_file() and t.is_relative_to((TRASH / job_id).resolve()):
        return t
    raise FileNotFoundError("找不到这张图")


def register_routes(router, engine):
    from fastapi import Body, HTTPException
    from fastapi.responses import FileResponse
    import mimetypes

    def guard(fn, *a):
        try:
            return fn(*a)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))

    @router.get("/prefilter/jobs", tags=["prefilter"])
    def pf_jobs():
        return list_jobs()

    @router.post("/prefilter/jobs", tags=["prefilter"])
    def pf_create(payload: dict = Body(...)):
        return guard(create_job, engine, payload.get("source", ""), payload.get("name", ""))

    @router.get("/prefilter/jobs/{job_id}", tags=["prefilter"])
    def pf_summary(job_id: str):
        return guard(summary, job_id)

    @router.delete("/prefilter/jobs/{job_id}", tags=["prefilter"])
    def pf_delete(job_id: str):
        return guard(delete_job, job_id)

    @router.post("/prefilter/jobs/{job_id}/params", tags=["prefilter"])
    def pf_params(job_id: str, payload: dict = Body(...)):
        guard(set_params, job_id, payload)
        return summary(job_id)

    @router.post("/prefilter/jobs/{job_id}/scan", tags=["prefilter"])
    def pf_scan(job_id: str, payload: dict = Body(default={})):
        return guard(lambda: start_scan(job_id, engine=engine, auto_export=bool((payload or {}).get("auto_export"))))

    @router.post("/prefilter/jobs/{job_id}/stop", tags=["prefilter"])
    def pf_stop(job_id: str):
        return stop_scan(job_id)

    @router.get("/prefilter/jobs/{job_id}/items", tags=["prefilter"])
    def pf_items(job_id: str, view: str = "keep", sort: str = "score_desc", offset: int = 0, limit: int = 60):
        return guard(items, job_id, view, sort, offset, limit)

    @router.post("/prefilter/jobs/{job_id}/override", tags=["prefilter"])
    def pf_override(job_id: str, payload: dict = Body(...)):
        return guard(lambda: set_override(job_id, resolve_paths(job_id, payload), payload.get("decision", "")))

    @router.post("/prefilter/jobs/{job_id}/trash", tags=["prefilter"])
    def pf_trash(job_id: str, payload: dict = Body(...)):
        """移进回收站：paths，或 all + view（manual_drop = 人工标了不要的，drop = 全部不导出的）"""
        return guard(lambda: trash_drop(job_id, resolve_paths(job_id, payload)))

    @router.post("/prefilter/jobs/{job_id}/trash/restore", tags=["prefilter"])
    def pf_trash_restore(job_id: str, payload: dict = Body(default={})):
        p = payload or {}
        return guard(lambda: trash_restore(job_id, None if p.get("all") else list(p.get("paths") or [])))

    @router.post("/prefilter/jobs/{job_id}/trash/purge", tags=["prefilter"])
    def pf_trash_purge(job_id: str, payload: dict = Body(default={})):
        p = payload or {}
        return guard(lambda: trash_purge(job_id, None if p.get("all") else list(p.get("paths") or [])))

    @router.post("/prefilter/jobs/{job_id}/export", tags=["prefilter"])
    def pf_export(job_id: str, payload: dict = Body(...)):
        return guard(export, engine, job_id, payload.get("target", ""), bool(payload.get("dry_run")))

    @router.get("/prefilter/dirs", tags=["prefilter"])
    def pf_dirs(path: str = ""):
        return guard(list_dirs, engine, path)

    @router.get("/prefilter/jobs/{job_id}/thumb", tags=["prefilter"])
    def pf_thumb(job_id: str, path: str, size: int = 320):
        return FileResponse(thumb(guard(_in_job, job_id, path), size), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/prefilter/jobs/{job_id}/original", tags=["prefilter"])
    def pf_original(job_id: str, path: str):
        p = guard(_in_job, job_id, path)
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
