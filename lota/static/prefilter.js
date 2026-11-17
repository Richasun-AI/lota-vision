// 初选：给原图目录逐张打画质分 → 分档看图、人工改判 → 入选的硬链接到精选目录（原图不动）→ 精选目录到“照片类型”页建图库
"use strict";
const { h } = UI;
const JK = "lota.prefilter.job";
let JOBS = [], cur = null, S = null, view = "keep", sort = "score_desc", offset = 0, sel = new Set(), poll = null, LIST = [], TOTAL = 0;
const TIER = { high: "高", medium: "中", low: "低", rejected: "淘汰" };
const thumb = (p, size = 320) => `/api/prefilter/jobs/${cur}/thumb?path=${encodeURIComponent(p)}&size=${size}`;
const original = (p) => `/api/prefilter/jobs/${cur}/original?path=${encodeURIComponent(p)}`;
const store = { get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* 不让存就算了 */ } } };

async function load() {
  JOBS = await UI.api("/api/prefilter/jobs");
  if (!cur || !JOBS.some((j) => j.id === cur)) cur = JOBS.find((j) => j.id === store.get(JK))?.id || JOBS[0]?.id || null;
  if (cur) S = await UI.api(`/api/prefilter/jobs/${cur}`);
  render();
}

function render() {
  const app = document.getElementById("app");
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "初选"), h("p", {}, "先把画质差的挡在外面：逐张算分辨率、清晰度、曝光、对比度，找出重复和几乎一样的照片；按档看图、人工改判；入选的",
        h("b", {}, "硬链接"), "到精选目录（原图不动，不占额外空间）。精选目录再到“照片类型”页建成图库。原图目录补了新照片，点“增量更新”只处理新增的。")),
      h("div", { class: "row" }, h("button", { class: "btn primary lg", onclick: newJobDlg }, "＋ 新建初选"))),
    h("div", { class: "pf" },
      h("div", { class: "card jlist" }, ...(JOBS.length ? JOBS.map((j) => h("div", { class: "jitem" + (j.id === cur ? " on" : ""), onclick: () => pick(j.id) },
        h("div", {}, h("b", {}, j.name), j.run?.running ? h("span", { class: "badge info", style: { marginLeft: "6px" } }, "扫描中") : null),
        h("div", { class: "p" }, j.source),
        h("div", { class: "tiny muted" }, j.scanned ? `${(j.files || 0).toLocaleString()} 张 · 扫描于 ${j.scanned}` : "还没扫描")))
        : [h("div", { class: "empty small" }, "还没有初选任务：点右上角“新建初选”，选一个原图目录")])),
      h("div", { id: "main" })));
  drawMain();
}
async function pick(id) {
  cur = id; store.set(JK, id); view = "keep"; offset = 0; clearSel();
  S = await UI.api(`/api/prefilter/jobs/${cur}`); render();
}

// ── 选目录 ──
function dirPicker(start, onPick, { title = "选择目录", allowNew = false } = {}) {
  let path = start || "";
  const list = h("div", { class: "dirs" }), here = h("div", { class: "small mono", style: { margin: "6px 0", wordBreak: "break-all" } });
  const extra = h("input", { class: "input", placeholder: "新建子目录名（可不填）", style: { display: allowNew ? "" : "none" } });
  const go = async (p) => {
    try {
      const d = await UI.api(`/api/prefilter/dirs?path=${encodeURIComponent(p || "")}`);
      path = d.path;
      here.textContent = d.path ? `${d.path}（这一层有 ${d.images_here} 张图片）` : "允许使用的图片目录：";
      list.replaceChildren(...(d.parent !== null && d.path ? [h("div", { onclick: () => go(d.parent) }, "⬆ 上一级")] : []),
        ...d.dirs.map((x) => h("div", { onclick: () => go(x.path) }, "📁 " + x.name)),
        ...(d.dirs.length ? [] : [h("div", { class: "muted" }, "（没有子目录）")]));
    } catch (e) { UI.toast(e.message, true); if (p) go(""); }
  };
  go(path);
  UI.modal(title, h("div", {}, here, list, allowNew ? h("div", { style: { marginTop: "8px" } }, extra) : null), [
    { label: "取消" },
    { label: "选这个目录", kind: "primary", run: () => {
      if (!path) throw new Error("先点进一个目录");
      onPick(extra.value.trim() ? path.replace(/\/$/, "") + "/" + extra.value.trim() : path);
    } }], { wide: true });
}

