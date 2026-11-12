// 数据集：每个版本有多少图、多少框、怎么划分、类别分布、样例（带框）；一键把最新标注打包成新版本
"use strict";
const { h } = UI;
let SETTINGS = {};
const SPLIT = { train: ["训练", "#1d6d52"], val: ["验证", "#1d5fd1"], test: ["测试", "#c2410c"] };

async function render() {
  const pid = UI.project(), app = document.getElementById("app");
  const [{ datasets, accepted_annotations }, { settings }] = await Promise.all([
    UI.api(`/api/projects/${pid}/hub/datasets`), UI.api(`/api/projects/${pid}/settings`)]);
  const last = datasets[0], more = Math.max(0, accepted_annotations - (last?.image_total || 0));
  const info = UI.projectInfo(pid);
  SETTINGS = settings;
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "数据集"), h("p", {}, `「${info?.config?.name || pid}」的数据集版本。每个版本是某一时刻已复核标注的“快照”，打包后不会再变，训练和评估都基于它，结果可追溯、可复现。`)),
      h("div", { class: "row" }, h("a", { class: "btn", href: "/models" }, "去模型中心"),
        h("button", { class: "btn primary lg", onclick: () => publish(pid, settings) }, "＋ 打包最新标注"))),
    h("div", { class: "grid k4" },
      kpi("已复核的标注", accepted_annotations + " 张", "在 Label Studio 提交、并回收进来的"),
      kpi("待打包", more + " 张", more ? "比最新版本多，可以打包新版本再训练" : "都已在最新版本里"),
      kpi("最新版本", last ? last.version : "—", last ? `${last.image_total} 张 · ${last.box_total} 个框` : "还没有"),
      kpi("版本数", datasets.length, `命名规则：${settings.naming.dataset}`)),
    h("div", { class: "callout", style: { marginTop: "14px" } },
      h("b", {}, "三部分各管什么："), " 「训练」给模型学；「验证」训练时用来挑最好的那一轮；「测试」完全不参与训练，专门用来公平地比较不同模型。",
      `比例在项目设置里统一改（现在：验证 ${Math.round(settings.split.val * 100)}%、测试 ${Math.round(settings.split.test * 100)}%）。同一站点/重复照片只会分到同一部分，避免“考题泄露”。`),
    h("div", { class: "section-title" }, h("h2", {}, "全部版本")),
    datasets.length ? h("div", { class: "grid", style: { gap: "12px" } }, datasets.map((d) => dsCard(pid, d)))
      : h("div", { class: "empty" }, "还没有数据集。先在 Label Studio 标注并回收，再点右上角「打包最新标注」。"),
    h("div", { class: "section-title" }, h("h2", {}, "标注质检报告"), h("span", { class: "muted small" }, "用模型复查人工标注，找漏标、多标、框不准")),
    h("div", { id: "qa" }));
  loadQA(pid);
}
function kpi(label, value, hint) { return h("div", { class: "card kpi" }, h("span", { class: "label" }, label), h("span", { class: "value" }, value), h("span", { class: "hint" }, hint)); }

function dsCard(pid, d) {
  const total = Object.values(d.counts || {}).reduce((a, b) => a + b, 0) || 1;
  const bar = h("div", { style: { display: "flex", height: "12px", borderRadius: "99px", overflow: "hidden", background: "#e8eeea" } },
    ...Object.entries(d.counts || {}).filter(([, n]) => n).map(([k, n]) => h("i", { title: `${SPLIT[k]?.[0] || k} ${n} 张`, style: { width: (n / total * 100) + "%", background: SPLIT[k]?.[1] || "#999" } })));
  const classes = {};
  for (const sp of Object.values(d.class_counts || {})) for (const [c, n] of Object.entries(sp)) classes[c] = (classes[c] || 0) + n;
  const maxC = Math.max(1, ...Object.values(classes));
  return h("div", { class: "card hover" },
    h("div", { class: "row between" },
      h("div", {}, h("h3", { class: "mono" }, d.version), h("div", { class: "sub" }, `${UI.when(d.created)} · ${TASK_ZH[d.task] || d.task} · ${(d.classes || []).length} 个类别`)),
      h("div", { class: "row" }, h("button", { class: "btn sm primary", title: "划分、类别、尺寸、标注位置等统计图表（参考 Ultralytics Platform）", onclick: () => detail(pid, d, SETTINGS, "charts") }, "📊 图表"),
        h("button", { class: "btn sm", onclick: () => detail(pid, d, SETTINGS, "classes") }, "类别"),
        h("button", { class: "btn sm", onclick: () => detail(pid, d, SETTINGS, "images") }, "看样例"),
        d.task !== "classify" ? h("button", { class: "btn sm", onclick: () => runQA(pid, d) }, "找标注错误") : null,
        h("button", { class: "btn sm", title: "标准 YOLO 格式（images/labels/data.yaml），可拿到别的机器或平台训练", onclick: async (e) => {
          e.target.disabled = true; e.target.textContent = "打包中…";
          try { const u = await UI.blobUrl(`/api/projects/${pid}/datasets/${d.version}/download`); const a = h("a", { href: u, download: d.version + ".zip" }); document.body.append(a); a.click(); a.remove(); }
          catch (err) { UI.toast(err.message, true); } finally { e.target.disabled = false; e.target.textContent = "下载 zip"; } } }, "下载 zip"),
        h("a", { class: "btn sm", href: "/models?train=" + encodeURIComponent(d.version) }, "用它训练"))),
    h("div", { class: "grid k4", style: { marginTop: "12px" } },
      mini("图片", d.image_total), mini("标注框", d.box_total), mini("无目标图（负样本）", d.empty_images), mini("排除", Object.values(d.excluded || {}).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0))),
    h("div", { style: { marginTop: "12px" } }, bar,
      h("div", { class: "legend", style: { marginTop: "6px" } }, ...Object.entries(d.counts || {}).map(([k, n]) => h("span", {}, h("i", { style: { background: SPLIT[k]?.[1] } }), `${SPLIT[k]?.[0] || k} ${n}`)))),
    Object.keys(classes).length ? h("div", { style: { marginTop: "12px" } }, h("div", { class: "small muted" }, "各类别的框数（类别不平衡时，少的那类要多标）"),
      ...Object.entries(classes).sort((a, b) => b[1] - a[1]).map(([c, n]) => h("div", { class: "score" }, h("span", { class: "mono" }, c),
        h("div", { class: "meter" }, h("i", { style: { width: (n / maxC * 100) + "%" } })), h("span", { class: "v" }, n)))) : null);
}
function mini(label, v) { return h("div", {}, h("div", { class: "small muted" }, label), h("div", { style: { fontSize: "18px", fontWeight: 700 } }, v ?? 0)); }

