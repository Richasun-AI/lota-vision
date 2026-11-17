"""图库：一个图片目录 + 它自己的照片类型清单、SigLIP2 特征索引、人工确认（样板）、归类结果、回收站。

照片类型页一次只看一个图库：通信巡检、物流分拣、水果分拣……各管各的目录和类型，切换后别的图库的照片和类型都看不到。
登记表在 {HOME}/imagelibs.json；新图库的数据放在 {HOME}/imagelibs/<代号>/。
旧照片归类流水线（LOTA_LEGACY_PIPELINE/work）的数据作为一个“沿用旧数据”的图库登记进来：数据文件原地不动、格式不变。

删除 = 移进本图库的回收站（可恢复）。初选页导出的精选目录是硬链接：删精选目录里的一张时，原目录里同一个文件
（初选导出时记在 {HOME}/prefilter/links.jsonl）也一起摘掉，磁盘空间才能在“永久删除”后真正释放；恢复时两处都链回去。
"""
import csv
import json
import os
import re
import shutil
import threading
import time
from collections import Counter
from pathlib import Path

from . import config as deploy

os.environ.setdefault("HF_HUB_OFFLINE", "1")      # SigLIP2 模型已缓存在本机，不联网
REGISTRY = deploy.HOME / "imagelibs.json"
LIBS_DIR = deploy.HOME / "imagelibs"
LINKS = deploy.HOME / "prefilter" / "links.jsonl"     # 初选导出的硬链接：精选目录里的文件 → 原图
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
BIN = "other"            # 临时仓库：暂时找不到归属的图；不参与训练；重新训练时会被重新预测
BIN_THRESHOLD = 0.35
MODEL_ID = "google/siglip2-base-patch16-224"
_reg_lock = threading.RLock()
_gpu_lock = threading.Lock()      # 建索引、训练同一时间只跑一个（显存只有 8GB）
_model = {"m": None, "p": None, "dev": None}
_libs = {}                        # 代号 → Library（带缓存）


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ts(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return 0.0


def _jsonl(path):
    if not Path(path).is_file():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _append(path, *recs):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── 登记表 ───────────────────────────────────────────────────────────────
def _bootstrap():
    """第一次用：有旧流水线数据就把它登记成第一个图库（沿用旧数据，原地不动）"""
    legacy = deploy.LEGACY_PIPELINE
    libs = {}
    if (legacy / "work").is_dir():
        libs["legacy"] = {"name": "照片类型（旧数据）", "root": str(deploy.IMAGES_DIR), "work": str(legacy / "work"),
                          "base_yaml": next((str(p) for p in legacy.glob("*.yaml") if p.name != "自定义类型.yaml"), ""),
                          "custom_yaml": str(legacy / "自定义类型.yaml"), "trash": str(deploy.DATA_ROOT / "_deleted"),
                          "candidates_csv": str(legacy / "work" / "candidates.csv"), "created": _now()}
    return {"default": next(iter(libs), ""), "libs": libs}


def registry():
    with _reg_lock:
        if not REGISTRY.is_file():
            reg = _bootstrap()
            if reg["libs"]:
                _save_registry(reg)
            return reg
        return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _save_registry(reg):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, REGISTRY)


def list_libs():
    reg = registry()
    out = []
    for k, v in reg["libs"].items():
        lib = get(k)
        out.append({"id": k, "name": v.get("name", k), "root": v["root"], "default": k == reg.get("default"),
                    "legacy": bool(v.get("candidates_csv")), "created": v.get("created", ""), "index": lib.index_status(quick=True),
                    "exists": Path(v["root"]).is_dir()})
    return {"default": reg.get("default", ""), "libs": out}


def get(lib_id=""):
    reg = registry()
    lib_id = lib_id or reg.get("default") or ""
    if lib_id not in reg["libs"]:
        if not reg["libs"]:
            raise ValueError("还没有图库：先在“照片类型”页新建一个（选一个图片目录）")
        raise ValueError(f"没有这个图库：{lib_id}")
    conf = reg["libs"][lib_id]
    lib = _libs.get(lib_id)
    if lib is None or lib.conf != conf:
        lib = _libs[lib_id] = Library(lib_id, conf)
    return lib


def create(name, root, engine=None, types=None):
    """新建图库：root = 图片目录（通常是初选导出的精选目录）。types = [{"name","name_en"}...] 可选，先建好一批类型"""
    name = (name or "").strip()
    if not name:
        raise ValueError("请给图库起个名字，如：水果分拣")
    rootp = Path(root or "").expanduser()
    if engine is not None:
        rootp = engine.allowed_source(rootp, directory=True)
    elif not rootp.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")
    rootp = rootp.resolve()
    with _reg_lock:
        reg = registry()
        for k, v in reg["libs"].items():
            if rootp in [Path(x).resolve() for x in [v["root"], *v.get("merged_roots", [])]]:
                raise ValueError(f"这个目录已经在图库“{v.get('name', k)}”里了")
        base = re.sub(r"[^a-z0-9_]+", "_", (Path(rootp).name or "lib").lower()).strip("_")[:24] or "lib"
        lib_id, n = base if re.match(r"[a-z]", base) else "lib_" + base, 1
        while lib_id in reg["libs"]:
            n += 1
            lib_id = f"{base}_{n}"
        work = LIBS_DIR / lib_id
        work.mkdir(parents=True, exist_ok=True)
        conf = {"name": name, "root": str(rootp), "work": str(work), "custom_yaml": str(work / "types.yaml"),
                "trash": str(work / "trash"), "created": _now()}
        reg["libs"][lib_id] = conf
        if not reg.get("default"):
            reg["default"] = lib_id
        _save_registry(reg)
    lib = get(lib_id)
    for t in types or []:
        lib.add_type(t.get("name", ""), t.get("name_en", ""), t.get("key", ""))
    return {"id": lib_id, **conf}