function newJobDlg() {
  const src = h("input", { class: "input", placeholder: "如 /data/images/raw/downloads" });
  const name = h("input", { class: "input", placeholder: "可不填，默认用目录名" });
  UI.modal("新建初选", h("div", { class: "form" },
    h("div", { class: "field" }, h("label", {}, "原图目录（含子目录）"), h("div", { class: "row" }, src,
      h("button", { class: "btn", onclick: () => dirPicker(src.value, (p) => { src.value = p; }) }, "浏览…"))),
    h("div", { class: "field" }, h("label", {}, "名称"), name),
    h("p", { class: "small muted" }, "只读原图、不移动不改名。建好后点“扫描”开始打分；同一个目录只会有一个初选任务。")), [
    { label: "取消" },
    { label: "建好并开始扫描", kind: "primary", run: async () => {
      const j = await UI.api("/api/prefilter/jobs", { method: "POST", body: { source: src.value.trim(), name: name.value.trim() } });
      cur = j.id; store.set(JK, j.id);
      await UI.api(`/api/prefilter/jobs/${cur}/scan`, { method: "POST", body: {} });
      await load(); watch();
    } }]);
}

// ── 主区 ──
function drawMain() {
  const main = document.getElementById("main"); if (!main) return;
  if (!cur || !S) return main.replaceChildren(h("div", { class: "card empty" }, "选一个初选任务，或新建一个"));
  const j = S.job, run = j.run || {}, p = j.params;
  const kpi = (l, v, hint, k) => h("div", { class: "card kpi", style: { cursor: k ? "pointer" : null, outline: k && view === k ? "2px solid var(--brand-600)" : null },
    onclick: k ? () => { view = k; offset = 0; clearSel(); drawMain(); } : null },
    h("span", { class: "label" }, l), h("span", { class: "value" }, (v ?? 0).toLocaleString()), h("span", { class: "hint" }, hint));
  const prog = run.running ? h("div", { class: "callout", style: { marginBottom: "12px" } },
    h("span", { class: "badge info" }, "扫描中"), " ", run.message || "",
    run.total ? h("div", { style: { height: "6px", background: "var(--line-2, #e5ebe8)", borderRadius: "4px", marginTop: "6px" } },
      h("div", { style: { width: `${Math.round(100 * run.done / run.total)}%`, height: "100%", background: "var(--brand-600)", borderRadius: "4px" } })) : null)
    : run.message || run.error ? h("div", { class: "callout" + (run.error ? " warn" : ""), style: { marginBottom: "12px" } }, run.error ? "❌ " + run.error : "✓ " + run.message) : null;
  main.replaceChildren(
    h("div", { class: "card", style: { marginBottom: "12px" } },
      h("div", { class: "row between" },
        h("div", {}, h("h2", { style: { margin: 0, fontSize: "18px" } }, j.name), h("div", { class: "small muted mono" }, j.source),
          h("div", { class: "tiny muted" }, j.scanned ? `上次扫描 ${j.scanned}` : "还没扫描")),
        h("div", { class: "row" },
          run.running ? h("button", { class: "btn", onclick: async () => { await UI.api(`/api/prefilter/jobs/${cur}/stop`, { method: "POST" }); UI.toast("正在停止…"); } }, "■ 停止")
            : h("button", { class: "btn", title: "只给新增或改过的文件打分；目录里已经没有的去掉", onclick: () => scan(false) }, "↻ 扫描（增量）"),
          !run.running && j.last_target ? h("button", { class: "btn primary", title: "扫描新增照片，并把新入选的接着导出到上次的精选目录：" + j.last_target, onclick: () => scan(true) }, "⟳ 增量更新（扫描 + 导出）") : null,
          h("button", { class: "btn ghost", title: "只删初选记录（打分、改判）；原图和已经导出的精选目录都不动", onclick: delJob }, "删除记录")))),
    prog,
    h("div", { class: "grid k4", style: { marginBottom: "12px", gridTemplateColumns: "repeat(auto-fill, minmax(130px, 1fr))" } },
      kpi("将导出", S.keep, `高+中档去重后，含人工改判`, "keep"), kpi("高", S.tiers.high, `≥ ${p.high} 分`, "high"), kpi("中", S.tiers.medium, `${p.medium}–${p.high} 分`, "medium"),
      kpi("低", S.tiers.low, `< ${p.medium} 分`, "low"), kpi("淘汰", S.tiers.rejected, "坏图、过暗过曝、太小太糊", "rejected"),
      kpi("重复 / 几乎一样", S.dup, "每组只留分最高的", "dup"), kpi("人工改判", S.manual_keep + S.manual_drop, `要 ${S.manual_keep} · 不要 ${S.manual_drop}`, "manual")),
    h("div", { class: "two", style: { display: "grid", gridTemplateColumns: "minmax(0, 1.2fr) minmax(0, 1fr)", gap: "12px", marginBottom: "12px" } },
      h("div", { class: "card" }, h("div", { class: "small", style: { fontWeight: 600 } }, `分数分布（共 ${S.total.toLocaleString()} 张，不含淘汰的）`), hist(S.hist, p)),
      exportCard(j)),
    paramsCard(p),
    h("div", { id: "grid" }));
  drawGrid();
}

