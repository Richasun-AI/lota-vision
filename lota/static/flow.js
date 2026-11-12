// 节点式工作流（LiteGraph.js）+ 简单模式（SwarmUI 风格）。
// 两种模式改的是同一张图；运行时把图转成引擎 Workflow（steps + $ref），由引擎 worker 执行。
"use strict";
const $ = (s) => document.querySelector(s);
// 访问口令（引擎对外监听时要求）：第一次 401 时询问，存在本浏览器，和工作台共用
const TK = "lota-token";
const getTok = () => { try { return localStorage.getItem(TK) || sessionStorage.getItem(TK) || ""; } catch { return ""; } };
const setTok = (t) => { try { localStorage.setItem(TK, t); sessionStorage.setItem(TK, t); } catch {} };
const authFetch = async (url, opt = {}) => {
  for (let i = 0; i < 3; i++) {
    const headers = { ...(opt.headers || {}) }, t = getTok();
    if (t) headers.Authorization = "Bearer " + t;
    const r = await fetch(url, { ...opt, headers });
    if (r.status !== 401) return r;
    const n = prompt("请输入 LOTA 引擎的访问口令（服务器 .env 里的 LOTA_API_TOKEN） / Enter the engine access token:");
    if (!n) return r;
    setTok(n.trim());
  }
  return fetch(url, opt);
};
const api = async (path, opt = {}) => {
  const r = await authFetch("/api" + path, { ...opt, headers: { "Content-Type": "application/json" } });
  const t = await r.text();
  let d; try { d = JSON.parse(t); } catch { d = t; }
  if (!r.ok) throw new Error((d && d.detail) ? (typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail)) : t);
  return d;
};
const TYPE_COLOR = { IMAGES: "#e8590c", DATASET: "#2f9e44", MODEL: "#7048e8", PREDICTIONS: "#1971c2", METRICS: "#868e96", REVIEW: "#c2255c" };
let CAT = {}, TPL = {}, WEIGHTS = [], graph, canvas, job = null, timer = null, currentTpl = null;

// ── 注册节点类型 ───────────────────────────────────────────────────────
function decimals(step) { const s = String(step); return s.includes(".") ? s.split(".")[1].length : 0; }
function registerNodes() {
  for (const [tool, def] of Object.entries(CAT)) {
    function N() {
      this.title = def.title;
      this.properties = { _tool: tool, _sid: "" };
      for (const i of def.inputs || []) this.addInput(i.name + (i.optional ? "（可选）" : ""), i.type, { color_on: TYPE_COLOR[i.type] });
      for (const o of def.outputs || []) this.addOutput(o.name, o.type, { color_on: TYPE_COLOR[o.type] });
      for (const [key, p] of Object.entries(def.params || {})) {
        const v = p.default ?? (p.kind === "toggle" ? false : p.kind === "number" ? 0 : "");
        this.properties[key] = v;
        const set = (val) => { this.properties[key] = val; syncSimple(); };
        let w;
        if (p.kind === "number") w = this.addWidget("number", p.label, v, set, { min: p.min, max: p.max, step: (p.step || 1) * 10, precision: decimals(p.step || 1) });
        else if (p.kind === "combo") w = this.addWidget("combo", p.label, v, set, { values: p.values });
        else if (p.kind === "weights") {  // 只显示文件名（全路径太长会盖住别的字），运行时用全路径
          const full = v || WEIGHTS[0] || "";
          this.properties[key] = full;
          w = this.addWidget("combo", p.label, base(full), (name) => { setParam(this, key, WEIGHTS.find((x) => base(x) === name) || name); },
                             { values: () => WEIGHTS.map(base) });
          w._weights = true;
        }
        else if (p.kind in DYN) {  // 下拉选项来自服务器（模型、类别、分类器、特征模型），显示名 ↔ 实际值
          const kind = p.kind;
          w = this.addWidget("combo", p.label, dynShow(kind, v), (name) => setParam(this, key, dynValue(kind, name)),
                             { values: () => dynList(kind, this.properties[key]).map((x) => x[1]) });
          w._dyn = kind;
        }
        else if (p.kind === "labels") {  // 多选：点一下弹出复选框（类别从权重里读）
          w = this.addWidget("button", p.label + "：" + (v || "全部（点我选择）"), null, () => pickLabels(this, key));
          w._labels = p.label;
        }
        else if (p.kind === "typerules" || p.kind === "phototypes") {  // 复杂参数：点按钮弹出编辑框
          w = this.addWidget("button", p.label + "：" + complexSummary(p.kind, v), null, () => pickComplex(this, key));
          w._complex = p.label; w._ckind = p.kind;
        }
        else if (p.kind === "toggle") w = this.addWidget("toggle", p.label, !!v, set);
        else w = this.addWidget("text", p.label, v ?? "", set);
        w._key = key;
      }
      this.size = this.computeSize(); this.size[0] = Math.max(this.size[0], 460);  // 宽一点，路径不盖住参数名
      this.status = null; this.summary = "";
    }
    N.title = def.title;
    N.prototype.onDrawForeground = function (ctx) {
      if (!this.status) return;
      const c = { running: "#1971c2", done: "#2f9e44", failed: "#e03131", queued: "#868e96" }[this.status];
      ctx.fillStyle = c; ctx.font = "12px sans-serif";
      ctx.fillText({ running: "⏳ 运行中", done: "✅ ", failed: "❌ ", queued: "… 排队" }[this.status] + (this.summary || ""), 8, this.size[1] - 6);
    };
    N.prototype.onSelected = function () { inspect(this); };
    LiteGraph.registerNodeType(def.category + "/" + def.title, N);
    def._type = def.category + "/" + def.title;
  }
}
const base = (p) => String(p || "").split("/").pop();
// 动态下拉：[实际值, 显示名]；按子项目加载
const DYN = { model: [], category: [], classifier: [], feature_model: [], phototype: [] };
const dynList = (kind, cur) => { const l = DYN[kind] || []; return cur && !l.some((x) => x[0] === cur) ? [[cur, cur], ...l] : l; };
const dynShow = (kind, v) => (DYN[kind] || []).find((x) => x[0] === v)?.[1] ?? (v || "");
const dynValue = (kind, name) => (DYN[kind] || []).find((x) => x[1] === name)?.[0] ?? name;
async function loadDyn(pid) {
  if (!DYN.phototype.length) {   // 照片类型（⑤归类结果）：和子项目无关，加载一次
    try { const t = await api("/types/summary"); DYN.phototype = [["", "（不限）"], ...t.types.map((x) => [x.key, `${x.name} · ${x.key}（${x.count} 张）`])]; } catch {}
  }
  const [models, project, cls, fm, std] = await Promise.all([
    api(`/projects/${pid}/models`).catch(() => ({})), api(`/projects/${pid}`).catch(() => ({})),
    api(`/projects/${pid}/classifiers`).catch(() => ({ classifiers: [] })), api("/feature_models").catch(() => ({ models: [] })),
    fetch("/static/standard_categories.json").then((r) => r.json()).catch(() => ({}))]);
  const al = Object.keys(models.aliases || {}), vs = (models.models || []).map((m) => m.version);
  DYN.model = [...new Set(["production", ...al, ...vs])].map((v) => [v, al.includes(v) ? `${v}（别名）` : v === "production" && !al.includes(v) ? "production（还没设置）" : v]);
  DYN.classes = project.config?.classes || [];   // 子项目模型认的类别（“找什么目标”在选了本场景模型时用）
  const cats = project.config?.categories || {};
  // 先列本场景已有的类别，再列标准照片类型（选了就在运行时自动加进场景）
  const own = Object.entries(cats).filter(([, c]) => c.enabled).map(([k, c]) => [k, c.name + (c.name_en ? " / " + c.name_en : "")]);
  const extra = Object.entries(std).filter(([k]) => !cats[k]?.enabled).map(([k, c]) => [k, `${c.name} / ${c.name_en}（标准类型）`]);
  DYN.category = [["", own.length ? "（请选择）" : "（请选择；本场景还没有类别，下面是标准照片类型）"], ...own, ...extra];
  DYN.classifier = [["", "（用连线的 / 未选）"], ...(cls.classifiers || []).map((c) => [c.version, c.version + (c.accuracy != null ? ` · 准确率 ${(c.accuracy * 100).toFixed(0)}%` : "")])];
  DYN.feature_model = [["", "（自动：服务器上的 SigLIP2）"], ...(fm.models || []).map((m) => [m.path, m.name])];
  if (graph) { for (const n of graph._nodes) for (const w of n.widgets || []) if (w._dyn) w.value = dynShow(w._dyn, n.properties[w._key]); canvas.setDirty(true, true); }
}
// 引擎在服务器上用 127.0.0.1:8081 访问 Label Studio；浏览器要用服务器地址
// 浏览器里打开 Label Studio：和本页同一台主机的 8081 端口（引擎内部用 127.0.0.1 连）
const LS_PUBLIC = `${location.protocol}//${location.hostname}:8081`;
const lsPublic = (u) => String(u).replace(/^https?:\/\/(127\.0\.0\.1|localhost):8081/, LS_PUBLIC);
function setParam(node, key, val) {
  node.properties[key] = val;
  const w = (node.widgets || []).find((w) => w._key === key);
  if (w && w._labels) { node.properties[key] = labelText(val).join(","); w.name = w._labels + "：" + (node.properties[key] || "全部（点我选择）"); }
  else if (w && w._complex) w.name = w._complex + "：" + complexSummary(w._ckind, val);
  else if (w && w._dyn) w.value = dynShow(w._dyn, val);
  else if (w) w.value = w._weights ? base(val) : val;
  if (w && w._weights) {  // 换了权重：去掉新权重不认识的目标
    const lk = Object.keys(CAT[node.properties._tool].params || {}).find((x) => CAT[node.properties._tool].params[x].kind === "labels");
    if (lk && node.properties[lk]) weightsInfo(val).then((i) => setParam(node, lk, labelText(node.properties[lk]).filter((x) => i.names.includes(x)).join(","))).catch(() => {});
  }
}
function fitView() {  // 缩放到整张流程都在屏幕里
  const ns = graph._nodes; if (!ns.length) return;
  const x0 = Math.min(...ns.map((n) => n.pos[0])), y0 = Math.min(...ns.map((n) => n.pos[1] - 30));
  const x1 = Math.max(...ns.map((n) => n.pos[0] + n.size[0])), y1 = Math.max(...ns.map((n) => n.pos[1] + n.size[1]));
  const el = $("#canvas"), s = Math.max(0.6, Math.min(1, (el.width - 60) / (x1 - x0), (el.height - 60) / (y1 - y0)));  // 小于 0.6 LiteGraph 不画文字
  canvas.ds.scale = s; canvas.ds.offset = [30 / s - x0, 30 / s - y0]; canvas.setDirty(true, true);
}
function nodeBySid(sid) { return graph._nodes.find((n) => n.properties._sid === sid); }