def update(lib_id, name=None, make_default=False):
    with _reg_lock:
        reg = registry()
        if lib_id not in reg["libs"]:
            raise ValueError(f"没有这个图库：{lib_id}")
        if name is not None and name.strip():
            reg["libs"][lib_id]["name"] = name.strip()
        if make_default:
            reg["default"] = lib_id
        _save_registry(reg)
    return reg["libs"][lib_id]


def remove(lib_id):
    """只从登记表里去掉（图片、索引、样板都不删，数据目录原地保留，重新新建同一目录不会恢复样板）"""
    with _reg_lock:
        reg = registry()
        if lib_id not in reg["libs"]:
            raise ValueError(f"没有这个图库：{lib_id}")
        conf = reg["libs"].pop(lib_id)
        if reg.get("default") == lib_id:
            reg["default"] = next(iter(reg["libs"]), "")
        _save_registry(reg)
    _libs.pop(lib_id, None)
    return {"removed": lib_id, "kept_data": conf["work"]}


# ── 合并图库：把几个图库并进一个主图库 ────────────────────────────────────────
def _fmt_ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else _now()


def _merge_one(M, S, add_conf):
    """把图库 S 的类型、样板、删除记录、特征索引并进 M；S 的照片原地不动，M 改为同时管理 S 的目录"""
    import numpy as np
    import yaml
    # 1) 类型：同名的并成一类；代号撞了但名字不同的换个代号
    mtax = dict(M.taxonomy)
    by_name = {v.get("name"): k for k, v in mtax.items()}
    kmap, added = {BIN: BIN}, []
    doc = (yaml.safe_load(M.custom_yaml.read_text(encoding="utf-8")) or {}) if M.custom_yaml.is_file() else {}
    wrapped = isinstance(doc, dict) and "photo_types" in doc
    custom = (doc.setdefault("photo_types", {}) or {}) if wrapped else doc
    if wrapped:
        doc["photo_types"] = custom
    for k, v in S.taxonomy.items():
        if k == BIN:
            continue
        if v.get("name") in by_name:
            kmap[k] = by_name[v["name"]]
            continue
        nk, n = k, 1
        while nk in mtax:
            n += 1
            nk = f"{k}_{n}"
        mtax[nk] = custom[nk] = {**v, "source": f"合并自图库“{S.name}”", "added": time.strftime("%Y-%m-%d %H:%M")}
        by_name[v.get("name")] = nk
        kmap[k] = nk
        added.append(v.get("name") or nk)
    if added:
        M.custom_yaml.parent.mkdir(parents=True, exist_ok=True)
        M.custom_yaml.write_text(f"# 图库“{M.name}”网页上新增 / 合并进来的照片类型\n"
                                 + yaml.safe_dump(doc if wrapped else custom, allow_unicode=True, sort_keys=False), encoding="utf-8")
    prompts = S.clip_prompts()
    if prompts:
        mp = M.clip_prompts()
        mp.update({kmap[k]: v for k, v in prompts.items() if k in kmap and kmap[k] not in mp})
        (M.work / "clip_prompts.json").write_text(json.dumps(mp, ensure_ascii=False, indent=1), encoding="utf-8")

    # 2) 删除记录（回收站文件留在原处，M 也能恢复 / 永久删除）
    mdl = M.load_deleted()
    recs = []
    for p, r in S.load_deleted().items():
        if p in mdl:
            continue
        recs.append({k: v for k, v in r.items() if k != "purged"})
        if r.get("purged"):
            recs.append({"action": "purge", "path": p, "ts": r.get("ts", _now())})
    if recs:
        _append(M.deleted_file, *recs)

    # 3) 人工确认的样板：主图库已经确认过的同一张照片以主图库为准
    mine = M.confirmed_labels()
    labels = [{"path": p, "photo_type": kmap.get(t, BIN), "source": f"合并自图库“{S.name}”", "ts": _fmt_ts(ts)}
              for p, (t, ts) in S.confirmed_labels(with_ts=True).items() if p not in mine]
    if labels:
        with M.lock:
            _append(M.type_labels, *labels)

    # 4) 旧流水线的 qwen 结果、位置表（有才并）
    if S.results_file.is_file() and S.results_file.resolve() != M.results_file.resolve():
        have = M.load_results()
        _append(M.results_file, *(d for p, d in S.load_results().items() if p not in have))
    sloc = S.load_loc()
    if sloc and S.loc_file.resolve() != M.loc_file.resolve():
        loc = {**sloc, **M.load_loc()}
        M.loc_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = M.loc_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(loc, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, M.loc_file)

    # 5) 特征索引：直接搬分片，不用重算
    have = set(M.load_index()[0])
    M.emb.mkdir(parents=True, exist_ok=True)
    k = max([int(s.stem.split("_")[1]) for s in M._shards()] or [-1]) + 1
    moved = 0
    for sh in S._shards():
        with np.load(sh, allow_pickle=False) as z:
            ps, fs = [str(x) for x in z["paths"]], z["feats"]
        keep = [i for i, p in enumerate(ps) if p not in have]
        if keep:
            np.savez(M.emb / f"shard_{k:05d}.npz", paths=np.array([ps[i] for i in keep]), feats=fs[keep])
            k += 1
            moved += len(keep)
            have.update(ps[i] for i in keep)

    # 6) 目录：新图库按目录扫；旧流水线图库只认它的候选清单（目录里还有没初选过的原图，不能整个扫进来）
    if S.cands_csv:
        lst = M.work / f"merged_{S.id}.txt"
        lst.write_text("\n".join(S.candidates()), encoding="utf-8")
        add_conf["merged_lists"].append(str(lst))
        add_conf["access_roots"] += [str(S.root), *map(str, S.extra_roots)]
    else:
        add_conf["merged_roots"] += [str(S.root), *map(str, S.merged_roots)]
        add_conf["access_roots"] += [str(x) for x in S.extra_roots]
    add_conf["merged_lists"] += [str(x) for x in S.merged_lists]
    add_conf["access_roots"] += [str(x) for x in S.access_roots]
    add_conf["extra_trash"] += [str(S.trash), *map(str, S.extra_trash)]
    return {"id": S.id, "name": S.name, "root": str(S.root), "types_added": added,
            "types_merged": len(kmap) - 1 - len(added),
            "labels": len(labels), "deleted": len(recs), "index_moved": moved, "ts": _now()}


