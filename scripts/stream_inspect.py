#!/usr/bin/env python3
"""流式质检：盯住照片下载器的下载目录，新照片一落地就推给 LOTA 引擎质检。

目录结构（下载器生成）：downloads/<工单>/<站点>/<图片类型>/<附件ID>.jpg
照片类型取自目录名（检查点名称），引擎按“质检规则”里的“适用照片”匹配。

用法（本机 PowerShell）：
  $env:LOTA_API_TOKEN="你的口令"
  python scripts/stream_inspect.py --project demo --watch D:/photos/downloads
  加 --once 只扫一遍就退出；--only-fail 只打印不合格的。已推过的记在 <watch>/.ailabel_inspected.json，可断点续推。
  --project all：不指定子项目，按照片类型交给所有适用的质检规则；结果按工单/站点汇总在网页 /report（巡检审核报告）。
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 改名兼容（原名 AILabel）：没设 LOTA_* 时用旧的 AILABEL_*
for _k, _v in list(os.environ.items()):
    if _k.startswith("AILABEL_"):
        os.environ.setdefault("LOTA_" + _k[8:], _v)


EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
MARK = {"pass": "✅ 合格", "warn": "⚠️ 需注意", "fail": "❌ 不合格", "skip": "·  无规则"}


def post(url, token, body):
    path = "/api/agent/inspect-all" if body.get("project") == "all" else "/api/agent/inspect"   # all = 交给所有适用的子项目
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def meta_of(path: Path, root: Path):
    parts = path.relative_to(root).parts
    m = {"file": path.name, "source": "stream_inspect"}
    if len(parts) >= 4:
        m.update(order=parts[-4], site=parts[-3], photo_type=parts[-2])
    side = path.with_suffix(".json")          # 下载器给每张图配的 JSON（检查点、巡检项等）
    if side.is_file():
        try:
            j = json.loads(side.read_text(encoding="utf-8"))
            for k in ("CHECK_POINT_NAME", "ITEM_NAME", "check_point", "item_name", "checkpoint"):
                if j.get(k):
                    m.setdefault("checkpoint", j[k])
        except ValueError:
            pass
    return m


def main():
    try:   # Windows 控制台（GBK）打印不了 ✅❌ 之类的符号：打印不了的字符替换掉，不崩
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="把下载目录里的新照片流式推给 LOTA 质检")
    ap.add_argument("--project", required=True, help="子项目 ID；all = 交给所有适用的子项目（推荐，配合 /report 审核报告）")
    ap.add_argument("--watch", required=True, help="下载目录")
    ap.add_argument("--url", default=os.environ.get("LOTA_URL", "http://127.0.0.1:8795"))
    ap.add_argument("--token", default=os.environ.get("LOTA_API_TOKEN", ""))
    ap.add_argument("--interval", type=float, default=10, help="多少秒扫一次新文件")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--only-fail", action="store_true")
    a = ap.parse_args()
    root = Path(a.watch)
    state_file = root / ".ailabel_inspected.json"
    done = set(json.loads(state_file.read_text(encoding="utf-8"))) if state_file.is_file() else set()
    stats = {}
    print(f"监视 {root} → {a.url}（子项目 {a.project}），已推过 {len(done)} 张")
    while True:
        new = sorted(p for p in root.rglob("*") if p.suffix.lower() in EXT and str(p) not in done
                     and time.time() - p.stat().st_mtime > 2)     # 2 秒内刚写的先不推，等下载完
        for p in new:
            m = meta_of(p, root)
            ptype = " ".join(x for x in (m.get("photo_type"), m.get("checkpoint")) if x)
            try:
                r = post(a.url, a.token, {"project": a.project, "image_base64": base64.b64encode(p.read_bytes()).decode(),
                                          "photo_type": ptype, "meta": m})
            except (urllib.error.URLError, OSError) as e:
                print(f"推送失败 {p.name}：{e}（稍后重试）", file=sys.stderr)
                break
            v = r.get("verdict", "skip")
            stats[v] = stats.get(v, 0) + 1
            done.add(str(p))
            if not a.only_fail or v in ("fail", "warn"):
                res = [x for pr in r.get("projects", [r]) for x in pr.get("results", [])]
                why = "；".join(f["message"] for x in res for f in x["findings"] if f.get("hit") and f.get("message"))
                print(f"{MARK.get(v, v)}  {m.get('site', '')} {m.get('photo_type', '')} {p.name}  {why}  ({r.get('ms')} ms)")
        if new:
            state_file.write_text(json.dumps(sorted(done), ensure_ascii=False), encoding="utf-8")
            print("累计：" + "，".join(f"{MARK.get(k, k)} {n}" for k, n in stats.items()))
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