// ── 模板 / 已保存 → 图 ──────────────────────────────────────────────────
function loadTemplate(name) {
  const t = TPL[name]; if (!t) return;
  currentTpl = t; graph.clear();
  for (const n of t.nodes) {
    const node = LiteGraph.createNode(CAT[n.tool]._type);
    node.properties._sid = n.id; node.pos = n.pos || [40, 40];
    for (const [k, v] of Object.entries(n.args || {})) setParam(node, k, v);
    graph.add(node);
  }
  for (const [a, ao, b, bi] of t.links) nodeBySid(a).connect(ao, nodeBySid(b), bi);
  if (t.project && $("#project").value !== t.project && [...$("#project").options].some((o) => o.value === t.project)) {   // 专用模板（如空开）切到它自己的子项目，并提示
    $("#project").value = t.project; shareProject(t.project); refreshSaved().catch(() => {}); loadDyn(t.project).catch(() => {});
    $("#status").textContent = `这个模板属于子项目「${$("#project").selectedOptions[0].text}」，已切换过去`;
  }
  $("#wf-id").value = name + "-e2e";
  renderSimple(); canvas.setDirty(true, true);
}
function loadSteps(wf) {
  graph.clear(); currentTpl = null;
  const made = {};
  wf.steps.forEach((s, i) => {
    const def = CAT[s.tool]; if (!def) return;
    const node = LiteGraph.createNode(def._type);
    node.properties._sid = s.id; node.pos = [s.position?.x ?? 40 + i * 300, s.position?.y ?? 60];
    for (const [k, v] of Object.entries(s.args || {})) if (!(v && typeof v === "object" && v.$ref)) setParam(node, k, v);
    graph.add(node); made[s.id] = { node, def, step: s };
  });
  for (const { node, def, step } of Object.values(made)) {
    (def.inputs || []).forEach((inp, idx) => {
      let src = null, field = null;
      const ref = inp.arg && step.args?.[inp.arg]?.$ref;
      if (ref) { [src, field] = ref.split(/\.(.+)/); }
      else if (!inp.arg) {  // 只用于排序的连线：连到前面最近的同类型输出
        const prev = wf.steps.slice(0, wf.steps.indexOf(step)).reverse().find((s) => (CAT[s.tool]?.outputs || []).some((o) => o.type === inp.type));
        if (prev) { src = prev.id; field = CAT[prev.tool].outputs.find((o) => o.type === inp.type).field; }
      }
      if (!src || !made[src]) return;
      const oi = made[src].def.outputs.findIndex((o) => o.field === field);
      if (oi >= 0) made[src].node.connect(oi, node, idx);
    });
  }
  // 认出它是从哪个模板来的（模板的节点都还在、工具没变），简单模式就按模板显示关键参数
  const k = Object.keys(TPL).find((key) => TPL[key].nodes.every((n) => wf.steps.some((s) => s.id === n.id && s.tool === n.tool)));
  currentTpl = k ? TPL[k] : null; savedName = wf.name || "";
  if (k) $("#template").value = k;
  renderSimple(); canvas.setDirty(true, true);
}
let savedName = "";
const LAST = (pid) => "flow-last-" + pid;
const shareProject = (pid) => { try { if (pid) { sessionStorage.setItem("lota-project", pid); localStorage.setItem("lota-project", pid); } } catch {} };   // 和工作台共用当前子项目
const remember = (pid, wid) => { try { localStorage.setItem(LAST(pid), wid); } catch {} };
const lastOf = (pid) => { try { return localStorage.getItem(LAST(pid)) || ""; } catch { return ""; } };