def merge(main_id, other_ids):
    """把 other_ids 这几个图库并进主图库 main_id：类型、样板、删除记录、特征索引都并过去，
    照片原地不动（主图库改为同时管理它们的目录）；被并的图库从列表里去掉，数据目录保留。并完自动重新训练。"""
    reg = registry()
    if main_id not in reg["libs"]:
        raise ValueError(f"没有这个图库：{main_id}")
    others = [i for i in dict.fromkeys(other_ids or []) if i != main_id]
    if not others:
        raise ValueError("请至少选一个要并进主图库的图库")
    for i in others:
        if i not in reg["libs"]:
            raise ValueError(f"没有这个图库：{i}")
    libs = [get(i) for i in [main_id, *others]]
    if any(l.job["running"] for l in libs) or not _gpu_lock.acquire(blocking=False):
        raise ValueError("有图库正在建索引或训练，等它跑完再合并")
    try:
        M = libs[0]
        add = {"merged_roots": [], "merged_lists": [], "access_roots": [], "extra_trash": []}
        done = [_merge_one(M, S, add) for S in libs[1:]]
        with _reg_lock:
            reg = registry()
            conf = reg["libs"][main_id]
            for key, vals in add.items():
                cur = conf.get(key, [])
                conf[key] = cur + [v for v in dict.fromkeys(vals) if v not in cur and v != conf["root"]]
            conf.setdefault("merged_from", []).extend(done)
            for i in others:
                reg["libs"].pop(i, None)
                _libs.pop(i, None)
            if reg.get("default") in others:
                reg["default"] = main_id
            _save_registry(reg)
        M = get(main_id)
        if M.scan_roots():
            M.scan_files()
        _append(M.work / "merge_log.jsonl", *done)
    finally:
        _gpu_lock.release()
    msg = f"已把 {len(done)} 个图库并进“{M.name}”：样板 +{sum(d['labels'] for d in done)} 张，特征索引 +{sum(d['index_moved'] for d in done)} 张"
    added = [t for d in done for t in d["types_added"]]
    if added:
        msg += f"，新增类型 {len(added)} 个"
    try:
        M.start_job("train")
        msg += "；正在重新训练归类"
    except ValueError as e:
        msg += f"；没能自动训练（{e}），稍后手动点“重新训练并归类全部”"
    return {"main": main_id, "merged": done, "message": msg, "index": M.index_status(quick=True)}


# ── 硬链接登记（初选导出时写） ──────────────────────────────────────────────
_links_cache = {"key": None, "map": {}}


def record_links(pairs):
    _append(LINKS, *({"path": str(d), "source": str(s), "ts": _now()} for d, s in pairs))


def links_map():
    try:
        st = LINKS.stat()
    except FileNotFoundError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    if _links_cache["key"] != key:
        m = {}
        for r in _jsonl(LINKS):
            m.setdefault(r["path"], set()).add(r["source"])
            m.setdefault(r["source"], set()).add(r["path"])
        _links_cache.update(key=key, map=m)
    return _links_cache["map"]


def _other_links(f):
    """同一个文件（同一 inode）的其他硬链接：只认初选导出登记过的"""
    try:
        st = f.stat()
    except OSError:
        return []
    if st.st_nlink < 2:
        return []
    out = []
    for p in links_map().get(str(f), ()):
        q = Path(p)
        try:
            if q != f and q.is_file() and os.path.samefile(q, f):
                out.append(q)
        except OSError:
            continue
    return out


# ── SigLIP2 模型（所有图库共用一份） ──────────────────────────────────────────
def _get_model():
    import torch
    from transformers import AutoModel, AutoProcessor
    try:    # 服务器连不上 huggingface.co：模型已缓存，强制离线（transformers 导入时就读了开关，引擎进程里设环境变量太晚）
        import transformers.utils.hub as _th
        _th._is_offline_mode = True
    except (ImportError, AttributeError):
        pass
    if _model["m"] is None:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        _model["m"] = AutoModel.from_pretrained(MODEL_ID, dtype=torch.float16 if dev == "cuda" else torch.float32,
                                                local_files_only=True).to(dev).eval()
        _model["p"] = AutoProcessor.from_pretrained(MODEL_ID, local_files_only=True)
        _model["dev"] = dev
    return _model["m"], _model["p"], _model["dev"]