async function publish(pid, settings) {
  const name = settings.naming.dataset.replace("{project}", pid);
  UI.modal("打包最新标注为新数据集", h("div", { class: "form" },
    h("p", {}, `会把所有已复核的标注打包成一个新版本，按“${name}”自动编号；验证 ${Math.round(settings.split.val * 100)}%、测试 ${Math.round(settings.split.test * 100)}%（随机种子 ${settings.split.seed}，同一张图每次都分到同一部分）。`),
    h("div", { class: "callout" }, "如果 Label Studio 里还有刚提交、没回收的标注，先在工作流里跑一次「回收标注」，或用「主动学习一轮」模板（它会先回收）。")),
    [{ label: "取消" }, { label: "回收并打包", kind: "primary", run: async () => {
      UI.toast("回收 Label Studio 标注…");
      try { await UI.runTool(pid, "labels.pull", {}); } catch (e) { UI.toast("回收跳过：" + e.message, true); }
      const out = await UI.runTool(pid, "dataset.publish", { version: name, val_fraction: settings.split.val, test_fraction: settings.split.test, seed: settings.split.seed });
      UI.toast(`已打包 ${out.version}`); render();
    } }]);
}

function samplesView(pid, d) {
  const grid = h("div", { class: "thumbs" }), info = h("span", { class: "small muted" });
  const tabs = h("div", { class: "tabs" });
  let split = "", empty = "", offset = 0;
  const load = async () => {
    const r = await UI.api(`/api/projects/${pid}/hub/datasets/${d.version}/samples?split=${split}&empty=${empty}&offset=${offset}&limit=48`);
    info.textContent = `共 ${r.total} 张，显示 ${offset + 1}–${Math.min(offset + 48, r.total)}`;
    grid.replaceChildren(...r.items.map((it) => {
      const cv = h("canvas"), cell = h("div", { class: "thumb", title: it.image_id }, cv,
        h("span", { class: "cap" }, h("span", { class: "badge " + (it.split === "train" ? "brand" : it.split === "val" ? "info" : "warn") }, SPLIT[it.split]?.[0] || it.split),
          " ", h("span", { class: "badge" }, (it.labels || []).length + " 框")));
      UI.blobUrl(`/api/projects/${pid}/images/${it.image_id}/preview?size=360`).then((u) =>
        UI.drawBoxes(cv, u, (it.labels || []).filter((l) => Array.isArray(l) && l.length >= 5).map((l) => ({ xywhn: l.slice(1, 5), label: l[0] })))).catch(() => {});
      return cell;
    }));
  };
  const tab = (label, sp, em) => { const b = h("button", { class: (split === sp && empty === em) ? "on" : "" }, label);
    b.onclick = () => { split = sp; empty = em; offset = 0; [...tabs.children].forEach((x) => x.classList.toggle("on", x === b)); load(); }; return b; };
  tabs.append(tab("全部", "", ""), tab("训练", "train", ""), tab("验证", "val", ""), tab("测试", "test", ""), tab("无目标图", "", "only"));
  tabs.firstChild.classList.add("on");
  const pager = h("div", { class: "row" }, h("button", { class: "btn sm", onclick: () => { offset = Math.max(0, offset - 48); load(); } }, "上一页"),
    h("button", { class: "btn sm", onclick: () => { offset += 48; load(); } }, "下一页"), info);
  load().catch((e) => UI.toast(e.message, true));
  return h("div", {}, tabs, grid, h("div", { style: { marginTop: "10px" } }, pager));
}

