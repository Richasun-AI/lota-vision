// combo.js —— 让页面上所有单选下拉框（<select>）都能输入文字模糊查找。
// 做法：保留原生 <select>（样式、.value、change 事件、程序赋值都不受影响），
// 只是点开时不弹原生列表，改弹一个带搜索框的列表；选中后写回 select 并触发 input/change。
// 不想要搜索的下拉框加属性 data-nosearch。触屏设备保留系统原生选择器。
(() => {
  if (window.__comboSearch) return;
  window.__comboSearch = true;
  if (window.matchMedia && matchMedia("(pointer: coarse)").matches) return;

  const style = document.createElement("style");
  style.textContent = `
.combo-pop { position: fixed; z-index: 2147483000; background: #fff; color: #1d2b26; border: 1px solid #b9cfc6;
  border-radius: 8px; box-shadow: 0 10px 30px rgba(17,43,33,.22); display: flex; flex-direction: column;
  font: 13px/1.4 system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; overflow: hidden; }
.combo-pop .combo-q { margin: 6px; padding: 6px 8px; border: 1px solid #b9cfc6; border-radius: 6px; font: inherit;
  color: inherit; background: #fff; outline: none; width: auto; height: auto; }
.combo-pop .combo-q:focus { border-color: #2f9e78; box-shadow: 0 0 0 3px rgba(47,158,120,.15); }
.combo-pop .combo-list { overflow-y: auto; padding: 0 4px 4px; }
.combo-pop .combo-item { padding: 5px 8px; border-radius: 5px; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.combo-pop .combo-item.sel { font-weight: 600; }
.combo-pop .combo-item.active { background: #e3f3ec; }
.combo-pop .combo-item.disabled { color: #9aa8a2; cursor: default; }
.combo-pop .combo-item mark { background: #ffe58a; color: inherit; padding: 0; border-radius: 2px; }
.combo-pop .combo-group { padding: 6px 8px 2px; font-size: 11px; color: #6b7d76; }
.combo-pop .combo-empty { padding: 8px; color: #6b7d76; }
.combo-pop .combo-count { padding: 0 10px 6px; font-size: 11px; color: #6b7d76; }`;
  document.head.append(style);

  const MAX = 500;
  let pop = null;   // {el, sel, input, list, count, shown: [{i, el}], active}

  const eligible = (t) => t instanceof HTMLSelectElement && !t.multiple && !(t.size > 1) && !t.disabled &&
    !t.hasAttribute("data-nosearch") && !t.closest("[data-nosearch]");
  const norm = (s) => String(s ?? "").toLowerCase().replace(/\s+/g, " ").trim();

  // 打分：越小越靠前；-1 不匹配。所有空格分隔的词都包含 → 子串匹配；否则按字符顺序（跳字）匹配。
  function score(text, q) {
    if (!q) return 0;
    const t = norm(text);
    const words = q.split(" ").filter(Boolean);
    if (words.every((w) => t.includes(w))) {
      const at = t.indexOf(words[0]);
      return at === 0 ? 0 : 1 + at / 1e4;
    }
    const chars = [...q.replace(/ /g, "")];
    let j = 0;
    for (const ch of t) if (ch === chars[j] && ++j === chars.length) return 2;
    return -1;
  }

  // 把匹配到的词标黄（安全地用 textContent 拼）
  function highlight(el, text, q) {
    const words = q.split(" ").filter(Boolean);
    const low = text.toLowerCase();
    const marks = [];
    for (const w of words) { const at = low.indexOf(w); if (at >= 0) marks.push([at, at + w.length]); }
    marks.sort((a, b) => a[0] - b[0]);
    let pos = 0;
    for (const [a, b] of marks) {
      if (a < pos) continue;
      el.append(text.slice(pos, a));
      const m = document.createElement("mark"); m.textContent = text.slice(a, b); el.append(m);
      pos = b;
    }
    el.append(text.slice(pos));
  }

  function render() {
    const { sel, input, list, count } = pop;
    const q = norm(input.value);
    const opts = [...sel.options].map((o, i) => {
      const s1 = score(o.text, q), s2 = o.value && o.value !== o.text ? score(o.value, q) : -1;
      const s = s1 < 0 ? (s2 < 0 ? -1 : s2 + 0.5) : (s2 < 0 ? s1 : Math.min(s1, s2 + 0.5));
      return { o, i, s };
    }).filter((x) => x.s >= 0);
    if (q) opts.sort((a, b) => a.s - b.s || a.i - b.i);
    list.replaceChildren();
    pop.shown = [];
    let group = null;
    for (const { o, i } of opts.slice(0, MAX)) {
      const g = o.parentElement instanceof HTMLOptGroupElement ? o.parentElement.label : null;
      if (!q && g && g !== group) {
        const gh = document.createElement("div"); gh.className = "combo-group"; gh.textContent = g; list.append(gh);
      }
      group = g;
      const it = document.createElement("div");
      it.className = "combo-item" + (i === sel.selectedIndex ? " sel" : "") + (o.disabled ? " disabled" : "");
      it.dataset.i = i;
      it.title = o.text;
      if (q) highlight(it, o.text || " ", q); else it.textContent = o.text || " ";
      list.append(it);
      pop.shown.push({ i, el: it, disabled: o.disabled });
    }
    if (!pop.shown.length) {
      const e = document.createElement("div"); e.className = "combo-empty"; e.textContent = "没有匹配的选项"; list.append(e);
    }
    count.textContent = q ? `匹配 ${opts.length} / ${sel.options.length} 项` + (opts.length > MAX ? `（只显示前 ${MAX} 项）` : "") : "";
    count.style.display = q ? "" : "none";
    const cur = pop.shown.findIndex((x) => x.i === sel.selectedIndex);
    setActive(!q && cur >= 0 ? cur : pop.shown.findIndex((x) => !x.disabled));
  }

  function setActive(k) {
    if (!pop) return;
    pop.shown[pop.active]?.el.classList.remove("active");
    pop.active = k;
    const it = pop.shown[k];
    if (it) { it.el.classList.add("active"); it.el.scrollIntoView({ block: "nearest" }); }
  }

  function move(d) {
    const n = pop.shown.length;
    if (!n) return;
    let k = pop.active;
    for (let step = 0; step < n; step++) {
      k = Math.max(0, Math.min(n - 1, (k < 0 ? (d > 0 ? -1 : n) : k) + d));
      if (!pop.shown[k].disabled) break;
    }
    setActive(k);
  }

  function place() {
    const { el, sel, list } = pop;
    const r = sel.getBoundingClientRect();
    const vw = innerWidth, vh = innerHeight;
    const width = Math.min(Math.max(r.width, 240), vw - 16);
    const below = vh - r.bottom - 8, above = r.top - 8;
    const down = below >= 220 || below >= above;
    const room = Math.max(120, Math.min(360, (down ? below : above) - 50));
    list.style.maxHeight = room + "px";
    el.style.width = width + "px";
    el.style.left = Math.max(8, Math.min(r.left, vw - width - 8)) + "px";
    el.style.top = "0px";
    // 如果某个祖先有 transform，fixed 会相对它定位：量一下偏差再校正
    const off = el.getBoundingClientRect();
    const left = parseFloat(el.style.left);
    el.style.left = left - (off.left - left) + "px";
    const top = down ? r.bottom + 2 : r.top - 2 - el.offsetHeight;
    el.style.top = top - off.top + "px";
  }

  function choose(i) {
    const sel = pop.sel;
    close();
    if (sel.selectedIndex !== i) {
      sel.selectedIndex = i;
      sel.dispatchEvent(new Event("input", { bubbles: true }));
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    sel.focus();
  }

  function close(refocus = false) {
    if (!pop) return;
    const { el, sel } = pop;
    pop = null;
    el.remove();
    if (refocus) sel.focus();
  }

  function open(sel, initial = "") {
    close();
    const host = sel.closest("dialog") || document.body;   // 模态对话框在顶层，弹层必须放进去
    const el = document.createElement("div"); el.className = "combo-pop";
    const input = document.createElement("input");
    input.type = "text"; input.className = "combo-q"; input.autocomplete = "off"; input.spellcheck = false;
    input.placeholder = "输入文字筛选（支持空格分隔多个词）";
    input.value = initial;
    const count = document.createElement("div"); count.className = "combo-count";
    const list = document.createElement("div"); list.className = "combo-list";
    el.append(input, count, list);
    host.append(el);
    pop = { el, sel, input, list, count, shown: [], active: -1 };
    render();
    place();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    input.addEventListener("input", () => { render(); place(); });
    input.addEventListener("keydown", (e) => {
      if (e.isComposing) return;
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "PageDown") { e.preventDefault(); move(8); }
      else if (e.key === "PageUp") { e.preventDefault(); move(-8); }
      else if (e.key === "Enter") {
        e.preventDefault(); e.stopPropagation();
        const it = pop.shown[pop.active]; if (it && !it.disabled) choose(it.i);
      } else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); }
      else if (e.key === "Tab") close();
      e.stopPropagation();   // 别让页面上的快捷键（← → X Delete 等）吃掉输入
    });
    list.addEventListener("mousedown", (e) => e.preventDefault());   // 保持搜索框焦点
    list.addEventListener("click", (e) => {
      const it = e.target.closest(".combo-item");
      if (it && !it.classList.contains("disabled")) choose(+it.dataset.i);
    });
    el.addEventListener("focusout", (e) => { if (pop && !el.contains(e.relatedTarget)) close(); });
  }

  // 事件委托：动态生成的下拉框也自动生效
  document.addEventListener("mousedown", (e) => {
    if (pop && !pop.el.contains(e.target) && e.target !== pop.sel) close();
    const sel = e.target instanceof HTMLOptionElement ? e.target.parentElement?.closest("select") : e.target;
    if (e.button !== 0 || !eligible(sel)) return;
    e.preventDefault();
    if (pop && pop.sel === sel) { close(true); return; }
    sel.focus();
    open(sel);
  }, true);

  document.addEventListener("keydown", (e) => {
    const sel = e.target;
    if (!eligible(sel) || (pop && pop.sel === sel)) return;
    const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (printable || e.key === "Process" || e.key === "F4" || e.key === "Enter" || (e.altKey && e.key === "ArrowDown")) {
      e.preventDefault(); e.stopPropagation();
      open(sel, printable && e.key !== " " ? e.key : "");
    }
  }, true);

  const reposition = (e) => { if (pop && !(e && e.target instanceof Node && pop.el.contains(e.target))) close(); };
  addEventListener("resize", () => close());
  document.addEventListener("scroll", reposition, true);
  document.addEventListener("close", () => close(), true);   // 对话框关闭时收起
})();