def free_model():
    _model.update(m=None, p=None)
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def _vec(out):
    return out.pooler_output if hasattr(out, "pooler_output") else out


def _load_img(p):
    from PIL import Image, ImageOps
    try:
        with Image.open(p) as im:
            im.draft("RGB", (448, 448))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((448, 448))
            return im.copy()
    except Exception:
        return None


def _embed_images(files, bs=64):
    import numpy as np
    import torch
    from concurrent.futures import ThreadPoolExecutor
    m, proc, dev = _get_model()
    feats, ok = [], []
    with torch.no_grad(), ThreadPoolExecutor(8) as ex:
        for i in range(0, len(files), bs):
            ims = list(ex.map(_load_img, files[i:i + bs]))
            valid = [j for j, im in enumerate(ims) if im is not None]
            row = np.zeros((len(ims), 768), np.float32)
            if valid:
                x = proc(images=[ims[j] for j in valid], return_tensors="pt")
                x = {k: (v.to(dev, dtype=m.dtype) if v.is_floating_point() else v.to(dev)) for k, v in x.items()}
                f = torch.nn.functional.normalize(_vec(m.get_image_features(**x)).float(), dim=-1).cpu().numpy()
                row = np.zeros((len(ims), f.shape[1]), np.float32)
                row[valid] = f
            feats.append(row)
            ok += [j in valid for j in range(len(ims))]
    return np.concatenate(feats), np.array(ok)


def _embed_texts(texts):
    import torch
    m, proc, dev = _get_model()
    with torch.no_grad():
        x = proc(text=[f"this is a photo of {t}." for t in texts], padding="max_length", max_length=64, return_tensors="pt")
        x = {k: v.to(dev) for k, v in x.items()}
        f = _vec(m.get_text_features(**x))
        return torch.nn.functional.normalize(f.float(), dim=-1).cpu().numpy()


