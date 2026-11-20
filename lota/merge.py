"""子项目合并：把子项目 B 并入 A，B 变成 A 里的一个标注任务。

- 图片目录（按内容哈希，相同的图只留一份）、原图副本 raw、图片来源、分拣结果、人工类别、特征缓存：并进 A；
  两边冲突时以 A 为准，冲突列在报告里
- B 的标注、数据集、模型、训练记录、预测 / 复核 / 质检记录、照片分类器 → A/annotation-tasks/<任务代码>/，
  B 的标签、任务类型、Label Studio 连接变成这个标注任务的设置；B 原有的标注任务加前缀一起搬过去
- B 的工作流搬过去（加前缀，标注 / 数据集 / 模型步骤绑定到这个任务）
- B 停用并改名“[已并入 A] …”，目录原样保留，确认没问题后再手动删

大文件（原图、权重、数据集图片）用硬链接，不占额外空间；数据库、文本文件复制，改 A 不会动到 B。
dry_run=True 只出报告，什么都不改。
"""
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .store import AnnotationTask, Scenario, Workflow, validate_id

# B 目录里按任务隔离的内容（标注任务里 c.directory 指向任务目录，这些都在任务目录下找）
TASK_ITEMS = ("annotations", "datasets", "models", "runs", "predictions", "inspections", "reviews", "qa", "find",
              "exports", "classifiers", "runtime", "settings.json", "guideline.md")
TEXT_SUFFIXES = {".json", ".yaml", ".yml", ".txt", ".csv", ".md"}
LINK_MIN_BYTES = 1 << 20      # 1MB 以上、又不是数据库 / 文本的文件用硬链接
SPECIAL_TOOLS = ("labels.", "dataset.", "model.")


def _busy(store, project_id):
    with store.connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, kind, status FROM jobs WHERE project_id=? AND status IN ('queued','running')", (project_id,))]


def _task_code(base, taken):
    code = base[:63].rstrip("-")
    n = 2
    while code in taken:
        suffix = f"-{n}"
        code = base[:63 - len(suffix)].rstrip("-") + suffix
        n += 1
    return validate_id(code)