// ── 数据集详情：图表 / 类别 / 图像（参考 Ultralytics Platform 数据集页）────────────────
const PAL = ["#1d6d52", "#1d5fd1", "#c2410c", "#7c3aed", "#d9922b", "#0e7490", "#be185d", "#4d7c0f", "#b45309", "#475569"];
const OTHER = "#c9d4ce";
const TASK_ZH = { detect: "检测", segment: "实例分割", obb: "旋转框", pose: "姿态", classify: "分类" };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtN = (v) => v >= 10000 ? (v / 1000).toFixed(0) + "k" : v >= 1000 ? (v / 1000).toFixed(1) + "k" : String(Math.round(v * 10) / 10);
const svg = (w, hgt, inner) => h("div", { class: "dchart", html: `<svg viewBox="0 0 ${w} ${hgt}" width="100%" style="display:block;font:11px var(--font);fill:var(--muted)">${inner}</svg>` });

function donut(items, { center = "", sub = "" } = {}) {
  const total = items.reduce((a, b) => a + b.value, 0) || 1, R = 62, r = 40, cx = 75, cy = 75;
  let a0 = -Math.PI / 2, arcs = "";
  for (const it of items) {
    const a1 = a0 + it.value / total * Math.PI * 2, big = a1 - a0 > Math.PI ? 1 : 0;
    const p = (a, rr) => `${(cx + rr * Math.cos(a)).toFixed(2)} ${(cy + rr * Math.sin(a)).toFixed(2)}`;
    arcs += it.value >= total ? `<circle cx="${cx}" cy="${cy}" r="${(R + r) / 2}" fill="none" stroke="${it.color}" stroke-width="${R - r}"><title>${esc(it.label)} ${it.value}</title></circle>`
      : `<path d="M${p(a0, R)} A${R} ${R} 0 ${big} 1 ${p(a1, R)} L${p(a1, r)} A${r} ${r} 0 ${big} 0 ${p(a0, r)}Z" fill="${it.color}" stroke="#fff" stroke-width="1.5"><title>${esc(it.label)} ${it.value}（${(it.value / total * 100).toFixed(1)}%）</title></path>`;
    a0 = a1;
  }
  arcs += `<text x="${cx}" y="${cy + 2}" text-anchor="middle" style="font-size:18px;font-weight:700;fill:var(--ink)">${esc(center)}</text><text x="${cx}" y="${cy + 18}" text-anchor="middle">${esc(sub)}</text>`;
  return h("div", { class: "row", style: { alignItems: "center", gap: "16px", flexWrap: "nowrap" } },
    h("div", { style: { width: "150px", flex: "none" } }, svg(150, 150, arcs)),
    h("div", { class: "small", style: { display: "grid", gap: "3px", minWidth: 0 } }, ...items.map((it) =>
      h("div", { class: "row", style: { gap: "6px", flexWrap: "nowrap" } }, h("i", { style: { width: "10px", height: "10px", borderRadius: "3px", background: it.color, flex: "none" } }),
        h("span", { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, title: it.label }, it.label),
        h("b", { style: { marginLeft: "auto", paddingLeft: "8px" } }, it.value), h("span", { class: "muted" }, `${(it.value / total * 100).toFixed(1)}%`)))));
}

// 直方图：series = [{name, color, hist:{edges,counts}, mean}]，多组共用同一组区间时并排画
function histo(series, { unit = "", ints = false } = {}) {
  const W = 380, H = 170, L = 34, B = 26, T = 10, Rr = 8, pw = W - L - Rr, ph = H - T - B;
  const s0 = series.find((s) => s.hist?.counts?.length);
  if (!s0) return h("div", { class: "empty small" }, "没有数据");
  const edges = s0.hist.edges, n = s0.hist.counts.length, max = Math.max(1, ...series.flatMap((s) => s.hist.counts));
  const bw = pw / n, k = series.length;
  let g = `<line x1="${L}" y1="${T + ph}" x2="${L + pw}" y2="${T + ph}" stroke="var(--line-2)"/>`;
  for (const f of [0.5, 1]) g += `<line x1="${L}" x2="${L + pw}" y1="${T + ph - f * ph}" y2="${T + ph - f * ph}" stroke="var(--line)" stroke-dasharray="3 3"/><text x="${L - 4}" y="${T + ph - f * ph + 4}" text-anchor="end">${fmtN(max * f)}</text>`;
  series.forEach((s, si) => s.hist.counts.forEach((c, i) => {
    const bh = c / max * ph, x = L + i * bw + 1 + si * (bw - 2) / k;
    g += `<rect x="${x.toFixed(1)}" y="${(T + ph - bh).toFixed(1)}" width="${Math.max(1, (bw - 2) / k - 1).toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="${s.color}"><title>${esc(s.name)} ${fmtN(edges[i])}–${fmtN(edges[i + 1])}${unit}：${c}</title></rect>`;
  }));
  const lab = (v) => ints ? String(Math.round(v)) : fmtN(v);
  const stp = Math.round(edges[1] - edges[0]);
  const ticks = ints && n <= 12 ? edges.slice(0, -1).map((e, i) => [L + (i + 0.5) * bw, stp > 1 ? `${Math.round(e)}–${Math.round(e) + stp - 1}` : lab(e)]) : [[L, lab(edges[0])], [L + pw / 2, lab(edges[Math.floor(n / 2)])], [L + pw, lab(edges[n])]];
  for (const [x, t] of ticks) g += `<text x="${x}" y="${H - 8}" text-anchor="middle">${t}</text>`;
  series.forEach((s) => {
    if (s.mean == null || ints) return;
    const x = L + (s.mean - edges[0]) / ((edges[n] - edges[0]) || 1) * pw;
    if (x >= L && x <= L + pw) g += `<line x1="${x}" x2="${x}" y1="${T}" y2="${T + ph}" stroke="${s.color}" stroke-width="1.5" stroke-dasharray="4 3"><title>平均 ${s.mean}${unit}</title></line>`;
  });
  return h("div", {}, svg(W, H, g),
    h("div", { class: "legend small" }, ...series.map((s) => h("span", {}, h("i", { style: { background: s.color } }), s.name + (s.mean != null ? `（平均 ${s.mean}${unit}）` : "")))));
}