# ── 图库 ────────────────────────────────────────────────────────────────
class Library:
    def __init__(self, lib_id, conf):
        self.id, self.conf = lib_id, dict(conf)
        self.name = conf.get("name", lib_id)
        self.root = Path(conf["root"])
        self.work = Path(conf["work"])
        self.base_yaml = Path(conf["base_yaml"]) if conf.get("base_yaml") else None
        self.custom_yaml = Path(conf["custom_yaml"])
        self.trash = Path(conf["trash"])
        self.extra_roots = [Path(p) for p in conf.get("extra_roots", [])]
        # 合并进来的别的图库：merged_roots = 要扫描的目录；merged_lists = 旧流水线图库的候选清单（只认清单里的图）；
        # access_roots = 那些清单所在的目录（只用来放行读取和删除）；extra_trash = 它们原来的回收站
        self.merged_roots = [Path(p) for p in conf.get("merged_roots", [])]
        self.merged_lists = [Path(p) for p in conf.get("merged_lists", [])]
        self.access_roots = [Path(p) for p in conf.get("access_roots", [])]
        self.extra_trash = [Path(p) for p in conf.get("extra_trash", [])]
        self.cands_csv = Path(conf["candidates_csv"]) if conf.get("candidates_csv") else None
        self.emb = self.work / "emb"
        self.type_labels = self.work / "type_labels.jsonl"
        self.pred_file = self.emb / "pred.json"
        self.report_file = self.emb / "train_report.json"
        self.corr_file = self.work / "corrections.jsonl"
        self.deleted_file = self.work / "deleted.jsonl"
        self.results_file = self.work / "vlm_results.jsonl"
        self.loc_file = self.work / "locations.json"
        self.files_list = self.work / "files.txt"
        self.suggest_file = self.work / "type_suggestions.jsonl"
        self.lock = threading.Lock()
        self.job = {"kind": None, "running": False, "started": None, "finished": None, "done": 0, "total": 0,
                    "result": None, "error": None, "message": ""}
        self._tax_key, self._tax = None, {}
        self._idx = {"key": None, "paths": [], "X": None, "pos": {}}
        self._cache = {}

    # 带文件修改时间的缓存：页面一刷新就读好几个大文件，没变就不重读
    def _cached(self, name, files, fn):
        key = tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) if f.exists() else (str(f), 0, 0) for f in files)
        hit = self._cache.get(name)
        if hit and hit[0] == key:
            return hit[1]
        val = fn()
        self._cache[name] = (key, val)
        return val

    # ── 类型清单 ──
    @property
    def taxonomy(self):
        import yaml
        files = [f for f in (self.base_yaml, self.custom_yaml) if f]

        def load():
            t = {}
            if self.base_yaml and self.base_yaml.is_file():
                t = dict((yaml.safe_load(self.base_yaml.read_text(encoding="utf-8")) or {}).get("photo_types") or {})
            if self.custom_yaml.is_file():
                custom = yaml.safe_load(self.custom_yaml.read_text(encoding="utf-8")) or {}
                t.update(custom.get("photo_types", custom) if isinstance(custom, dict) else {})
            other = t.pop(BIN, None) or {"name": "其他/无关", "name_en": "Other / Unrelated"}
            t[BIN] = other                  # 临时仓库总在最后
            return t
        return self._cached("taxonomy", files, load)

    def add_type(self, name, name_en="", key="", must_have=None, anomalies=None, anomalies_en=None):
        import yaml
        name, key, name_en = (name or "").strip(), (key or "").strip().lower(), (name_en or "").strip()
        if not name:
            raise ValueError("请填写中文名")
        if not name_en:
            raise ValueError("请填写英文名（中英文都要有）")
        anomalies, anomalies_en = anomalies or [], anomalies_en or []
        if anomalies_en and len(anomalies_en) != len(anomalies):
            raise ValueError(f"常见异常的中文 {len(anomalies)} 项、英文 {len(anomalies_en)} 项，数量要一一对应")
        tax = self.taxonomy
        if any(v.get("name") == name for v in tax.values()):
            raise ValueError(f"已经有同名类型：{name}")
        if not key:
            key = re.sub(r"[^a-z0-9_]+", "_", name_en.lower()).strip("_")[:30]
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", key or "") or key in tax:
                n = 1
                while f"custom_{n}" in tax:
                    n += 1
                key = f"custom_{n}"
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", key):
            raise ValueError("英文代号只能用小写字母、数字、下划线，并以字母开头（如 generator）")
        if key in tax:
            raise ValueError(f"英文代号已存在：{key}")
        with self.lock:
            custom = (yaml.safe_load(self.custom_yaml.read_text(encoding="utf-8")) or {}) if self.custom_yaml.is_file() else {}
            custom[key] = {"name": name, "name_en": name_en, "devices": {}, "must_have": must_have or [],
                           "anomalies": anomalies, "anomalies_en": anomalies_en or [""] * len(anomalies),
                           "added": time.strftime("%Y-%m-%d %H:%M"), "source": "网页新增"}
            self.custom_yaml.parent.mkdir(parents=True, exist_ok=True)
            self.custom_yaml.write_text(f"# 图库“{self.name}”网页上新增的照片类型\n"
                                        + yaml.safe_dump(custom, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return {"key": key, "message": f"已新增照片类型：{name} / {name_en}（{key}）"}

    def clip_prompts(self):
        f = self.work / "clip_prompts.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}

    # ── 删除记录、位置表、旧流水线的人工更正和 qwen 结果 ──
    def load_deleted(self):
        def load():
            st = {}
            for d in _jsonl(self.deleted_file):
                a, p = d.get("action"), d.get("path")
                if a == "restore":
                    st.pop(p, None)
                elif a == "delete":
                    st[p] = d
                elif a == "purge" and p in st:
                    st[p]["purged"] = True
            return st
        return dict(self._cached("deleted", [self.deleted_file], load))

    def _log_deleted(self, rec):
        rec["ts"] = _now()
        _append(self.deleted_file, rec)

    def load_loc(self):
        return self._cached("loc", [self.loc_file],
                            lambda: json.loads(self.loc_file.read_text(encoding="utf-8") or "{}") if self.loc_file.is_file() else {})

    def locate(self, path):
        """登记路径 → 文件现在实际在哪（旧流水线把部分图移进过数据集，位置表记着）"""
        if Path(path).exists():
            return path
        cur = self.load_loc().get(path)
        return cur if cur and Path(cur).exists() else path

    def load_corrections(self):
        def load():
            corr = {}
            for d in _jsonl(self.corr_file):
                if "path" not in d:
                    continue
                if all(d.get(k) is None for k in ("keep", "photo_type", "objects", "anomalies")):
                    corr.pop(d["path"], None)
                else:
                    corr[d["path"]] = d
            return corr
        return self._cached("corr", [self.corr_file], load)

    def load_results(self):
        def load():
            out = {}
            for d in _jsonl(self.results_file):
                if d.get("path"):
                    out[d["path"]] = d
            return out
        return self._cached("results", [self.results_file], load)

    # ── 候选图片 ──
    def scan_roots(self):
        """要扫描的目录：本图库目录（旧流水线图库按候选清单，不扫）+ 合并进来的目录；去掉被别的目录包含的"""
        roots = ([] if self.cands_csv else [self.root]) + self.merged_roots
        out = []
        for r in roots:
            if not any(r == o or r.is_relative_to(o) for o in out):
                out = [o for o in out if not o.is_relative_to(r)] + [r]
        return out

    def scan_files(self):
        """扫目录里全部图片（不含隐藏目录），记到 files.txt"""
        out, seen = [], set()
        for root in self.scan_roots():
            for dp, dns, fns in os.walk(root):
                dns[:] = sorted(d for d in dns if not d.startswith("."))
                for f in sorted(fns):
                    p = os.path.join(dp, f)
                    if os.path.splitext(f)[1].lower() in IMAGE_EXT and p not in seen:
                        seen.add(p)
                        out.append(p)
        self.work.mkdir(parents=True, exist_ok=True)
        tmp = self.files_list.with_suffix(".tmp")
        tmp.write_text("\n".join(out), encoding="utf-8")
        os.replace(tmp, self.files_list)
        return out

    def candidates(self, rescan=False):
        dl = self.load_deleted()
        parts = []
        if self.cands_csv:
            parts.append(self._cached("cands", [self.cands_csv],
                                      lambda: [c["path"] for c in csv.DictReader(open(self.cands_csv, encoding="utf-8"))]))
        if self.scan_roots():
            if rescan or not self.files_list.is_file():
                self.scan_files()
            parts.append(self._cached("files", [self.files_list],
                                      lambda: [x for x in self.files_list.read_text(encoding="utf-8").splitlines() if x]))
        for i, lst in enumerate(self.merged_lists):
            parts.append(self._cached(f"mlist{i}", [lst],
                                      lambda lst=lst: [x for x in lst.read_text(encoding="utf-8").splitlines() if x] if lst.is_file() else []))
        if len(parts) == 1:
            return [p for p in parts[0] if p not in dl]
        out, seen = [], set(dl)
        for rows in parts:
            for p in rows:
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    # ── 特征索引 ──
    def _shards(self):
        return sorted(self.emb.glob("shard_*.npz"))

    def load_index(self):
        import numpy as np
        paths, feats = [], []
        for s in self._shards():
            with np.load(s, allow_pickle=False) as z:
                paths += [str(x) for x in z["paths"]]
                feats.append(z["feats"].astype(np.float32))
        return paths, (np.concatenate(feats) if feats else np.zeros((0, 768), np.float32))

    def cached_index(self):
        key = tuple((s.name, s.stat().st_mtime_ns) for s in self._shards())
        if self._idx["key"] != key:
            paths, X = self.load_index()
            self._idx.update(key=key, paths=paths, X=X, pos={p: i for i, p in enumerate(paths)})
        return self._idx["paths"], self._idx["X"], self._idx["pos"]

    def indexed_paths(self):
        return self.cached_index()[0]

    def index_status(self, quick=False):
        """quick = 列图库清单用：索引还没加载进内存时只数分片里的路径，不加载特征"""
        dl = self.load_deleted()
        if quick and self._idx["key"] is None:
            import numpy as np
            done = 0
            for s in self._shards():
                with np.load(s, allow_pickle=False) as z:
                    done += sum(1 for p in z["paths"] if str(p) not in dl)
        else:
            done = sum(1 for p in self.indexed_paths() if p not in dl)
        total = None
        if self.cands_csv or self.files_list.is_file():
            total = len(self.candidates())
        return {"indexed": done, "total": total}

    def build_index(self, chunk=512):
        """断点续跑：重新扫一遍目录，只给还没进索引的图算特征；目录里已经没有的图从索引里去掉"""
        import numpy as np
        self.emb.mkdir(parents=True, exist_ok=True)
        cands = self.candidates(rescan=True)
        alive = set(cands)
        paths, _ = self.load_index()
        gone = [p for p in paths if p not in alive and not Path(self.locate(p)).is_file()]
        if gone and not self.cands_csv:                 # 新图库：目录里删掉的图，索引里也去掉
            gone_set = set(gone)
            for s in self._shards():
                with np.load(s, allow_pickle=False) as z:
                    ps, fs = [str(x) for x in z["paths"]], z["feats"]
                keep = [i for i, p in enumerate(ps) if p not in gone_set]
                if len(keep) != len(ps):
                    if keep:
                        np.savez(s, paths=np.array([ps[i] for i in keep]), feats=fs[keep])
                    else:
                        s.unlink()
        done = set(self.load_index()[0])
        todo = [p for p in cands if p not in done]
        self.job.update(done=0, total=len(todo), message=f"要算特征 {len(todo)} 张（已有 {len(done)} 张）")
        if not todo:
            return f"索引已是最新：{len(done)} 张" + (f"；去掉目录里已没有的 {len(gone)} 张" if gone and not self.cands_csv else "")
        k = max([int(s.stem.split("_")[1]) for s in self._shards()] or [-1]) + 1
        t0 = time.time()
        for i in range(0, len(todo), chunk):
            part = todo[i:i + chunk]
            X, ok = _embed_images([self.locate(p) for p in part])
            keep = [j for j in range(len(part)) if ok[j]]
            if keep:
                np.savez(self.emb / f"shard_{k:05d}.npz", paths=np.array([part[j] for j in keep]), feats=X[keep].astype(np.float16))
                k += 1
            el = max(time.time() - t0, 1e-6)
            n = i + len(part)
            self.job.update(done=n, message=f"{n}/{len(todo)} 张 · {n / el:.0f} 张/秒")
        return f"索引完成：共 {len(done) + len(todo)} 张，本次新增 {len(todo)} 张，用时 {time.time() - t0:.0f} 秒"

    # ── 类型标签 ──
    def load_type_labels(self):
        def load():
            out = {}
            for d in _jsonl(self.type_labels):
                if "path" in d and "photo_type" in d:
                    out[d["path"]] = (d["photo_type"], _ts(d.get("ts")))
            return out
        return self._cached("labels", [self.type_labels], load)

    def save_type_labels(self, items, source="LOTA 照片类型页"):
        ts = _now()
        with self.lock:
            _append(self.type_labels, *({"path": p, "photo_type": t, "source": source, "ts": ts} for p, t in items))

    def confirmed_labels(self, with_ts=False):
        """人工指定的类型（含放进临时仓库的）。→ {path: 类型} 或 {path: (类型, 时间)}"""
        out = dict(self.load_type_labels())
        for p, c in self.load_corrections().items():
            if c.get("photo_type") and p not in out:
                out[p] = (c["photo_type"], _ts(c.get("ts")))
        tax, dl = self.taxonomy, self.load_deleted()
        out = {p: v for p, v in out.items() if v[0] in tax and p not in dl}
        return out if with_ts else {p: v[0] for p, v in out.items()}

    def training_labels(self):
        """→ (人工确认 {path: 类型}, qwen 弱标签 {path: 类型})；都不含临时仓库"""
        allc = self.confirmed_labels()
        human = {p: t for p, t in allc.items() if t != BIN}
        tax, dl, weak = self.taxonomy, self.load_deleted(), {}
        for p, d in self.load_results().items():
            if d.get("error") or p in dl or p in allc:
                continue
            t = (d.get("vlm") or {}).get("photo_type")
            if t in tax and t != BIN:
                weak[p] = t
        return human, weak

    def load_pred(self):
        return self._cached("pred", [self.pred_file],
                            lambda: json.loads(self.pred_file.read_text(encoding="utf-8")) if self.pred_file.is_file() else {})

    def effective_types(self):
        """每张图现在归哪类：人工确认优先；放进临时仓库后又重新训练过的，按模型新预测走；
        建了索引但还没训练过的图先都在临时仓库里。已删除的不在里面。"""
        pred, dl = self.load_pred(), self.load_deleted()
        last = self.pred_file.stat().st_mtime if self.pred_file.exists() else 0
        out = {p: BIN for p in self.indexed_paths() if p not in dl}
        out.update({p: v[0] for p, v in pred.items() if p not in dl})
        for p, (t, ts) in self.confirmed_labels(with_ts=True).items():
            if t == BIN and p in pred and ts <= last:
                continue
            out[p] = t
        tax = self.taxonomy
        return {p: (t if t in tax else BIN) for p, t in out.items()}

    def similar_to_type(self, ptype, exclude, k_seed=5):
        import numpy as np
        if ptype == BIN:
            return [], [], 0
        paths, X, pos = self.cached_index()
        seeds = [pos[p] for p, t in self.confirmed_labels().items() if t == ptype and p in pos]
        if not seeds:
            return [], [], 0
        S = X @ X[seeds].T
        k = min(k_seed, len(seeds))
        score = np.sort(S, axis=1)[:, -k:].mean(1) if k > 1 else S[:, 0]
        out, sc = [], []
        for i in np.argsort(-score):
            if paths[i] not in exclude:
                out.append(paths[i])
                sc.append(float(score[i]))
        return out, sc, len(seeds)

    def train_predict(self):
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import KFold
        paths, X = self.load_index()
        if not paths:
            return {"error": "还没有特征索引：先点“更新索引”"}
        pos = {p: i for i, p in enumerate(paths)}
        human, weak = self.training_labels()
        H = [p for p in human if p in pos]
        W = [p for p in weak if p in pos]
        if len(H) + len(W) < 20 or len({human[p] for p in H} | {weak[p] for p in W}) < 2:
            return {"error": f"样板太少：现在人工确认了 {len(H)} 张。至少确认 20 张、分属 2 个以上类型再训练"}

        def fit(train_h):
            idx = [pos[p] for p in train_h] + [pos[p] for p in W]
            y = [human[p] for p in train_h] + [weak[p] for p in W]
            clf = LogisticRegression(max_iter=3000, C=10)
            clf.fit(X[idx], y, sample_weight=[3.0] * len(train_h) + [1.0] * len(W))
            return clf

        acc, per = None, {}
        if len(H) >= 25:
            Ha = np.array(H)
            correct, tot = Counter(), Counter()
            for tr, te in KFold(5, shuffle=True, random_state=0).split(Ha):
                if len({human[p] for p in Ha[tr]} | {weak[p] for p in W}) < 2:
                    continue
                pr = fit(list(Ha[tr])).predict(X[[pos[p] for p in Ha[te]]])
                for p, q in zip(Ha[te], pr):
                    tot[human[p]] += 1
                    correct[human[p]] += int(q == human[p])
            if tot:
                acc = sum(correct.values()) / sum(tot.values())
                per = {t: [correct[t], tot[t]] for t in tot}

        clf = fit(H)
        P = clf.predict_proba(X)
        order = np.argsort(-P, axis=1)
        cls = clf.classes_
        tax, prompts = self.taxonomy, self.clip_prompts()
        keys = [k for k in tax if k != BIN]
        T = _embed_texts([prompts.get(k) or tax[k].get("name_en") or k for k in keys])
        zs = np.array(keys)[(X @ T.T).argmax(1)]          # 还没有样板的类型：模型没把握时用零样本兜底
        known = set(cls)
        pred = {}
        for i, p in enumerate(paths):
            a = order[i, 0]
            b = order[i, 1] if P.shape[1] > 1 else a
            t, c = cls[a], float(P[i, a])
            if c < 0.5 and zs[i] not in known:
                t, c = zs[i], 0.0
            elif c < BIN_THRESHOLD:
                t = BIN
            pred[p] = [str(t), round(c, 3), str(cls[a] if t == BIN else cls[b]), round(float(P[i, a if t == BIN else b]), 3)]
        for p, t in human.items():
            if p in pred:
                pred[p] = [t, 1.0, "", 0.0]
        binned = [p for p, t in self.confirmed_labels().items() if t == BIN and p in pred]
        rescued = Counter(pred[p][0] for p in binned if pred[p][0] != BIN)
        self.emb.mkdir(parents=True, exist_ok=True)
        self.pred_file.write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
        rep = {"ts": time.strftime("%Y-%m-%d %H:%M"), "n_index": len(paths), "n_human": len(H), "n_weak": len(W),
               "cv_acc": acc, "per_type": per, "dist": Counter(v[0] for v in pred.values()).most_common(),
               "human_per_type": Counter(human[p] for p in H).most_common(),
               "bin_total": len(binned), "bin_rescued": sum(rescued.values()), "bin_rescued_to": rescued.most_common(10)}
        self.report_file.write_text(json.dumps(rep, ensure_ascii=False), encoding="utf-8")
        return rep

    def report(self):
        return json.loads(self.report_file.read_text(encoding="utf-8")) if self.report_file.is_file() else {}

    # ── 后台任务：建索引 / 训练（同一时间整个引擎只跑一个） ──
    def start_job(self, kind):
        if self.job["running"]:
            return self.job
        if not _gpu_lock.acquire(blocking=False):
            raise ValueError("另一个图库正在建索引或训练，等它跑完再来")

        def run():
            try:
                if kind == "index":
                    self.job["result"] = {"message": self.build_index()}
                else:
                    r = self.train_predict()
                    self.job.update(result={k: r.get(k) for k in ("ts", "n_human", "n_weak", "cv_acc", "bin_total", "bin_rescued", "error")},
                                    error=r.get("error"))
            except Exception as e:
                self.job["error"] = str(e)[:300]
            finally:
                free_model()
                self.job.update(running=False, finished=time.strftime("%H:%M:%S"))
                _gpu_lock.release()

        self.job.update(kind=kind, running=True, started=time.strftime("%H:%M:%S"), finished=None, done=0, total=0,
                        result=None, error=None, message="准备中…")
        threading.Thread(target=run, daemon=True).start()
        return self.job

    # ── 访问控制：只给本图库目录、回收站、旧数据集目录里的文件 ──
    def allowed_roots(self):
        return [self.root, self.trash, *self.extra_roots, *self.merged_roots, *self.access_roots, *self.extra_trash]

    def _trash_path(self, orig):
        """删除时放进回收站的位置：本图库目录按相对路径放；合并进来的目录放在 _merged/<目录名>/ 下"""
        if orig.is_relative_to(self.root):
            return self.trash / orig.relative_to(self.root)
        for r in [*self.merged_roots, *self.access_roots]:
            if orig.is_relative_to(r):
                tag = re.sub(r"[^\w.-]+", "_", r.name) or "root"
                return self.trash / "_merged" / tag / orig.relative_to(r)
        return None

    def source(self, path):
        src = Path(self.locate(path))
        if not src.is_file():
            rec = self.load_deleted().get(path)
            if rec and rec.get("trash"):
                src = Path(rec["trash"])
        src = src.resolve()
        if not src.is_file() or not any(src.is_relative_to(r.resolve()) for r in self.allowed_roots()):
            raise FileNotFoundError("找不到这张图")
        return src

    # ── 删除 / 恢复 / 永久删除 ──
    def delete_image(self, path, note=""):
        orig = Path(path)
        dst = self._trash_path(orig)
        if dst is None:
            return f"不是本图库目录里的图片：{path}"
        cur = Path(self.locate(path))
        if not cur.is_file():
            return f"文件不存在（可能已删除）：{path}"
        others = _other_links(cur)
        try:
            nlink = cur.stat().st_nlink
        except OSError:
            nlink = 1
        if dst.exists():
            dst = dst.with_name(f"{dst.stem}__{int(time.time())}{dst.suffix}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cur), str(dst))
        unlinked = []
        for o in others:            # 初选导出的硬链接：原目录里同一个文件也摘掉（inode 还在回收站里，能恢复）
            try:
                o.unlink()
                unlinked.append(str(o))
            except OSError:
                pass
        if self.load_loc().get(path):
            with self.lock:
                loc = dict(self.load_loc())
                loc.pop(path, None)
                tmp = self.loc_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(loc, ensure_ascii=False, indent=0), encoding="utf-8")
                os.replace(tmp, self.loc_file)
        self._log_deleted({"action": "delete", "path": path, "trash": str(dst), "note": note, "unlinked": unlinked})
        left = nlink - 1 - len(unlinked)
        extra = f"；原目录里的同一张也一起移走了（{len(unlinked)} 处）" if unlinked else ""
        if left > 0:
            extra += f"；还有 {left} 个别处的硬链接没找到，永久删除后磁盘空间也不会释放"
        return f"已删除：文件已移到回收站{extra}"

    def restore_image(self, path):
        rec = self.load_deleted().get(path)
        if not rec:
            return f"回收站里没有这张：{path}"
        if rec.get("purged"):
            return "这张已被永久删除，无法恢复"
        src, t = Path(path), Path(rec["trash"])
        if src.exists():
            return f"原位置已有同名文件，未恢复：{path}"
        if not t.is_file():
            return f"回收站里的文件不见了：{t}"
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(t), str(src))
        relinked = 0
        for o in rec.get("unlinked") or []:
            o = Path(o)
            if not o.exists():
                try:
                    o.parent.mkdir(parents=True, exist_ok=True)
                    os.link(src, o)
                    relinked += 1
                except OSError:
                    pass
        self._log_deleted({"action": "restore", "path": path})
        return "已恢复" + (f"（原目录里的 {relinked} 处也链回去了）" if relinked else "")

    def purge(self, paths=None):
        rows, want = self.load_deleted(), None if paths is None else set(paths)
        n, done = 0, []
        for p, rec in rows.items():
            if rec.get("purged") or (want is not None and p not in want):
                continue
            t = Path(rec.get("trash") or "")
            if t.is_file() and any(t.resolve().is_relative_to(x.resolve()) for x in [self.trash, *self.extra_trash]):
                t.unlink()
                n += 1
            self._log_deleted({"action": "purge", "path": p})
            done.append(p)
        return n, done
