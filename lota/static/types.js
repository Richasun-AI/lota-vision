// 照片类型（⑤类型归类移植）：类型列表 → 看图（全部/已确认/待确认/没把握）→ 多选确认/移类/放临时仓库 → 找相似图 → 重新训练并归类全部
"use strict";
const { h } = UI;
const TRASH = "__trash__";
let S = null, TR = { total: 0, bytes: 0 }, cur = null, view = "predicted", offset = 0, sel = new Set(), mode = "browse", simItems = [];
const thumb = (p, size = 320) => `/api/types/thumb?path=${encodeURIComponent(p)}&size=${size}`;
const tname = (k) => { const t = S.types.find((x) => x.key === k); return t ? t.name : k; };

async function load() {
  [S, TR] = await Promise.all([UI.api("/api/types/summary"), UI.api("/api/types/trash?limit=1").catch(() => ({ total: 0, bytes: 0 }))]);
  if (!cur) cur = (S.types.find((t) => !t.bin && t.count) || S.types[0])?.key;
  await render();
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
        h("span", { class: "n" }, `${t.count}`, t.confirmed ? h("span", { style: { color: "var(--ok)" } }, ` ✓${t.confirmed}`) : ""))),
      h("div", { class: "titem" + (cur === TRASH ? " on" : ""), style: { marginTop: "8px", borderTop: "1px dashed var(--line-2)", paddingTop: "10px" },
        onclick: () => { cur = TRASH; offset = 0; sel.clear(); mode = "browse"; render(); } },
        h("span", {}, "🗑️ 回收站", h("span", { class: "tiny muted" }, " 删除的照片")), h("span", { class: "n" }, `${TR.total}`)));
  };
  q.oninput = drawList; drawList();
  const tr = S.train || {};
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "照片类型"), h("p", {}, "给海量巡检照片自动分类（站内地面、大门、电池柜……）：人工确认一些“样板”，模型学会后把其余照片自动归类；拿不准的进“临时仓库”。“按照片类型抽图”“用权重找图”都用这里的结果。")),
      h("div", { class: "row" },
        tr.running ? h("span", { class: "badge info" }, `训练中…（${tr.started} 开始）`) : null,
        h("button", { class: "btn primary lg", disabled: !!tr.running, onclick: train }, "重新训练并归类全部"))),
    h("div", { class: "grid k4", style: { marginBottom: "14px" } },
      kpi("已索引照片", (S.total || 0).toLocaleString(), "建过 SigLIP2 特征的"), kpi("人工确认", (S.confirmed_total || 0).toLocaleString(), "样板越多越准"),
      kpi("交叉验证准确率", r.cv_acc != null ? UI.pct(r.cv_acc, 1) : "—", r.ts ? "上次训练 " + r.ts + " · 点开看每类" : "还没训练", r.ts ? reportDlg : null),
      kpi("临时仓库", S.types.find((t) => t.bin)?.count ?? 0, r.bin_rescued ? `上次训练捞出 ${r.bin_rescued} 张` : "拿不准的照片")),
    h("div", { class: "types" },
      h("div", { class: "card tlist" }, q, list,
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

// 新增照片类型（中英双语），写进 自定义类型.yaml，和 44 类总表合并使用
function newTypeDlg() {
  const inp = (ph) => h("input", { class: "input", placeholder: ph });
  const f = { name: inp("如：发电机房"), name_en: inp("如：Generator room"), key: inp("可不填，自动生成 custom_N；如 generator_room"),
    must_have: inp("可不填；用逗号分隔，如：发电机,油箱"), anomalies: inp("可不填；用逗号分隔，如：漏油,锈蚀"), anomalies_en: inp("和上面一一对应，如：Oil leak,Rust") };
  const row = (l, el, hint) => h("label", { class: "field", style: { display: "block", marginBottom: "10px" } },
    h("div", { class: "small", style: { fontWeight: 600, marginBottom: "4px" } }, l), el, hint ? h("div", { class: "tiny muted" }, hint) : null);
  UI.modal("新增照片类型", h("div", {},
    h("p", { class: "small muted" }, "清单里没有合适的类型时用。新类型会和 44 个标准类型一起出现在左侧；先确认几张样板，再“重新训练并归类全部”，模型才会往这个类型里归。"),
    row("中文名 *", f.name), row("英文名 *", f.name_en, "国际项目，中英文都要有"), row("英文代号", f.key, "小写字母开头，只能用小写字母、数字、下划线"),
    row("必拍内容", f.must_have), row("常见异常（中文）", f.anomalies), row("常见异常（英文）", f.anomalies_en)), [
    { label: "取消" },
    { label: "新增", kind: "primary", run: async () => {
      const body = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value.trim()]));
      const r = await UI.api("/api/types/new", { method: "POST", body });
      UI.toast(r.message); S = await UI.api("/api/types/summary"); cur = r.key; offset = 0; sel.clear(); mode = "browse"; render();
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
    await UI.api("/api/types/confirm", { method: "POST", body: { paths: [...sel], type: target } });
    UI.toast(`已${label} ${sel.size} 张`); sel.clear(); S = await UI.api("/api/types/summary"); render();
  };
  moveSel.onchange = () => { if (moveSel.value) act(moveSel.value, "移到“" + tname(moveSel.value) + "”"); };
  const bar = h("div", { class: "bar" },
    h("div", { class: "row between" },
      h("div", {}, h("h2", { style: { margin: 0, fontSize: "18px" } }, (t.bin ? "🗃️ " : "") + (t.name || cur), h("span", { class: "muted small" }, `  ${t.name_en || ""} · ${cur}`)),
        h("div", { class: "small muted" }, `共 ${t.count} 张 · 人工确认 ${t.confirmed} · 没把握 ${t.low}` +
          (typeAcc(cur) ? ` · 本类准确率 ${UI.pct(typeAcc(cur)[0], 0)}` : ""))),
      h("div", { class: "row" },
        !t.bin ? h("button", { class: "btn" + (mode === "similar" ? " primary" : ""), onclick: () => { mode = mode === "similar" ? "browse" : "similar"; sel.clear(); drawMain(); } },
          mode === "similar" ? "← 回到本类照片" : "🔍 找相似图（用已确认的当样板）") : null)),
    h("div", { class: "row", style: { marginTop: "8px" } },
      mode === "browse" ? h("div", { class: "tabs", style: { margin: 0, border: 0 } }, ...[["predicted", "待确认（模型猜的）"], ["low", "没把握"], ["confirmed", "已确认"], ["all", "全部"]].map(([k, l]) =>
        h("button", { class: view === k ? "on" : "", onclick: () => { view = k; offset = 0; sel.clear(); drawMain(); } }, l))) : null,
      h("span", { class: "spacer", style: { flex: 1 } }),
      h("span", { class: "small muted" }, `已选 ${sel.size}`),
      h("button", { class: "btn sm", onclick: () => { document.querySelectorAll(".pcell").forEach((c) => { sel.add(c.dataset.path); c.classList.add("sel"); }); drawMain(); } }, "全选本页"),
      h("button", { class: "btn sm", onclick: () => { sel.clear(); drawMain(); } }, "清空"),
      !t.bin ? h("button", { class: "btn sm primary", onclick: () => act(cur, "确认为“" + t.name + "”") }, mode === "similar" ? "✓ 收进本类" : "✓ 确认为本类") : null,
      moveSel,
      !t.bin ? h("button", { class: "btn sm", onclick: () => act(S.types.find((x) => x.bin).key, "放进临时仓库") }, "🗃️ 放进临时仓库") : null,
      h("button", { class: "btn sm danger", title: "把原图从服务器原目录里删掉（先进回收站，可恢复）", onclick: () => { if (!sel.size) return UI.toast("先点选照片（可多选）", true); delDlg([...sel]); } }, "🗑️ 删除所选")));
  const grid = h("div", { class: "pgrid" });
  main.replaceChildren(bar, grid, h("div", { id: "pager", class: "row", style: { marginTop: "12px" } }));
  let items, total;
  if (mode === "similar") {
    grid.replaceChildren(h("div", { class: "muted small" }, "在其他类型里找和本类已确认照片最像的…"));
    const d = await UI.api(`/api/types/${cur}/similar?limit=120`);
    if (!d.seeds) { grid.replaceChildren(h("div", { class: "empty" }, "这个类型还没有人工确认的样板：先在“待确认”里确认几张。")); return; }
    items = d.items.map((x) => ({ path: x.path, badge: `相似 ${Math.round(x.score * 100)}`, sub: "现在：" + tname(x.current || "") })); total = items.length;
    bar.querySelector(".small.muted").textContent += ` · 样板 ${d.seeds} 张`;
  } else {
    const d = await UI.api(`/api/types/${cur}/images?view=${view}&offset=${offset}&limit=60`);
    total = d.total;
    items = d.items.map((x) => ({ path: x.path, badge: x.confirmed ? "✓ 已确认" : `${Math.round(x.conf * 100)}%`, ok: x.confirmed,
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
    document.getElementById("pager").replaceChildren(
      h("button", { class: "btn sm", disabled: !offset, onclick: () => { offset = Math.max(0, offset - 60); drawMain(); } }, "上一页"),
      h("span", { class: "small muted" }, `第 ${offset + 1}–${Math.min(offset + 60, total)} 张 / 共 ${total}`),
      h("button", { class: "btn sm", disabled: offset + 60 >= total, onclick: () => { offset += 60; drawMain(); } }, "下一页"),
      h("span", { class: "tiny muted" }, "单击选中，双击看大图（滚轮缩放、拖动、← → 切换）"));
  }
}
// ── 看大图：默认适配窗口；滚轮以鼠标为中心缩放；拖动看细节；← → 切换（到页尾自动翻页）；空格选中；先显示预览再换成原图 ──
let LIST = [], LIST_PAGED = false, LIST_TOTAL = 0;
const original = (p) => `/api/types/original?path=${encodeURIComponent(p)}`;

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
    if (d > 0 && offset + LIST.length < LIST_TOTAL) { offset += 60; await drawMain(); show(0); }
    else if (d < 0 && offset > 0) { offset = Math.max(0, offset - 60); await drawMain(); show(LIST.length - 1); }
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
async function train() {
  await UI.api("/api/types/train", { method: "POST" });
  UI.toast("开始训练并归类全部照片（约 1–3 分钟）…");
  const poll = setInterval(async () => {
    const t = await UI.api("/api/types/train");
    if (!t.running) {
      clearInterval(poll);
      if (t.error) UI.toast("训练失败：" + t.error, true);
      else UI.toast(`训练完成：交叉验证准确率 ${UI.pct(t.result?.cv_acc, 1)}，临时仓库捞出 ${t.result?.bin_rescued ?? 0} 张`);
      load();
    }
  }, 4000);
  S.train = { running: true, started: new Date().toLocaleTimeString() }; render();
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
    h("div", { class: "small muted" }, "LOTA 子项目里登记过的同一张图：移到回收站时只标为“排除”（不进数据集，标注保留），恢复时还原；永久删除时连登记和标注一起删掉。"));
  UI.modal(`删除 ${paths.length} 张照片`, body, [{ label: "取消" }, { label: "删除", kind: "danger", run: async () => {
    const permanent = mode.querySelector("input:checked").value === "permanent";
    if (permanent && !tc.ok()) throw new Error("请在输入框里输入“删除”两个字");
    const r = await UI.api("/api/types/delete", { method: "POST", body: { paths, permanent } });
    const proj = Object.entries(r.catalog || {}).map(([k, n]) => `${k} ${n} 张`).join("、");
    UI.toast(`${permanent ? "已永久删除" : "已移到回收站"} ${r.deleted} 张${proj ? `；子项目里同时${permanent ? "删除" : "排除"}：${proj}` : ""}${r.failed.length ? `；${r.failed.length} 张没删成` : ""}`, !!r.failed.length && !r.deleted);
    if (r.failed.length) UI.modal("这些照片没删成", h("ul", { class: "small" }, ...r.failed.slice(0, 50).map((f) => h("li", {}, f.msg))), [{ label: "知道了" }]);
    for (const p of paths) sel.delete(p);
    await load();
    if (r.deleted) onDone?.();
  } }]);
}
async function restoreDlg(paths) {
  const r = await UI.api("/api/types/trash/restore", { method: "POST", body: { paths } });
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
      const r = await UI.api("/api/types/trash/purge", { method: "POST", body: all ? { all: true } : { paths } });
      const proj = Object.entries(r.catalog || {}).map(([k, n]) => `${k} ${n} 张`).join("、");
      UI.toast(`已永久删除 ${r.purged} 个文件${proj ? `；子项目里同时删除：${proj}` : ""}`); sel.clear(); await load();
    } }]);
}
async function drawTrash(main) {
  const d = await UI.api(`/api/types/trash?offset=${offset}&limit=60`);
  const count = h("span", { class: "small muted" }, `已选 ${sel.size}`);
  const need = (f) => () => { if (!sel.size) return UI.toast("先点选照片（可多选）", true); f([...sel]); };
  const bar = h("div", { class: "bar" },
    h("div", { class: "row between" },
      h("div", {}, h("h2", { style: { margin: 0, fontSize: "18px" } }, "🗑️ 回收站"),
        h("div", { class: "small muted" }, `${d.total} 张 · 占用 ${UI.bytes(d.bytes)} · 删除的照片先放在这里（数据目录下的 _deleted），可以恢复到原来的位置`)),
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
  main.replaceChildren(bar, grid, h("div", { class: "row", style: { marginTop: "12px" } },
    h("button", { class: "btn sm", disabled: !offset, onclick: () => { offset = Math.max(0, offset - 60); drawMain(); } }, "上一页"),
    h("span", { class: "small muted" }, d.total ? `第 ${offset + 1}–${Math.min(offset + 60, d.total)} 张 / 共 ${d.total}` : ""),
    h("button", { class: "btn sm", disabled: offset + 60 >= d.total, onclick: () => { offset += 60; drawMain(); } }, "下一页"),
    h("span", { class: "tiny muted" }, "单击选中，双击看大图（滚轮缩放、拖动、← → 切换）")));
}

(async () => { await UI.nav("types", { picker: false }); load().catch((e) => UI.toast(e.message, true)); })();