function hist(bins, p) {
  const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg"), W = 400, H = 120, B = 16;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "hist");
  const max = Math.max(1, ...bins), bw = W / bins.length;
  const add = (tag, a, text) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); if (text != null) e.textContent = text; svg.append(e); return e; };
  bins.forEach((n, i) => {
    const s = i * 5, color = s >= p.high ? "#2f9e78" : s >= p.medium ? "#3b82c4" : "#c98a1b", hh = (H - B) * n / max;
    const r = add("rect", { x: i * bw + 1, y: H - B - hh, width: bw - 2, height: hh, fill: color, rx: 2 });
    const t = document.createElementNS(NS, "title"); t.textContent = `${s}–${s + 5} 分：${n} 张`; r.append(t);
  });
  for (const [v, l] of [[p.medium, "中"], [p.high, "高"]]) { add("line", { x1: v / 100 * W, x2: v / 100 * W, y1: 0, y2: H - B, stroke: "#333", "stroke-dasharray": "3 3" }); add("text", { x: v / 100 * W + 3, y: 10, "font-size": 10 }, l + " " + v); }
  for (const v of [0, 50, 100]) add("text", { x: Math.min(v / 100 * W, W - 14), y: H - 3, "font-size": 10, fill: "#777" }, v);
  return svg;
}

function exportCard(j) {
  const def = j.last_target || j.source.replace(/\/[^/]+\/?$/, "") + "/精选/" + j.source.split("/").filter(Boolean).pop();
  const tgt = h("input", { class: "input mono", value: def });
  const out = h("div", { class: "small", style: { marginTop: "6px" } });
  const last = (j.exports || []).slice(-1)[0];
  return h("div", { class: "card" },
    h("div", { class: "small", style: { fontWeight: 600 } }, "导出到精选目录（硬链接，原图不动）"),
    h("div", { class: "row", style: { marginTop: "6px" } }, tgt, h("button", { class: "btn sm", onclick: () => dirPicker(tgt.value.replace(/\/[^/]*$/, ""), (p) => { tgt.value = p; }, { title: "选精选目录（可以在下面新建子目录）", allowNew: true }) }, "浏览…")),
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("button", { class: "btn sm", onclick: async () => { const r = await UI.api(`/api/prefilter/jobs/${cur}/export`, { method: "POST", body: { target: tgt.value.trim(), dry_run: true } }); out.replaceChildren(`将导出 ${r.keep} 张：新链 ${r.new} 张，精选目录里已有 ${r.already} 张（不重复）`); } }, "预览"),
      h("button", { class: "btn sm primary", onclick: async (e) => {
        e.target.disabled = true;
        try {
          const r = await UI.api(`/api/prefilter/jobs/${cur}/export`, { method: "POST", body: { target: tgt.value.trim() } });
          UI.toast(`已导出：硬链接 ${r.linked} 张${r.copied ? `，复制 ${r.copied} 张（不同磁盘）` : ""}，已有 ${r.already} 张${r.failed ? `，失败 ${r.failed} 张` : ""}`, !!r.failed, 8000);
          S = await UI.api(`/api/prefilter/jobs/${cur}`); drawMain();
        } finally { e.target.disabled = false; }
      } }, "导出"),
      j.last_target ? h("a", { class: "btn sm", href: "/types?newlib=" + encodeURIComponent(j.last_target) + "&name=" + encodeURIComponent(j.name) }, "→ 去照片类型建图库") : null),
    out,
    h("div", { class: "tiny muted", style: { marginTop: "6px" } }, last ? `上次导出 ${last.ts}：新链 ${last.linked + last.copied} 张，已有 ${last.already} 张 → ${last.target}` :
      "目录结构照原图目录保留。只增不减：后来改判成“不要”的，已经导出的不会被删掉（到照片类型页里删）。"));
}