// ── “找什么目标”：从权重里读出类别，打勾多选 ─────────────────────────────
const WINFO = {};
const weightsInfo = (path) => (WINFO[path] ||= api("/weights/info?path=" + encodeURIComponent(path)).catch((e) => { delete WINFO[path]; throw e; }));
const weightsOf = (node) => { const def = CAT[node.properties._tool]; const k = Object.keys(def.params || {}).find((x) => def.params[x].kind === "weights"); return k ? node.properties[k] : ""; };
const labelText = (v) => (Array.isArray(v) ? v.join(",") : String(v || "")).split(",").map((x) => x.trim()).filter(Boolean);
async function labelBoxes(node, key, onDone) {  // 返回一组复选框（DOM）
  const wrap = document.createElement("div"); wrap.className = "label-pick"; wrap.textContent = "读取权重类别…";
  const w = weightsOf(node), m = node.properties.model;
  if (!w && !m) { wrap.textContent = "先选权重或本场景的模型"; return wrap; }
  // 选了本场景的模型（如 latest）：类别按子项目的类别；否则从权重文件里读
  (m ? Promise.resolve({ names: DYN.classes || [] }) : weightsInfo(w)).then((info) => {
    wrap.innerHTML = "";
    const chosen = new Set(labelText(node.properties[key]));
    for (const name of info.names) {
      const l = document.createElement("label"), c = document.createElement("input");
      c.type = "checkbox"; c.checked = chosen.has(name);
      c.onchange = () => { c.checked ? chosen.add(name) : chosen.delete(name); setParam(node, key, info.names.filter((x) => chosen.has(x)).join(",")); onDone && onDone(); };
      l.append(c, " " + name); wrap.append(l);
    }
    const tip = document.createElement("div"); tip.className = "help";
    tip.textContent = `${base(w)}（${info.task === "classify" ? "分类" : "检测"}模型）共 ${info.names.length} 类；都不勾 = 任何目标都算`;
    wrap.append(tip);
  }).catch((e) => { wrap.textContent = "读不到权重类别：" + e.message; });
  return wrap;
}
// ── 归类规则（权重类别 → 照片类型，多条）和多选照片类型 ─────────────────────────
// 存成文字：“grounding_bar>grounding_lightning; rust,crack>site_ground”、“site_ground,other”
const parseRules = (v) => String(v || "").replace(/；/g, ";").split(";").map((x) => x.trim()).filter(Boolean).map((x) => {
  const [l, t] = x.split(">"); return { labels: (l || "").split(",").map((y) => y.trim()).filter(Boolean), type: (t || "").trim() };
});
const rulesText = (rs) => rs.filter((r) => r.labels.length || r.type).map((r) => r.labels.join(",") + ">" + r.type).join("; ");
const splitList = (v) => String(v || "").split(",").map((x) => x.trim()).filter(Boolean);
function complexSummary(kind, v) {
  if (kind === "typerules") { const n = parseRules(v).length; return n ? `${n} 条规则` : "（点我设置）"; }
  const n = splitList(v).length; return n ? `${n} 类` : "全部";
}
async function complexEditor(node, key, onDone) {
  const kind = CAT[node.properties._tool].params[key].kind;
  const wrap = document.createElement("div"); wrap.className = "label-pick";
  const types = (DYN.phototype || []).filter(([k]) => k);
  if (kind === "phototypes") {
    const chosen = new Set(splitList(node.properties[key]));
    const q = document.createElement("input"); q.placeholder = "搜索类型"; q.style.marginBottom = "6px";
    const list = document.createElement("div");
    const draw = () => {
      list.innerHTML = "";
      for (const [k, show] of types.filter(([k, show]) => !q.value || (k + show).toLowerCase().includes(q.value.toLowerCase()))) {
        const l = document.createElement("label"), c = document.createElement("input");
        c.type = "checkbox"; c.checked = chosen.has(k);
        c.onchange = () => { c.checked ? chosen.add(k) : chosen.delete(k); setParam(node, key, [...chosen].join(",")); onDone && onDone(); };
        l.append(c, " " + show); l.style.display = "block"; list.append(l);
      }
    };
    q.oninput = draw; draw(); wrap.append(q, list);
    return wrap;
  }
  // typerules：类别从“当前子项目的模型”或权重文件里读
  const w = weightsOf(node), m = node.properties.model;
  let names = [];
  if (m) names = DYN.classes || [];
  else if (w) names = (await weightsInfo(w).catch(() => ({ names: [] }))).names;
  let rules = parseRules(node.properties[key]);
  if (!rules.length) rules = [{ labels: [], type: "" }];
  const commit = () => { setParam(node, key, rulesText(rules)); onDone && onDone(); canvas.setDirty(true, true); };
  const draw = () => {
    wrap.innerHTML = "";
    if (!names.length) { const t = document.createElement("div"); t.className = "muted"; t.textContent = "先选“当前子项目的模型”或权重，才能勾选它认识的类别"; wrap.append(t); }
    rules.forEach((r, i) => {
      const box = document.createElement("div"); box.className = "rule-row";
      box.style.cssText = "border:1px solid var(--line,#ddd);border-radius:8px;padding:6px 8px;margin:6px 0";
      const head = document.createElement("div"); head.innerHTML = `<b>规则 ${i + 1}</b>：权重里的这些类别 →`;
      const labs = document.createElement("div");
      for (const n of names) {
        const l = document.createElement("label"), c = document.createElement("input");
        c.type = "checkbox"; c.checked = r.labels.includes(n);
        c.onchange = () => { r.labels = c.checked ? [...new Set([...r.labels, n])] : r.labels.filter((x) => x !== n); commit(); };
        l.append(c, " " + n); l.style.marginRight = "10px"; labs.append(l);
      }
      const sel = document.createElement("select");
      sel.add(new Option("（选择照片类型）", ""));
      for (const [k, show] of types) if (k !== "other") sel.add(new Option(show, k));
      sel.value = r.type; sel.onchange = () => { r.type = sel.value; commit(); };
      const del = document.createElement("button"); del.textContent = "删除这条"; del.className = "small";
      del.onclick = () => { rules.splice(i, 1); if (!rules.length) rules.push({ labels: [], type: "" }); commit(); draw(); };
      box.append(head, labs, sel, document.createTextNode(" "), del); wrap.append(box);
    });
    const add = document.createElement("button"); add.textContent = "＋ 添加一条规则";
    add.onclick = () => { rules.push({ labels: [], type: "" }); draw(); };
    wrap.append(add);
  };
  draw();
  return wrap;
}
async function pickComplex(node, key) {  // 节点上点按钮弹出的编辑框
  document.querySelector(".pick-modal")?.remove();
  const m = document.createElement("div"); m.className = "pick-modal";
  const card = document.createElement("div"); card.className = "card"; card.style.maxHeight = "80vh"; card.style.overflow = "auto";
  card.innerHTML = `<h3 style="margin:0 0 8px">${CAT[node.properties._tool].params[key].label}</h3>`;
  card.append(await complexEditor(node, key));
  const ok = document.createElement("button"); ok.textContent = "确定"; ok.className = "primary";
  ok.onclick = () => { m.remove(); renderSimple(); canvas.setDirty(true, true); };
  card.append(ok); m.append(card); m.onclick = (e) => { if (e.target === m) ok.onclick(); };
  document.body.append(m);
}
async function pickLabels(node, key) {  // 节点上点按钮弹出的多选框
  document.querySelector(".pick-modal")?.remove();
  const m = document.createElement("div"); m.className = "pick-modal";
  const card = document.createElement("div"); card.className = "card";
  card.innerHTML = `<h3 style="margin:0 0 8px">${CAT[node.properties._tool].params[key].label}（可多选）</h3>`;
  card.append(await labelBoxes(node, key));
  const ok = document.createElement("button"); ok.textContent = "确定"; ok.className = "primary";
  ok.onclick = () => { m.remove(); renderSimple(); canvas.setDirty(true, true); };
  card.append(ok); m.append(card); m.onclick = (e) => { if (e.target === m) ok.onclick(); };
  document.body.append(m);
}

