"""提取界面里所有中文文字 → i18n/strings.json（给翻译用）。

来源：lota/static/*.js、*.html（字符串、模板字符串、HTML 文字），lota/*.py（字符串、f-string；跳过文档字符串）。
模板里的变量换成 {0} {1}…：`已选 ${n} 张` → "已选 {0} 张"。改了界面后重新运行一次，新文字会追加进来。

用法：python scripts/i18n_extract.py
"""
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "lota"
OUT = ROOT / "i18n" / "strings.json"
CJK = re.compile(r"[㐀-鿿豈-﫿]")
# 和 i18n.js 里的 SEP 一致
SEP = re.compile(r"\s*[：:，,；;。！!？?、（）()【】\[\]「」“”\"·|/→←\n]\s*")


# ── JavaScript：简单词法扫描（注释、字符串、模板字符串、正则字面量）──────────────────────
def js_strings(src):
    out, i, n = [], 0, len(src)
    prev = ""            # 上一个有意义的字符，用来判断 / 是除号还是正则

    def skip_expr(j):    # 跳过 ${ ... }，返回 } 之后的位置
        depth = 1
        while j < n and depth:
            c = src[j]
            if c in "\"'":
                j = skip_quote(j, c)
                continue
            if c == "`":
                _, j = read_template(j)
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        return j

    def skip_quote(j, q):
        j += 1
        while j < n and src[j] != q:
            if src[j] == "\\":
                j += 1
            elif src[j] == "\n":
                break
            j += 1
        return j + 1

    def read_template(j):
        j += 1
        parts, buf, k = [], [], 0
        while j < n and src[j] != "`":
            c = src[j]
            if c == "\\" and j + 1 < n:
                buf.append(_unescape(src[j:j + 2]))
                j += 2
                continue
            if c == "$" and j + 1 < n and src[j + 1] == "{":
                buf.append("{%d}" % k)
                k += 1
                j = skip_expr(j + 2)
                continue
            buf.append(c)
            j += 1
        return "".join(buf), j + 1

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            i = src.find("\n", i)
            i = n if i < 0 else i
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i = src.find("*/", i + 2)
            i = n if i < 0 else i + 2
            continue
        if c == "/" and (prev in "(,=:[!&|?{};+-*%<>~^" or prev == "" or src[max(0, i - 7):i].rstrip().endswith("return")):
            j = i + 1          # 正则字面量
            cls = False
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    cls = True
                elif src[j] == "]":
                    cls = False
                elif src[j] == "/" and not cls:
                    break
                j += 1
            i = j + 1
            prev = "x"
            continue
        if c in "\"'":
            j = skip_quote(i, c)
            out.append(_unescape_all(src[i + 1:j - 1]))
            i, prev = j, "x"
            continue
        if c == "`":
            s, i = read_template(i)
            out.append(s)
            prev = "x"
            continue
        if not c.isspace():
            prev = c if not (c.isalnum() or c in "_$") else "x"
        i += 1
    return out


def _unescape(two):
    return {"\\n": "\n", "\\t": "\t", "\\\"": "\"", "\\'": "'", "\\`": "`", "\\\\": "\\", "\\$": "$"}.get(two, two[1:])


def _unescape_all(s):
    return re.sub(r"\\(.)", lambda m: _unescape(m.group(0)), s)


# ── HTML：标签之间的文字、title / placeholder 属性 ───────────────────────────────────
def html_strings(src):
    src = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>|<!--.*?-->", " ", src, flags=re.S)
    out = [m.group(1) for m in re.finditer(r">([^<>]+)<", src)]
    out += [m.group(2) for m in re.finditer(r'\b(title|placeholder|alt|aria-label)="([^"]*)"', src)]
    return out


# ── Python：字符串和 f-string（跳过文档字符串）─────────────────────────────────────────
def py_strings(src):
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs.add(id(first.value))
    out, inside = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            buf, k = [], 0
            for v in node.values:
                inside.add(id(v))
                if isinstance(v, ast.Constant):
                    buf.append(str(v.value))
                else:
                    buf.append("{%d}" % k)
                    k += 1
            out.append("".join(buf))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs and id(node) not in inside:
            out.append(node.value)
    return out


def split_lines(s):
    """多行文字按行拆开（界面上每行常常是独立的文字节点）"""
    return [x for x in (p.strip() for p in s.split("\n")) if x]


def main():
    found = {}
    files = sorted(APP.glob("static/*.js")) + sorted(APP.glob("static/*.html")) + sorted(APP.glob("*.py"))
    for f in files:
        if f.name.startswith("i18n"):
            continue
        src = f.read_text(encoding="utf-8")
        try:
            items = js_strings(src) if f.suffix == ".js" else html_strings(src) if f.suffix == ".html" else py_strings(src)
        except SyntaxError as e:
            print("跳过（解析失败）", f.name, e, file=sys.stderr)
            continue
        for s in items:
            texts = [s]
            if "<" in s and ">" in s:          # 字符串里拼的 HTML：拆出标签之间的文字和 title 等属性
                texts += html_strings(s)
            for t in texts:
                for line in split_lines(t):
                    if not CJK.search(line) or len(line) > 600:
                        continue
                    found.setdefault(line, set()).add(f.name)
                    # 按标点拆出的短语也单独翻：运行时拼起来的句子，能逐段换成像样的英文
                    for seg in SEP.split(line):
                        seg = seg.strip()
                        if seg != line and len(CJK.findall(seg)) >= 2 and len(seg) <= 80:
                            found.setdefault(seg, set()).add(f.name)
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.is_file() else {}
    data = {k: sorted(v) for k, v in sorted(found.items())}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    new = [k for k in data if k not in old]
    print(f"共 {len(data)} 条中文（新增 {len(new)} 条），{sum(len(k) for k in data)} 个字 → {OUT}")


if __name__ == "__main__":
    main()
