// 照片类型：按“图库”管理（一个图片目录 + 自己的类型清单、索引、样板、归类结果、回收站），页面一次只看一个图库。
// 类型列表 → 看图（全部/已确认/待确认/没把握）→ 多选确认/移类/放临时仓库 → 找相似图 → 重新训练并归类全部
"use strict";
const { h } = UI;
const TRASH = "__trash__", LIB_KEY = "lota.types.lib", PAGE_KEY = "lota.types.page";
let PAGE = 60;
try { PAGE = [60, 120, 200, 300].includes(+localStorage.getItem(PAGE_KEY)) ? +localStorage.getItem(PAGE_KEY) : 60; } catch { PAGE = 60; }
let S = null, TR = { total: 0, bytes: 0 }, cur = null, view = "predicted", offset = 0, sel = new Set(), mode = "browse", simItems = [];
let LIBS = { default: "", libs: [] }, LIB = "", jobPoll = null;
try { LIB = new URLSearchParams(location.search).get("lib") || localStorage.getItem(LIB_KEY) || ""; } catch { LIB = ""; }
const withLib = (u) => u + (u.includes("?") ? "&" : "?") + "lib=" + encodeURIComponent(LIB);
const api = (u, o) => UI.api(withLib(u), o);
const thumb = (p, size = 320) => withLib(`/api/types/thumb?path=${encodeURIComponent(p)}&size=${size}`);
const tname = (k) => { const t = S.types.find((x) => x.key === k); return t ? t.name : k; };