// ── 图 → 引擎 Workflow ─────────────────────────────────────────────────
function toWorkflow() {
  const nodes = graph._nodes.slice();
  nodes.forEach((n, i) => { if (!n.properties._sid) n.properties._sid = "n" + n.id; });
  const deps = new Map(nodes.map((n) => [n.id, []]));
  for (const l of Object.values(graph.links || {})) if (l) deps.get(l.target_id).push(l.origin_id);
  const order = [], seen = new Set();
  const visit = (id, path = new Set()) => {
    if (seen.has(id)) return; if (path.has(id)) throw new Error("工作流里有环（A 连 B、B 又连回 A），请检查连线");
    path.add(id); deps.get(id).forEach((d) => visit(d, path)); path.delete(id); seen.add(id); order.push(id);
  };
  nodes.sort((a, b) => a.pos[0] - b.pos[0]).forEach((n) => visit(n.id));
  const steps = order.map((id) => {
    const n = graph.getNodeById(id), tool = n.properties._tool, def = CAT[tool];
    const args = {};
    for (const [key, p] of Object.entries(def.params || {})) {
      let v = n.properties[key];
      if (v === "" || v === null || v === undefined) continue;
      if (p.array && typeof v === "string") v = v.replace(/，/g, ",").split(",").map((x) => x.trim()).filter(Boolean);  // “a,b” → ["a","b"]
      if (p.json && typeof v === "string") { try { v = JSON.parse(v); } catch { throw new Error(`节点「${def.title}」的「${p.label}」不是合法的 JSON`); } }
      args[key] = v;
    }
    (def.inputs || []).forEach((inp, idx) => {
      const lid = n.inputs?.[idx]?.link; if (lid == null || !inp.arg) return;
      const l = graph.links[lid], src = graph.getNodeById(l.origin_id);
      args[inp.arg] = { $ref: src.properties._sid + "." + CAT[src.properties._tool].outputs[l.origin_slot].field };
    });
    for (const inp of def.inputs || []) {  // 必填输入没连线
      const idx = def.inputs.indexOf(inp);
      if (inp.arg && !inp.optional && n.inputs?.[idx]?.link == null && !(inp.arg in args))
        throw new Error(`节点「${def.title}」的输入「${inp.name}」还没连线`);
    }
    return { id: n.properties._sid, tool, args, position: { x: n.pos[0], y: n.pos[1] } };
  });
  if (!steps.length) throw new Error("画布上还没有节点");
  return { name: (currentTpl?.name || $("#wf-id").value || "工作流").slice(0, 100), steps };
}

