// 智能质检：检测管线（规则用大白话）→ 拖图试跑 → 实时质检记录（agent / 下载器 / 手机 App 推送的结果都在这里）
"use strict";
const { h } = UI;
const VERDICT = { pass: ["合格", "ok"], warn: ["需注意", "warn"], fail: ["不合格", "danger"], skip: ["没有匹配的规则", ""] };
const METRIC_TEXT = { count: "目标数量", coverage: "覆盖率", max_conf: "最高置信度", top1: "分类结果" };
let PIPES = [], META = null, timer = null, filter = "";

async function render() {
  const pid = UI.project(), app = document.getElementById("app");
  const d = await UI.api(`/api/projects/${pid}/pipelines`);
  PIPES = d.pipelines; META = d;
  const info = UI.projectInfo(pid);
  const drop = h("div", { class: "drop" }, h("div", { style: { fontSize: "28px" } }, "⤓"), h("b", {}, "把照片拖到这里，或点击选择（可多选）"),
    h("div", { class: "small" }, "按下面的质检规则逐张判定；结果同时记入下方“质检记录”"));
  const file = h("input", { type: "file", accept: "image/*", multiple: true, style: { display: "none" } });
  const ptype = h("input", { class: "input", placeholder: "照片类型（可不填），如 site_ground、站内地面", style: { maxWidth: "340px" } });
  drop.onclick = () => file.click();
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); runFiles([...e.dataTransfer.files], ptype.value); };
  file.onchange = () => runFiles([...file.files], ptype.value);
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "智能质检"), h("p", {}, `「${info?.config?.name || pid}」的自动审核：照片 → 模型 → 规则判定 →（可选）视觉大模型说明 → 合格 / 不合格。` +
        "下载器、Agent、手机 App 通过接口 POST /api/agent/inspect 推送的照片，结果也会实时出现在下面。")),
      h("div", { class: "row" }, h("a", { class: "btn", href: "/developers#inspect" }, "接口说明"), h("a", { class: "btn", href: "/report" }, "📄 巡检审核报告"),
        h("button", { class: "btn primary", onclick: () => editPipeline() }, "＋ 新建规则"))),
    h("div", { class: "grid k2" },
      h("div", { class: "card" }, h("h3", {}, "质检规则"), h("div", { class: "sub" }, "每条规则：用哪个模型、对哪类照片、什么条件算不合格。照片类型匹配不上任何规则时显示“没有匹配的规则”。"),
        PIPES.length ? PIPES.map(pipeCard) : h("div", { class: "empty", style: { marginTop: "10px" } }, "还没有规则，点右上角「新建规则」")),
      h("div", { class: "card" }, h("h3", {}, "拖图试一试"), h("div", { class: "row", style: { margin: "8px 0" } }, ptype), drop, file, h("div", { id: "tryres", style: { marginTop: "10px" } }))),
    h("div", { class: "section-title" }, h("h2", {}, "质检记录（实时）"),
      h("div", { class: "row", id: "filters" }, ...[["", "全部"], ["fail", "不合格"], ["warn", "需注意"], ["pass", "合格"]].map(([v, t]) =>
        h("button", { class: "btn sm" + (filter === v ? " primary" : ""), onclick: () => { filter = v; render(); } }, t)))),
    h("div", { id: "stats", class: "grid k4", style: { marginBottom: "12px" } }),
    h("div", { id: "history" }));
  loadHistory();
}