function paramsCard(p) {
  const F = [
    ["high", "“高”档分数线", "≥ 这个分算高", 50, 100, 1], ["medium", "“中”档分数线", "≥ 这个分算中", 20, 100, 1],
    ["target_min_dim", "短边达到多少像素满分", "分辨率分", 64, 4000, 16], ["target_sharp", "清晰度满分值", "拉普拉斯方差，越大越清晰", 10, 2000, 10],
    ["hard_min_sharp", "低于多少算太模糊（淘汰）", "", 0, 200, 1], ["hard_min_dim", "短边低于多少像素淘汰", "", 0, 2000, 8],
    ["hard_min_bright", "平均亮度低于多少算过暗", "0–255", 0, 128, 1], ["hard_max_bright", "平均亮度高于多少算过曝", "0–255", 128, 255, 1],
    ["hard_min_contrast", "对比度低于多少淘汰", "几乎纯色的图", 0, 50, 0.5], ["max_aspect", "长宽比超过多少淘汰", "长条截图之类", 1, 20, 0.5],
    ["hamming", "几乎一样：哈希相差不超过几位", "0 = 只去完全相同的；8 常用；越大去得越狠", 0, 20, 1],
  ];
  const inputs = {};
  const fields = F.map(([k, l, help, min, max, step]) => {
    inputs[k] = h("input", { class: "input", type: "number", min, max, step, value: p[k] });
    return h("div", {}, h("label", {}, l), inputs[k], help ? h("div", { class: "help" }, help) : null);
  });
  const dedup = h("input", { type: "checkbox", checked: p.dedup });
  const tiers = Object.fromEntries(["high", "medium", "low"].map((t) => [t, h("input", { type: "checkbox", checked: p.export_tiers.includes(t) })]));
  const det = h("details", { class: "card", style: { marginBottom: "12px" } },
    h("summary", { style: { cursor: "pointer", fontWeight: 600 } }, "阈值和导出范围（改了立刻重新分档，不用重扫）"),
    h("div", { class: "params", style: { marginTop: "10px" } }, ...fields,
      h("div", {}, h("label", {}, "去重"), h("label", { class: "row", style: { gap: "6px", fontWeight: 400 } }, dedup, "几乎一样的每组只导出分最高的一张")),
      h("div", {}, h("label", {}, "导出哪几档"), h("div", { class: "row", style: { gap: "10px" } },
        ...Object.entries(tiers).map(([t, el]) => h("label", { class: "row", style: { gap: "4px", fontWeight: 400 } }, el, TIER[t]))))),
    h("div", { class: "row", style: { marginTop: "10px" } },
      h("button", { class: "btn primary sm", onclick: async () => {
        const body = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, Number(el.value)]));
        body.dedup = dedup.checked; body.export_tiers = Object.entries(tiers).filter(([, el]) => el.checked).map(([t]) => t);
        S = await UI.api(`/api/prefilter/jobs/${cur}/params`, { method: "POST", body }); offset = 0; drawMain();
        document.querySelector("details.card")?.setAttribute("open", "");
      } }, "应用"),
      h("button", { class: "btn sm ghost", onclick: async () => {
        S = await UI.api(`/api/prefilter/jobs/${cur}/params`, { method: "POST", body: { high: 80, medium: 55, target_min_dim: 640, target_sharp: 150, hard_min_sharp: 8, hard_min_dim: 32,
          hard_min_bright: 12, hard_max_bright: 245, hard_min_contrast: 4, max_aspect: 6, hamming: 8, dedup: true, export_tiers: ["high", "medium"] } }); drawMain();
      } }, "恢复默认"),
      h("span", { class: "tiny muted" }, "分数 = 分辨率 35% + 清晰度 35% + 曝光 15% + 对比度 15%（沿用原来的图片分拣脚本）")));
  return det;
}

// 选中：sel = 单张点选；ALL = 选了当前这一栏全部（不止本页），EXCL = 全选后又点掉的
let ALL = false;
const EXCL = new Set();
const isSel = (p) => ALL ? !EXCL.has(p) : sel.has(p);
const selCount = () => ALL ? Math.max(0, TOTAL - EXCL.size) : sel.size;
const selBody = () => ALL ? { all: true, view, exclude: [...EXCL] } : { paths: [...sel] };
function clearSel() { sel.clear(); ALL = false; EXCL.clear(); }
let PAGE = Number(store.get("lota.prefilter.page")) || 60;

