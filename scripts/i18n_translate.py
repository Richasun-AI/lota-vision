"""把 i18n/strings.json 里的中文翻成英文 → i18n/en.auto.json（用服务器本机的 Ollama，可断点续跑）。

- 术语表 i18n/glossary.json 强制统一叫法（子项目 = Project、正式使用 = Production …）
- 校验：{0} 这类占位符一个不能少、译文里不能再有中文；不合格的重试一次，还不行就留空（界面上回退到逐词替换）
- 每批之前查一下引擎有没有任务在跑（训练要用显卡），有就等，不和训练抢显存；模型用完 2 分钟后自动卸载

用法（服务器）：python3 scripts/i18n_translate.py [--model qwen3.5:9b] [--batch 30]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# 改名兼容（原名 AILabel）：没设 LOTA_* 时用旧的 AILABEL_*
for _k, _v in list(os.environ.items()):
    if _k.startswith("AILABEL_"):
        os.environ.setdefault("LOTA_" + _k[8:], _v)


ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "i18n"
CJK = re.compile(r"[㐀-鿿豈-﫿]")
PH = re.compile(r"\{\d+\}")


def http(url, body=None, headers=None, timeout=600):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def engine_busy():
    """引擎里有排队或运行中的任务（训练、找图……）就算忙"""
    token = os.environ.get("LOTA_API_TOKEN", "")
    try:
        s = http("http://127.0.0.1:8795/api/system", headers={"Authorization": "Bearer " + token} if token else {}, timeout=10)
        return [f"{j['project']}/{j['kind']}" for j in s.get("jobs", [])]
    except Exception:
        return []


def prompt(batch, glossary):
    terms = "\n".join(f"- {k} = {v}" for k, v in glossary.items())
    items = json.dumps({str(i): s for i, s in enumerate(batch)}, ensure_ascii=False, indent=0)
    return f"""You translate the user interface of "LOTA", a computer-vision platform (photo annotation with Label Studio, YOLO model training, AI inspection of telecom site photos), from Simplified Chinese to English.

Rules:
- Natural, concise product English (like GitHub / Ultralytics UI). Buttons short, sentences clear. Keep the friendly plain-language tone.
- Keep placeholders like {{0}} {{1}} exactly, in a sensible position.
- Keep emoji, symbols (✓ → ← · %), numbers, units, file names, code, URLs, model names (YOLO26, yolov8n, mAP50 …) and English words unchanged.
- Chinese measure words: 张 = photo(s)/image(s), 个 = (omit or "items"), 轮 = epochs, 类 = classes.
- Output ONLY a JSON object with the same keys and the English text as values. No Chinese characters in the output.
- Use these fixed terms:
{terms}

Translate:
{items}"""


def translate(batch, glossary, model, url):
    r = http(url + "/api/chat", {"model": model, "stream": False, "format": "json", "think": False, "keep_alive": "2m",
                                 "options": {"temperature": 0.1, "num_ctx": 8192},
                                 "messages": [{"role": "user", "content": prompt(batch, glossary)}]})
    txt = r["message"]["content"]
    try:
        data = json.loads(txt)
    except ValueError:
        m = re.search(r"\{.*\}", txt, re.S)
        data = json.loads(m.group(0)) if m else {}
    return {batch[int(k)]: v for k, v in data.items() if k.isdigit() and int(k) < len(batch) and isinstance(v, str)}


def ok(zh, en):
    return bool(en.strip()) and not CJK.search(en) and sorted(PH.findall(zh)) == sorted(PH.findall(en))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--batch", type=int, default=30)
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    a = ap.parse_args()
    strings = list(json.loads((I18N / "strings.json").read_text(encoding="utf-8")))
    glossary = json.loads((I18N / "glossary.json").read_text(encoding="utf-8"))
    manual = json.loads((I18N / "en.manual.json").read_text(encoding="utf-8")) if (I18N / "en.manual.json").is_file() else {}
    out_f = I18N / "en.auto.json"
    done = json.loads(out_f.read_text(encoding="utf-8")) if out_f.is_file() else {}
    todo = [s for s in strings if s not in done and s not in manual]
    print(f"共 {len(strings)} 条，已有 {len(done)}，手工 {len(manual)}，待翻译 {len(todo)}", flush=True)
    failed = []
    t0 = time.time()
    for i in range(0, len(todo), a.batch):
        while busy := engine_busy():
            print("引擎有任务在跑，先不占显卡：", "、".join(busy[:3]), "（60 秒后再看）", flush=True)
            time.sleep(60)
        batch = todo[i:i + a.batch]
        try:
            got = translate(batch, glossary, a.model, a.url)
        except Exception as e:
            print("这一批失败：", e, flush=True)
            got = {}
        retry = [s for s in batch if not ok(s, got.get(s, ""))]
        if retry:     # 不合格的单独再来一次（批小一点更稳）
            try:
                got.update({k: v for k, v in translate(retry, glossary, a.model, a.url).items() if ok(k, v)})
            except Exception:
                pass
        for s in batch:
            if ok(s, got.get(s, "")):
                done[s] = got[s].strip()
            else:
                failed.append(s)
        out_f.write_text(json.dumps(done, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        n = min(i + a.batch, len(todo))
        eta = (time.time() - t0) / n * (len(todo) - n)
        print(f"{n}/{len(todo)}  失败 {len(failed)}  还要约 {eta / 60:.0f} 分钟", flush=True)
    (I18N / "en.failed.json").write_text(json.dumps(failed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"完成：译好 {len(done)} 条，没译成 {len(failed)} 条（见 en.failed.json）")


if __name__ == "__main__":
    sys.exit(main())