// 热力图：grid[行][列]，flipY = 纵轴向上（尺寸图），否则和图片坐标一致（左上角是原点）
function heat(grid, { flipY = false, frame = false, xlabel = "", ylabel = "", guides = null, xmax = 1, ymax = 1 } = {}) {
  const n = grid.length, S = 260, L = 40, T = 8, B = 30, cs = S / n, max = Math.max(1, ...grid.flat());
  let g = "";
  const col = (v) => {
    if (!v) return "#f4f7f5";
    const t = Math.sqrt(v / max), a = [226, 243, 235], b = [14, 38, 32];
    return `rgb(${a.map((x, i) => Math.round(x + (b[i] - x) * t)).join(",")})`;
  };
  grid.forEach((row, j) => row.forEach((v, i) => {
    const y = T + (flipY ? n - 1 - j : j) * cs;
    g += `<rect x="${(L + i * cs).toFixed(1)}" y="${y.toFixed(1)}" width="${(cs + 0.3).toFixed(1)}" height="${(cs + 0.3).toFixed(1)}" fill="${col(v)}">${v ? `<title>${v}</title>` : ""}</rect>`;
  }));
  if (frame) g += `<rect x="${L}" y="${T}" width="${S}" height="${S}" fill="none" stroke="var(--line-2)"/>`;
  if (guides) for (const [name, r] of guides) {      // 宽高比参考线：宽 = r × 高
    const hh = Math.min(ymax, xmax / r), x2 = L + r * hh / xmax * S, y2 = T + S - hh / ymax * S;
    g += `<line x1="${L}" y1="${T + S}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="#c2410c" stroke-width="1" stroke-dasharray="4 3" opacity=".8"/><text x="${(x2 - 2).toFixed(1)}" y="${(y2 + 12).toFixed(1)}" text-anchor="end" style="fill:#c2410c">${name}</text>`;
  }
  const yt = flipY ? [[T + S, 0], [T, ymax]] : [[T + 8, 0], [T + S, ymax]];
  g += `<text x="${L}" y="${T + S + 14}">0</text><text x="${L + S}" y="${T + S + 14}" text-anchor="end">${fmtN(xmax)}</text>`;
  for (const [y, v] of yt) g += `<text x="${L - 4}" y="${y}" text-anchor="end">${fmtN(v)}</text>`;
  g += `<text x="${L + S / 2}" y="${T + S + 26}" text-anchor="middle">${esc(xlabel)}</text><text transform="translate(12 ${T + S / 2}) rotate(-90)" text-anchor="middle">${esc(ylabel)}</text>`;
  return svg(L + S + 10, T + S + B + 4, g);
}