async function drawGrid() {
  const box = document.getElementById("grid"); if (!box) return;
  const inTrash = view === "trash";
  const VIEWS = [["keep", "将导出"], ["high", "高"], ["medium", "中"], ["low", "低"], ["rejected", "淘汰"], ["dup", "重复"], ["drop", "不导出的"], ["manual", "人工改判"], ["manual_drop", "人工“不要”"], ["all", "全部"], ["trash", `🗑 回收站${S.trash ? " " + S.trash : ""}`]];
  const cnt = h("span", { class: "small muted" });
  const showCnt = () => { cnt.textContent = ALL ? `已选 全部 ${selCount().toLocaleString()} 张` : `已选 ${sel.size}`; };
  showCnt();
  const need = () => { if (!selCount()) { UI.toast("先点选照片（可多选，或“全选全部”）", true); return false; } return true; };
  const refresh = async () => { clearSel(); S = await UI.api(`/api/prefilter/jobs/${cur}`); drawMain(); };
  const decide = async (d, label) => {
    if (!need()) return;
    const r = await UI.api(`/api/prefilter/jobs/${cur}/override`, { method: "POST", body: { ...selBody(), decision: d } });
    UI.toast(`${label} ${r.changed} 张`); refresh();
  };
  const toTrash = () => {
    if (!need()) return;
    const n = selCount();
    UI.modal("移进回收站", h("div", {},
      h("p", {}, `把选中的 ${n.toLocaleString()} 张原图移进这个初选任务的回收站（同一块盘，只是挪位置，不占额外空间）。`),
      h("ul", { class: "small" },
        h("li", {}, "原图目录里就看不到它们了，以后扫描也不会再扫进来；"),
        h("li", {}, "已经导出到精选目录的，精选目录里的同一张也一起摘掉；"),
        h("li", {}, "在图库里当过人工样板的照片会跳过；"),
        h("li", {}, "在“🗑 回收站”里能恢复；点“清空回收站”才真正删除、释放磁盘空间。"))), [
      { label: "取消" },
      { label: `移进回收站（${n.toLocaleString()} 张）`, kind: "danger", run: async () => {
        const r = await UI.api(`/api/prefilter/jobs/${cur}/trash`, { method: "POST", body: selBody() });
        UI.toast(r.message, !!r.skipped, 8000); refresh();
      } }]);
  };
  const restore = async (all) => {
    if (!all && !need()) return;
    const r = await UI.api(`/api/prefilter/jobs/${cur}/trash/restore`, { method: "POST", body: all || ALL ? { all: true } : { paths: [...sel] } });
    UI.toast(r.message, !!r.failed, 6000); refresh();
  };
  const purge = (all) => {
    if (!all && !need()) return;
    const n = all || ALL ? S.trash : sel.size;
    UI.modal("永久删除", h("p", {}, `永久删除回收站里的 ${n.toLocaleString()} 张照片？`, h("b", { style: { color: "var(--danger)" } }, "删了就不能恢复。")), [
      { label: "取消" },
      { label: `永久删除 ${n.toLocaleString()} 张`, kind: "danger", run: async () => {
        const r = await UI.api(`/api/prefilter/jobs/${cur}/trash/purge`, { method: "POST", body: all || ALL ? { all: true } : { paths: [...sel] } });
        UI.toast(r.message, false, 8000); refresh();
      } }]);
  };
  const sortSel = h("select", { class: "select", style: { width: "130px" }, disabled: inTrash, onchange: (e) => { sort = e.target.value; offset = 0; drawGrid(); } },
    ...[["score_desc", "分高的在前"], ["score_asc", "分低的在前"], ["path", "按文件名"]].map(([v, l]) => h("option", { value: v, selected: v === sort }, l)));
  const pageSel = h("select", { class: "select", style: { width: "110px" }, title: "每页显示多少张", onchange: (e) => { PAGE = Number(e.target.value); store.set("lota.prefilter.page", PAGE); offset = 0; drawGrid(); } },
    ...[30, 60, 120, 200, 300, 500].map((n) => h("option", { value: n, selected: n === PAGE }, `每页 ${n} 张`)));
  const allBtn = h("button", { class: "btn sm", title: "这一栏的全部照片（不止本页）", onclick: () => { sel.clear(); EXCL.clear(); ALL = true; drawGrid(); } }, "全选全部");
  const grid = h("div", { class: "pgrid" }), pager = h("div", { class: "row", style: { marginTop: "12px" } });
  const actions = inTrash ? [
    h("button", { class: "btn sm", onclick: () => restore(false) }, "↩ 恢复选中的"),
    h("button", { class: "btn sm", disabled: !S.trash, onclick: () => restore(true) }, "↩ 全部恢复"),
    h("button", { class: "btn sm danger", onclick: () => purge(false) }, "永久删除选中的"),
    h("button", { class: "btn sm danger", disabled: !S.trash, onclick: () => purge(true) }, "清空回收站（永久删除）"),
  ] : [
    h("button", { class: "btn sm primary", title: "不管分数，一定导出（快捷键 K）", onclick: () => decide("keep", "改判为“要”") }, "✓ 要"),
    h("button", { class: "btn sm", title: "不管分数，一定不导出（快捷键 D）", onclick: () => decide("drop", "改判为“不要”") }, "✕ 不要"),
    h("button", { class: "btn sm", title: "撤销人工改判，回到按分数自动判断（快捷键 A）", onclick: () => decide("clear", "恢复自动判断") }, "↺ 按分数"),
    h("button", { class: "btn sm danger", title: "物理删除：原图移进回收站（能恢复，清空回收站才真正删掉）", onclick: toTrash }, "🗑 移进回收站"),
  ];
  box.replaceChildren(h("div", { class: "bar" },
    h("div", { class: "row" }, h("div", { class: "tabs", style: { margin: 0, border: 0 } }, ...VIEWS.map(([k, l]) =>
      h("button", { class: view === k ? "on" : "", onclick: () => { view = k; offset = 0; clearSel(); drawGrid(); } }, l))),
      h("span", { style: { flex: 1 } }), sortSel, pageSel),
    !inTrash && (S.manual_drop || S.drop) ? h("div", { class: "callout", style: { margin: "6px 0", fontSize: "12px" } },
      "不要的照片想从原图目录里彻底删掉：",
      h("button", { class: "btn sm", style: { margin: "0 4px" }, disabled: !S.manual_drop, onclick: () => { view = "manual_drop"; ALL = true; offset = 0; drawGrid().then(toTrash); } }, `人工标了“不要”的 ${S.manual_drop} 张`),
      h("button", { class: "btn sm", style: { margin: "0 4px" }, disabled: !S.drop, onclick: () => { view = "drop"; ALL = true; offset = 0; drawGrid().then(toTrash); } }, `全部不导出的 ${(S.drop || 0).toLocaleString()} 张`),
      "先移进回收站，确认没问题再到“🗑 回收站”里清空。") : null,
    h("div", { class: "row", style: { marginTop: "6px" } },
      h("span", { style: { flex: 1 } }), cnt,
      h("button", { class: "btn sm", onclick: () => { ALL = false; EXCL.clear(); LIST.forEach((x) => sel.add(x.path)); drawGrid(); } }, "全选本页"),
      allBtn,
      h("button", { class: "btn sm", onclick: () => { clearSel(); drawGrid(); } }, "清空"),
      ...actions)),
    grid, pager);
  grid.replaceChildren(h("div", { class: "muted small" }, "加载中…"));
  const d = await UI.api(`/api/prefilter/jobs/${cur}/items?view=${view}&sort=${sort}&offset=${offset}&limit=${PAGE}`);
  LIST = d.items; TOTAL = d.total; showCnt();
  allBtn.textContent = `全选全部 ${TOTAL.toLocaleString()} 张`;
  grid.replaceChildren(...(LIST.length ? LIST.map((it) => cell(it, showCnt)) : [h("div", { class: "empty" }, inTrash ? "回收站是空的" : S.total ? "这里没有照片" : "还没扫描：点上面的“扫描”")]));
  pager.replaceChildren(
    h("button", { class: "btn sm", disabled: !offset, onclick: () => { offset = Math.max(0, offset - PAGE); drawGrid(); } }, "上一页"),
    h("span", { class: "small muted" }, TOTAL ? `第 ${offset + 1}–${Math.min(offset + PAGE, TOTAL)} 张 / 共 ${TOTAL.toLocaleString()}` : ""),
    h("button", { class: "btn sm", disabled: offset + PAGE >= TOTAL, onclick: () => { offset += PAGE; drawGrid(); } }, "下一页"),
    h("span", { class: "tiny muted" }, inTrash ? "单击选中，双击看大图" : "单击选中，双击看大图（滚轮缩放、拖动；← → 切换，K 要 / D 不要 / A 按分数）"));
}
function cell(it, showCnt) {
  const c = h("div", { class: "pcell" + (isSel(it.path) ? " sel" : "") + (it.keep ? "" : " out"), title: `${it.path}\n${it.w}×${it.h} · 清晰度 ${it.sharp} · 亮度 ${it.bright} · 对比度 ${it.contrast}` },
    h("img", { loading: "lazy" }), h("span", { class: "chk" }, isSel(it.path) ? "✓" : ""),
    it.why.length || it.dup_of ? h("div", { class: "why" }, it.why.join("；") || "和另一张几乎一样") : null,
    it.tier === "trash" ? null : h("span", { class: "tag" }, h("span", { class: "badge t-" + it.tier }, `${TIER[it.tier]} ${it.score}`),
      it.override ? h("span", { class: "badge " + (it.override === "keep" ? "ok" : "") }, it.override === "keep" ? "人工：要" : "人工：不要") : null,
      it.keep ? h("span", { class: "badge ok" }, "导出") : null));
  UI.blobUrl(thumb(it.path)).then((u) => { c.querySelector("img").src = u; }).catch(() => {});
  c.onclick = (e) => {
    if (e.detail === 2) setTimeout(() => viewer(it.path), 0);
    if (ALL) { EXCL.has(it.path) ? EXCL.delete(it.path) : EXCL.add(it.path); }
    else { sel.has(it.path) ? sel.delete(it.path) : sel.add(it.path); }
    c.classList.toggle("sel", isSel(it.path)); c.querySelector(".chk").textContent = isSel(it.path) ? "✓" : ""; showCnt();
  };
  return c;
}