// ── 简单模式：模板里列出的关键参数 ─────────────────────────────────────
function renderSimple() {
  const box = $("#simple"); box.innerHTML = "";
  const t = currentTpl;
  const head = document.createElement("div"); head.className = "card";
  const wid = $("#wf-id").value;
  const steps = `<div class="steps">${toSafeSteps().map((s) => `<span class="step" data-sid="${s.sid}">${s.title}</span>`).join(" → ")}</div>`;
  head.innerHTML = t ? `<h2 style="margin:0 0 6px">${t.name}</h2>${wid ? `<div class="muted">当前工作流：<b>${wid}</b>（保存后下次打开页面会自动打开它）</div>` : ""}
    <div class="muted">${t.desc || ""}</div>${steps}
    <div class="muted">这里只列出每次需要改的参数，其余用默认值；要看全部参数和连线，切到右上角「节点模式」。</div>`
    : graph._nodes.length ? `<h2 style="margin:0 0 6px">${savedName || wid || "自定义工作流"}</h2><div class="muted">当前工作流：<b>${wid}</b>（不是从模板来的，下面列出全部参数）</div>${steps}`
    : `<div class="muted">先在上方选一个模板点“载入模板”，或打开已保存的工作流。</div>`;
  box.append(head);
  const pairs = t?.simple || graph._nodes.flatMap((n) => Object.keys(CAT[n.properties._tool].params || {}).map((k) => [n.properties._sid || ("n" + n.id), k]));
  if (!pairs.length) return;
  const card = document.createElement("div"); card.className = "card";
  for (const [sid, key] of pairs) {
    const node = nodeBySid(sid) || graph._nodes.find((n) => "n" + n.id === sid); if (!node) continue;
    const p = CAT[node.properties._tool].params[key];
    const row = document.createElement("div"); row.className = "field";
    const id = `f-${sid}-${key}`;
    let input;
    if (p.kind === "typerules" || p.kind === "phototypes") {
      row.innerHTML = `<label>${CAT[node.properties._tool].title} · ${p.label}</label>`;
      complexEditor(node, key).then((el) => row.insertBefore(el, row.children[1] || null));
      const help = document.createElement("div"); help.className = "help"; help.textContent = p.help; row.append(help);
      card.append(row); continue;
    }
    if (p.kind === "labels") {
      row.innerHTML = `<label>${CAT[node.properties._tool].title} · ${p.label}（可多选）</label>`;
      labelBoxes(node, key).then((el) => row.insertBefore(el, row.children[1] || null));
      const help = document.createElement("div"); help.className = "help"; help.textContent = p.help; row.append(help);
      card.append(row); continue;
    }
    if (p.kind === "toggle") {
      input = document.createElement("input"); input.type = "checkbox"; input.style.width = "auto";
      input.checked = !!node.properties[key]; input.onchange = () => setParam(node, key, input.checked);
      row.innerHTML = `<label for="${id}">${CAT[node.properties._tool].title} · ${p.label}</label>`;
      input.id = id; row.append(input);
      const help = document.createElement("div"); help.className = "help"; help.textContent = p.help; row.append(help);
      card.append(row); continue;
    }
    if (p.kind in DYN) {
      input = document.createElement("select");
      for (const [v, show] of dynList(p.kind, node.properties[key])) input.add(new Option(show, v));
    } else if (p.kind === "combo" || p.kind === "weights") {
      input = document.createElement("select");
      for (const v of (p.kind === "weights" ? WEIGHTS : p.values)) input.add(new Option(p.kind === "weights" ? v.split("/").pop() : v, v));
    } else {
      input = document.createElement("input");
      if (p.kind === "number") { input.type = "number"; input.step = p.step || 1; if (p.min != null) input.min = p.min; if (p.max != null) input.max = p.max; }
    }
    input.id = id; input.value = node.properties[key] ?? "";
    // 换权重或换“本场景的模型”都要重画：“找什么目标”的类别跟着变
    input.onchange = () => { setParam(node, key, p.kind === "number" ? Number(input.value) : input.value); if (p.kind === "weights" || p.kind === "model") renderSimple(); };
    row.innerHTML = `<label for="${id}">${CAT[node.properties._tool].title} · ${p.label}</label>`;
    row.append(input);
    const help = document.createElement("div"); help.className = "help"; help.textContent = p.help; row.append(help);
    if (p.kind === "weights" && node.properties.model) {   // 选了本场景的模型，权重文件就用不上了
      input.disabled = true;
      help.textContent = `已选本场景的模型「${node.properties.model}」，用它找图，这一项不用管`;
    }
    card.append(row);
  }
  box.append(card);
}
function toSafeSteps() { try { return toWorkflow().steps.map((s) => ({ sid: s.id, title: CAT[s.tool].title })); } catch { return []; } }
function syncSimple() { if (!$("#simple").hidden) { /* 简单模式输入框在 onchange 时已写回 */ } }

function inspect(node) {
  const def = CAT[node.properties._tool];
  $("#inspect").innerHTML = `<b>${def.title}</b><div>${def.desc.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")}</div>
    <div class="muted">输入：${(def.inputs || []).map((i) => `${i.name}（${i.type}）`).join("、") || "无"}；输出：${(def.outputs || []).map((o) => `${o.name}（${o.type}）`).join("、") || "无"}</div>
    <dl class="param-help">${Object.values(def.params || {}).map((p) => `<dt>${p.label}</dt><dd>${p.help}</dd>`).join("")}</dl>`;
  const wk = Object.keys(def.params || {}).find((k) => def.params[k].kind === "weights");
  if (wk && node.properties[wk]) {  // 显示这个权重能认出哪些类别
    const box = document.createElement("div"); box.className = "muted"; box.textContent = "读取权重类别…";
    $("#inspect").append(box);
    api("/weights/info?path=" + encodeURIComponent(node.properties[wk]))
      .then((i) => { box.innerHTML = `<b>${node.properties[wk].split("/").pop()}</b>（${i.task === "classify" ? "分类" : "检测"}模型）能识别：${i.names.join("、")}`; })
      .catch((e) => { box.textContent = "读不到权重类别：" + e.message; });
  }
}

