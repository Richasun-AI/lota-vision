"""照片清理：移除人工确认的类别、批量排除/恢复、删除照片（可选连原图文件一起删）。"""
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import Field

from .tools import TOOLS, Selection, Tool


class Unconfirm(Selection):
    pass


class Reject(Selection):
    decision: Literal["rejected", "pending", "accepted"] = "rejected"
    note: str | None = Field(default=None, max_length=2000)
    checkpoint: str | None = Field(default=None, max_length=200)   # 不填 = 保留原值
    site_id: str | None = Field(default=None, max_length=200)


class Delete(Selection):
    delete_source: bool = False


def unconfirm(c, image_ids):
    """撤销人工确认的照片类别：分类器不再学这些图，分拣结果回到“未知、待复核”。"""
    from .learning import connect
    from .sorting import store_results
    ids = list(dict.fromkeys(image_ids))
    with closing(connect(c.directory)) as db:
        with db:
            removed = 0
            for key in ids:
                removed += db.execute("DELETE FROM labels WHERE image_id=?", (key,)).rowcount
                db.execute("INSERT INTO history(image_id,category) VALUES(?,?)", (key, "(removed)"))
    store_results(c.directory, [{"image_id": key, "category": "unknown", "review": "pending", "provenance": "removed"}
                                for key in ids])
    return {"image_ids": ids, "removed": removed}


def reject(c, image_ids, decision="rejected", note=None, checkpoint=None, site_id=None):
    """批量复核：确认 / 排除 / 恢复待复核，可顺便统一填检查点、站点、备注（不填的保留原值）。"""
    from .catalog import review_image
    ids = list(dict.fromkeys(image_ids))
    for key in ids:
        review_image(c.directory, key, decision=decision, note=note or None,
                     checkpoint=checkpoint or None, site_id=site_id or None)
    return {"image_ids": ids, "decision": decision, "count": len(ids)}


def delete_images(c, image_ids, delete_source=False):
    """从本场景彻底删除照片：登记、复制件、缩略图、特征、类别、分拣结果、当前标注。
    记下“已删除”，以后扫描同样内容的图片不会再登记回来。勾选 delete_source 才会删服务器上的原图文件。"""
    from .catalog import _database
    root = Path(c.directory)
    ids = list(dict.fromkeys(image_ids))
    now = time.time_ns()
    sources, raw_files = [], []
    with _database(root) as db:
        with db:
            for key in ids:
                row = db.execute("SELECT raw_relpath FROM images WHERE image_id=?", (key,)).fetchone()
                if row is None:
                    continue
                if row["raw_relpath"]:
                    raw_files.append(root / row["raw_relpath"])
                sources += [r["path"] for r in db.execute("SELECT path FROM source_files WHERE image_id=?", (key,))]
                sources += [r["path"] for r in db.execute("SELECT path FROM observations WHERE image_id=?", (key,))]
                db.execute("DELETE FROM review_history WHERE image_id=?", (key,))
                db.execute("UPDATE source_files SET image_id=NULL, state='invalid', detail=? WHERE image_id=?",
                           ("已删除 / deleted", key))
                db.execute("DELETE FROM observations WHERE image_id=?", (key,))
                db.execute("DELETE FROM images WHERE image_id=?", (key,))
                db.execute("INSERT OR REPLACE INTO deleted VALUES(?,?,?)",
                           (key, now, "source deleted" if delete_source else "catalog only"))
    # 本场景里的派生文件
    removed_files = 0
    for f in raw_files:
        if f.is_file() and f.resolve().is_relative_to(root.resolve()):
            f.unlink(); removed_files += 1
    for key in ids:
        for f in list(root.glob(f"previews/*/{key[:2]}/{key}.jpg")) + list(root.glob(f"features/*/{key}.npy")):
            f.unlink(missing_ok=True)
    # 类别、分拣结果、当前标注
    for db_path, sqls in (
        (root / "catalog" / "learning.sqlite3", ["DELETE FROM labels WHERE image_id=?"]),
        (root / "catalog" / "sorting.sqlite3", ["DELETE FROM results WHERE image_id=?"]),
        (root / "annotations" / "index.sqlite3", ["DELETE FROM current_annotations WHERE image_id=?"]),
    ):
        if db_path.is_file():
            with closing(sqlite3.connect(db_path, timeout=30)) as db:
                with db:
                    for key in ids:
                        for sql in sqls:
                            try:
                                db.execute(sql, (key,))
                            except sqlite3.OperationalError:
                                pass
    # 原图文件（可选，不可恢复）
    deleted_sources, kept = [], []
    if delete_source:
        for p in dict.fromkeys(sources):
            try:
                path = c.engine.allowed_source(p)
            except (ValueError, FileNotFoundError):
                kept.append(p); continue
            if path.is_file():
                path.unlink(); deleted_sources.append(str(path))
    return {"image_ids": [], "deleted": ids, "count": len(ids), "removed_copies": removed_files,
            "deleted_sources": deleted_sources, "kept_sources": kept}


TOOLS.update({
    "curation.unconfirm": Tool("移除照片类别", Unconfirm, unconfirm),
    "catalog.reject": Tool("批量复核（确认 / 排除 / 恢复）", Reject, reject),
    "catalog.delete": Tool("删除照片", Delete, delete_images),
})