function hbars(items) {
  const max = Math.max(1, ...items.map((x) => x.value));
  return h("div", {}, ...items.map((it, i) => h("div", { class: "score" }, h("span", { class: "mono", title: it.label, style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, it.label),
    h("div", { class: "meter" }, h("i", { style: { width: (it.value / max * 100) + "%", background: it.color || PAL[i % PAL.length] } })), h("span", { class: "v" }, it.value))));
}

function chartCard(title, help, body) {
  return h("div", { class: "card", style: { boxShadow: "none" } }, h("div", { class: "row between" }, h("b", {}, title), help ? h("span", { class: "tiny muted", style: { maxWidth: "62%", textAlign: "right" } }, help) : null),
    h("div", { style: { marginTop: "10px" } }, body));
}

// 大白话体检结论：从统计里挑出最值得注意的几条
function insights(s, d) {
  const out = [], cls = s.classes || [];
  const n = (sp) => s.split?.[sp] || 0;
  if (s.task !== "classify" && s.bbox?.mean_w && s.dims?.mean_w) {
    const area = (s.bbox.mean_w * s.bbox.mean_h) / (s.dims.mean_w * s.dims.mean_h);
    if (area < 0.01) out.push(["warn", `目标很小：平均每个框只占图片面积的 ${(area * 100).toFixed(2)}%。训练时图像大小建议用 960 或 1280，试一试时打开「切片推理」。`]);
    else if (area < 0.04) out.push(["info", `目标偏小（平均占图片 ${(area * 100).toFixed(1)}%）：图像大小 640 可以，想更准可以试 960。`]);
    else out.push(["ok", `目标大小适中（平均占图片 ${(area * 100).toFixed(1)}%），图像大小 640 就够。`]);
  }
  if (cls.length > 1) {
    const mx = cls[0].count, mn = cls[cls.length - 1];
    if (mn.count < mx * 0.1) out.push(["warn", `类别很不均衡：「${mn.name}」只有 ${mn.count} 个，最多的「${cls[0].name}」有 ${mx} 个。少的那类要多标一些，否则模型容易忽略它。`]);
  }
  const unused = (d.classes || []).filter((c) => !cls.some((x) => x.name === c));
  if (unused.length) out.push(["warn", `这些类别一个标注都没有：${unused.slice(0, 6).join("、")}${unused.length > 6 ? " 等" : ""}。模型学不会它们。`]);
  if (n("val") && n("val") < 20) out.push(["info", `验证集只有 ${n("val")} 张，分数波动会比较大；标注再多一些更可靠。`]);
  if (s.task !== "classify" && s.images) {
    const neg = s.images - s.labeled;
    if (neg === 0) out.push(["info", "没有“无目标”的图（负样本）。加 5–10% 不含目标的照片，可以减少误报。"]);
    else if (neg / s.images > 0.5) out.push(["warn", `超过一半（${neg} 张）是无目标图，正样本偏少。`]);
  }
  const ar = s.dims2d?.mean_ar;
  if (ar && (ar > 2 || ar < 0.5)) out.push(["info", `图片很扁或很长（平均宽高比 ${ar}），训练时会补边成正方形，小目标会更小。`]);
  return out;
}

function chartsView(pid, d, stats) {
  const box = h("div", {}, h("div", { class: "muted small" }, "统计中…（第一次要读一遍所有图片的尺寸，之后会缓存）"));
  stats().then((s) => {
    const sp = Object.entries(s.split || {}).map(([k, v]) => ({ label: SPLIT[k]?.[0] || k, value: v, color: SPLIT[k]?.[1] || "#999" }));
    const top = s.classes.slice(0, 10).map((c, i) => ({ label: c.name, value: c.count, color: PAL[i % PAL.length] }));
    const rest = s.classes.slice(10).reduce((a, b) => a + b.count, 0);
    if (rest) top.push({ label: `其他 ${s.classes.length - 10} 类`, value: rest, color: OTHER });
    const det = s.task !== "classify";
    const tips = insights(s, d);
    box.replaceChildren(
      h("div", { class: "grid k4" }, kpi("图片", s.images, `${s.labeled} 张有标注`), kpi(det ? "标注（框/多边形）" : "标注", s.annotations, det ? `平均每张 ${s.objects?.mean ?? 0} 个` : "每张一个类别"),
        kpi("类别", `${s.classes.length} / ${s.class_total}`, "有标注的 / 定义的"), kpi("平均尺寸", `${Math.round(s.dims.mean_w)}×${Math.round(s.dims.mean_h)}`, `平均 ${fmtN(s.filesize.mean_kb)} KB · 宽高比 ${s.dims2d.mean_ar}`)),
      tips.length ? h("div", { class: "card", style: { marginTop: "12px", boxShadow: "none" } }, h("b", {}, "🩺 数据体检"),
        h("ul", { class: "small", style: { margin: "6px 0 0", paddingLeft: "18px" } }, ...tips.map(([lv, t]) => h("li", { style: { color: lv === "warn" ? "var(--warn)" : lv === "ok" ? "var(--ok)" : "" } }, t)))) : null,
      h("div", { class: "grid k2", style: { marginTop: "12px" } },
        chartCard("划分分布", "训练 / 验证 / 测试各多少张", donut(sp, { center: s.images, sub: "张图片" })),
        chartCard("主要类别", "前 10 个类别，其余合并为“其他”", top.length ? donut(top, { center: s.annotations, sub: "个标注" }) : h("div", { class: "empty small" }, "没有标注")),
        chartCard("图像尺寸", "宽、高的分布；虚线 = 平均", histo([{ name: "宽", color: PAL[0], hist: s.dims.width, mean: s.dims.mean_w }, { name: "高", color: PAL[1], hist: s.dims.height, mean: s.dims.mean_h }], { unit: " px" })),
        chartCard("图像文件大小", "太小可能压缩过度、看不清细节", histo([{ name: "文件大小", color: PAL[5], hist: s.filesize, mean: s.filesize.mean_kb }], { unit: " KB" })),
        chartCard("图像二维尺寸", `每格颜色越深，这种尺寸的图越多；虚线是常见宽高比。平均宽高比 ${s.dims2d.mean_ar}`,
          h("div", { style: { maxWidth: "340px", margin: "0 auto" } }, heat(s.dims2d.grid, { flipY: true, xlabel: "宽（px）", ylabel: "高（px）", xmax: s.dims2d.max_w, ymax: s.dims2d.max_h, guides: [["1:1", 1], ["4:3", 4 / 3], ["3:2", 1.5], ["16:9", 16 / 9]] }))),
        det ? chartCard("标注位置", "所有框中心点落在画面哪里（方框 = 整张图）。全挤在中间，说明拍法单一", h("div", { style: { maxWidth: "340px", margin: "0 auto" } }, heat(s.centers, { frame: true, xlabel: "横向位置（0 = 左边，1 = 右边）", ylabel: "纵向（0 = 顶部）" }))) : null,
        chartCard("图像格式", "", hbars(Object.entries(s.formats || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: k, value: v })))),
        det ? chartCard("边界框尺寸", "框的宽、高（像素）。很多框只有十几像素时，要加大训练图像大小", histo([{ name: "框宽", color: PAL[2], hist: s.bbox.width, mean: s.bbox.mean_w }, { name: "框高", color: PAL[3], hist: s.bbox.height, mean: s.bbox.mean_h }], { unit: " px" })) : null,
        det ? chartCard("每张图像中的对象数", `平均每张 ${s.objects.mean} 个；0 = 无目标图（负样本）`, histo([{ name: "张数", color: PAL[0], hist: s.objects }], { ints: true })) : null,
        s.points ? chartCard(s.task === "pose" ? "每个实例的可见关键点数" : "每个实例的点数", s.task === "pose" ? "点太少说明遮挡多" : "多边形的顶点数；太少说明轮廓画得粗", histo([{ name: "实例数", color: PAL[6], hist: s.points, mean: s.points.mean }])) : null));
  }).catch((e) => box.replaceChildren(h("div", { class: "callout warn" }, "统计失败：" + e.message)));
  return box;
}

