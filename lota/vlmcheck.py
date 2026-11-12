"""AI 预检（qwen 看图）：用本机 Ollama 的视觉大模型判断照片能不能做训练数据。

从旧 7870「巡检图片筛选」（pipeline/hq_filter.py 的 ask_vlm + judge）迁移过来，改成工作流节点：
- 问 qwen：是不是现场实拍、是不是截图/文档、主体清不清楚（1–5）、有哪些画质问题、看到哪些设备和异常；
  本场景定义了照片类别时，顺便让它挑一个类别
- 按规则判定 通过 / 不通过，两路输出分别接下游；可选把不通过的直接标为“排除”（不删除，可恢复）
- 同一张图 + 同一个模型判断过就复用结果（存在场景的 catalog/vlm_check.sqlite3），重复运行不重复花时间
"""
import base64
import io
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing

from pydantic import Field

from . import config as deploy
from .tools import TOOLS, Selection, Tool

OLLAMA = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
PROBLEMS = ["模糊", "过暗", "过曝", "遮挡", "主体太小", "严重倾斜", "大量水印或文字覆盖", "手指遮挡", "反光"]
# 巡检 App 照片普遍带时间/地点水印，属正常，不作为淘汰理由（仍记录在 problems 里）
SEVERE = {"模糊", "过暗", "过曝", "遮挡", "手指遮挡"}


class VlmCheck(Selection):
    model: str = Field(default="qwen2.5vl:7b", max_length=100)
    min_clarity: int = Field(default=4, ge=1, le=5)
    reject_failed: bool = False
    recheck: bool = False


# 默认提示词；<<PROBLEMS>>、<<TYPE_LINE>> 运行时替换。私有目录有 vlm_prompt.txt 就用那个
DEFAULT_PROMPT = """你是现场设备巡检专家，正在为AI训练挑选高质量的现场照片。仔细看这张图，只输出JSON：
{
 "is_site_photo": 是否为在现场实拍的设备或设施照片(true/false),
 "is_screenshot_or_doc": 是否为手机截图、App界面、表格、文档、聊天记录、证件等非现场照片(true/false),
 "subject_clarity": 主体设备是否清晰可辨，1=完全看不清 2=很差 3=一般 4=清楚 5=非常清楚,
 "problems": 从[<<PROBLEMS>>]中选出存在的，没有则[],
 "objects": 图中主要设备部件(中文，最多6个),
 "anomalies": 可见的设备异常(如锈蚀,漏油,线缆凌乱,柜门未关,杂草,异物,告警灯红,接地线缺失,电池鼓包)，没有则[]<<TYPE_LINE>>
}"""


def _prompt(types):
    type_line = ""
    if types:
        desc = "；".join(f"{k}={v}" for k, v in types.items())
        type_line = f',\n "photo_type": 从这些key中选一个最符合的，都不符合填"other"：{desc}'
    tmpl = deploy.private_text("vlm_prompt.txt") or DEFAULT_PROMPT   # 自己业务专用的提示词放私有目录
    return tmpl.replace("<<PROBLEMS>>", ",".join(PROBLEMS)).replace("<<TYPE_LINE>>", type_line)


def ask(image_path, prompt, model):
    from PIL import Image, ImageOps
    with Image.open(image_path) as im:   # 原图直接送会让 Ollama 报 500：先缩到长边 1024
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
    body = {"model": model, "prompt": prompt, "images": [base64.b64encode(buf.getvalue()).decode()],
            "format": "json", "stream": False, "keep_alive": "10m", "options": {"temperature": 0}}
    req = urllib.request.Request(f"{OLLAMA}/api/generate", json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(json.loads(r.read())["response"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"Ollama 里没有模型 {model}：先在服务器上 ollama pull {model}") from e
        raise
    except urllib.error.URLError as e:
        raise ValueError(f"连不上 Ollama（{OLLAMA}）：确认服务器上 Ollama 在运行") from e


def judge(v, min_clarity=4, types=None):
    """规则判定（沿用旧 hq_filter.judge）：返回 (通过?, 原因)"""
    if not v:
        return False, "没有结果"
    if v.get("is_screenshot_or_doc") is True:
        return False, "截图/文档"
    if v.get("is_site_photo") is not True:
        return False, "不是现场照片"
    if types and v.get("photo_type") not in types:
        return False, "和本场景的照片类别都不相关"
    try:
        clarity = int(v.get("subject_clarity", 0))
    except (TypeError, ValueError):
        clarity = 0
    if clarity < min_clarity:
        return False, f"主体清晰度 {clarity} < {min_clarity}"
    bad = SEVERE & set(v.get("problems") or [])
    if bad:
        return False, "画质问题：" + "/".join(sorted(bad))
    return True, "通过"


def _cache(directory):
    d = directory / "catalog"
    d.mkdir(exist_ok=True)
    db = sqlite3.connect(d / "vlm_check.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS results(image_id TEXT, model TEXT, prompt_hash TEXT, payload TEXT, ts REAL,"
               " PRIMARY KEY(image_id, model, prompt_hash))")
    return db


def vlm_check(c, image_ids, model="qwen2.5vl:7b", min_clarity=4, reject_failed=False, recheck=False):
    import hashlib
    from .catalog import get_image, review_image
    ids = list(dict.fromkeys(image_ids))
    cats = {k: v for k, v in (c.scenario.categories or {}).items() if v.enabled}
    types = {k: v.name + (f"/{v.name_en}" if v.name_en else "") for k, v in cats.items()}
    prompt = _prompt(types)
    ph = hashlib.sha1(prompt.encode()).hexdigest()[:12]
    items, passed, failed, errors, reused = [], [], [], [], 0
    t0 = time.time()
    with closing(_cache(c.directory)) as db:
        for key in ids:
            row = None if recheck else db.execute(
                "SELECT payload FROM results WHERE image_id=? AND model=? AND prompt_hash=?", (key, model, ph)).fetchone()
            if row:
                v, reused = json.loads(row[0]), reused + 1
            else:
                try:
                    v = ask(c.engine.image_path(c.directory, get_image(c.directory, key), False), prompt, model)
                except ValueError:
                    raise   # Ollama 没开 / 没模型：整个任务直接报中文错误
                except Exception as e:   # 单张图坏了不影响其他图
                    errors.append({"image_id": key, "error": str(e)[:200]})
                    continue
                with db:
                    db.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?)",
                               (key, model, ph, json.dumps(v, ensure_ascii=False), time.time()))
            ok, reason = judge(v, min_clarity, types)
            (passed if ok else failed).append(key)
            items.append({"image_id": key, "pass": ok, "reason": reason, "clarity": v.get("subject_clarity"),
                          "photo_type": v.get("photo_type"), "problems": v.get("problems") or [],
                          "objects": v.get("objects") or [], "anomalies": v.get("anomalies") or []})
            if not ok and reject_failed:
                review_image(c.directory, key, decision="rejected", note=f"AI 预检不通过：{reason}")
    reasons = {}
    for it in items:
        if not it["pass"]:
            reasons[it["reason"]] = reasons.get(it["reason"], 0) + 1
    return {"image_ids": passed, "failed_ids": failed, "passed": len(passed), "failed": len(failed),
            "reasons": reasons, "reused": reused, "errors": errors, "seconds": round(time.time() - t0, 1),
            "rejected": len(failed) if reject_failed else 0, "items": items}


TOOLS.update({"sorting.vlm_check": Tool("AI 预检（qwen 看图）", VlmCheck, vlm_check)})
