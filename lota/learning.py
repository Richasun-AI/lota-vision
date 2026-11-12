"""Human labels, cached SigLIP features and versioned linear classifiers."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from .catalog import get_image
from .store import validate_id

def connect(root):
    folder=root/"catalog";folder.mkdir(exist_ok=True)
    db=sqlite3.connect(folder/"learning.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS labels(image_id TEXT PRIMARY KEY,category TEXT NOT NULL,updated TEXT DEFAULT CURRENT_TIMESTAMP)")
    db.execute("CREATE TABLE IF NOT EXISTS history(image_id TEXT,category TEXT,updated TEXT DEFAULT CURRENT_TIMESTAMP)")
    return db

def confirm(c,image_ids,category):
    if category not in c.scenario.categories or not c.scenario.categories[category].enabled:
        raise ValueError("请选择一个已启用的照片类别（先在“类别编辑器”里添加）/ Choose an enabled scene category")
    for key in image_ids:get_image(c.directory,key)
    with closing(connect(c.directory)) as db:
        with db:
            for key in set(image_ids):
                db.execute("INSERT OR REPLACE INTO labels(image_id,category) VALUES(?,?)",(key,category))
                db.execute("INSERT INTO history(image_id,category) VALUES(?,?)",(key,category))
    from .sorting import store_results
    store_results(c.directory,[{"image_id":key,"category":category,"review":"confirmed","provenance":"human"}
                              for key in set(image_ids)])
    return {"confirmed":len(set(image_ids)),"category":category}

def human_labels(root):
    with closing(connect(root)) as db:return dict(db.execute("SELECT image_id,category FROM labels"))

def features(c,ids,model_dir):
    import numpy as np
    import torch
    from transformers import AutoModel,AutoProcessor
    from PIL import Image,ImageOps
    from .vision import _fingerprint,_pooled
    model_dir=c.engine.allowed_weights(model_dir,True)
    fingerprint=_fingerprint(model_dir)
    folder=c.directory/"features"/fingerprint;folder.mkdir(parents=True,exist_ok=True)
    missing=[key for key in ids if not (folder/(key+".npy")).exists()]
    if missing:
        processor=AutoProcessor.from_pretrained(model_dir,local_files_only=True,trust_remote_code=False)
        import os
        from contextlib import ExitStack
        from .vision import _batches
        device=os.environ.get("LOTA_FEATURE_DEVICE","cuda" if torch.cuda.is_available() else "cpu")
        model=AutoModel.from_pretrained(model_dir,local_files_only=True,trust_remote_code=False).to(device).eval()
        if model.config.model_type not in {"siglip","siglip2"}:raise ValueError("Expected a SigLIP image encoder")
        for batch in _batches(missing,16):
            with ExitStack() as stack:
                paths=[c.engine.image_path(c.directory,get_image(c.directory,key),False) for key in batch]
                images=[]
                for key,path in zip(batch,paths):
                    with path.open("rb") as stream:
                        if hashlib.file_digest(stream,"sha256").hexdigest()!=key:raise ValueError("Image changed since indexing")
                    source=stack.enter_context(Image.open(path))
                    oriented=ImageOps.exif_transpose(source)
                    stack.callback(oriented.close)
                    image=oriented.convert("RGB")
                    stack.callback(image.close)
                    images.append(image)
                inputs=processor(images=images,return_tensors="pt").to(device)
                with torch.inference_mode():
                    vectors=torch.nn.functional.normalize(_pooled(model.get_image_features(**inputs)),dim=-1).cpu().numpy()
                for key,path,vector in zip(batch,paths,vectors):
                    with path.open("rb") as stream:
                        if hashlib.file_digest(stream,"sha256").hexdigest()!=key:raise ValueError("Image changed during feature extraction")
                    import tempfile
                    with tempfile.NamedTemporaryFile(dir=folder,delete=False) as stream:
                        temporary=Path(stream.name);np.save(stream,vector)
                    os.replace(temporary,folder/(key+".npy"))
    return np.stack([np.load(folder/(key+".npy"),allow_pickle=False) for key in ids]),fingerprint

def train(c,version,model_dir=None):
    import os
    model_dir=model_dir or os.environ.get("LOTA_FEATURE_MODEL_DIR")
    if not model_dir:  # 没填就用权重目录里找到的第一个 SigLIP2 模型
        from .flow import find_feature_models
        found=find_feature_models(c.engine)
        model_dir=found[0]["path"] if found else None
    if not model_dir:raise ValueError("没找到 SigLIP2 特征模型：请把模型放到权重目录（HF 缓存）里 / No local SigLIP2 feature model found")

    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupShuffleSplit
    if not version:  # 没填就按时间命名
        import time
        version=time.strftime("cls-%Y%m%d-%H%M%S")
    validate_id(version)
    target=c.directory/"classifiers"/version
    if target.exists():raise ValueError("这个分类器版本名已经用过了，换一个 / Classifier version already exists")
    labels={key:value for key,value in human_labels(c.directory).items() if value in c.scenario.categories and c.scenario.categories[value].enabled}
    ids=sorted(labels)
    if len(ids)<4 or len(set(labels.values()))<2:
        raise ValueError("已确认的照片不够：至少要 4 张、覆盖 2 个类别（先选图点“确认所选图片类别”）/ Confirm at least 4 images covering two categories")
    rows=[get_image(c.directory,key) for key in ids]
    y=np.array([labels[key] for key in ids])
    groups=[row.get("site_id") or row.get("duplicate_group") or key for key,row in zip(ids,rows)]
    if len(set(groups))<2:raise ValueError("已确认的照片至少要来自 2 个不同站点或分组 / At least two distinct image groups are required")
    x,fingerprint=features(c,ids,model_dir)
    partition=None
    for tr,va in GroupShuffleSplit(n_splits=40,test_size=.25,random_state=42).split(x,y,groups):
        if set(y[tr])==set(y) and set(y[va])==set(y):
            partition=(tr,va);break
    if partition is None:raise ValueError("每个类别都要有足够多、来自不同站点的确认照片，才能分出训练和验证（每类建议 ≥5 张）/ Each category needs separate training and validation image groups")
    tr,va=partition
    classifier=LogisticRegression(max_iter=2000,C=1)
    classifier.fit(x[tr],y[tr])
    report={"validation_accuracy":float(classifier.score(x[va],y[va])),"train_count":len(tr),"validation_count":len(va),
            "train_sites":sorted(set(np.array(groups)[tr])),"validation_sites":sorted(set(np.array(groups)[va])),
            "grouping":"site" if all(row.get("site_id") for row in rows) else "image_or_duplicate_group"}
    # Final fit uses training groups only; held-out labels stay unused.
    payload={"version":version,"model_dir":str(Path(model_dir).resolve()),"fingerprint":fingerprint,
             "classes":classifier.classes_.tolist(),"coef":classifier.coef_.tolist(),"intercept":classifier.intercept_.tolist(),
             "training_labels":{ids[i]:labels[ids[i]] for i in tr},"training_ids":[ids[i] for i in tr],"validation_ids":[ids[i] for i in va],"report":report}
    import tempfile,shutil
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix="."+version+".",dir=target.parent))
    try:
        (stage/"classifier.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
        stage.rename(target)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return {"version":version,"classes":payload["classes"],"report":report}

def reclassify(c,image_ids,version,min_confidence=.7):
    import numpy as np
    validate_id(version)
    payload=json.loads((c.directory/"classifiers"/version/"classifier.json").read_text(encoding="utf-8"))
    x,fingerprint=features(c,image_ids,payload["model_dir"])
    if fingerprint!=payload["fingerprint"]:raise ValueError("Feature model changed; train a new classifier version")
    logits=x@np.array(payload["coef"]).T+np.array(payload["intercept"])
    if len(payload["classes"])==2:
        positive=1/(1+np.exp(-np.clip(logits[:,0],-60,60)))
        probabilities=np.column_stack([1-positive,positive])
    else:
        logits-=logits.max(axis=1,keepdims=True);probabilities=np.exp(logits);probabilities/=probabilities.sum(axis=1,keepdims=True)
    confirmed=human_labels(c.directory);items=[]
    from .sorting import store_results
    for key,prob in zip(image_ids,probabilities):
        order=np.argsort(prob)[::-1]
        candidates=[{"label":payload["classes"][int(i)],"score":float(prob[i])} for i in order[:5]]
        label=candidates[0]["label"] if candidates[0]["score"]>=min_confidence else "unknown"
        items.append({"image_id":key,"category":confirmed.get(key,label),"candidates":candidates,
                      "review":"confirmed" if key in confirmed else "pending","provenance":"human" if key in confirmed else "trained_classifier",
                      "classifier_version":version})
    store_results(c.directory,items)
    from .store import dumps
    import os,tempfile
    folder=c.directory/"classifiers"
    with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=folder,delete=False) as stream:
        temporary=Path(stream.name)
        stream.write(dumps({"version":version,"min_confidence":min_confidence}))
    os.replace(temporary,folder/"active.json")
    return {"image_ids":image_ids,"items":items,"classifier_version":version}