function classesView(pid, d, settings, stats) {
  const names = settings.class_names || {}, splits = Object.keys(d.class_counts || {});
  const rows = (d.classes || []).map((c) => {
    const per = Object.fromEntries(splits.map((sp) => [sp, d.class_counts[sp]?.[c] || 0]));
    return { c, zh: names[c]?.zh && names[c].zh !== c ? names[c].zh : "", per, total: Object.values(per).reduce((a, b) => a + b, 0) };
  }).sort((a, b) => b.total - a.total);
  const all = rows.reduce((a, b) => a + b.total, 0) || 1, max = Math.max(1, ...rows.map((r) => r.total));
  const q = h("input", { class: "input", placeholder: "搜索类别…", style: { maxWidth: "240px" } });
  const tb = h("tbody");
  const draw = () => {
    const k = q.value.trim().toLowerCase();
    tb.replaceChildren(...rows.filter((r) => !k || r.c.toLowerCase().includes(k) || r.zh.toLowerCase().includes(k)).map((r, i) => h("tr", {},
      h("td", {}, h("i", { style: { display: "inline-block", width: "10px", height: "10px", borderRadius: "3px", background: i < 10 && !k ? PAL[i] : OTHER, marginRight: "6px" } }), h("span", { class: "mono" }, r.c)),
      h("td", { class: "small" }, r.zh || h("span", { class: "muted" }, "—")),
      ...splits.map((sp) => h("td", { class: "num" }, r.per[sp] || h("span", { class: r.total ? "muted" : "" }, "0"))),
      h("td", { class: "num" }, h("b", {}, r.total)),
      h("td", { style: { minWidth: "160px" } }, h("div", { class: "row", style: { flexWrap: "nowrap", gap: "8px" } },
        h("div", { class: "meter" + (r.total < max * 0.1 ? " warn" : ""), style: { flex: 1 } }, h("i", { style: { width: (r.total / max * 100) + "%" } })),
        h("span", { class: "tiny muted", style: { width: "44px", textAlign: "right" } }, (r.total / all * 100).toFixed(1) + "%"))),
      h("td", { class: "small" }, !r.total ? h("span", { class: "badge danger" }, "没有标注") : r.total < max * 0.1 ? h("span", { class: "badge warn" }, "太少，多标") : r.per.val === 0 && splits.includes("val") ? h("span", { class: "badge info" }, "验证集里没有") : h("span", { class: "badge ok" }, "正常")))));
  };
  q.oninput = draw; draw();
  return h("div", {},
    h("div", { class: "row between", style: { marginBottom: "10px" } }, h("span", { class: "small muted" }, `${rows.length} 个类别，共 ${all === 1 && !rows.some((r) => r.total) ? 0 : all} 个标注。中文名在「项目设置 → 类别名称」里改。`), q),
    rows.length > 1 ? h("div", { class: "card", style: { boxShadow: "none", marginBottom: "12px" } }, h("b", { class: "small" }, "各类别数量"),
      hbars(rows.slice(0, 30).map((r, i) => ({ label: r.zh ? `${r.c}（${r.zh}）` : r.c, value: r.total, color: i < 10 ? PAL[i] : OTHER }))),
      rows.length > 30 ? h("div", { class: "tiny muted" }, `只显示前 30 个，全部见下表`) : null) : null,
    h("table", { class: "table" }, h("thead", {}, h("tr", {}, h("th", {}, "类别"), h("th", {}, "中文名"), ...splits.map((sp) => h("th", { class: "num" }, SPLIT[sp]?.[0] || sp)), h("th", { class: "num" }, "合计"), h("th", {}, "占比"), h("th", {}, "状态"))), tb));
}

