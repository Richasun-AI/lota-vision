// 中英文切换：页面照常用中文写；选了 English 时，这里把界面上的中文实时换成英文（字典 /static/i18n/en.json）。
// 顺序：整句精确匹配 → 带变量的模板（已选 {0} 张）→ 按标点拆开逐段翻 → 已知词组替换。
// 新出现的内容（弹窗、提示、接口返回的文字）也会自动翻；输入框里的内容、代码、标了 data-i18n-skip 的不翻。
"use strict";
// 改名兼容（原名 AILabel）：浏览器里存的 ailabel-*（访问口令、当前子项目、语言、勾选）搬到 lota-*，不用重新输入
(() => {
  for (const st of [window.localStorage, window.sessionStorage]) {
    try {
      for (const k of Object.keys(st)) if (k.startsWith("ailabel-")) {
        const n = "lota-" + k.slice(8);
        if (st.getItem(n) == null) st.setItem(n, st.getItem(k));
      }
    } catch {}
  }
})();
(() => {
  const KEY = "lota-lang";
  let lang = "zh";
  try { lang = localStorage.getItem(KEY) || (/^zh/i.test(navigator.language || "zh") ? "zh" : "en"); } catch {}
  const CJK = /[㐀-鿿豈-﫿]/;
  const I18N = window.I18N = {
    lang,
    set(l) { try { localStorage.setItem(KEY, l); } catch {} location.reload(); },
    t: (s) => s,
    toggle() {   // 中 / EN 切换按钮
      const b = document.createElement("button");
      b.className = "lang-toggle"; b.dataset.i18nSkip = "1"; b.type = "button";
      b.title = lang === "en" ? "切换到中文" : "Switch to English";
      b.innerHTML = lang === "en" ? "<b>EN</b> · 中" : "<b>中</b> · EN";
      b.onclick = () => I18N.set(lang === "en" ? "zh" : "en");
      return b;
    },
  };
  document.documentElement.lang = lang === "en" ? "en" : "zh-CN";
  const style = document.createElement("style");
  style.textContent = `.lang-toggle{height:30px;padding:0 10px;border-radius:99px;border:1px solid rgba(255,255,255,.22);background:rgba(255,255,255,.08);color:inherit;cursor:pointer;font-size:12px;white-space:nowrap}
.lang-toggle b{font-weight:800}.lang-toggle:hover{background:rgba(255,255,255,.16)}
.topnav select{max-width:230px!important}@media (max-width:1680px){.topnav .brand small{display:none!important}}@media (max-width:1500px){.topnav a.nav{padding:7px 7px!important;font-size:13px!important}.topnav{gap:2px!important;padding:0 10px!important}.topnav select{max-width:170px!important}}
html[lang=en] .topnav a.nav{padding:7px 7px!important;font-size:13px!important}html[lang=en] .topnav .proj-label{display:none!important}@media (max-width:1500px){html[lang=en] .topnav a.nav{padding:6px 5px!important;font-size:12.5px!important}html[lang=en] .topnav select{max-width:150px!important}}
.lang-float{position:fixed;left:12px;bottom:12px;z-index:95;background:#0e2620;color:#d6e7df;box-shadow:0 2px 10px rgba(0,0,0,.25)}`;
  document.head.append(style);
  // 没有顶部导航的页面（手机页、旧页面）：左下角放一个浮动的切换按钮
  addEventListener("load", () => setTimeout(() => {
    if (!document.querySelector(".lang-toggle")) { const b = I18N.toggle(); b.classList.add("lang-float"); document.body.append(b); }
  }, 800));
  if (lang !== "en") return;

  // ── 字典（同步加载，避免先闪一下中文）──
  let dict = { exact: {}, patterns: [] };
  try {
    const x = new XMLHttpRequest();
    x.open("GET", "/static/i18n/en.json?v=" + (document.currentScript?.src.split("v=")[1] || "1"), false);
    x.send();
    if (x.status === 200) dict = JSON.parse(x.responseText);
  } catch {}
  const exact = new Map(Object.entries(dict.exact || {}));
  // 模板 → 正则；按开头两个字分桶，查找快
  const buckets = new Map(), loose = [];
  for (const [zh, en] of dict.patterns || []) {
    const parts = zh.split(/\{(\d+)\}/), order = [];
    let re = "^";
    parts.forEach((p, i) => { if (i % 2) { re += "([\\s\\S]+?)"; order.push(+p); } else re += p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); });
    const item = { re: new RegExp(re + "$"), order, en };
    const head = parts[0].slice(0, 2);
    if (parts[0].length >= 2) { if (!buckets.has(head)) buckets.set(head, []); buckets.get(head).push(item); } else loose.push(item);
  }
  // 词组替换（最后一招）：两个字以上的已知词，长的优先
  const words = [...exact.keys()].filter((k) => k.length >= 2 && k.length <= 16 && CJK.test(k) && !/[{}]/.test(k)).sort((a, b) => b.length - a.length);
  const wordRe = words.length ? new RegExp(words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g") : null;
  const cache = new Map();

  function pattern(s) {
    for (const list of [buckets.get(s.slice(0, 2)) || [], loose]) {
      for (const p of list) {
        const m = p.re.exec(s);
        if (!m) continue;
        const vals = {};
        p.order.forEach((n, i) => { vals[n] = tr(m[i + 1]); });   // 变量里也可能有中文（类型名、类别名）
        return p.en.replace(/\{(\d+)\}/g, (_, n) => vals[n] ?? "");
      }
    }
    return null;
  }
  const SEP = /(\s*[：:，,；;。！!？?、（）()【】\[\]「」“”"·|\/→←\n]\s*)/;
  function core(s) {
    if (exact.has(s)) return exact.get(s);
    if (s.length > 300) return words1(s);   // 很长的文字（日志、说明）只做词组替换，避免模板匹配回溯太久
    const p = pattern(s);
    if (p != null) return p;
    const segs = s.split(SEP);
    if (segs.length > 60) return words1(s);
    if (segs.length > 1) {
      const out = segs.map((x, i) => (i % 2 ? mapSep(x) : x && CJK.test(x) ? (exact.get(x.trim()) ?? pattern(x.trim()) ?? words1(x)) : x));
      return out.join("");
    }
    return words1(s);
  }
  const SEPMAP = { "：": ": ", "，": ", ", "；": "; ", "。": ". ", "！": "! ", "？": "? ", "、": ", ", "（": " (", "）": ")", "【": "[", "】": "]", "「": "“", "」": "”" };
  const mapSep = (x) => { const t = x.trim(); return t in SEPMAP ? SEPMAP[t] : x; };
  function words1(s) {
    if (!wordRe) return s;
    return s.replace(wordRe, (w) => " " + exact.get(w) + " ").replace(/\s{2,}/g, " ").trim();
  }
  function tr(text) {
    if (!text || !CJK.test(text)) return text;
    if (cache.has(text)) return cache.get(text);
    const lead = text.match(/^\s*/)[0], tail = text.match(/\s*$/)[0], s = text.trim();
    let out = exact.has(s) ? exact.get(s) : null;
    if (out == null) {   // 前后带图标 / 符号的（🗃️ 其他/无关、✓ 确认为本类）：去掉符号再查一次整句
      const m = s.match(/^([^\p{L}\p{N}]*)([\s\S]*?)([^\p{L}\p{N}）)]*)$/u);
      if (m && (m[1] || m[3]) && m[2] && exact.has(m[2])) out = m[1] + exact.get(m[2]) + m[3];
    }
    if (out == null) out = core(s);
    out = lead + out.replace(/\s+([,.;:!?)])/g, "$1").replace(/\(\s+/g, "(") + tail;
    if (cache.size > 20000) cache.clear();
    cache.set(text, out);
    cache.set(out, out);   // 幂等：翻过的结果再翻还是它自己（里面剩的中文不会被反复处理）
    return out;
  }
  I18N.t = tr;

  // ── 页面：文字节点 + 常用属性；新内容自动翻 ──
  const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA", "CODE", "NOSCRIPT"]);
  const ATTRS = ["title", "placeholder", "aria-label", "alt"];
  const skipped = (el) => !el || SKIP.has(el.nodeName) || el.isContentEditable || el.closest?.("[data-i18n-skip],pre.code,.no-i18n");
  const mine = new WeakMap();   // 节点 → 我们刚写进去的文字：监听到自己改的，直接跳过
  function doText(n) {
    if (mine.get(n) === n.data || !CJK.test(n.data) || skipped(n.parentElement)) return;
    const t = tr(n.data);
    mine.set(n, t);
    if (t !== n.data) n.data = t;
  }
  function doAttrs(el) {
    for (const a of ATTRS) {
      const v = el.getAttribute?.(a);
      if (!v || !CJK.test(v) || skipped(el)) continue;
      const t = tr(v);
      if (t !== v) el.setAttribute(a, t);
    }
  }
  function walk(root) {
    if (root.nodeType === 3) return doText(root);
    if (root.nodeType !== 1 || skipped(root)) return;
    doAttrs(root);
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = w.nextNode(); n; n = w.nextNode()) n.nodeType === 3 ? doText(n) : doAttrs(n);
  }
  const mo = new MutationObserver((list) => {
    for (const m of list) {
      if (m.type === "characterData") doText(m.target);
      else if (m.type === "attributes") doAttrs(m.target);
      else m.addedNodes.forEach(walk);
    }
  });
  // 脚本在 <head> 里最先运行：现在就开始监听（解析器后面插入的正文也会触发），DOMContentLoaded 时再完整扫一遍兜底
  walk(document.documentElement);
  mo.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  document.addEventListener("DOMContentLoaded", () => walk(document.documentElement));
  // 浏览器自带的弹框
  for (const k of ["alert", "confirm", "prompt"]) { const f = window[k].bind(window); window[k] = (msg, ...a) => f(tr(String(msg ?? "")), ...a); }
})();