async function load() {
  LIBS = await UI.api("/api/types/libs");
  if (!LIBS.libs.some((l) => l.id === LIB)) LIB = LIBS.default || LIBS.libs[0]?.id || "";
  if (!LIB) { S = null; return renderEmpty(); }
  [S, TR] = await Promise.all([api("/api/types/summary"), api("/api/types/trash?limit=1").catch(() => ({ total: 0, bytes: 0 }))]);
  if (!cur || (cur !== TRASH && !S.types.some((t) => t.key === cur))) cur = (S.types.find((t) => !t.bin && t.count) || S.types.find((t) => t.count) || S.types[0])?.key;
  if (S.train?.running) watchJob();
  await render();
}
function switchLib(id) {
  LIB = id; cur = null; offset = 0; sel.clear(); mode = "browse"; view = "predicted";
  try { localStorage.setItem(LIB_KEY, id); } catch { /* 不让存就算了 */ }
  const u = new URL(location.href); ["lib", "newlib", "name"].forEach((k) => u.searchParams.delete(k)); history.replaceState(null, "", u);
  load().catch((e) => UI.toast(e.message, true));
}
function renderEmpty() {
  document.getElementById("app").replaceChildren(h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "照片类型"), h("p", {}, "还没有图库。图库 = 一个图片目录（通常是“初选”导出的精选目录）+ 它自己的照片类型。通信巡检、物流分拣、水果分拣……各建一个，互不干扰。")),
    h("div", { class: "row" }, h("a", { class: "btn lg", href: "/prefilter" }, "先去初选"), h("button", { class: "btn primary lg", onclick: () => newLibDlg() }, "＋ 新建图库"))));
}
// 图库切换条：切换后别的图库的照片、类型都看不到
function libBar() {
  const L = S.lib, ix = S.index || {}, job = S.train || {};
  const pick = h("select", { class: "select", style: { minWidth: "220px", fontWeight: 600 }, onchange: (e) => switchLib(e.target.value) },
    ...LIBS.libs.map((l) => h("option", { value: l.id, selected: l.id === LIB }, `${l.name}${l.default ? "（默认）" : ""}`)));
  const pending = ix.total != null ? Math.max(0, ix.total - ix.indexed) : null;
  return h("div", { class: "card", style: { display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap", padding: "10px 14px", marginBottom: "14px" } },
    h("span", { class: "small", style: { fontWeight: 600 } }, "图库"), pick,
    h("span", { class: "small muted mono", title: "图库目录：只显示这个目录里的照片", style: { wordBreak: "break-all" } }, L.root),
    h("span", { class: "tiny muted" }, `代号 ${L.id}`),
    h("span", { style: { flex: 1 } }),
    job.running && job.kind === "index" ? h("span", { class: "badge info idx-msg" }, "建索引中… " + (job.message || ""))
      : h("span", { class: "small" + (pending ? "" : " muted") }, pending == null ? `已索引 ${ix.indexed ?? 0} 张` : pending ? `目录里有 ${pending} 张新照片还没索引` : "索引是最新的"),
    h("button", { class: "btn sm" + (pending ? " primary" : ""), disabled: !!job.running, title: "重新扫图库目录：只给新照片算特征，目录里删掉的从索引去掉。原图目录补了新照片、初选又导出了新的之后点这个", onclick: reindex }, "↻ 更新索引"),
    exportPick(),
    h("button", { class: "btn sm", onclick: () => newLibDlg() }, "＋ 新建图库"),
    h("button", { class: "btn sm ghost", onclick: manageLibDlg }, "管理"));
}
// 导出当前图库的照片类型：CSV（Excel 直接打开，UTF-8 带 BOM）或 JSON（字段与 /api/types/summary 一致，方便再导入）
function exportPick() {
  const sel = h("select", { class: "btn sm", title: "导出当前图库的照片类型清单（代号、中英文名、描述、必拍内容、常见异常、张数）" },
    h("option", { value: "" }, "⇩ 导出"), h("option", { value: "csv" }, "CSV（Excel）"), h("option", { value: "json" }, "JSON"));
  sel.onchange = () => { const f = sel.value; sel.value = ""; if (f) exportTypes(f); };
  return sel;
}
function exportTypes(fmt) {
  const L = S.lib || {}, types = (S.types || []).filter((t) => !t.bin), stamp = new Date().toISOString().slice(0, 10);
  const name = `照片类型_${L.name || L.id || "图库"}_${stamp}`;
  if (fmt === "json") {
    downloadText(name + ".json", JSON.stringify({ lib: { id: L.id, name: L.name }, exported_at: new Date().toISOString(),
      types: types.map(({ bin, ...t }) => t) }, null, 2), "application/json");
  } else {
    const join = (a) => (a || []).join("，");
    const rows = [["代号", "中文名", "英文名", "英文描述", "必拍内容", "常见异常", "常见异常（英）", "照片数", "已确认", "低置信", "待确认建议"],
      ...types.map((t) => [t.key, t.name, t.name_en, t.prompt, join(t.must_have), join(t.anomalies), join(t.anomalies_en), t.count, t.confirmed, t.low, t.suggested])];
    downloadText(name + ".csv", toCsv(rows), "text/csv");
  }
  UI.toast(`已导出 ${types.length} 个照片类型`);
}
function toCsv(rows) {
  const cell = (v) => { v = v == null ? "" : String(v); return /[",\r\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  return "﻿" + rows.map((r) => r.map(cell).join(",")).join("\r\n");
}
function downloadText(filename, text, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: mime + ";charset=utf-8" }));
  const a = h("a", { href: url, download: filename }); document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
async function reindex() {
  await api("/api/types/index", { method: "POST" });
  UI.toast("开始更新索引：只给新照片算特征");
  S.train = { running: true, kind: "index", message: "准备中…" }; render(); watchJob();
}
function watchJob() {
  clearInterval(jobPoll);
  jobPoll = setInterval(async () => {
    const t = await api("/api/types/train").catch(() => null); if (!t) return;
    if (t.running) { const b = document.querySelector(".idx-msg"); if (b) b.textContent = "建索引中… " + (t.message || ""); return; }
    clearInterval(jobPoll);
    if (t.error || t.result?.error) UI.toast((t.kind === "index" ? "更新索引失败：" : "没有训练：") + (t.error || t.result.error), true, 8000);
    else if (t.kind === "index") UI.toast(t.result?.message || "索引更新完成", false, 6000);
    else UI.toast(`训练完成：交叉验证准确率 ${UI.pct(t.result?.cv_acc, 1)}，临时仓库捞出 ${t.result?.bin_rescued ?? 0} 张`);
    load();
  }, 3000);
}
function newLibDlg(root = "", name = "") {
  const inp = (ph, v = "") => h("input", { class: "input", placeholder: ph, value: v });
  const f = { name: inp("如：水果分拣", name), root: inp("如 /data/images/精选/水果", root),
    types: h("textarea", { class: "input", rows: 4, placeholder: "可不填，以后再加。每行一个：中文名,英文名\n如：苹果,Apple\n香蕉,Banana" }) };
  const row = (l, el, hint) => h("div", { class: "field", style: { marginBottom: "10px" } }, h("label", {}, l), el, hint ? h("div", { class: "help" }, hint) : null);
  UI.modal("新建图库", h("div", { class: "form" },
    h("p", { class: "small muted" }, "一个图库管理一个图片目录（含子目录）：自己的照片类型、样板、归类结果、回收站。切到别的图库后，这里的照片和类型都看不到。建好后会自动“更新索引”（给每张图算特征，显卡上约每分钟几千张）。"),
    row("名称 *", f.name),
    row("图片目录 *", h("div", { class: "row" }, f.root, h("a", { class: "btn sm", href: "/prefilter", title: "还没有精选目录？先去初选" }, "去初选")), "通常是“初选”导出的精选目录；要在服务器允许读的图片目录里"),
    row("先建一批照片类型", f.types, "每行一个“中文名,英文名”；“其他/无关”（临时仓库）会自动有")), [
    { label: "取消" },
    { label: "新建", kind: "primary", run: async () => {
      const types = f.types.value.split("\n").map((l) => l.replace("，", ",").split(",").map((x) => x.trim())).filter((x) => x[0]).map(([n, e]) => ({ name: n, name_en: e || n }));
      const r = await UI.api("/api/types/libs", { method: "POST", body: { name: f.name.value.trim(), root: f.root.value.trim(), types } });
      UI.toast(`已新建图库“${r.name}”，开始建索引`);
      LIB = r.id;
      await api("/api/types/index", { method: "POST" }).catch((e) => UI.toast(e.message, true));
      switchLib(r.id);
    } }], { wide: true });
}
function manageLibDlg() {
  let close = () => {};
  const rows = LIBS.libs.map((l) => {
    const name = h("input", { class: "input", value: l.name, style: { maxWidth: "200px" } });
    return h("tr", {},
      h("td", {}, name, h("div", { class: "tiny muted" }, l.id + (l.legacy ? " · 沿用旧流水线数据" : ""))),
      h("td", { class: "small mono", style: { wordBreak: "break-all" } }, l.root, l.exists ? null : h("div", { style: { color: "var(--danger)" } }, "目录不存在")),
      h("td", { class: "small" }, `${(l.index?.indexed ?? 0).toLocaleString()} 张`),
      h("td", {}, h("div", { class: "row", style: { gap: "4px" } },
        h("button", { class: "btn sm", onclick: async () => { await UI.api(`/api/types/libs/${l.id}`, { method: "POST", body: { name: name.value } }); UI.toast("已改名"); close(); load(); } }, "改名"),
        l.default ? h("span", { class: "badge ok" }, "默认")
          : h("button", { class: "btn sm", title: "工作流里“按照片类型抽图”等工具不填图库时用默认图库", onclick: async () => { await UI.api(`/api/types/libs/${l.id}`, { method: "POST", body: { default: true } }); UI.toast("已设为默认图库"); close(); load(); } }, "设为默认"),
        h("button", { class: "btn sm ghost", title: "只从列表里去掉；照片、样板、索引文件都不删", onclick: async () => {
          if (!confirm(`从列表里去掉图库“${l.name}”？照片和数据文件都不删。`)) return;
          await UI.api(`/api/types/libs/${l.id}`, { method: "DELETE" }); UI.toast("已从列表去掉"); close(); if (l.id === LIB) LIB = ""; load();
        } }, "移除"))));
  });
  close = UI.modal("管理图库", h("div", {},
    h("table", { class: "table" }, h("thead", {}, h("tr", {}, ...["名称", "目录", "已索引", ""].map((x) => h("th", {}, x)))), h("tbody", {}, ...rows)),
    h("p", { class: "tiny muted", style: { marginTop: "8px" } }, "工作流里的“按照片类型抽图”“按照片类型补类别”“用权重找图”“按权重给照片类型归类”不填“图库”时用默认图库。")),
  [{ label: "合并图库…", run: () => { setTimeout(mergeLibDlg, 0); } }, { label: "关闭" }], { wide: true });
}
// 合并图库：多选几个图库，指定一个主图库，其余的并进主图库
function mergeLibDlg() {
  if (LIBS.libs.length < 2) return UI.toast("只有一个图库，没有可合并的", true);
  const picked = new Set(), out = h("div", { class: "small", style: { marginTop: "8px" } });
  let main = LIB;
  const tbody = h("tbody");
  const draw = () => {
    tbody.replaceChildren(...LIBS.libs.map((l) => h("tr", { style: { background: l.id === main ? "var(--brand-50, #eef7f3)" : null } },
      h("td", {}, h("input", { type: "checkbox", checked: picked.has(l.id) || l.id === main, disabled: l.id === main,
        onchange: (e) => { e.target.checked ? picked.add(l.id) : picked.delete(l.id); draw(); } })),
      h("td", {}, h("label", { class: "row", style: { gap: "4px", fontWeight: 400 } }, h("input", { type: "radio", name: "mainlib", checked: l.id === main,
        onchange: () => { picked.delete(l.id); main = l.id; draw(); } }), "主图库")),
      h("td", {}, h("b", {}, l.name), h("div", { class: "tiny muted" }, l.id + (l.legacy ? " · 旧流水线数据" : ""))),
      h("td", { class: "small mono", style: { wordBreak: "break-all" } }, l.root),
      h("td", { class: "small" }, `${(l.index?.indexed ?? 0).toLocaleString()} 张`))));
    const m = LIBS.libs.find((l) => l.id === main), others = LIBS.libs.filter((l) => picked.has(l.id));
    out.replaceChildren(others.length ? `将把 ${others.map((l) => "“" + l.name + "”").join("、")} 并进主图库“${m?.name}”。` : "勾选要并进主图库的图库（可多选）。");
  };
  draw();
  UI.modal("合并图库", h("div", {},
    h("p", { class: "small muted" }, "选一个主图库，再勾选要并进去的图库。合并后："),
    h("ul", { class: "small" },
      h("li", {}, "照片原地不动，主图库同时管理被并图库的目录（“更新索引”时一起扫）；"),
      h("li", {}, "照片类型并过去：同名的合成一类，其余的加进主图库；"),
      h("li", {}, "人工确认的样板、回收站记录、已算好的特征索引都并过去，不用重算；同一张照片两边都确认过的，以主图库为准；"),
      h("li", {}, "被并的图库从列表里去掉（数据文件保留）；并完自动重新训练归类。")),
    h("table", { class: "table" }, h("thead", {}, h("tr", {}, ...["并入", "", "名称", "目录", "已索引"].map((x) => h("th", {}, x)))), tbody), out), [
    { label: "取消" },
    { label: "合并", kind: "primary", run: async () => {
      if (!picked.size) throw new Error("至少勾选一个要并进主图库的图库");
      const names = LIBS.libs.filter((l) => picked.has(l.id)).map((l) => l.name).join("、");
      if (!confirm(`把 ${names} 并进“${LIBS.libs.find((l) => l.id === main)?.name}”？合并后被并的图库从列表里消失（不能一键拆回）。`)) return false;
      const r = await UI.api("/api/types/libs/merge", { method: "POST", body: { main, others: [...picked] } });
      UI.toast(r.message, false, 10000);
      switchLib(main); setTimeout(watchJob, 500);
    } }], { wide: true });
}
function render() {
  const app = document.getElementById("app");
  const r = S.report || {};
  const q = h("input", { class: "input", placeholder: "搜索类型（中文 / 英文 / 代号）", style: { marginBottom: "6px" } });
  const list = h("div");
  const drawList = () => {
    const s = q.value.trim().toLowerCase();
    list.replaceChildren(...S.types.filter((t) => !s || (t.key + t.name + t.name_en).toLowerCase().includes(s)).map((t) =>
      h("div", { class: "titem" + (t.key === cur ? " on" : ""), onclick: () => { cur = t.key; offset = 0; sel.clear(); mode = "browse"; render(); } },
        h("span", {}, t.bin ? "🗃️ " : "", t.name, h("span", { class: "tiny muted" }, " " + (t.name_en || ""))),
        h("span", { class: "n" }, `${t.count}`, t.confirmed ? h("span", { style: { color: "var(--ok)" } }, ` ✓${t.confirmed}`) : "",
          t.suggested ? h("span", { style: { color: "var(--brand-600)" }, title: "权重建议（待确认）" }, ` 💡${t.suggested}`) : ""))),
      h("div", { class: "titem" + (cur === TRASH ? " on" : ""), style: { marginTop: "8px", borderTop: "1px dashed var(--line-2)", paddingTop: "10px" },
        onclick: () => { cur = TRASH; offset = 0; sel.clear(); mode = "browse"; render(); } },
        h("span", {}, "🗑️ 回收站", h("span", { class: "tiny muted" }, " 删除的照片")), h("span", { class: "n" }, `${TR.total}`)));
  };
  q.oninput = drawList; drawList();
  const tr = S.train || {};
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "照片类型"), h("p", {}, "给图库里的海量照片自动分类：人工确认一些“样板”，模型学会后把其余照片自动归类；拿不准的进“临时仓库”。“按照片类型抽图”“用权重找图”都用这里的结果。")),
      h("div", { class: "row" },
        tr.running && tr.kind !== "index" ? h("span", { class: "badge info" }, `训练中…（${tr.started} 开始）`) : null,
        h("button", { class: "btn lg", title: "用子项目里训练好的检测 / 分类模型扫全部照片，给出类型建议（💡），确认后成为样板", onclick: assistDlg }, "🤖 用模型辅助归类"),
        h("button", { class: "btn primary lg", disabled: !!tr.running, onclick: train }, "重新训练并归类全部"))),
    libBar(),
    h("div", { class: "grid k4", style: { marginBottom: "14px" } },
      kpi("已索引照片", (S.total || 0).toLocaleString(), "建过 SigLIP2 特征的"), kpi("人工确认", (S.confirmed_total || 0).toLocaleString(), "样板越多越准"),
      kpi("交叉验证准确率", r.cv_acc != null ? UI.pct(r.cv_acc, 1) : "—", r.ts ? "上次训练 " + r.ts + " · 点开看每类" : "还没训练", r.ts ? reportDlg : null),
      kpi("临时仓库", S.types.find((t) => t.bin)?.count ?? 0, r.bin_rescued ? `上次训练捞出 ${r.bin_rescued} 张` : "拿不准的照片")),
    h("div", { class: "types" },
      h("div", { class: "card tlist" }, q, list,
        S.types.length <= 1 ? h("div", { class: "callout", style: { marginTop: "8px", fontSize: "12px" } }, "这个图库还没有照片类型：先新增几个，再到“临时仓库”里给每类确认几张样板，然后“重新训练并归类全部”。") : null,
        h("button", { class: "btn sm", style: { marginTop: "10px", width: "100%" }, title: "清单里没有合适的类型时新增（中英双语）", onclick: newTypeDlg }, "＋ 新增照片类型")),
      h("div", { id: "main" })));
  return drawMain();
}
function kpi(l, v, hint, onclick) {
  return h("div", { class: "card kpi", onclick, style: onclick ? { cursor: "pointer" } : null, title: onclick ? "点开看详细报告" : null },
    h("span", { class: "label" }, l), h("span", { class: "value" }, v), h("span", { class: "hint" }, hint));
}
const typeAcc = (k) => { const x = (S.report?.per_type || {})[k]; return x && x[1] ? [x[0] / x[1], x[1]] : null; };