function pipeCard(p) {
  return h("div", { class: "card", style: { marginTop: "10px", boxShadow: "none" } },
    h("div", { class: "row between" }, h("div", {}, h("b", {}, p.name || p.id), " ", h("span", { class: "badge " + (p.enabled === false ? "" : "ok") }, p.enabled === false ? "停用" : "启用"),
      " ", h("span", { class: "badge info" }, "模型 " + (p.model || "production")), p.tile ? h("span", { class: "badge violet" }, "切片推理") : null),
      h("button", { class: "btn sm", onclick: () => editPipeline(p) }, "编辑")),
    h("div", { class: "small muted", style: { margin: "4px 0" } }, "适用照片：", (p.match || []).length ? p.match.join(" / ") : "全部照片"),
    ...p.rules.map((r) => h("div", { class: "small" }, "• 当 ", h("b", {}, r.label || "任意目标"), ` 的${METRIC_TEXT[r.metric]} ${r.op} `,
      h("b", {}, r.metric === "coverage" ? UI.pct(r.value) : r.value), ` → `, h("span", { class: "badge " + (VERDICT[r.verdict]?.[1] || "") }, VERDICT[r.verdict]?.[0] || r.verdict), " ", r.message || "")),
    p.vlm?.enabled ? h("div", { class: "small", style: { marginTop: "4px" } }, "🧠 不合格时让视觉大模型 ", h("code", {}, p.vlm.model), " 补一句说明") : null);
}