function detail(pid, d, settings, start = "charts") {
  let cache = null;
  const stats = () => cache || (cache = UI.api(`/api/projects/${pid}/datasets/${encodeURIComponent(d.version)}/stats`));
  const body = h("div"), tabs = h("div", { class: "tabs" });
  const views = { charts: ["📊 图表", () => chartsView(pid, d, stats)], classes: [`🏷 类别 ${(d.classes || []).length}`, () => classesView(pid, d, settings, stats)], images: [`🖼 图像 ${d.image_total}`, () => samplesView(pid, d)] };
  const show = (k) => { [...tabs.children].forEach((b) => b.classList.toggle("on", b.dataset.k === k)); body.replaceChildren(views[k][1]()); };
  for (const [k, [label]] of Object.entries(views)) { const b = h("button", { onclick: () => show(k) }, label); b.dataset.k = k; tabs.append(b); }
  UI.modal(`数据集 ${d.version} · ${TASK_ZH[d.task] || d.task}`, h("div", {}, tabs, body), [], { wide: true });
  show(start);
}

// ── 找标注错误（借鉴 FiftyOne mistakenness）────────────────────────────────
const ISSUE = { missing: ["疑似漏标", "danger", "#d4513f"], spurious: ["疑似多标/标错", "warn", "#d9922b"], loc: ["框不准", "info", "#1d5fd1"] };
async function loadQA(pid) {
  const box = document.getElementById("qa"); if (!box) return;
  const list = await UI.api(`/api/projects/${pid}/qa`).catch(() => []);
  box.replaceChildren(list.length ? h("table", { class: "table" }, h("tr", {}, h("th", {}, "时间"), h("th", {}, "数据集"), h("th", {}, "复查模型"), h("th", { class: "num" }, "查了"), h("th", { class: "num" }, "可疑"), h("th", {}, "明细"), h("th", {}, "")),
    ...list.map((q) => h("tr", {}, h("td", { class: "small" }, q.created), h("td", { class: "mono small" }, q.dataset_version), h("td", { class: "mono small" }, q.model),
      h("td", { class: "num" }, q.checked), h("td", { class: "num" }, h("b", {}, q.flagged)),
      h("td", { class: "small" }, Object.entries(q.totals).map(([k, n]) => `${ISSUE[k][0]} ${n}`).join(" · ")),
      h("td", {}, h("button", { class: "btn sm primary", onclick: () => openQA(pid, q.qa_id) }, "查看")))))
    : h("div", { class: "empty" }, "还没有报告。在上面任一数据集版本点「找标注错误」。"));
}
function runQA(pid, d) {
  const split = h("div", { class: "seg" },
    ...[["", "全部", "所有图都查"], ["val", "只查验证集", "模型没见过，结论更可靠"], ["train", "只查训练集", "找训练数据里的错"]].map(([v, t, ds], i) =>
      h("label", {}, h("input", { type: "radio", name: "qsplit", value: v, checked: i === 0 }), h("b", {}, t), h("span", {}, ds))));
  const miss = h("input", { type: "range", min: 0.2, max: 0.9, step: 0.05, value: 0.5, style: { flex: 1 } }), mv = h("b", { class: "mono" }, "0.5");
  miss.oninput = () => { mv.textContent = miss.value; };
  UI.modal(`找标注错误：${d.version}`, h("div", { class: "form" },
    h("p", { class: "small" }, "用“正式使用”的模型把这批已标注的图重新看一遍，和人工标注对比：模型很确定有、标注里没有 → 疑似漏标；标了但模型完全不认 → 疑似多标或标错类别；重叠不够 → 框不准。结果按可疑程度排序。"),
    h("div", { class: "field" }, h("label", {}, "查哪部分"), split),
    h("div", { class: "field" }, h("label", {}, "漏标门槛（模型置信度）"), h("div", { class: "row" }, miss, mv), h("div", { class: "help" }, "越低查出的“疑似漏标”越多，但误报也越多；0.5 稳妥")),
    h("div", { class: "callout warn" }, "注意：用训练过这批图的模型去查训练集，模型会“记住”标注，查出来的会偏少；查验证集更可靠。")),
    [{ label: "取消" }, { label: "开始检查", kind: "primary", run: async () => {
      UI.toast("检查中…（每 100 张约 10–20 秒）");
      const r = await UI.runTool(pid, "quality.mistakes", { dataset_version: d.version, split: document.querySelector("input[name=qsplit]:checked").value, conf_missing: Number(miss.value), device: "0" });
      UI.toast(`查了 ${r.checked} 张，可疑 ${r.flagged} 张`); loadQA(pid); openQA(pid, r.qa_id);
    } }]);
}
async function openQA(pid, id) {
  const q = await UI.api(`/api/projects/${pid}/qa/${encodeURIComponent(id)}`);
  const grid = h("div", { class: "grid k3" }), info = h("div", { class: "small muted" });
  let filter = "", offset = 0;
  const DEC = { fixed: ["已改好", "ok"], fine: ["标注没问题", "brand"], later: ["稍后再看", ""] };
  const draw = () => {
    const items = q.items.filter((it) => !filter || it.issues.some((x) => x.type === filter));
    info.textContent = `共 ${items.length} 张可疑（查了 ${q.checked} 张，模型 ${q.model}）。蓝色虚线 = 人工标注，橙色 = 模型预测，红框 = 疑似问题。`;
    grid.replaceChildren(...items.slice(offset, offset + 30).map((it) => {
      const cv = h("canvas", { style: { width: "100%", borderRadius: "8px", background: "#e7ece9", cursor: "zoom-in" } });
      const boxes = [...it.gt.map((g) => ({ xyxy: g.xyxy, color: "#1d5fd1", dashed: true })),
        ...it.detections.map((dd) => ({ xyxy: dd.xyxy, color: "#e8590c" })),
        ...it.issues.map((x) => ({ xyxy: x.xyxy, color: "#d4513f", label: ISSUE[x.type][0] }))];
      UI.blobUrl(`/api/projects/${pid}/images/${it.image_id}/preview?size=720`).then((u) => {
        UI.drawBoxes(cv, u, boxes, { natural: [it.width, it.height] });
        cv.onclick = () => { const big = h("canvas", { style: { width: "100%" } }); UI.modal("放大看", big, [], { wide: true }); UI.drawBoxes(big, u, boxes, { natural: [it.width, it.height] }); };
      }).catch(() => {});
      const dec = q.decisions[it.image_id];
      const btns = h("div", { class: "row", style: { marginTop: "6px" } }, ...Object.entries(DEC).map(([k, [t]]) =>
        h("button", { class: "btn sm" + (dec === k ? " primary" : ""), onclick: async () => {
          const v = dec === k ? "" : k;
          await UI.api(`/api/projects/${pid}/qa/${encodeURIComponent(id)}/decisions`, { method: "POST", body: { decisions: { [it.image_id]: v } } });
          if (v) q.decisions[it.image_id] = v; else delete q.decisions[it.image_id];
          draw();
        } }, t)),
        it.ls_url ? h("a", { class: "btn sm", href: it.ls_url, target: "_blank" }, "去 Label Studio 改") : null);
      return h("div", { class: "card", style: { boxShadow: "none", padding: "10px" } }, cv,
        h("div", { class: "row", style: { marginTop: "6px" } }, ...[...new Set(it.issues.map((x) => x.type))].map((t) => h("span", { class: "badge " + ISSUE[t][1] }, ISSUE[t][0])),
          h("span", { class: "tiny muted" }, `可疑分 ${it.score} · ${it.split}`)),
        h("ul", { class: "small", style: { margin: "4px 0", paddingLeft: "16px" } }, ...it.issues.slice(0, 4).map((x) => h("li", {}, x.text))), btns);
    }));
  };
  const tabs = h("div", { class: "tabs" });
  for (const [k, t] of [["", "全部"], ["missing", "疑似漏标"], ["spurious", "疑似多标/标错"], ["loc", "框不准"]]) {
    const b = h("button", { class: k === "" ? "on" : "" }, t + (k ? `（${q.totals[k]}）` : ""));
    b.onclick = () => { filter = k; offset = 0; [...tabs.children].forEach((x) => x.classList.toggle("on", x === b)); draw(); };
    tabs.append(b);
  }
  const pager = h("div", { class: "row", style: { marginTop: "10px" } }, h("button", { class: "btn sm", onclick: () => { offset = Math.max(0, offset - 30); draw(); } }, "上一页"),
    h("button", { class: "btn sm", onclick: () => { offset += 30; draw(); } }, "下一页"));
  UI.modal(`标注质检报告 · ${q.dataset_version}`, h("div", {}, info, tabs, grid, pager,
    h("div", { class: "callout", style: { marginTop: "10px" } }, "改完标注后：回收标注 → 打包新数据集 → 再训练。改得越干净，模型越准。")), [], { wide: true });
  draw();
}

(async () => { await UI.nav("datasets"); UI.onProject(render); render().catch((e) => UI.toast(e.message, true)); })();