// ── 看大图：整张放得下；滚轮缩放（以鼠标处为中心）、按住拖动、双击在“适应窗口 / 原始大小”间切换；
//    ← → 切换，K 要 / D 不要 / A 按分数（当前这张），0 = 适应窗口，1 = 原始大小，+ / - 缩放 ──
function viewer(start) {
  let idx = Math.max(0, LIST.findIndex((x) => x.path === start)), token = 0;
  let scale = 1, fit = 1, tx = 0, ty = 0, drag = null;
  const img = h("img", { draggable: false, style: { position: "absolute", left: 0, top: 0, transformOrigin: "0 0", userSelect: "none", maxWidth: "none", maxHeight: "none", cursor: "grab", imageRendering: "auto" } });
  const stage = h("div", { style: { flex: 1, position: "relative", overflow: "hidden", touchAction: "none" } }, img);
  const zoomLbl = h("span", { style: { minWidth: "48px", textAlign: "center" } });
  const info = h("div", { style: { flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } });
  const inTrash = view === "trash";
  const btn = (t, fn, tip) => h("button", { class: "btn sm", title: tip, onclick: (e) => { e.stopPropagation(); fn(); }, style: { background: "rgba(255,255,255,.08)", color: "#e6f1ec", borderColor: "rgba(255,255,255,.2)" } }, t);
  const back = h("div", { style: { position: "fixed", inset: 0, zIndex: 70, background: "#0b1512", color: "#e6f1ec", display: "flex", flexDirection: "column" } },
    h("div", { style: { display: "flex", gap: "8px", alignItems: "center", padding: "10px 14px", background: "rgba(8,20,16,.92)", fontSize: "13px", flexWrap: "wrap" } }, info,
      btn("－", () => zoomAt(1 / 1.25), "缩小（-）"), zoomLbl, btn("＋", () => zoomAt(1.25), "放大（+）"),
      btn("适应", () => fitView(), "适应窗口（0）"), btn("1:1", () => setScale(1), "原始大小（1）"),
      btn("‹ 上一张", () => go(-1), "←"), btn("下一张 ›", () => go(1), "→"),
      ...(inTrash ? [] : [btn("✓ 要", () => decide("keep"), "K"), btn("✕ 不要", () => decide("drop"), "D"), btn("↺ 按分数", () => decide("clear"), "A")]),
      btn("✕ 关闭", () => close(), "Esc")),
    stage);
  document.body.append(back); document.body.style.overflow = "hidden";

  const apply = () => { img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`; zoomLbl.textContent = Math.round(scale * 100) + "%"; };
  const natural = () => [img.naturalWidth || 1, img.naturalHeight || 1];
  function fitView() {
    const [w, hh] = natural(), r = stage.getBoundingClientRect();
    fit = Math.min(r.width / w, r.height / hh, 1) || 1;       // 小图不放大
    scale = fit; tx = (r.width - w * scale) / 2; ty = (r.height - hh * scale) / 2; apply();
  }
  function setScale(ns, cx, cy) {
    const r = stage.getBoundingClientRect();
    cx = cx ?? r.width / 2; cy = cy ?? r.height / 2;
    ns = Math.max(Math.min(fit, 0.05), Math.min(ns, 16));
    tx = cx - (cx - tx) * ns / scale; ty = cy - (cy - ty) * ns / scale; scale = ns; apply();
  }
  const zoomAt = (f, cx, cy) => setScale(scale * f, cx, cy);
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  stage.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, tx, ty }; stage.setPointerCapture(e.pointerId); img.style.cursor = "grabbing"; });
  stage.addEventListener("pointermove", (e) => { if (!drag) return; tx = drag.tx + e.clientX - drag.x; ty = drag.ty + e.clientY - drag.y; apply(); });
  const endDrag = () => { drag = null; img.style.cursor = "grab"; };
  stage.addEventListener("pointerup", endDrag); stage.addEventListener("pointercancel", endDrag);
  stage.addEventListener("dblclick", (e) => {
    const r = stage.getBoundingClientRect();
    if (Math.abs(scale - fit) < 1e-3) setScale(1, e.clientX - r.left, e.clientY - r.top); else fitView();
  });
  const onResize = () => fitView();
  window.addEventListener("resize", onResize);

  async function show(i) {
    idx = i; const it = LIST[idx]; if (!it) return; const my = ++token;
    info.replaceChildren(h("b", {}, it.path.split("/").pop()), `  ${offset + idx + 1}/${TOTAL}  ·  `,
      it.tier === "trash" ? "回收站  ·  " : `${TIER[it.tier]} ${it.score} 分  ·  `, `${it.w}×${it.h}  ·  清晰度 ${it.sharp}  ·  亮度 ${it.bright}  ·  对比度 ${it.contrast}`,
      it.why.length ? "  ·  " + it.why.join("；") : "", it.dup_of ? "  ·  和 " + it.dup_of.split("/").pop() + " 几乎一样" : "",
      it.override ? `  ·  人工：${it.override === "keep" ? "要" : "不要"}` : "", it.tier === "trash" ? "" : it.keep ? "  ·  ✓ 导出" : "  ·  不导出");
    const load = (u) => new Promise((res) => { img.onload = res; img.onerror = res; img.src = u; });
    try {
      const u = await UI.blobUrl(thumb(it.path, 1600)); if (my !== token) return;
      await load(u); if (my !== token) return;
      fitView();                                               // 先用 1600 的预览图，马上能看
      const o = await UI.blobUrl(original(it.path)); if (my !== token) return;
      const pw = img.naturalWidth || 1, zoomed = Math.abs(scale - fit) > 1e-3;
      await load(o); if (my !== token) return;
      if (!zoomed) return fitView();
      const k = pw / (img.naturalWidth || 1);                  // 换成原图时已经放大了：保持当前显示大小和位置
      const r = stage.getBoundingClientRect();
      fit = Math.min(r.width / (img.naturalWidth || 1), r.height / (img.naturalHeight || 1), 1) || 1;
      scale *= k; apply();
    } catch { /* 看不了就算了 */ }
  }
  async function go(d) {
    const i = idx + d;
    if (i >= 0 && i < LIST.length) return show(i);
    if (d > 0 && offset + LIST.length < TOTAL) { offset += PAGE; await drawGrid(); show(0); }
    else if (d < 0 && offset > 0) { offset = Math.max(0, offset - PAGE); await drawGrid(); show(LIST.length - 1); }
  }
  async function decide(dc) {
    const it = LIST[idx]; if (!it) return;
    await UI.api(`/api/prefilter/jobs/${cur}/override`, { method: "POST", body: { paths: [it.path], decision: dc } });
    it.override = dc === "clear" ? null : dc;
    it.keep = dc === "keep" || (dc === "clear" ? null : false);
    UI.toast(dc === "keep" ? "要" : dc === "drop" ? "不要" : "恢复按分数", false, 900); dirty = true;
    go(1);
  }
  let dirty = false;
  const onKey = (e) => {
    const k = e.key.toLowerCase();
    if (k === "arrowright") go(1); else if (k === "arrowleft") go(-1); else if (k === "escape") close();
    else if (k === "+" || k === "=") zoomAt(1.25); else if (k === "-") zoomAt(1 / 1.25); else if (k === "0") fitView(); else if (k === "1") setScale(1);
    else if (inTrash) return;
    else if (k === "k") decide("keep"); else if (k === "d") decide("drop"); else if (k === "a") decide("clear"); else return;
    e.preventDefault(); e.stopPropagation();
  };
  window.addEventListener("keydown", onKey, true);
  async function close() {
    token++; back.remove(); document.body.style.overflow = ""; window.removeEventListener("keydown", onKey, true); window.removeEventListener("resize", onResize);
    if (dirty) { S = await UI.api(`/api/prefilter/jobs/${cur}`); drawMain(); }
  }
  show(idx);
}

async function scan(autoExport) {
  await UI.api(`/api/prefilter/jobs/${cur}/scan`, { method: "POST", body: { auto_export: autoExport } });
  UI.toast(autoExport ? "开始增量更新：扫描新照片，扫完把新入选的导出到精选目录" : "开始扫描（只处理新增或改过的文件）");
  S = await UI.api(`/api/prefilter/jobs/${cur}`); drawMain(); watch();
}
function watch() {
  clearInterval(poll);
  poll = setInterval(async () => {
    if (!cur) return clearInterval(poll);
    const d = await UI.api(`/api/prefilter/jobs/${cur}`).catch(() => null); if (!d) return;
    const wasRunning = S?.job?.run?.running; S = d;
    if (d.job.run?.running) { const m = document.querySelector("#main .callout"); if (m) drawMainHead(); else drawMain(); }
    else { clearInterval(poll); if (wasRunning) { JOBS = await UI.api("/api/prefilter/jobs"); render(); } }
  }, 2500);
}
function drawMainHead() {   // 扫描中只刷新进度条，不重画照片
  const m = document.querySelector("#main .callout"), run = S.job.run || {};
  if (!m) return;
  m.childNodes[2] && (m.childNodes[2].textContent = run.message || "");
  const bar = m.querySelector("div > div"); if (bar && run.total) bar.style.width = `${Math.round(100 * run.done / run.total)}%`;
}
async function delJob() {
  UI.modal("删除初选记录", h("p", {}, "只删这个初选任务的打分和人工改判记录。原图目录、已经导出的精选目录都不动。"), [{ label: "取消" }, { label: "删除", kind: "danger", run: async () => {
    await UI.api(`/api/prefilter/jobs/${cur}`, { method: "DELETE" }); cur = null; S = null; await load();
  } }]);
}

(async () => {
  await UI.nav("prefilter", { picker: false });
  try { await load(); if (S?.job?.run?.running) watch(); } catch (e) { UI.toast(e.message, true); }
})();