// 训练报告：每类准确率（低的在前，优先多确认）+ 全部照片归类分布（从旧 7870 页面迁移）
function reportDlg() {
  const r = S.report || {};
  const rows = Object.keys(r.per_type || {}).map((k) => [k, ...typeAcc(k)]).sort((a, b) => a[1] - b[1]);
  const tbl = (head, body) => h("table", { class: "table" }, h("thead", {}, h("tr", {}, ...head.map((x) => h("th", {}, x)))), h("tbody", {}, ...body));
  const pick = (k) => { cur = k; offset = 0; sel.clear(); mode = "browse"; close(); render(); };
  const close = UI.modal("训练报告（" + (r.ts || "") + "）", h("div", {},
    h("p", { class: "small" }, `索引 ${r.n_index ?? "—"} 张；人工确认 ${r.n_human ?? "—"} 张，qwen 弱标签 ${r.n_weak ?? 0} 张；` +
      (r.cv_acc != null ? `归类准确率（只在人工确认的图上，5 折交叉验证）${UI.pct(r.cv_acc, 1)}` : "人工确认不足 25 张，暂不计算准确率")),
    r.bin_total != null ? h("p", { class: "small muted" }, `临时仓库：放进去的 ${r.bin_total} 张里，这次被重新归到其他类 ${r.bin_rescued || 0} 张` +
      ((r.bin_rescued_to || []).length ? "（" + r.bin_rescued_to.map(([k, n]) => tname(k) + " " + n).join("，") + "）" : "")) : null,
    rows.length ? h("div", {}, h("h3", {}, "每类准确率（低的排前面，优先给它们多确认几张；点类型名跳过去）"),
      tbl(["类型", "准确率", "确认数"], rows.map(([k, a, n]) => h("tr", {},
        h("td", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); pick(k); } }, tname(k))),
        h("td", { style: { color: a < 0.8 ? "var(--bad, #c0392b)" : "" } }, UI.pct(a, 0)), h("td", {}, n))))) : null,
    (r.dist || []).length ? h("div", {}, h("h3", { style: { marginTop: "14px" } }, "全部照片归类分布"),
      h("p", { class: "small" }, r.dist.map(([k, n]) => `${tname(k)} ${n}`).join("，"))) : null), [], { wide: true });
}

// 新增照片类型（中英双语），写进本图库的类型清单
function newTypeDlg() {
  const inp = (ph) => h("input", { class: "input", placeholder: ph });
  const f = { name: inp("如：发电机房"), name_en: inp("如：Generator room"), key: inp("可不填，自动生成 custom_N；如 generator_room"),
    must_have: inp("可不填；用逗号分隔，如：发电机,油箱"), anomalies: inp("可不填；用逗号分隔，如：漏油,锈蚀"), anomalies_en: inp("和上面一一对应，如：Oil leak,Rust") };
  const row = (l, el, hint) => h("label", { class: "field", style: { display: "block", marginBottom: "10px" } },
    h("div", { class: "small", style: { fontWeight: 600, marginBottom: "4px" } }, l), el, hint ? h("div", { class: "tiny muted" }, hint) : null);
  UI.modal("新增照片类型", h("div", {},
    h("p", { class: "small muted" }, `只加到当前图库“${S.lib.name}”。先确认几张样板，再“重新训练并归类全部”，模型才会往这个类型里归。`),
    row("中文名 *", f.name), row("英文名 *", f.name_en, "国际项目，中英文都要有"), row("英文代号", f.key, "小写字母开头，只能用小写字母、数字、下划线"),
    row("必拍内容", f.must_have), row("常见异常（中文）", f.anomalies), row("常见异常（英文）", f.anomalies_en)), [
    { label: "取消" },
    { label: "新增", kind: "primary", run: async () => {
      const body = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value.trim()]));
      const r = await api("/api/types/new", { method: "POST", body });
      UI.toast(r.message); S = await api("/api/types/summary"); cur = r.key; offset = 0; sel.clear(); mode = "browse"; render();
    } }]);
}