// ── 运行与状态 ─────────────────────────────────────────────────────────
function summary(tool, o) {
  if (!o) return "";
  const m = o.metrics || {};
  const f = (k) => (m[k] != null ? (+m[k]).toFixed(2) : "?");
  switch (tool) {
    case "catalog.import_folder": return `${o.count} 张`;
    case "labels.import_yolo": return `${o.accepted}/${o.processed} 张，${o.boxes} 框`;
    case "dataset.publish": return `${o.version}：train ${o.counts?.train} / val ${o.counts?.val} / test ${o.counts?.test || 0}`;
    case "model.train": return `${o.version}${o.continued_from ? "（接着 " + o.continued_from + " 训）" : ""}：P ${f("metrics/precision(B)")} R ${f("metrics/recall(B)")} mAP50 ${f("metrics/mAP50(B)")}`;
    case "model.evaluate": return `P ${f("metrics/precision(B)")} R ${f("metrics/recall(B)")} mAP50 ${f("metrics/mAP50(B)")}`;
    case "model.alias": return `${o.alias} → ${o.version}`;
    case "model.predict": { const p = o.predictions || []; return `${p.length} 张，有检出 ${p.filter((x) => (x.detections || []).length).length}`; }
    case "judge.coverage": return `超标 ${o.flagged}/${o.total}`;
    case "curation.find_by_model": return `扫了 ${o.scanned} 张，找到 ${o.hits} 张（累计扫 ${o.scanned_total}/${o.total_files}，还剩 ${o.remaining}）`;
    case "curation.pick_type": return `抽到 ${o.count} 张（还剩 ${o.pool_left} 张可抽）`;
    case "labels.ls_setup": return `Label Studio 项目 #${o.project_id}${o.reused ? "（已存在，复用）" : "（新建）"}`;
    case "labels.push_prelabel": return `送出 ${o.pushed} 张，预框 ${o.prelabel_boxes || 0} 个${o.skipped ? `，${o.skipped} 张之前送过` : ""}`;
    case "labels.pull": return `回收 ${o.processed ?? 0} 张，已复核可用 ${o.accepted ?? 0} 张`;
    case "model.register": return `${o.version}`;
    case "labels.ls_webhook": return o.enabled ? `自动回收已开启（Label Studio 项目 #${o.ls_project}）` : `自动回收已关闭`;
    case "quality.mistakes": return `查了 ${o.checked} 张，可疑 ${o.flagged} 张（漏标 ${o.missing}、多标 ${o.spurious}、框不准 ${o.loc}）`;
    case "inspect.batch": return `共 ${o.total} 张：不合格 ${o.fail}，需注意 ${o.warn}，合格 ${o.pass}${o.skip ? `，无规则 ${o.skip}` : ""}`;
    case "model.export": return `${o.file}（${(o.size / 1048576).toFixed(1)} MB）`;
    case "image.enhance": return `增强 ${o.enhanced} 张（${o.method}）`;
    case "video.frames": return `视频 ${o.duration_sec} 秒 → ${o.frames} 帧（跳过相似 ${o.skipped_similar}）`;
    case "labels.ls_video": return `Label Studio 视频项目 #${o.project_id}`;
    case "catalog.scan": return `发现 ${o.discovered} 个文件，新登记 ${o.indexed}，重复 ${o.duplicates}${o.limited ? "（到上限，没扫完，再跑一次接着扫）" : ""}`;
    case "sorting.candidates": case "sorting.quality": case "catalog.extract": return `${(o.image_ids || []).length} 张`;
    case "sorting.vlm_check": return `通过 ${o.passed} 张，不通过 ${o.failed} 张` + (o.reused ? `（${o.reused} 张用了以前的结果）` : "") +
      (Object.keys(o.reasons || {}).length ? "；" + Object.entries(o.reasons).map(([k, n]) => `${k} ${n}`).join("，") : "") +
      (o.rejected ? `；已标为排除 ${o.rejected} 张` : "") + ((o.errors || []).length ? `；${o.errors.length} 张读图失败` : "");
    case "sorting.duplicates": return `${(o.image_ids || []).length} 张，${(o.candidate_groups || []).length} 组疑似重复`;
    case "sorting.classify": case "curation.reclassify": {
      const c = o.categories || (o.items || []).reduce((a, x) => (a[x.category] = (a[x.category] || 0) + 1, a), {});
      return Object.entries(c).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k, n]) => `${k} ${n}`).join("，") || "完成";
    }
    case "selection.find": return `命中 ${(o.image_ids || []).length} 张，未命中 ${(o.remaining_ids || []).length} 张`;
    case "selection.files": return `${o.execute ? "已复制" : "预演（未复制）"} ${(o.items || []).length} 张 → ${o.destination}`;
    case "workflow.route": return `${(o.items || []).length} 张已分去向`;
    case "curation.confirm": return `确认 ${o.confirmed} 张为 ${o.category}`;
    case "curation.train": return `${o.version}：${(o.classes || []).length} 类，验证准确率 ${o.report?.validation_accuracy != null ? (o.report.validation_accuracy * 100).toFixed(0) + "%" : "?"}`;
    case "ocr.read": case "curation.suggest": return `${(o.items || []).length} 张`;
    default: return "完成";
  }
}
async function poll() {
  const pid = $("#project").value;
  const j = await api(`/projects/${pid}/jobs/${job}`);
  const outs = j.outputs || {};
  const wf = j.spec?.steps || [];
  $("#status").innerHTML = { queued: "… 排队中（前面有任务）", running: "⏳ 运行中", succeeded: "✅ 全部完成", failed: "❌ 失败", cancelled: "⏹ 已取消", paused: "⏸ 已暂停", interrupted: "⚠️ 中断（执行进程被重启或退出）" }[j.status] || j.status;
  if (j.error) $("#status").innerHTML += `<div class="err">${j.error === "Worker lease expired" ? "执行任务的后台进程失联（通常是引擎被重启了）" : j.error}</div>`;
  if (["failed", "interrupted"].includes(j.status)) {  // 续跑：已完成的步骤不重跑；“用权重找图”会接着上次扫到的位置
    const b = document.createElement("button"); b.className = "primary"; b.textContent = "▶ 续跑（从中断处接着跑）";
    b.onclick = async () => {
      try {
        await api(`/projects/${pid}/jobs/${job}/control`, { method: "POST", body: JSON.stringify({ command: "resume" }) });
        $("#run").disabled = true; $("#cancel").disabled = false;
        await poll(); if (!timer) timer = setInterval(() => poll().catch(showErr), 3000);
      } catch (e) { showErr(e); }
    };
    $("#status").append(b);
  }
  const log = [];
  for (const s of wf) {
    const node = nodeBySid(s.id);
    let st = s.id in outs ? "done" : j.current_step === s.id ? (j.status === "failed" ? "failed" : "running") : "queued";
    if (node) { node.status = st; node.summary = st === "done" ? summary(s.tool, outs[s.id]) : ""; }
    const chip = document.querySelector(`.step[data-sid="${s.id}"]`); if (chip) chip.className = "step " + (st === "queued" ? "" : st);
    if (st === "done") {
      let line = `✅ ${CAT[s.tool]?.title || s.tool}：${summary(s.tool, outs[s.id])}`;
      if ((s.tool === "judge.coverage" || s.tool === "curation.find_by_model") && outs[s.id].review_id) line += ` · <a href="/review?project=${pid}&id=${outs[s.id].review_id}" target="_blank">打开复核单</a>`;
      if (outs[s.id]?.url && s.tool.startsWith("labels.")) line += ` · <a href="${lsPublic(outs[s.id].url)}" target="_blank">去 Label Studio 标注</a>`;
      log.push(line);
    } else if (st !== "queued") {
      let line = `${st === "failed" ? "❌" : "⏳"} ${CAT[s.tool]?.title || s.tool}`;
      if (st === "running" && s.tool === "curation.find_by_model") {  // 实时进度
        try {
          const g = await api(`/projects/${pid}/find_progress?job=${job}&step=${s.id}`);
          if (g.scanned != null) {
            const rate = g.seconds ? (g.scanned / g.seconds).toFixed(1) : "?";
            line += `：已扫 ${g.scanned}/${g.todo} 张，找到 ${g.hits} 张（${rate} 张/秒，累计 ${g.scanned_total}/${g.total_files}）`;
            if (node) node.summary = `已扫 ${g.scanned}/${g.todo}，找到 ${g.hits}`;
          } else line += "：加载模型、列文件中…";
        } catch {}
      }
      log.push(line);
    }
  }
  $("#log").innerHTML = log.map((l) => `<div>${l}</div>`).join("");
  canvas.setDirty(true, true);
  if (["succeeded", "failed", "cancelled", "interrupted"].includes(j.status)) { clearInterval(timer); timer = null; $("#run").disabled = false; $("#cancel").disabled = true; }
}
async function save() {
  const pid = $("#project").value, wid = ($("#wf-id").value || "").trim();
  if (!/^[a-z][a-z0-9_-]{0,63}$/.test(wid)) throw new Error("工作流代码只能用小写字母、数字、- 和 _，并以字母开头，如 demo-e2e");
  await api(`/projects/${pid}/workflows/${wid}`, { method: "PUT", body: JSON.stringify(toWorkflow()) });
  remember(pid, wid); await refreshSaved(); $("#saved").value = wid; renderSimple(); return wid;
}
function openSaved(id) {
  const l = $("#saved")._list || [];
  const w = (Array.isArray(l) ? l : l.workflows || []).find((x) => x.id === id);
  if (!w) return false;
  $("#wf-id").value = id; $("#saved").value = id;
  loadSteps(w.definition || w); remember($("#project").value, id);
  if (!$("#nodes").hidden) fitView();
  return true;
}
async function run() {
  const wid = await save();
  const pid = $("#project").value;
  const submit = () => api(`/projects/${pid}/workflows/${wid}/run`, { method: "POST", body: JSON.stringify({ inputs: {} }) });
  let j;
  try { j = await submit(); }
  catch (e) {
    if (!/Start the scenario/i.test(e.message)) throw e;
    // 引擎要求子项目先“启动”才接任务：自动启动后重新提交
    $("#status").textContent = "子项目还没启动，正在自动启动…";
    await api(`/projects/${pid}/activate`, { method: "POST", body: JSON.stringify({ active: true }) });
    j = await submit();
  }
  job = j.id || j.job_id; $("#run").disabled = true; $("#cancel").disabled = false;
  try { localStorage.setItem("flow-job-" + pid, job); } catch {}
  graph._nodes.forEach((n) => { n.status = "queued"; n.summary = ""; });
  $("#log").innerHTML = ""; await poll(); timer = setInterval(() => poll().catch(showErr), 3000);
}
async function refreshSaved() {
  const pid = $("#project").value, sel = $("#saved");
  const list = await api(`/projects/${pid}/workflows`).catch(() => []);
  sel.innerHTML = '<option value="">—</option>';
  for (const w of (Array.isArray(list) ? list : list.workflows || [])) sel.add(new Option(`${w.id}（${w.definition?.name || w.name || ""}）`, w.id));
  sel._list = list;
}
function showErr(e) { $("#status").innerHTML = `<div class="err">⚠️ ${e.message || e}</div>`; }