function editPipeline(p) {
  const pid = UI.project(), cls = (UI.projectInfo(pid)?.config?.classes || []).slice(0, 200);
  const d = p ? JSON.parse(JSON.stringify(p)) : { id: "rule-" + Date.now().toString(36), name: "", enabled: true, match: [], model: "production", conf: 0.25, imgsz: null,
    rules: [{ label: cls[0] || "", metric: "count", min_conf: 0.5, op: ">", value: 0, verdict: "fail", message: "", message_en: "" }],
    vlm: { enabled: false, model: "qwen2.5vl:7b", prompt: "这是一张巡检照片。请用一句中文和一句英文描述问题，不要编造。" } };
  const name = h("input", { class: "input", value: d.name, placeholder: "如：站内杂草检查" });
  const match = h("input", { class: "input", value: (d.match || []).join(", "), placeholder: "如：site_ground, 站内地面（留空 = 全部照片）" });
  const model = h("input", { class: "input", value: d.model });
  const enabled = h("input", { type: "checkbox", checked: d.enabled !== false });
  const tile = h("input", { type: "checkbox", checked: !!d.tile });
  const vlmOn = h("input", { type: "checkbox", checked: !!d.vlm?.enabled }), vlmPrompt = h("textarea", { class: "input" }, d.vlm?.prompt || "");
  const rulesBox = h("div");
  const ruleRow = (r) => {
    const lab = h("select", { class: "select" }, h("option", { value: "" }, "任意目标"), ...cls.map((c) => h("option", { value: c }, c))); lab.value = r.label || "";
    const met = h("select", { class: "select" }, ...Object.entries(METRIC_TEXT).map(([k, t]) => h("option", { value: k }, t))); met.value = r.metric;
    const op = h("select", { class: "select" }, ...[">", ">=", "<", "<=", "==", "!="].map((o) => h("option", { value: o }, o))); op.value = r.op;
    const val = h("input", { class: "input", value: r.value, style: { width: "80px" }, title: "覆盖率填 0–1 的小数，如 0.05 = 5%" });
    const ver = h("select", { class: "select" }, h("option", { value: "fail" }, "不合格"), h("option", { value: "warn" }, "需注意")); ver.value = r.verdict || "fail";
    const msg = h("input", { class: "input", value: r.message || "", placeholder: "给人看的原因，如：站内杂草较多", style: { flex: 1, minWidth: "180px" } });
    const mc = h("input", { class: "input", value: r.min_conf ?? 0.5, style: { width: "64px" }, title: "只统计置信度不低于它的框" });
    const row = h("div", { class: "rule" }, "当", lab, "的", met, op, val, "→", ver, msg, h("span", { class: "tiny muted" }, "框置信度≥"), mc,
      h("button", { class: "btn sm ghost", onclick: () => row.remove() }, "删除"));
    row.get = () => ({ label: lab.value, metric: met.value, op: op.value, value: met.value === "top1" ? val.value : Number(val.value), verdict: ver.value,
      message: msg.value, min_conf: Number(mc.value), region: r.region || (met.value === "coverage" ? "full" : undefined) });
    return row;
  };
  d.rules.forEach((r) => rulesBox.append(ruleRow(r)));
  const f = (l, el, hp) => h("div", { class: "field" }, h("label", {}, l), el, hp ? h("div", { class: "help" }, hp) : null);
  UI.modal(p ? "编辑质检规则" : "新建质检规则", h("div", { class: "form" },
    h("div", { class: "grid k2" }, f("规则名称", name), f("用哪个模型", model, "模型版本或别名；production = 当前正式使用的模型")),
    f("适用哪些照片", match, "照片类型或检查点里包含这些词就用这条规则；多个用逗号隔开。留空 = 本子项目所有照片（但“全子项目质检 / 审核报告”只用写了关键词的规则）"),
    f("判定条件", h("div", {}, rulesBox, h("button", { class: "btn sm", onclick: () => rulesBox.append(ruleRow({ metric: "count", op: ">", value: 0, verdict: "fail", min_conf: 0.5 })) }, "＋ 再加一条条件")),
      "覆盖率 = 目标框面积占整张图（或指定区域）的比例；目标数量 = 置信度达标的框有几个。任意一条命中就判定为对应结果"),
    h("label", { class: "row" }, enabled, "启用这条规则"),
    h("label", { class: "row" }, tile, "切片推理：高清大图里的小目标（远处锈点、裂纹、铁丝网破口）切成小块识别，更准但慢 3–10 倍"),
    h("details", { class: "adv" }, h("summary", {}, "🧠 视觉大模型补充说明（可选，借鉴 Ultralytics Agents：条件命中才调用）"),
      h("label", { class: "row", style: { margin: "8px 0" } }, vlmOn, "不合格 / 需注意时，让本机 Ollama 的 qwen2.5vl 看图补一句说明（每张约 3–10 秒）"),
      f("提示词", vlmPrompt))),
    [...(p ? [{ label: "删除规则", kind: "danger", run: async () => { await savePipes(PIPES.filter((x) => x.id !== p.id)); } }] : []),
     { label: "取消" }, { label: "保存", kind: "primary", run: async () => {
      const item = { ...d, name: name.value.trim() || d.id, match: match.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean), model: model.value.trim() || "production",
        enabled: enabled.checked, tile: tile.checked, tile_size: d.tile_size || 1024, rules: [...rulesBox.children].map((r) => r.get()), vlm: { ...d.vlm, enabled: vlmOn.checked, prompt: vlmPrompt.value } };
      await savePipes(p ? PIPES.map((x) => x.id === p.id ? item : x) : [...PIPES, item]);
    } }], { wide: true });
}
async function savePipes(list) { await UI.api(`/api/projects/${UI.project()}/pipelines`, { method: "PUT", body: { pipelines: list } }); UI.toast("规则已保存"); render(); }

async function runFiles(files, photoType) {
  const box = document.getElementById("tryres"); box.replaceChildren();
  for (const f of files) {
    const row = h("div", { class: "card", style: { marginTop: "8px", boxShadow: "none" } }, f.name + " 识别中…"); box.append(row);
    try {
      const fd = new FormData(); fd.append("project", UI.project()); fd.append("file", f); fd.append("photo_type", photoType || "");
      const r = await UI.api("/api/agent/inspect/upload", { method: "POST", body: fd });
      row.replaceWith(resultCard(r, URL.createObjectURL(f), f.name));
    } catch (e) { row.textContent = f.name + "：" + e.message; }
  }
  loadHistory();
}