// 编辑类型：改名称和描述 / 并入另一个类型 / 删除。后两项把本类已确认的样板改过去，再自动重新训练
function editTypeDlg(t) {
  const inp = (v, ph) => h("input", { class: "input", value: v || "", placeholder: ph || "" });
  const row = (l, el, hint) => h("label", { class: "field", style: { display: "block", marginBottom: "10px" } },
    h("div", { class: "small", style: { fontWeight: 600, marginBottom: "4px" } }, l), el, hint ? h("div", { class: "tiny muted" }, hint) : null);
  const f = { name: inp(t.name), name_en: inp(t.name_en), prompt: inp(t.prompt, "可不填；如：a photo of a diesel generator room"),
    must_have: inp((t.must_have || []).join(",")), anomalies: inp((t.anomalies || []).join(",")), anomalies_en: inp((t.anomalies_en || []).filter(Boolean).join(",")) };
  const others = S.types.filter((x) => x.key !== t.key && !x.bin);
  const binKey = S.types.find((x) => x.bin)?.key;
  const opts = (list) => list.map((x) => h("option", { value: x.key }, (x.bin ? "🗃️ " : "") + x.name + (x.bin ? "" : `（${x.count} 张）`)));
  const mergeSel = h("select", { class: "select", style: { width: "100%" } }, h("option", { value: "" }, "选一个类型…"), ...opts(others));
  const delSel = h("select", { class: "select", style: { width: "100%" } }, ...opts([S.types.find((x) => x.bin), ...others].filter(Boolean)));
  const tc = typedConfirm("删除");
  const done = async (r) => {
    UI.toast(r.message, false, 7000); sel.clear(); offset = 0; mode = "browse";
    if (r.dest) cur = r.dest === binKey ? cur : r.dest;
    await load(); if (S.train?.running) watchJob();
  };
  const panes = {
    edit: h("div", {},
      h("p", { class: "small muted" }, `代号 ${t.key} 不变，已确认的 ${t.confirmed} 张样板和现有归类都不动。`),
      row("中文名 *", f.name), row("英文名 *", f.name_en),
      row("英文描述", f.prompt, "给模型看的一句英文，用于还没有样板时的零样本兜底；改了要重新训练才生效"),
      row("必拍内容", f.must_have, "用逗号分隔"), row("常见异常（中文）", f.anomalies, "用逗号分隔"), row("常见异常（英文）", f.anomalies_en, "和上面一一对应")),
    merge: h("div", {},
      h("p", {}, `把“${t.name}”并入另一个类型：本类已确认的 ${t.confirmed} 张样板全部改成目标类型，模型猜成本类的 ${Math.max(0, t.count - t.confirmed)} 张也先归过去；然后删掉“${t.name}”，自动重新训练。`),
      row("并入到 *", mergeSel),
      h("p", { class: "small muted" }, "适合两个类型其实是同一类的情况。操作记在图库的类型修改日志里；并错了可以新建类型再把样板挑回来。")),
    del: h("div", {},
      h("p", {}, `删除类型“${t.name}”：本类已确认的 ${t.confirmed} 张样板改到下面选的地方，模型猜成本类的照片先进临时仓库，然后自动重新训练。照片本身不删。`),
      row("样板改到 *", delSel, "拿不准就放临时仓库，以后再分"),
      h("div", { class: "field" }, h("label", {}, "确认删除"), tc.el)),
  };
  let tab = "edit";
  const tabs = h("div", { class: "tabs", style: { marginTop: 0 } });
  const body = h("div", {}, tabs, h("div", { style: { marginTop: "12px" } }));
  const paint = () => {
    tabs.replaceChildren(...[["edit", "改名称和描述"], ["merge", "并入另一个类型"], ["del", "删除类型"]].map(([k, l]) =>
      h("button", { class: tab === k ? "on" : "", onclick: () => { tab = k; paint(); } }, l)));
    body.lastChild.replaceChildren(panes[tab]);
  };
  paint();
  UI.modal(`编辑类型：${t.name}`, body, [
    { label: "取消" },
    { label: "确定", kind: "primary", run: async () => {
      if (tab === "edit") {
        const b = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value.trim()]));
        const r = await api("/api/types/edit", { method: "POST", body: { key: t.key, ...b } });
        UI.toast(r.message, false, 6000); await load();
      } else if (tab === "merge") {
        if (!mergeSel.value) throw new Error("请选要并入的类型");
        if (!confirm(`确定把“${t.name}”并入“${tname(mergeSel.value)}”？`)) throw new Error("已取消");
        await done(await api("/api/types/remove", { method: "POST", body: { key: t.key, dest: mergeSel.value, merge: true } }));
      } else {
        if (!tc.ok()) throw new Error("请在输入框里输入“删除”两个字");
        await done(await api("/api/types/remove", { method: "POST", body: { key: t.key, dest: delSel.value } }));
      }
    } }]);
}

async function drawMain() {
  const main = document.getElementById("main"); if (!main || !cur) return;
  if (cur === TRASH) return drawTrash(main);
  const t = S.types.find((x) => x.key === cur) || {};
  const others = S.types.filter((x) => x.key !== cur);
  const moveSel = h("select", { class: "select", style: { width: "220px" } }, h("option", { value: "" }, "移到其他类型…"),
    ...others.map((x) => h("option", { value: x.key }, (x.bin ? "🗃️ " : "") + x.name)));
  const act = async (target, label) => {
    if (!sel.size) return UI.toast("先点选照片（可多选）", true);
    await api("/api/types/confirm", { method: "POST", body: { paths: [...sel], type: target } });
    UI.toast(`已${label} ${sel.size} 张`); sel.clear(); S = await api("/api/types/summary"); render();
  };
  moveSel.onchange = () => { if (moveSel.value) act(moveSel.value, "移到“" + tname(moveSel.value) + "”"); };
  const dismissSel = async () => {   // 权重建议：不采纳（照片留在原来的类型）
    if (!sel.size) return UI.toast("先点选照片（可多选）", true);
    await api("/api/types/suggestions/dismiss", { method: "POST", body: { paths: [...sel], type: cur } });
    UI.toast(`已忽略 ${sel.size} 张的建议`); sel.clear(); S = await api("/api/types/summary"); render();
  };
  const bar = h("div", { class: "bar" },
    h("div", { class: "row between" },
      h("div", {}, h("h2", { style: { margin: 0, fontSize: "18px" } }, (t.bin ? "🗃️ " : "") + (t.name || cur), h("span", { class: "muted small" }, `  ${t.name_en || ""} · ${cur}`)),
        h("div", { class: "small muted" }, `共 ${t.count} 张 · 人工确认 ${t.confirmed} · 没把握 ${t.low}` +
          (typeAcc(cur) ? ` · 本类准确率 ${UI.pct(typeAcc(cur)[0], 0)}` : ""))),
      h("div", { class: "row" },
        !t.bin ? h("button", { class: "btn", title: "改名称和描述；并入另一个类型；删除这个类型", onclick: () => editTypeDlg(t) }, "✏️ 编辑类型") : null,
        !t.bin ? h("button", { class: "btn" + (mode === "similar" ? " primary" : ""), onclick: () => { mode = mode === "similar" ? "browse" : "similar"; sel.clear(); drawMain(); } },
          mode === "similar" ? "← 回到本类照片" : "🔍 找相似图（用已确认的当样板）") : null)),
    h("div", { class: "row", style: { marginTop: "8px" } },
      mode === "browse" ? h("div", { class: "tabs", style: { margin: 0, border: 0 } }, ...[["predicted", "待确认（模型猜的）"], ["low", "没把握"], ["confirmed", "已确认"], ["all", "全部"],
        ...(t.suggested || view === "suggested" ? [["suggested", `💡 权重建议（${t.suggested || 0}）`]] : [])].map(([k, l]) =>
        h("button", { class: view === k ? "on" : "", onclick: () => { view = k; offset = 0; sel.clear(); drawMain(); } }, l))) : null,
      h("span", { class: "spacer", style: { flex: 1 } }),
      h("span", { class: "small muted" }, `已选 ${sel.size}`),
      h("button", { class: "btn sm", onclick: () => { document.querySelectorAll(".pcell").forEach((c) => { sel.add(c.dataset.path); c.classList.add("sel"); }); drawMain(); } }, "全选本页"),
      h("button", { class: "btn sm", onclick: () => { sel.clear(); drawMain(); } }, "清空"),
      !t.bin ? h("button", { class: "btn sm primary", onclick: () => act(cur, "确认为“" + t.name + "”") }, mode === "similar" ? "✓ 收进本类" : "✓ 确认为本类") : null,
      moveSel,
      view === "suggested" && mode === "browse" ? h("button", { class: "btn sm", title: "这些照片不再建议成本类型（不改它们现在的类型）", onclick: dismissSel }, "✕ 忽略所选建议") : null,
      !t.bin ? h("button", { class: "btn sm", onclick: () => act(S.types.find((x) => x.bin).key, "放进临时仓库") }, "🗃️ 放进临时仓库") : null,
      h("button", { class: "btn sm danger", title: "把原图从服务器原目录里删掉（先进回收站，可恢复）", onclick: () => { if (!sel.size) return UI.toast("先点选照片（可多选）", true); delDlg([...sel]); } }, "🗑️ 删除所选")));
  const grid = h("div", { class: "pgrid" });
  const tip = view === "suggested" && mode === "browse" ? UI.pitfall(["这些是模型按权重给的建议，还没人看过。先双击抽看几张（尤其置信度低的），确定没问题再整批「确认为本类」；确认后会成为照片类型分类器的样板，错的也会被学进去。",
    "不对的选中后点「✕ 忽略所选建议」，以后不会再建议成本类型。"]) : null;
  main.replaceChildren(bar, ...(tip ? [tip] : []), grid, h("div", { id: "pager", class: "row", style: { marginTop: "12px", flexWrap: "wrap" } }));
  let items, total;
  if (mode === "similar") {
    grid.replaceChildren(h("div", { class: "muted small" }, "在其他类型里找和本类已确认照片最像的…"));
    const d = await api(`/api/types/${cur}/similar?limit=120`);
    if (!d.seeds) { grid.replaceChildren(h("div", { class: "empty" }, "这个类型还没有人工确认的样板：先在“待确认”里确认几张。")); return; }
    items = d.items.map((x) => ({ path: x.path, badge: `相似 ${Math.round(x.score * 100)}`, sub: "现在：" + tname(x.current || "") })); total = items.length;
    bar.querySelector(".small.muted").textContent += ` · 样板 ${d.seeds} 张`;
  } else {
    const d = await api(`/api/types/${cur}/images?view=${view}&offset=${offset}&limit=${PAGE}`);
    total = d.total;
    items = d.items.map((x) => x.suggested ? ({ path: x.path, badge: `💡 ${x.suggested.label} ${Math.round((x.suggested.confidence || 0) * 100)}%`,
      sub: `权重 ${x.suggested.weights || ""} 检出 ${x.suggested.label}` + (x.current ? `；现在归在：${tname(x.current)}` : "") })
      : ({ path: x.path, badge: x.confirmed ? "✓ 已确认" : `${Math.round(x.conf * 100)}%`, ok: x.confirmed,
      sub: !x.confirmed && x.second ? `也像：${tname(x.second)} ${Math.round(x.second_conf * 100)}%` : "" }));
  }
  LIST = items; LIST_PAGED = mode === "browse"; LIST_TOTAL = total;
  grid.replaceChildren(...(items.length ? items.map((it) => {
    const cell = h("div", { class: "pcell" + (sel.has(it.path) ? " sel" : ""), "data-path": it.path, title: (it.sub || "") + "\n" + it.path.split("/").pop() },
      h("img", { loading: "lazy" }), h("span", { class: "chk" }, sel.has(it.path) ? "✓" : ""),
      h("span", { class: "tag" }, h("span", { class: "badge " + (it.ok ? "ok" : "") }, it.badge)));
    UI.blobUrl(thumb(it.path)).then((u) => { cell.querySelector("img").src = u; }).catch(() => {});
    cell.onclick = (e) => {
      if (e.detail === 2) setTimeout(() => viewer(it.path), 0);   // 第二下照常切换，等于把第一下的选中撤销
      sel.has(it.path) ? sel.delete(it.path) : sel.add(it.path);
      cell.classList.toggle("sel"); cell.querySelector(".chk").textContent = sel.has(it.path) ? "✓" : "";
      bar.querySelector(".spacer").nextSibling.textContent = `已选 ${sel.size}`;
    };
    return cell;
  }) : [h("div", { class: "empty" }, "这里没有照片")]));
  if (mode === "browse") {
    document.getElementById("pager").replaceChildren(...pager(total));
  }
}
// 翻页：首页 / 上一页 / 跳到第几页 / 下一页 / 末页 + 每页张数（记在本机浏览器）。选中的照片翻页后保留
function pager(total) {
  const pages = Math.max(1, Math.ceil(total / PAGE)), pg = Math.floor(offset / PAGE) + 1;
  const go = (n) => { n = Math.min(Math.max(1, n | 0), pages); if (n !== pg) { offset = (n - 1) * PAGE; drawMain(); window.scrollTo({ top: 0 }); } };
  const box = h("input", { class: "input", type: "number", min: 1, max: pages, value: pg, style: { width: "72px", textAlign: "center" },
    title: "输入页码，回车跳转", onkeydown: (e) => { if (e.key === "Enter") go(+box.value); } });
  const size = h("select", { class: "select", style: { width: "auto" }, title: "每页显示多少张",
    onchange: () => { PAGE = +size.value; try { localStorage.setItem(PAGE_KEY, PAGE); } catch { /* 不让存就算了 */ } offset = Math.floor(offset / PAGE) * PAGE; drawMain(); } },
    ...[60, 120, 200, 300].map((n) => h("option", { value: n, selected: n === PAGE }, `每页 ${n} 张`)));
  return [
    h("button", { class: "btn sm", disabled: pg <= 1, title: "第一页", onclick: () => go(1) }, "⏮ 首页"),
    h("button", { class: "btn sm", disabled: pg <= 1, onclick: () => go(pg - 1) }, "上一页"),
    h("span", { class: "small" }, "第"), box, h("span", { class: "small" }, `/ ${pages} 页`),
    h("button", { class: "btn sm", onclick: () => go(+box.value) }, "跳转"),
    h("button", { class: "btn sm", disabled: pg >= pages, onclick: () => go(pg + 1) }, "下一页"),
    h("button", { class: "btn sm", disabled: pg >= pages, title: "最后一页", onclick: () => go(pages) }, "末页 ⏭"),
    h("span", { class: "small muted" }, total ? `第 ${offset + 1}–${Math.min(offset + PAGE, total)} 张 / 共 ${total}` : ""),
    size,
    h("span", { class: "tiny muted" }, "单击选中，双击看大图（滚轮缩放、拖动、← → 切换）")];
}
// ── 看大图：默认适配窗口；滚轮以鼠标为中心缩放；拖动看细节；← → 切换（到页尾自动翻页）；空格选中；先显示预览再换成原图 ──
let LIST = [], LIST_PAGED = false, LIST_TOTAL = 0;
const original = (p) => withLib(`/api/types/original?path=${encodeURIComponent(p)}`);