// ── 启动 ───────────────────────────────────────────────────────────────
async function init() {
  const [nd, w, projects] = await Promise.all([api("/nodes"), api("/weights"), api("/projects")]);
  CAT = nd.nodes; TPL = nd.templates; WEIGHTS = w.weights;
  for (const p of (Array.isArray(projects) ? projects : projects.projects || [])) $("#project").add(new Option(`${p.config?.name || p.id}（${p.id}）`, p.id));
  for (const [k, t] of Object.entries(TPL)) $("#template").add(new Option(t.name, k));
  LiteGraph.clearRegisteredTypes(); registerNodes();
  graph = new LGraph();
  const el = $("#canvas");
  canvas = new LGraphCanvas(el, graph);
  LiteGraph.release_link_on_empty_shows_menu = true;  // 拖线松开在空白处 → 弹出“接下来接什么”
  canvas.showConnectionMenu = suggestNext;
  canvas.background_image = null;
  canvas.render_info = false; canvas.renderInfo = () => {};  // 不显示左下角的 FPS 等调试信息
  const fit = () => { const r = $("#nodes").getBoundingClientRect(); el.width = r.width; el.height = r.height; canvas.resize(r.width, r.height); };
  window.addEventListener("resize", fit);
  document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    $("#simple").hidden = b.dataset.mode !== "simple"; $("#nodes").hidden = b.dataset.mode !== "nodes";
    if (b.dataset.mode === "nodes") { fit(); fitView(); } else renderSimple();
  });
  $("#load-tpl").onclick = () => loadTemplate($("#template").value);
  $("#load-saved").onclick = () => openSaved($("#saved").value);
  $("#saved").onchange = () => openSaved($("#saved").value);  // 下拉选中就直接打开
  $("#project").onchange = async () => { shareProject($("#project").value); await loadDyn($("#project").value); await refreshSaved(); if (!openSaved(lastOf($("#project").value))) { graph.clear(); currentTpl = null; $("#wf-id").value = ""; renderSimple(); } };
  $("#save").onclick = () => save().then((id) => ($("#status").textContent = `💾 已保存为 ${id}`)).catch(showErr);
  $("#run").onclick = () => run().catch(showErr);
  $("#cancel").onclick = () => api(`/projects/${$("#project").value}/jobs/${job}/control`, { method: "POST", body: JSON.stringify({ command: "cancel" }) }).then(() => poll()).catch(showErr);
  graph.start();
  const q = new URLSearchParams(location.search);
  // 默认用工作台当前选中的子项目（同一个标签页里共用 sessionStorage 的 lota-project），网址里指定的优先
  const want = q.get("project") || (() => { try { return sessionStorage.getItem("lota-project") || localStorage.getItem("lota-project") || ""; } catch { return ""; } })();
  if (want && [...$("#project").options].some((o) => o.value === want)) $("#project").value = want;
  shareProject($("#project").value);
  await loadDyn($("#project").value);
  await refreshSaved();
  // 打开顺序：网址里指定的模板 → 上次打开/保存的工作流 → 第一个模板
  if (q.get("template") && TPL[q.get("template")]) { $("#template").value = q.get("template"); loadTemplate(q.get("template")); }
  else if (!openSaved(q.get("workflow") || lastOf($("#project").value))) {
    const first = Object.keys(TPL)[0];
    if (first) { $("#template").value = first; loadTemplate(first); }
  }
  // 恢复上次运行的任务状态（刷新页面后还能看到进度 / 续跑）
  let last = q.get("job") || ""; try { last = last || localStorage.getItem("flow-job-" + $("#project").value) || ""; } catch {}
  if (last) {
    job = last;
    try {
      await poll();
      const j = await api(`/projects/${$("#project").value}/jobs/${job}`);
      if (["queued", "running"].includes(j.status)) { $("#run").disabled = true; $("#cancel").disabled = false; timer = setInterval(() => poll().catch(showErr), 3000); }
    } catch { job = null; }
  }
}
init().catch(showErr);