def _place(src: Path, dst: Path, replace_text, stats):
    """单个文件：数据库 / 小文件复制，文本里 B 的绝对路径换成新目录，大文件硬链接（跨盘就复制）"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    name = src.name
    if src.suffix.lower() in TEXT_SUFFIXES and src.stat().st_size < 50 << 20:
        try:
            text = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None:
            new = replace_text(text)
            dst.write_text(new, encoding="utf-8")
            shutil.copystat(src, dst)
            stats["rewritten"] += new != text
            stats["copied"] += 1
            return
    if ".sqlite" in name or name.endswith(("-wal", "-shm")) or src.stat().st_size < LINK_MIN_BYTES:
        shutil.copy2(src, dst)
        stats["copied"] += 1
        return
    try:
        os.link(src, dst)
        stats["linked"] += 1
    except OSError:
        shutil.copy2(src, dst)
        stats["copied"] += 1


def _place_tree(src: Path, dst: Path, replace_text, stats, overwrite=False):
    if src.is_file():
        if overwrite or not dst.exists():
            _place(src, dst, replace_text, stats)
        return
    for path in sorted(src.rglob("*")):
        if path.is_file() and not path.is_symlink():
            target = dst / path.relative_to(src)
            if overwrite or not target.exists():
                _place(path, target, replace_text, stats)


def _sqlite_copy(db_path: Path):
    """合并前先把 B 的数据库做个一致的快照（带 WAL 的库直接 ATTACH 可能读不到最新内容）"""
    import tempfile
    fd, name = tempfile.mkstemp(suffix=".sqlite3", prefix="lota-merge-")   # 快照放临时目录：试合并不往 B 里写任何东西
    os.close(fd)
    snap = Path(name)
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as source, closing(sqlite3.connect(snap)) as out:
        source.backup(out)
    return snap


def _tables(db, schema="main"):
    return {r[0] for r in db.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}


def _columns(db, table, schema="main"):
    return [r[1] for r in db.execute(f"PRAGMA {schema}.table_info({table})")]


def _merge_catalog(a_dir: Path, b_dir: Path, dry_run, report):
    """图片目录：images / source_files / observations / manifests / review_history / deleted"""
    from .catalog import _database
    a_db, b_db = a_dir / "catalog" / "catalog.sqlite3", b_dir / "catalog" / "catalog.sqlite3"
    out = report["catalog"] = {"images_b": 0, "new": 0, "already_in_a": 0, "skipped_deleted_in_a": 0,
                               "raw_linked": 0, "raw_missing": 0, "review_taken_from_b": 0}
    if not b_db.is_file():
        return
    with _database(a_dir):   # 确保 A 的库和表存在
        pass
    snap = _sqlite_copy(b_db)
    try:
        with closing(sqlite3.connect(a_db, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute("ATTACH DATABASE ? AS b", (str(snap),))
            a_ids = {r[0] for r in db.execute("SELECT image_id FROM main.images")}
            a_deleted = {r[0] for r in db.execute("SELECT image_id FROM main.deleted")}
            b_rows = [dict(r) for r in db.execute("SELECT * FROM b.images")]
            out["images_b"] = len(b_rows)
            raw_plan = []
            for row in b_rows:
                iid = row["image_id"]
                if iid in a_deleted and iid not in a_ids:
                    out["skipped_deleted_in_a"] += 1
                    continue
                if iid in a_ids:
                    out["already_in_a"] += 1
                else:
                    out["new"] += 1
                if row["raw_relpath"]:
                    raw_plan.append((iid, row["raw_relpath"], iid in a_ids))
            if dry_run:
                a_raw = {r[0]: r[1] for r in db.execute("SELECT image_id, raw_relpath FROM main.images")}
                out["raw_linked"] = sum(1 for iid, rel, in_a in raw_plan if not (in_a and a_raw.get(iid)))
                return
            skip = a_deleted - a_ids
            cols = _columns(db, "images")
            with db:
                for row in b_rows:
                    iid = row["image_id"]
                    if iid in skip:
                        continue
                    if iid not in a_ids:
                        db.execute(f"INSERT INTO main.images({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                   [row[c] if c != "raw_relpath" else None for c in cols])
                    elif row["status"] != "pending":   # A 还没复核、B 复核过：用 B 的复核结果
                        cur = db.execute("SELECT status FROM main.images WHERE image_id=?", (iid,)).fetchone()
                        if cur["status"] == "pending":
                            db.execute("UPDATE main.images SET status=?, manual_site=?, manual_checkpoint=?, review_note=? "
                                       "WHERE image_id=?", (row["status"], row["manual_site"], row["manual_checkpoint"],
                                                            row["review_note"], iid))
                            out["review_taken_from_b"] += 1
                keep = "image_id IN (SELECT image_id FROM main.images)"
                for table in ("source_files", "observations"):
                    c = ",".join(_columns(db, table))
                    db.execute(f"INSERT OR IGNORE INTO main.{table}({c}) SELECT {c} FROM b.{table} WHERE {keep}"
                               + (" OR image_id IS NULL" if table == "source_files" else ""))
                c = ",".join(_columns(db, "manifests"))
                db.execute(f"INSERT OR IGNORE INTO main.manifests({c}) SELECT {c} FROM b.manifests")
                c = ",".join(x for x in _columns(db, "review_history") if x != "review_id")
                db.execute(f"INSERT INTO main.review_history({c}) SELECT {c} FROM b.review_history WHERE {keep}")
                if "deleted" in _tables(db, "b"):   # B 删掉、A 也没有的：留墓碑，以后扫描不再登记回来
                    db.execute("INSERT OR IGNORE INTO main.deleted SELECT * FROM b.deleted "
                               "WHERE image_id NOT IN (SELECT image_id FROM main.images)")
            # 原图副本：B 有、A 没有的硬链接过去
            a_raw = {r[0]: r[1] for r in db.execute("SELECT image_id, raw_relpath FROM main.images")}
            with db:
                for iid, rel, _ in raw_plan:
                    if iid not in a_raw or a_raw[iid]:
                        continue
                    src, dst = b_dir / rel, a_dir / rel
                    if not src.is_file():
                        out["raw_missing"] += 1
                        continue
                    if not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.link(src, dst)
                        except OSError:
                            shutil.copy2(src, dst)
                    db.execute("UPDATE main.images SET raw_relpath=? WHERE image_id=?", (rel, iid))
                    out["raw_linked"] += 1
            db.execute("DETACH DATABASE b")
    finally:
        snap.unlink(missing_ok=True)


def _merge_simple_db(a_dir: Path, b_dir: Path, name: str, dry_run, report, conflict_table=None):
    """sorting.sqlite3 / learning.sqlite3：按主键并，A 已有的不动；人工类别不一致的列出来"""
    a_db, b_db = a_dir / "catalog" / name, b_dir / "catalog" / name
    out = report.setdefault("sorting", {})
    key = name.split(".")[0]
    out[key] = {"added": 0, "conflicts": []}
    if not b_db.is_file():
        return
    snap = _sqlite_copy(b_db)
    try:
        if not a_db.is_file():   # A 还没有这个库：整个拿过去
            if not dry_run:
                a_db.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap, a_db)
            with closing(sqlite3.connect(snap)) as db:
                t = conflict_table or "results"
                out[key]["added"] = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in _tables(db) else 0
            return
        with closing(sqlite3.connect(a_db, timeout=30)) as db:
            db.execute("ATTACH DATABASE ? AS b", (str(snap),))
            if conflict_table and conflict_table in _tables(db) and conflict_table in _tables(db, "b"):
                out[key]["conflicts"] = [{"image_id": r[0], "a": r[1], "b": r[2]} for r in db.execute(
                    f"SELECT a.image_id, a.category, b.category FROM main.{conflict_table} a JOIN b.{conflict_table} b "
                    f"ON a.image_id=b.image_id WHERE a.category IS NOT b.category")]
            with db:
                for table in _tables(db, "b") & _tables(db):
                    cols = [c for c in _columns(db, table, "b") if c in _columns(db, table)]
                    c = ",".join(cols)
                    before = db.total_changes
                    verb = "INSERT INTO" if table == "history" else "INSERT OR IGNORE INTO"
                    db.execute(f"{verb} main.{table}({c}) SELECT {c} FROM b.{table}")
                    if table in ("results", "labels"):
                        out[key]["added"] += db.total_changes - before
                if dry_run:
                    db.rollback()
            db.execute("DETACH DATABASE b")
    finally:
        snap.unlink(missing_ok=True)


def _ls_rebind(scenario_ls, a_raw: Path, a_id: str, b_id: str, task_code: str, report):
    """B 的 Label Studio 项目：登记 A 的原图目录（以后从任务送图要读 A/raw），webhook 改成回收到 A 的这个任务"""
    from .labels import LabelStudioClient
    from urllib.parse import quote
    out = report["label_studio"] = {"project_id": scenario_ls.get("project_id"), "storage_added": False,
                                    "webhooks_updated": 0, "warning": ""}
    try:
        cli = LabelStudioClient.from_config(scenario_ls)
        pid = scenario_ls["project_id"]
        storages = cli._request("GET", "/api/storages/localfiles", query={"project": pid}) or []
        if not any(str(s.get("path", "")).rstrip("/") == str(a_raw).rstrip("/") for s in storages):
            a_raw.mkdir(parents=True, exist_ok=True)
            cli._request("POST", "/api/storages/localfiles", payload={
                "project": pid, "path": str(a_raw), "use_blob_urls": True, "title": "LOTA raw (merged)"})
            out["storage_added"] = True
        for w in cli._request("GET", "/api/webhooks", query={"project": pid}) or []:
            url = str(w.get("url", ""))
            if "/api/hooks/label-studio" in url and f"project={b_id}" in url:
                base = url.split("?")[0]
                cli._request("PATCH", f"/api/webhooks/{w['id']}",
                             payload={"url": f"{base}?project={a_id}&annotation_task={quote(task_code)}"})
                out["webhooks_updated"] += 1
    except Exception as e:   # Label Studio 连不上不挡合并：报告里提示，之后在任务里重新“连接 Label Studio”即可
        out["warning"] = f"Label Studio 没改成：{str(e)[:200]}"


def merge(engine, target: str, source: str, dry_run=True, task_code: str = ""):
    store = engine.store
    validate_id(target), validate_id(source)
    if target == source:
        raise ValueError("不能把子项目并入它自己")
    a, b = store.project(target), store.project(source)
    a_cfg, b_cfg = Scenario.model_validate(a["config"]), Scenario.model_validate(b["config"])
    if b_cfg.name.startswith("[已并入"):
        raise ValueError(f"“{b_cfg.name}”已经并入过别的子项目了")
    for pid in (target, source):
        if busy := _busy(store, pid):
            raise ValueError(f"子项目 {pid} 还有 {len(busy)} 个任务在排队或运行（{busy[0]['kind']}），等它们结束再合并")
    a_dir, b_dir = store.project_dir(target), store.project_dir(source)
    taken = set(a_cfg.annotation_tasks)
    code = validate_id(task_code) if task_code else _task_code(source, taken)
    if code in taken:
        raise ValueError(f"A 里已经有标注任务“{code}”，换一个任务代码")
    task_dir = a_dir / "annotation-tasks" / code
    if task_dir.exists() and any(task_dir.iterdir()):
        raise ValueError(f"目录 {task_dir} 已存在且不为空，换一个任务代码")

    report = {"target": target, "source": source, "task_code": code, "dry_run": dry_run,
              "target_name": a_cfg.name, "source_name": b_cfg.name, "warnings": []}

    # ── 配置：类别、图片来源、标注任务 ──
    cfg = a_cfg.model_dump(mode="json")
    cats = dict(cfg.get("categories") or {})
    cat_conflicts, cat_added = [], []
    for k, v in b_cfg.categories.items():
        v = v.model_dump(mode="json")
        if k not in cats:
            cats[k] = v
            cat_added.append(k)
        elif (cats[k].get("name"), cats[k].get("name_en")) != (v.get("name"), v.get("name_en")):
            cat_conflicts.append({"code": k, "a": cats[k].get("name"), "b": v.get("name")})
    for k in b_cfg.classes:   # B 的标签代码在类别里没有定义的，补个占位，界面不显示“待命名”就用 B 的
        if k not in cats and k[0].isalpha() and k == k.lower():
            cats[k] = {"name": k, "name_en": "", "description": f"来自子项目 {source}", "enabled": True}
            cat_added.append(k)
    cfg["categories"] = cats
    report["categories"] = {"added": cat_added, "conflicts_a_kept": cat_conflicts}

    sources, names = list(cfg.get("sources") or []), {s["name"] for s in cfg.get("sources") or []}
    paths = {str(Path(s["path"])) for s in cfg.get("sources") or []}
    src_added = []
    for s in b_cfg.sources:
        s = s.model_dump(mode="json")
        if str(Path(s["path"])) in paths:
            continue
        name, n = s["name"], 2
        while name in names:
            name = f"{source}-{s['name']}" if n == 2 else f"{source}-{s['name']}-{n}"
            n += 1
        s["name"] = name
        sources.append(s)
        names.add(name)
        src_added.append(name)
    cfg["sources"] = sources
    report["sources_added"] = src_added

    tasks = dict(cfg.get("annotation_tasks") or {})
    tasks[code] = AnnotationTask.model_validate({
        "name": b_cfg.name[:100], "task": b_cfg.task, "classes": b_cfg.classes, "require_site": b_cfg.require_site,
        "label_studio": b_cfg.label_studio.model_dump(mode="json") if b_cfg.label_studio else None}).model_dump(mode="json")
    sub_tasks = {}
    for t_id, t in b_cfg.annotation_tasks.items():
        new_id = _task_code(f"{code}-{t_id}", set(tasks))
        tasks[new_id] = {**t.model_dump(mode="json"), "name": f"{b_cfg.name} · {t.name}"[:100]}
        sub_tasks[t_id] = new_id
    cfg["annotation_tasks"] = tasks
    report["annotation_tasks"] = {"main": code, "moved": sub_tasks}
    if b_cfg.routes:
        report["warnings"].append(f"B 的目录分流规则（{len(b_cfg.routes)} 条）没有搬，需要的话在 A 的配置里重新设")
    new_cfg = Scenario.model_validate(cfg)

    # ── 文件 ──
    # 文本里的 B 绝对路径按先后换：B 的子任务目录 → A 里对应任务目录；B/raw → A/raw（原图并到 A）；其余 B 目录 → 新任务目录
    pairs = [(b_dir / "annotation-tasks" / t, a_dir / "annotation-tasks" / n) for t, n in sub_tasks.items()]
    pairs += [(b_dir / "raw", a_dir / "raw"), (b_dir, task_dir)]
    # “/”、系统分隔符、JSON 转义后的写法都认（Windows 下 JSON 里反斜杠是双写的）
    forms = (lambda p: p.as_posix(), lambda p: json.dumps(str(p))[1:-1], str)
    pairs = list(dict.fromkeys((f(old), f(new)) for old, new in pairs for f in forms))

    def rewrite(text):
        if not any(old in text for old, _ in pairs):
            return text
        marks = {}
        for i, (old, new) in enumerate(pairs):   # 先换成占位符，避免长路径被短规则二次替换
            mark = f"\x00{i}\x00"
            text = text.replace(old, mark)
            marks[mark] = new
        for mark, new in marks.items():
            text = text.replace(mark, new)
        return text

    files = report["files"] = {"copied": 0, "linked": 0, "rewritten": 0, "items": []}
    plan = [(b_dir / item, task_dir / item) for item in TASK_ITEMS if (b_dir / item).exists()]
    for t_id, new_id in sub_tasks.items():
        plan.append((b_dir / "annotation-tasks" / t_id, a_dir / "annotation-tasks" / new_id))
    files["items"] = [str(src.relative_to(b_dir)) for src, _ in plan]

    workflows = []
    for w in store.workflows(source):
        wid = _task_code(f"{code}-{w['id']}", {x["id"] for x in store.workflows(target)} | {x[0] for x in workflows})
        spec = w["definition"]
        for step in spec.get("steps", []):
            if step.get("tool", "").startswith(SPECIAL_TOOLS):
                cur = step.setdefault("args", {}).get("annotation_task")
                step["args"]["annotation_task"] = sub_tasks.get(cur, code) if cur else code
        spec["name"] = f"[{b_cfg.name}] {spec.get('name', w['id'])}"[:100]
        workflows.append((wid, Workflow.model_validate(spec)))
    report["workflows"] = [wid for wid, _ in workflows]

    if dry_run:
        _merge_catalog(a_dir, b_dir, True, report)
        _merge_simple_db(a_dir, b_dir, "sorting.sqlite3", True, report)
        _merge_simple_db(a_dir, b_dir, "learning.sqlite3", True, report, conflict_table="labels")
        if b_cfg.label_studio:
            report["label_studio"] = {"project_id": b_cfg.label_studio.project_id,
                                      "plan": "登记 A 的原图目录、webhook 改为回收到这个任务"}
        return report

    # 正式合并：先文件和数据，最后改配置（配置一改，任务就能用了）
    for src, dst in plan:
        _place_tree(src, dst, rewrite, files)
    if (b_dir / "features").is_dir():   # 特征缓存：按内容哈希，A 没有的拿过去，省得重算
        _place_tree(b_dir / "features", a_dir / "features", lambda t: t, files)
    _merge_catalog(a_dir, b_dir, False, report)
    _merge_simple_db(a_dir, b_dir, "sorting.sqlite3", False, report)
    _merge_simple_db(a_dir, b_dir, "learning.sqlite3", False, report, conflict_table="labels")

    engine.save(target, new_cfg, a["revision"])
    for wid, spec in workflows:
        store.save_workflow(target, wid, spec)
    if b_cfg.label_studio:
        _ls_rebind(b_cfg.label_studio.model_dump(mode="json"), a_dir / "raw", target, source, code, report)
    for t_id, new_id in sub_tasks.items():
        t = b_cfg.annotation_tasks[t_id]
        if t.label_studio:
            sub = {}
            _ls_rebind(t.label_studio.model_dump(mode="json"), a_dir / "raw", target, source, new_id, sub)
            report.setdefault("label_studio_tasks", {})[new_id] = sub.get("label_studio")

    # B：停用、改名，目录不动
    b_new = b_cfg.model_dump(mode="json")
    b_new["name"] = f"[已并入 {target}] {b_cfg.name}"[:100]
    store.save_project(source, Scenario.model_validate(b_new), b["revision"])
    store.activate(source, False)
    record = {**report, "merged_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    (b_dir / "MERGED_INTO.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    log = a_dir / "merges.json"
    history = json.loads(log.read_text(encoding="utf-8")) if log.is_file() else []
    log.write_text(json.dumps(history + [record], ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def register_routes(router, engine):
    from fastapi import Body, HTTPException

    @router.post("/projects/{project_id}/merge", tags=["scenarios"])
    def merge_project(project_id: str, payload: dict = Body(...)):
        """把 payload.source 子项目并入 project_id：B 变成 A 的标注任务。dry_run 默认 true，只出报告"""
        try:
            return merge(engine, project_id, payload.get("source", ""), dry_run=payload.get("dry_run", True) is not False,
                         task_code=(payload.get("task_code") or "").strip())
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
