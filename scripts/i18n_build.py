"""合并翻译 → lota/static/i18n/en.json（界面用的字典）。

优先级：i18n/en.manual.json（手工）> i18n/en.auto.json（机器翻）> i18n/glossary.json（术语，首字母大写）
另外把 pipeline 照片类型的中文名 → 英文名也加进来（TAXONOMY 里本来就有 name_en）。
带 {0} 的进 patterns（模板），其余进 exact（整句）。

用法：python scripts/i18n_build.py
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "i18n"
APP = ROOT / "app" / "lota" if (ROOT / "app" / "lota").is_dir() else ROOT / "lota"   # 服务器上代码在 app/lota
OUT = APP / "static" / "i18n" / "en.json"
CJK = re.compile(r"[㐀-鿿豈-﫿]")


def load(name):
    f = I18N / name
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def main():
    merged = {}
    for k, v in load("glossary.json").items():
        merged[k] = v[:1].upper() + v[1:]
    try:   # 照片类型名（服务器上才有 pipeline）
        sys.path.insert(0, os.environ.get("LOTA_LEGACY_PIPELINE") or str(Path.home() / "lota-data/pipeline"))
        import hq_filter as hf
        for k, t in hf.TAXONOMY.items():
            if t.get("name") and t.get("name_en"):
                merged.setdefault(t["name"], t["name_en"])
    except Exception:
        pass
    merged.update({k: v for k, v in load("en.auto.json").items() if v and not CJK.search(v)})
    merged.update(load("en.manual.json"))
    exact, patterns = {}, []
    for k, v in merged.items():
        if re.search(r"\{\d+\}", k):
            patterns.append([k, v])
        else:
            exact[k] = v
    patterns.sort(key=lambda p: -len(re.sub(r"\{\d+\}", "", p[0])))   # 字面部分长的先试，匹配更准
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"exact": exact, "patterns": patterns}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    strings = load("strings.json")
    missing = [s for s in strings if s not in merged]
    print(f"字典：整句 {len(exact)} 条，模板 {len(patterns)} 条；界面文字 {len(strings)} 条里还缺 {len(missing)} 条 → {OUT}"
          f"（{OUT.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()