// ── 拖线推荐：从插口拖出连线、松开在空白处，弹出“接下来接什么”的建议 ──────────────
// 按业务流程排的推荐顺序（同类型插口能接的节点里，这些排最前）
const NEXT = {
  "curation.find_by_model": ["labels.push_prelabel", "inspect.batch", "judge.coverage", "image.enhance", "sorting.quality", "catalog.reject", "selection.files"],
  "curation.pick_type": ["sorting.vlm_check", "labels.push_prelabel", "model.predict", "labels.push"],
  "labels.import_yolo": ["dataset.publish"],
  "labels.pull": ["dataset.publish"],
  "dataset.publish": ["model.train", "model.evaluate", "quality.mistakes"],
  "model.train": ["model.evaluate", "model.alias", "model.predict", "model.export"],
  "model.alias": ["model.predict", "model.export", "curation.find_by_model"],
  "model.register": ["model.alias", "model.evaluate"],
  "model.predict": ["judge.coverage", "workflow.route"],
  "catalog.scan": ["sorting.candidates"],
  "sorting.candidates": ["sorting.vlm_check", "inspect.batch", "sorting.quality", "selection.find", "model.predict", "sorting.classify", "image.enhance"],
  "sorting.quality": ["sorting.vlm_check", "sorting.duplicates", "catalog.reject"],
  "sorting.vlm_check": ["labels.push_prelabel", "sorting.classify", "sorting.duplicates", "catalog.reject", "labels.push"],
  "sorting.duplicates": ["sorting.classify"],
  "sorting.classify": ["curation.confirm", "labels.push_prelabel", "catalog.reject"],
  "selection.find": ["selection.files", "labels.push_prelabel", "judge.coverage"],
  "catalog.extract": ["labels.push_prelabel", "labels.push", "model.predict"],
  "curation.confirm": ["curation.train"],
  "curation.train": ["curation.reclassify"],
  "curation.reclassify": ["curation.confirm", "selection.files"],
  "curation.unconfirm": ["curation.confirm"],
  "catalog.reject": ["sorting.quality"],
  "inspect.batch": ["labels.push_prelabel", "selection.files", "catalog.reject", "labels.push"],
  "video.frames": ["labels.push_prelabel", "labels.push", "image.enhance", "inspect.batch", "sorting.quality"],
  "image.enhance": ["labels.push", "labels.push_prelabel", "inspect.batch"],
  "catalog.import_folder": ["labels.import_yolo", "labels.push_prelabel", "model.predict", "sorting.quality", "image.enhance", "inspect.batch"],
};
const firstSentence = (d) => String(d || "").replace(/\*\*/g, "").split(/[。；]/)[0] + "。";
function suggestNext(opt) {
  document.querySelector(".suggest")?.remove();
  const e = opt.e, fromOut = !!opt.nodeFrom;
  const node = fromOut ? opt.nodeFrom : opt.nodeTo, slot = fromOut ? opt.slotFrom : opt.slotTo;
  const tool = node.properties._tool, type = slot.type;
  // 能接的：拖输出 → 有同类型输入的节点；拖输入 → 有同类型输出的节点
  const ok = Object.entries(CAT).filter(([, d]) => (fromOut ? d.inputs : d.outputs || []).some((s) => s.type === type)).map(([k]) => k);
  const rec = fromOut ? (NEXT[tool] || []).filter((k) => ok.includes(k))
                      : Object.keys(NEXT).filter((k) => NEXT[k].includes(tool) && ok.includes(k));
  const rest = ok.filter((k) => !rec.includes(k)).sort((a, b) => (CAT[a].category + CAT[a].title).localeCompare(CAT[b].category + CAT[b].title));
  const box = document.createElement("div"); box.className = "suggest";
  box.style.left = Math.min(e.clientX, innerWidth - 380) + "px"; box.style.top = Math.min(e.clientY, innerHeight - 420) + "px";
  const tname = { IMAGES: "图片", DATASET: "数据集", MODEL: "模型", PREDICTIONS: "检测结果", METRICS: "评估指标", REVIEW: "复核单", SCAN: "扫描完成", CONFIRMED: "已确认", CLASSIFIER: "分类器" }[type] || type;
  box.innerHTML = `<div class="sg-head">${fromOut ? "「" + slot.name + "」接下来可以接" : "「" + slot.name + "」可以来自"}<span class="muted">（类型：${tname}）</span></div>
    <input class="sg-search" placeholder="搜索节点名…">`;
  const list = document.createElement("div"); list.className = "sg-list"; box.append(list);
  const item = (k, star) => {
    const d = CAT[k], el = document.createElement("div"); el.className = "sg-item";
    el.innerHTML = `<b>${star ? "⭐ " : ""}${d.title}</b> <span class="muted">${d.category}</span><div class="sg-desc">${firstSentence(d.desc)}</div>`;
    el.title = d.desc.replace(/\*\*/g, "");
    el.onclick = () => {
      const n = LiteGraph.createNode(d._type);
      n.pos = fromOut ? [e.canvasX + 20, e.canvasY - 20] : [e.canvasX - n.size[0] - 20, e.canvasY - 20];
      graph.add(n);
      if (fromOut) {
        const oi = node.outputs.indexOf(slot), ii = (d.inputs || []).findIndex((s) => s.type === type);
        if (oi >= 0 && ii >= 0) node.connect(oi, n, ii);
      } else {
        const ii = node.inputs.indexOf(slot), oi = (d.outputs || []).findIndex((s) => s.type === type);
        if (oi >= 0 && ii >= 0) n.connect(oi, node, ii);
      }
      box.remove(); canvas.selectNode(n); inspect(n); canvas.setDirty(true, true);
    };
    return el;
  };
  const draw = (q = "") => {
    list.innerHTML = "";
    const m = (k) => !q || (CAT[k].title + CAT[k].desc + k).toLowerCase().includes(q.toLowerCase());
    const r = rec.filter(m), o = rest.filter(m);
    if (r.length) { const h = document.createElement("div"); h.className = "sg-sec"; h.textContent = "推荐（按常用流程）"; list.append(h, ...r.map((k) => item(k, true))); }
    if (o.length) { const h = document.createElement("div"); h.className = "sg-sec"; h.textContent = "其他能接的"; list.append(h, ...o.map((k) => item(k, false))); }
    if (!r.length && !o.length) list.innerHTML = `<div class="muted" style="padding:8px">没有能接“${tname}”的节点</div>`;
  };
  draw();
  const s = box.querySelector(".sg-search"); s.oninput = () => draw(s.value);
  const close = (ev) => { if (!box.contains(ev.target)) { box.remove(); document.removeEventListener("mousedown", close, true); } };
  setTimeout(() => document.addEventListener("mousedown", close, true), 0);
  box.onkeydown = (ev) => { if (ev.key === "Escape") box.remove(); };
  document.body.append(box); s.focus();
}