function viewer(startPath) {
  let idx = Math.max(0, LIST.findIndex((x) => x.path === startPath)), s = 1, tx = 0, ty = 0, fit = true, nat = [1, 1], token = 0;
  const img = h("img", { draggable: false, style: { position: "absolute", left: 0, top: 0, transformOrigin: "0 0", userSelect: "none", willChange: "transform", imageRendering: "auto", boxShadow: "0 6px 30px rgba(0,0,0,.45)" } });
  const stage = h("div", { style: { position: "absolute", inset: "52px 0 40px 0", overflow: "hidden", cursor: "grab", touchAction: "none" } }, img);
  const title = h("div", { style: { minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } });
  const zoomTxt = h("span", { class: "mono", style: { minWidth: "52px", textAlign: "center" } });
  const loadTxt = h("span", { class: "tiny", style: { color: "#9cc3b3" } });
  const btn = (t, tip, fn, extra = {}) => h("button", { class: "btn sm", title: tip, onclick: (e) => { e.stopPropagation(); fn(); }, style: { background: "rgba(255,255,255,.08)", color: "#e6f1ec", borderColor: "rgba(255,255,255,.18)", ...extra } }, t);
  const selBtn = btn("", "空格：选中 / 取消选中（关掉后可以批量确认、移类、删除）", () => toggleSel());
  const head = h("div", { style: { position: "absolute", left: 0, right: 0, top: 0, height: "52px", display: "flex", alignItems: "center", gap: "8px", padding: "0 14px", background: "rgba(8,20,16,.92)", color: "#e6f1ec", fontSize: "13px" } },
    title, h("span", { style: { flex: 1 } }), loadTxt,
    btn("－", "缩小（-）", () => zoomAt(1 / 1.25)), zoomTxt, btn("＋", "放大（+）", () => zoomAt(1.25)),
    btn("适配窗口", "按 0", () => doFit()), btn("1:1 原始像素", "按 1", () => actual()),
    selBtn,
    cur === TRASH ? btn("↩ 恢复", "恢复这张", () => { close(); restoreDlg([LIST[idx].path]); }, { color: "#b7f0d4" })
      : btn("🗑️ 删除", "Delete 键", () => delDlg([LIST[idx].path], () => removeCurrent()), { color: "#ffb4a8", borderColor: "rgba(255,140,120,.4)" }),
    btn("✕", "Esc 关闭", () => close()));
  const prevB = h("button", { title: "上一张（←）", style: navStyle("left") }, "‹"), nextB = h("button", { title: "下一张（→）", style: navStyle("right") }, "›");
  const foot = h("div", { class: "tiny", style: { position: "absolute", left: 0, right: 0, bottom: 0, height: "40px", display: "flex", alignItems: "center", gap: "14px", padding: "0 14px", background: "rgba(8,20,16,.92)", color: "#9cc3b3" } });
  const back = h("div", { style: { position: "fixed", inset: 0, zIndex: 70, background: "#0b1512" } }, stage, head, prevB, nextB, foot);
  document.body.append(back);
  document.body.style.overflow = "hidden";

  function navStyle(side) {
    return { position: "absolute", top: "50%", [side]: "12px", transform: "translateY(-50%)", width: "44px", height: "64px", borderRadius: "12px", border: "0",
      background: "rgba(255,255,255,.1)", color: "#fff", fontSize: "34px", lineHeight: "60px", cursor: "pointer", zIndex: 2 };
  }
  const apply = () => { img.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`; zoomTxt.textContent = Math.round(s * 100) + "%"; };
  function doFit() {
    const W = stage.clientWidth, H = stage.clientHeight;
    s = Math.min(W / nat[0], H / nat[1]) * 0.98; tx = (W - nat[0] * s) / 2; ty = (H - nat[1] * s) / 2; fit = true; apply();
  }
  function zoomAt(k, cx = stage.clientWidth / 2, cy = stage.clientHeight / 2) {
    const fitS = Math.min(stage.clientWidth / nat[0], stage.clientHeight / nat[1]);
    const ns = Math.min(Math.max(s * k, fitS * 0.3), 16);
    tx = cx - (cx - tx) * (ns / s); ty = cy - (cy - ty) * (ns / s); s = ns; fit = false; apply();
  }
  function actual(cx = stage.clientWidth / 2, cy = stage.clientHeight / 2) { zoomAt(1 / s, cx, cy); }
  function toggleSel() {
    const p = LIST[idx].path;
    sel.has(p) ? sel.delete(p) : sel.add(p);
    const c = [...document.querySelectorAll(".pcell")].find((x) => x.dataset.path === p);
    if (c) { c.classList.toggle("sel", sel.has(p)); c.querySelector(".chk").textContent = sel.has(p) ? "✓" : ""; }
    const cnt = [...document.querySelectorAll("#main .small.muted")].find((x) => /^已选/.test(x.textContent)); if (cnt) cnt.textContent = `已选 ${sel.size}`;
    paintInfo();
  }
  function paintInfo() {
    const it = LIST[idx]; if (!it) return;
    const on = sel.has(it.path);
    selBtn.textContent = on ? "✓ 已选中" : "☐ 选中"; selBtn.style.background = on ? "var(--brand-600)" : "rgba(255,255,255,.08)";
    title.replaceChildren(h("b", {}, it.path.split("/").pop()), h("span", { style: { color: "#9cc3b3", marginLeft: "10px" } },
      `${(LIST_PAGED ? offset : 0) + idx + 1} / ${LIST_PAGED ? LIST_TOTAL : LIST.length}`), it.badge ? h("span", { class: "badge", style: { marginLeft: "10px" } }, it.badge) : null,
      it.sub ? h("span", { style: { color: "#9cc3b3", marginLeft: "8px" } }, it.sub) : null);
    foot.replaceChildren(h("span", { style: { flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, it.path),
      h("span", {}, "滚轮缩放 · 拖动看细节 · 双击 适配/1:1 · ← → 切换 · 空格 选中 · Del 删除 · Esc 关闭"));
  }
  async function show(i) {
    idx = i; const it = LIST[idx]; if (!it) return;
    const my = ++token; paintInfo(); loadTxt.textContent = "加载中…";
    prevB.style.visibility = idx > 0 || (LIST_PAGED && offset > 0) ? "" : "hidden";
    nextB.style.visibility = idx < LIST.length - 1 || (LIST_PAGED && offset + LIST.length < LIST_TOTAL) ? "" : "hidden";
    try {
      const u = await UI.blobUrl(thumb(it.path, 1600));            // 先显示预览，切换快
      if (my !== token) return;
      await setSrc(u, true);
      loadTxt.textContent = "加载原图…";
      const o = await UI.blobUrl(original(it.path));               // 再换原图，放大看细节更清楚
      if (my !== token) return;
      await setSrc(o, false);
      loadTxt.textContent = `原图 ${nat[0]}×${nat[1]}`;
    } catch (e) { if (my === token) loadTxt.textContent = "原图加载失败：" + e.message; }
  }
  async function setSrc(u, first) {
    const pre = new Image(); pre.src = u; await pre.decode().catch(() => {});
    const old = nat[0], wasFit = fit;
    nat = [pre.naturalWidth || 1, pre.naturalHeight || 1];
    img.src = u; img.style.width = nat[0] + "px"; img.style.height = nat[1] + "px";
    if (first || wasFit) doFit(); else { s *= old / nat[0]; apply(); }   // 预览换原图：画面位置、大小保持不变
  }
  async function go(d) {
    let i = idx + d;
    if (i >= 0 && i < LIST.length) return show(i);
    if (!LIST_PAGED) return;
    if (d > 0 && offset + LIST.length < LIST_TOTAL) { offset += PAGE; await drawMain(); show(0); }
    else if (d < 0 && offset > 0) { offset = Math.max(0, offset - PAGE); await drawMain(); show(LIST.length - 1); }
  }
  function removeCurrent() {   // 删除后列表已刷新（删掉的那张不在了）：原位置上就是下一张
    if (!LIST.length) return close();
    show(Math.min(idx, LIST.length - 1));
  }
  // 鼠标：滚轮缩放、拖动、双击
  stage.addEventListener("wheel", (e) => { e.preventDefault(); const r = stage.getBoundingClientRect(); zoomAt(Math.min(1.25, Math.max(0.8, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0022)))), e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  let drag = null;
  const onMove = (e) => { if (!drag) return; tx = drag.tx + e.clientX - drag.x; ty = drag.ty + e.clientY - drag.y; fit = false; apply(); };
  const endDrag = () => { drag = null; stage.style.cursor = "grab"; window.removeEventListener("pointermove", onMove); window.removeEventListener("pointerup", endDrag); };
  stage.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); drag = { x: e.clientX, y: e.clientY, tx, ty }; stage.style.cursor = "grabbing";
    window.addEventListener("pointermove", onMove); window.addEventListener("pointerup", endDrag);
  });
  stage.addEventListener("dblclick", (e) => { const r = stage.getBoundingClientRect(); fit ? actual(e.clientX - r.left, e.clientY - r.top) : doFit(); });
  prevB.onclick = () => go(-1); nextB.onclick = () => go(1);
  const onKey = (e) => {
    if (document.querySelector(".modal-back")) return;      // 删除确认框打开时，键盘交给对话框
    const k = e.key;
    if (k === "ArrowRight" || k === "PageDown") go(1);
    else if (k === "ArrowLeft" || k === "PageUp") go(-1);
    else if (k === "Escape") close();
    else if (k === "+" || k === "=") zoomAt(1.25);
    else if (k === "-" || k === "_") zoomAt(1 / 1.25);
    else if (k === "0") doFit();
    else if (k === "1") actual();
    else if (k === " ") toggleSel();
    else if (k === "Delete" && cur !== TRASH) delDlg([LIST[idx].path], () => removeCurrent());
    else return;
    e.preventDefault(); e.stopPropagation();
  };
  const onResize = () => { if (fit) doFit(); };
  window.addEventListener("keydown", onKey, true); window.addEventListener("resize", onResize);
  function close() {
    token++; endDrag(); back.remove(); document.body.style.overflow = "";
    window.removeEventListener("keydown", onKey, true); window.removeEventListener("resize", onResize);
  }
  show(idx);
}
// 用模型辅助归类：选一个子项目里训练好的模型 → 规则“模型认识的类别 → 照片类型” → 扫全部照片给出 💡 建议
// （后台跑“按权重给照片类型归类”任务；建议不算人工确认，到各类型的“权重建议”里看图确认或忽略）
const ASSIST_KEY = "lota.types.assist";
async function assistDlg() {
  const TASKS = { detect: "检测", segment: "分割", classify: "分类" };
  const projects = (await UI.api("/api/projects")).filter((p) => TASKS[p.config?.task]);
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(ASSIST_KEY) || "{}"); } catch { saved = {}; }
  const pSel = h("select", { class: "select" }, ...projects.map((p) => h("option", { value: p.id, selected: p.id === (saved.project || UI.project()) },
    `${p.config.name || p.id}（${TASKS[p.config.task]}）`)));
  const mSel = h("select", { class: "select" });
  // 模型可能训练在子项目的某个标注任务里（annotation-tasks/<任务>/models），要能选
  const tSel = h("select", { class: "select" });
  const tField = h("div", { class: "field" }, h("label", {}, "标注任务"), tSel, h("div", { class: "help" }, "模型是在哪个标注任务里训练的"));
  const drawTasks = () => {
    const p = projects.find((x) => x.id === pSel.value), tasks = Object.entries(p?.config?.annotation_tasks || {});
    const want = saved.project === pSel.value && "task" in saved ? saved.task : UI.task(pSel.value);
    tSel.replaceChildren(h("option", { value: "" }, "子项目本身（历史默认任务）"),
      ...tasks.map(([k, d]) => h("option", { value: k, selected: k === want }, `${d.name || k}（${k}）`)));
    tField.style.display = tasks.length ? "" : "none";
  };
  const rulesBox = h("div"), status = h("div", { class: "small" });
  const conf = h("input", { class: "input", type: "number", min: 0.05, max: 0.95, step: 0.05, value: saved.min_confidence ?? 0.5 });
  const boxes = h("input", { class: "input", type: "number", min: 1, max: 100, step: 1, value: 1 });
  const types = S.types.filter((t) => !t.bin);
  const scope = h("select", { class: "select" }, h("option", { value: "" }, "全部照片"),
    ...S.types.map((t) => h("option", { value: t.key }, `只扫现在归在「${t.name}」里的（${t.count} 张）`)));
  const limit = h("input", { class: "input", type: "number", min: 100, step: 1000, value: 20000 });
  const restart = h("input", { type: "checkbox" });
  let models = [], classes = [], rules = saved.rules?.length ? saved.rules : [{ labels: [], type: "" }];

  const drawRules = () => {
    rulesBox.replaceChildren(...rules.map((r, i) => {
      r.labels = r.labels.filter((x) => classes.includes(x));
      const tSel = h("select", { class: "select", style: { maxWidth: "260px" }, onchange: (e) => { r.type = e.target.value; } },
        h("option", { value: "" }, "（选照片类型）"), ...types.map((t) => h("option", { value: t.key, selected: t.key === r.type }, t.name)));
      return h("div", { class: "card", style: { padding: "8px 10px", margin: "6px 0", boxShadow: "none" } },
        h("div", { class: "row between" }, h("b", {}, `规则 ${i + 1}：模型认出这些类别 →`),
          h("button", { class: "btn sm ghost", onclick: () => { rules.splice(i, 1); if (!rules.length) rules.push({ labels: [], type: "" }); drawRules(); } }, "删除")),
        h("div", { class: "row", style: { flexWrap: "wrap", gap: "4px 12px", margin: "6px 0" } },
          classes.length ? classes.map((c) => h("label", { class: "row", style: { gap: "4px" } },
            h("input", { type: "checkbox", checked: r.labels.includes(c), onchange: (e) => { r.labels = e.target.checked ? [...new Set([...r.labels, c])] : r.labels.filter((x) => x !== c); } }), c))
            : h("span", { class: "small muted" }, "这个模型没有类别信息")),
        h("div", { class: "row" }, h("span", { class: "small" }, "归到照片类型："), tSel));
    }), h("button", { class: "btn sm", onclick: () => { rules.push({ labels: [], type: "" }); drawRules(); } }, "＋ 再加一条规则"));
  };
  const loadModels = async () => {
    mSel.replaceChildren(h("option", {}, "加载中…"));
    const q = tSel.value ? `?annotation_task=${encodeURIComponent(tSel.value)}` : "";
    const d = await UI.api(`/api/projects/${pSel.value}/models${q}`).catch(() => ({ models: [], aliases: {} }));
    models = d.models || [];
    const byVer = Object.fromEntries(models.map((m) => [m.version, m]));
    const opts = [...Object.entries(d.aliases || {}).filter(([, v]) => byVer[v]).map(([a, v]) => [a, `${a}（= ${v}）`, byVer[v]]),
      ...models.slice().reverse().map((m) => [m.version, m.version, m])];
    mSel.replaceChildren(...(opts.length ? opts.map(([v, label]) => h("option", { value: v, selected: v === saved.model }, label))
      : [h("option", { value: "" }, tSel.value ? "这个标注任务还没有训练好的模型" : "这个子项目还没有训练好的模型（模型可能在某个标注任务里，选一下上面的标注任务）")]));
    const pick = () => { const o = opts.find(([v]) => v === mSel.value); classes = o ? (o[2].classes || []) : []; drawRules(); };
    mSel.onchange = pick; pick();
  };
  pSel.onchange = () => { drawTasks(); loadModels(); };
  tSel.onchange = loadModels;
  drawTasks();
  await loadModels();

  const field = (label, el, help) => h("div", { class: "field" }, h("label", {}, label), el, help ? h("div", { class: "help" }, help) : null);
  const body = h("div", { class: "form" },
    h("p", { class: "small muted" }, `用子项目里训练好的模型扫图库“${S.lib.name}”的照片：`, "图里认出规则里的类别，就建议归到对应的照片类型（左侧类型后面的 💡 数字）。",
      "建议不算人工确认，要到该类型的「💡 权重建议」里看图，整批确认或忽略；确认后成为样板，再点“重新训练并归类全部”，分类器就更准。人工确认过类型的照片不扫、不改。"),
    h("div", { class: "three" }, field("子项目", pSel), tField, field("模型", mSel, "latest = 最新训练的；production = 正式使用的")),
    field("归类规则", rulesBox, "比如：grounding_bar → 接地/防雷；一张图中了多条规则，取把握最大的那条"),
    h("div", { class: "three" }, field("最低置信度", conf, "0.5 稳妥；调低建议多，误判也多"), field("至少认出几个目标", boxes, "分类模型忽略这项"), field("本次最多扫多少张", limit, "没扫完的下次接着扫")),
    h("div", { class: "two" }, field("扫描范围", scope, "比如只扫“其他/无关”，给临时仓库里的照片找归宿"),
      field("从头扫", h("label", { class: "row", style: { gap: "6px" } }, restart, "忘掉之前扫过的记录，全部重扫"))),
    status);
  UI.modal("🤖 用模型辅助归类", body, [
    { label: "关闭" },
    { label: "开始扫描", kind: "primary", run: async () => {
      const good = rules.filter((r) => r.labels.length && r.type);
      if (!mSel.value) throw new Error("先选一个有模型的子项目");
      if (!good.length) throw new Error("至少要一条完整的规则：勾选类别，再选照片类型");
      const args = { library: LIB, model: mSel.value, rules: good.map((r) => ({ labels: r.labels, photo_type: r.type })), min_confidence: Number(conf.value) || 0.5,
        min_boxes: Number(boxes.value) || 1, scan_limit: Number(limit.value) || 20000, restart: restart.checked, ...(scope.value ? { scope_types: scope.value } : {}),
        ...(tSel.value ? { annotation_task: tSel.value } : {}) };
      try { localStorage.setItem(ASSIST_KEY, JSON.stringify({ project: pSel.value, task: tSel.value, model: mSel.value, rules: good, min_confidence: args.min_confidence })); } catch { /* 浏览器不让存就算了 */ }
      const t0 = Date.now();
      status.replaceChildren(h("span", { class: "badge info" }, "扫描中…"), " 显卡上大约每分钟几千张；可以关掉这个窗口，扫完会提示");
      const result = await UI.runTool(pSel.value, "types.classify_by_weights", args, { timeoutSec: 6 * 3600,
        onProgress: (j) => status.replaceChildren(h("span", { class: "badge info" }, j.status === "running" ? "扫描中…" : "排队中…"),
          ` 已用 ${UI.dur((Date.now() - t0) / 1000)}；可以关掉这个窗口，扫完会提示（任务记录里也能看进度）`, UI.jobProgress(j)) })
        .catch((e) => { status.replaceChildren(h("span", { style: { color: "var(--danger)" } }, "❌ " + e.message)); throw e; });
      const by = Object.entries(result.by_type || {});
      UI.toast(`扫完 ${result.scanned} 张，给出 ${result.suggested} 条建议` + (result.remaining ? `；还有 ${result.remaining} 张没扫，再点一次接着扫` : ""), false, 8000);
      S = await api("/api/types/summary");
      const top = by.length ? S.types.find((t) => t.name === by[0][0]) : null;
      if (top) { cur = top.key; view = "suggested"; offset = 0; sel.clear(); mode = "browse"; }
      render();
    } }], { wide: true });
}

async function train() {
  await api("/api/types/train", { method: "POST" });
  UI.toast("开始训练并归类全部照片（约 1–3 分钟）…");
  S.train = { running: true, kind: "train", started: new Date().toLocaleTimeString() }; render(); watchJob();
}

// ── 删除：先进回收站（原目录里就没有了，可恢复）；选“永久删除”或在回收站里永久删除，才真正抹掉 ──
function previewStrip(paths) {
  return h("div", { class: "row", style: { gap: "6px", flexWrap: "wrap" } }, ...paths.slice(0, 12).map((p) => {
    const im = h("img", { style: { width: "84px", height: "63px", objectFit: "cover", borderRadius: "6px", background: "#e7ece9" } });
    UI.blobUrl(thumb(p, 200)).then((u) => { im.src = u; }).catch(() => {});
    return im;
  }), paths.length > 12 ? h("span", { class: "small muted", style: { alignSelf: "center" } }, `… 共 ${paths.length} 张`) : null);
}
function typedConfirm(word) {
  const inp = h("input", { class: "input", placeholder: `输入“${word}”两个字确认`, style: { maxWidth: "220px" } });
  return { el: inp, ok: () => inp.value.trim() === word };
}
function delDlg(paths, onDone) {
  const tc = typedConfirm("删除");
  const confirmRow = h("div", { class: "field", style: { display: "none" } }, h("label", {}, "永久删除不能恢复，请确认"), tc.el);
  const mode = h("div", { class: "seg", style: { gridAutoFlow: "row" } },
    h("label", {}, h("input", { type: "radio", name: "delmode", value: "trash", checked: true }), h("b", {}, "移到回收站（推荐）"),
      h("span", {}, "原图从服务器原目录里移走，所有页面不再出现；放错了可以在「🗑️ 回收站」里恢复")),
    h("label", {}, h("input", { type: "radio", name: "delmode", value: "permanent" }), h("b", {}, "永久删除"),
      h("span", {}, "直接从硬盘上抹掉，不能恢复。确定这些照片没用了再选")));
  mode.onchange = () => { confirmRow.style.display = mode.querySelector("input:checked").value === "permanent" ? "" : "none"; };
  const body = h("div", { class: "form" },
    h("p", {}, `要删除选中的 ${paths.length} 张照片：`), previewStrip(paths), mode, confirmRow,
    h("div", { class: "small muted" }, "初选导出的硬链接：原图目录里的同一张也一起移进回收站，恢复时两处都回来；永久删除后磁盘空间才真正释放。"),
    h("div", { class: "small muted" }, "LOTA 子项目里登记过的同一张图：移到回收站时只标为“排除”（不进数据集，标注保留），恢复时还原；永久删除时连登记和标注一起删掉。"));
  UI.modal(`删除 ${paths.length} 张照片`, body, [{ label: "取消" }, { label: "删除", kind: "danger", run: async () => {
    const permanent = mode.querySelector("input:checked").value === "permanent";
    if (permanent && !tc.ok()) throw new Error("请在输入框里输入“删除”两个字");
    const r = await api("/api/types/delete", { method: "POST", body: { paths, permanent } });
    const proj = Object.entries(r.catalog || {}).map(([k, n]) => `${k} ${n} 张`).join("、");
    UI.toast(`${permanent ? "已永久删除" : "已移到回收站"} ${r.deleted} 张${proj ? `；子项目里同时${permanent ? "删除" : "排除"}：${proj}` : ""}${r.failed.length ? `；${r.failed.length} 张没删成` : ""}${(r.notes || []).length ? "（" + r.notes.join("；") + "）" : ""}`, !!r.failed.length && !r.deleted, 7000);
    if (r.failed.length) UI.modal("这些照片没删成", h("ul", { class: "small" }, ...r.failed.slice(0, 50).map((f) => h("li", {}, f.msg))), [{ label: "知道了" }]);
    for (const p of paths) sel.delete(p);
    await load();
    if (r.deleted) onDone?.();
  } }]);
}
async function restoreDlg(paths) {
  const r = await api("/api/types/trash/restore", { method: "POST", body: { paths } });
  const proj = Object.entries(r.catalog || {}).map(([k, n]) => `${k} ${n} 张`).join("、");
  UI.toast(`已恢复 ${r.restored} 张到原来的位置${proj ? `（子项目里还原：${proj}）` : ""}${r.failed.length ? `；${r.failed.length} 张没恢复：${r.failed[0].msg}` : ""}`, !!r.failed.length && !r.restored);
  for (const p of paths) sel.delete(p);
  await load();
}
function purgeDlg(paths) {
  const all = !paths, tc = typedConfirm("删除");
  UI.modal(all ? "清空回收站" : `永久删除 ${paths.length} 张`, h("div", { class: "form" },
    h("div", { class: "callout warn" }, all ? `回收站里的 ${TR.total} 张照片（${UI.bytes(TR.bytes)}）会从硬盘上永久抹掉，不能恢复。` : "这些照片会从硬盘上永久抹掉，不能恢复。"),
    paths ? previewStrip(paths) : null, h("div", { class: "field" }, h("label", {}, "确认"), tc.el)),
    [{ label: "取消" }, { label: "永久删除", kind: "danger", run: async () => {
      if (!tc.ok()) throw new Error("请在输入框里输入“删除”两个字");
      const r = await api("/api/types/trash/purge", { method: "POST", body: all ? { all: true } : { paths } });
      const proj = Object.entries(r.catalog || {}).map(([k, n]) => `${k} ${n} 张`).join("、");
      UI.toast(`已永久删除 ${r.purged} 个文件${proj ? `；子项目里同时删除：${proj}` : ""}`); sel.clear(); await load();
    } }]);
}
async function drawTrash(main) {
  const d = await api(`/api/types/trash?offset=${offset}&limit=${PAGE}`);
  const count = h("span", { class: "small muted" }, `已选 ${sel.size}`);
  const need = (f) => () => { if (!sel.size) return UI.toast("先点选照片（可多选）", true); f([...sel]); };
  const bar = h("div", { class: "bar" },
    h("div", { class: "row between" },
      h("div", {}, h("h2", { style: { margin: 0, fontSize: "18px" } }, "🗑️ 回收站"),
        h("div", { class: "small muted" }, `${d.total} 张 · 占用 ${UI.bytes(d.bytes)} · 本图库删除的照片先放在这里（${d.dir}），可以恢复到原来的位置`)),
      h("button", { class: "btn danger", disabled: !d.total, onclick: () => purgeDlg(null) }, "清空回收站")),
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("span", { class: "spacer", style: { flex: 1 } }), count,
      h("button", { class: "btn sm", onclick: () => { d.items.forEach((x) => sel.add(x.path)); drawMain(); } }, "全选本页"),
      h("button", { class: "btn sm", onclick: () => { sel.clear(); drawMain(); } }, "清空"),
      h("button", { class: "btn sm primary", onclick: need(restoreDlg) }, "↩ 恢复所选"),
      h("button", { class: "btn sm danger", onclick: need(purgeDlg) }, "永久删除所选")));
  LIST = d.items.map((x) => ({ path: x.path, badge: "删除于 " + x.ts.slice(5, 16), sub: x.note })); LIST_PAGED = true; LIST_TOTAL = d.total;
  const grid = h("div", { class: "pgrid" }, ...(d.items.length ? d.items.map((it) => {
    const cell = h("div", { class: "pcell" + (sel.has(it.path) ? " sel" : ""), "data-path": it.path, title: `${it.path}\n删除于 ${it.ts}${it.note ? " · " + it.note : ""}` },
      h("img", { loading: "lazy" }), h("span", { class: "chk" }, sel.has(it.path) ? "✓" : ""),
      h("span", { class: "tag" }, h("span", { class: "badge" }, it.ts.slice(5, 16))));
    UI.blobUrl(thumb(it.path)).then((u) => { cell.querySelector("img").src = u; }).catch(() => {});
    cell.onclick = (e) => {
      if (e.detail === 2) setTimeout(() => viewer(it.path), 0);   // 第二下照常切换，等于把第一下的选中撤销
      sel.has(it.path) ? sel.delete(it.path) : sel.add(it.path);
      cell.classList.toggle("sel"); cell.querySelector(".chk").textContent = sel.has(it.path) ? "✓" : ""; count.textContent = `已选 ${sel.size}`;
    };
    return cell;
  }) : [h("div", { class: "empty" }, "回收站是空的")]));
  main.replaceChildren(bar, grid, h("div", { class: "row", style: { marginTop: "12px", flexWrap: "wrap" } }, ...pager(d.total)));
}

(async () => {
  await UI.nav("types", { picker: false });
  try { await load(); } catch (e) { UI.toast(e.message, true); }
  const q = new URLSearchParams(location.search);   // 从初选页“去照片类型建图库”跳过来
  if (q.get("newlib")) {
    const hit = LIBS.libs.find((l) => l.root === q.get("newlib"));
    if (hit) { if (hit.id !== LIB) switchLib(hit.id); UI.toast(`这个目录已经是图库“${hit.name}”了；新导出的照片点“更新索引”就进来`, false, 6000); }
    else newLibDlg(q.get("newlib"), q.get("name") || "");
  }
})();
