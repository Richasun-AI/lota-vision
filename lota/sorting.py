"""Workflow stages for automatic, reviewable intake sorting."""
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from .catalog import get_image, list_images

def candidates(c, limit=1000):
    return {"image_ids":[row["image_id"] for row in list_images(c.directory,limit=limit)]}

def quality(c, image_ids):
    return {"image_ids":image_ids,"items":[{"image_id":key,"flags":get_image(c.directory,key)["quality"].get("flags",[])} for key in image_ids]}

def duplicates(c, image_ids):
    groups={}
    for key in image_ids:
        row=get_image(c.directory,key)
        groups.setdefault(row["dhash"],[]).append(key)
    return {"image_ids":image_ids,"candidate_groups":[v for v in groups.values() if len(v)>1]}

def classify(c,image_ids,hints_path=None,model_dir=None):
    active=c.directory/"classifiers/active.json"
    if active.is_file() and not model_dir:
        from .learning import reclassify
        profile=json.loads(active.read_text(encoding="utf-8"))
        return reclassify(c,image_ids,profile["version"],profile["min_confidence"])
    hints={}
    if hints_path:
        path=c.engine.allowed_source(hints_path)
        hints=json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(hints,dict): raise ValueError("Type hints must be a path-to-prediction mapping")
    from .learning import human_labels
    human=human_labels(c.directory)
    suggestions={}
    if model_dir:
        from .vision import suggest_types
        paths=[c.engine.image_path(c.directory,get_image(c.directory,key),False) for key in image_ids]
        result=suggest_types(c.directory,paths,[key for key,value in c.scenario.categories.items() if value.enabled] or c.scenario.classes,c.engine.allowed_weights(model_dir,True),16,"cpu")
        suggestions={x["image_id"]:x["candidates"] for x in result["items"]}
    directory=c.directory/"catalog"; directory.mkdir(exist_ok=True)
    items=[]
    with closing(sqlite3.connect(directory/"sorting.sqlite3")) as db:
        db.execute("CREATE TABLE IF NOT EXISTS results(image_id TEXT PRIMARY KEY,category TEXT,payload TEXT)")
        for key in image_ids:
            row=get_image(c.directory,key)
            hint=next((hints.get(a["path"]) for a in row["aliases"] if a["path"] in hints),None)
            ranked=suggestions.get(key,[])
            category=human.get(key) or row["checkpoint"] or (ranked[0]["label"] if ranked else hint[0] if isinstance(hint,list) and hint else "unknown")
            item={"image_id":key,"category":category,"review":"pending","quality":row["quality"],
                  "candidates":ranked,"provenance":"human" if key in human else "metadata" if row["checkpoint"] else "configured_model" if ranked else "historical_hint" if hint else "unknown"}
            db.execute("INSERT OR REPLACE INTO results VALUES(?,?,?)",(key,category,json.dumps(item,ensure_ascii=False)))
            items.append(item)
        db.commit()
    return {"image_ids":image_ids,"categories":dict(Counter(x["category"] for x in items)),"items":items,
            "notice":"类型为待复核候选；未知图片可配置分类模型后重新运行。"}

def results(directory,category=None,limit=100,offset=0,exclude_rejected=False):
    path=directory/"catalog/sorting.sqlite3"
    if not path.exists(): return {"categories":{},"items":[]}
    with closing(sqlite3.connect(path)) as db:
        where,args=[],[]
        catalog=directory/"catalog/catalog.sqlite3"
        if exclude_rejected and catalog.exists():   # 已排除的照片不算数、不列出
            db.execute("ATTACH DATABASE ? AS cat",(str(catalog),))
            where.append("image_id NOT IN (SELECT image_id FROM cat.images WHERE status='rejected')")
        base=" WHERE "+" AND ".join(where) if where else ""
        counts=dict(db.execute("SELECT category,COUNT(*) FROM results"+base+" GROUP BY category"))
        query="SELECT image_id,payload FROM results"+base
        if category: query+=(" AND" if where else " WHERE")+" category=?";args.append(category)
        query+=" ORDER BY image_id LIMIT ? OFFSET ?";args.extend([limit,offset])
        items=[]
        for key,payload in db.execute(query,args):
            row=get_image(directory,key);row["sorting"]=json.loads(payload);items.append(row)
    return {"categories":counts,"items":items}

def store_results(directory,items):
    with closing(sqlite3.connect(directory/"catalog/sorting.sqlite3")) as db:
        db.execute("CREATE TABLE IF NOT EXISTS results(image_id TEXT PRIMARY KEY,category TEXT,payload TEXT)")
        for item in items:
            db.execute("INSERT OR REPLACE INTO results VALUES(?,?,?)",(item["image_id"],item["category"],json.dumps(item,ensure_ascii=False)))
        db.commit()