function resultCard(r, src, title) {
  const cv = h("canvas");
  const dets = (r.results || []).flatMap((x) => x.detections.map((d) => ({ xyxy: d.xyxy, poly: d.polygon, kpts: d.keypoints, label: `${d.label} ${(d.confidence * 100).toFixed(0)}%` })));
  const nat = r.results?.[0] ? [r.results[0].width, r.results[0].height] : null;
  if (src) UI.drawBoxes(cv, src, dets, { natural: nat });
  cv.onclick = () => { const big = h("canvas", { style: { width: "100%" } }); UI.modal(title || "照片", big, [], { wide: true }); UI.drawBoxes(big, src, dets, { natural: nat }); };
  const v = VERDICT[r.verdict] || [r.verdict, ""];
  return h("div", { class: "card res verdict-" + r.verdict, style: { marginTop: "8px", boxShadow: "none" } }, cv,
    h("div", {}, h("div", { class: "row between" }, h("b", {}, title || r.meta?.filename || r.id || ""), h("span", { class: "badge " + v[1] }, v[0])),
      h("div", { class: "small muted" }, `${r.at || ""} · ${r.photo_type || "未指定照片类型"} · ${r.ms} 毫秒`),
      ...(r.results || []).map((x) => h("div", { class: "small", style: { marginTop: "4px" } }, h("b", {}, x.name || x.pipeline), `（模型 ${x.model}）：`,
        x.findings.filter((f) => f.hit).map((f) => `${f.message || "命中"}（${METRIC_TEXT[f.metric]} ${f.metric === "coverage" ? UI.pct(f.value, 1) : f.value}）`).join("；") || "全部合格",
        x.vlm ? h("div", { class: "callout", style: { marginTop: "4px" } }, "🧠 " + x.vlm) : null))));
}

async function loadHistory() {
  clearTimeout(timer);
  const pid = UI.project(), box = document.getElementById("history"); if (!box) return;
  const d = await UI.api(`/api/projects/${pid}/inspections?limit=40&verdict=${filter}`).catch(() => ({ items: [], stats: {} }));
  const st = d.stats || {}, total = Object.values(st).reduce((a, b) => a + b, 0);
  document.getElementById("stats").replaceChildren(
    ...[["全部", total, ""], ["不合格", st.fail || 0, "danger"], ["需注意", st.warn || 0, "warn"], ["合格", st.pass || 0, "ok"]].map(([t, n, c]) =>
      h("div", { class: "card kpi" }, h("span", { class: "label" }, t), h("span", { class: "value", style: { color: c ? `var(--${c === "ok" ? "ok" : c})` : "" } }, n),
        h("span", { class: "hint" }, total ? UI.pct(n / total) : "—"))));
  box.replaceChildren(d.items.length ? h("div", { class: "grid k2" }, ...d.items.map((r) => {
    const card = resultCard(r, null, r.meta?.filename || r.meta?.file || r.id);
    UI.blobUrl(`/api/projects/${pid}/inspections/image/${r.sha}`).then((u) => {
      const dets = (r.results || []).flatMap((x) => x.detections.map((dd) => ({ xyxy: dd.xyxy, poly: dd.polygon, kpts: dd.keypoints, label: `${dd.label} ${(dd.confidence * 100).toFixed(0)}%` })));
      const cv = card.querySelector("canvas"); const nat = r.results?.[0] ? [r.results[0].width, r.results[0].height] : null;
      UI.drawBoxes(cv, u, dets, { natural: nat });
      cv.onclick = () => { const big = h("canvas", { style: { width: "100%" } }); UI.modal(r.id, big, [], { wide: true }); UI.drawBoxes(big, u, dets, { natural: nat }); };
    }).catch(() => {});
    return card;
  })) : h("div", { class: "empty" }, "还没有质检记录。拖几张照片试试，或用接口推送。"));
  timer = setTimeout(loadHistory, 10000);   // 实时：每 10 秒刷新
}

(async () => { await UI.nav("inspect"); UI.onProject(render); render().catch((e) => UI.toast(e.message, true)); })();
