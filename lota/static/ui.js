// LOTA 共享前端工具：顶部导航、带口令的接口调用、弹窗、提示、折线图、提交任务并等待结果
"use strict";
const UI = (() => {
  const TK = "lota-token", PK = "lota-project";
  const store = {
    get: (k) => { try { return localStorage.getItem(k) || sessionStorage.getItem(k) || ""; } catch { return ""; } },
    set: (k, v) => { try { localStorage.setItem(k, v); sessionStorage.setItem(k, v); } catch {} },
  };
  async function rawFetch(url, opt = {}) {
    for (let i = 0; i < 3; i++) {
      const headers = { ...(opt.headers || {}) }, t = store.get(TK);
      if (t) headers.Authorization = "Bearer " + t;
      const r = await fetch(url, { ...opt, headers });
      if (r.status !== 401) return r;
      const n = prompt("请输入 LOTA 引擎的访问口令（服务器 .env 里的 LOTA_API_TOKEN） / Enter the engine access token:");
      if (!n) return r;
      store.set(TK, n.trim());
    }
    return fetch(url, opt);
  }
  async function api(path, opt = {}) {
    const isForm = opt.body instanceof FormData;
    const r = await rawFetch(path.startsWith("/") ? path : "/api/" + path, {
      ...opt, headers: isForm ? (opt.headers || {}) : { "Content-Type": "application/json", ...(opt.headers || {}) },
      body: opt.body && !isForm && typeof opt.body !== "string" ? JSON.stringify(opt.body) : opt.body,
    });
    const text = await r.text();
    let d; try { d = JSON.parse(text); } catch { d = text; }
    if (!r.ok) {
      const det = d && d.detail;
      throw new Error(typeof det === "string" ? det : Array.isArray(det) ? det.map((x) => (x.loc || []).slice(1).join(".") + "：" + x.msg).join("；") : (text || r.statusText));
    }
    return d;
  }
  async function blobUrl(path) { const r = await rawFetch(path); if (!r.ok) throw new Error(r.statusText); return URL.createObjectURL(await r.blob()); }

  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else if (k in el && typeof v !== "string") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat(Infinity)) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return el;
  }

  // ── 顶部导航（所有页面统一）──────────────────────────────────────
  // 和工作台左侧导航同一顺序：开始 | 数据 | 训练与应用 | 系统（null = 分组竖线）
  const LINKS = [
    ["/overview", "总览", "overview"], ["/guide", "使用指南", "guide"], null,
    ["/", "工作台", "home"], ["/types", "照片类型", "types"], ["/datasets", "数据集", "datasets"], null,
    ["/flow", "工作流", "flow"], ["/models", "模型中心", "models"], ["/inspect", "智能质检", "inspect"], ["/streams", "实时监控", "streams"], ["/review", "结果复核", "review"], ["/toolbox", "工具箱", "toolbox"], null,
    ["/settings", "项目设置", "settings"], ["/developers", "开发者 API", "developers"],
  ];
  let projects = [], projectListeners = [];
  function project() { return store.get(PK) || (projects[0] && projects[0].id) || ""; }
  function onProject(cb) { projectListeners.push(cb); }
  async function nav(active, { picker = true } = {}) {
    const sel = h("select", { title: "当前子项目 / Project" });
    const bar = h("nav", { class: "topnav" },
      h("a", { class: "brand", href: "/overview" }, h("span", { class: "mark" }, "◆"), h("span", {}, "LOTA 视觉平台", h("small", {}, "数据 · 标注 · 训练 · 模型 · 推理"))),
      ...LINKS.map((l) => l ? h("a", { class: "nav" + (l[2] === active ? " on" : ""), href: l[0] }, l[1])
        : h("span", { class: "nav-sep", "aria-hidden": "true", style: { width: "1px", height: "18px", background: "rgba(255,255,255,.16)", margin: "0 4px", flex: "none" } })),
      h("span", { class: "spacer" }),
      window.I18N ? I18N.toggle() : null,
      picker ? h("span", { class: "proj-label" }, "子项目") : null, picker ? sel : null);
    document.body.prepend(bar);
    if (!document.querySelector(".toasts")) document.body.append(h("div", { class: "toasts" }));
    if (!picker) return;
    try { projects = await api("/api/projects"); } catch (e) { toast(e.message, true); projects = []; }
    projects = Array.isArray(projects) ? projects : projects.projects || [];
    const q = new URLSearchParams(location.search).get("project");
    if (q) store.set(PK, q);
    const tn = (x) => (window.I18N ? I18N.t(x) : x);   // 名称单独翻，再拼上代号
    for (const p of projects) sel.add(new Option(`${tn(p.config?.name || p.id)}（${p.id}）`, p.id));
    if (!projects.some((p) => p.id === store.get(PK)) && projects[0]) store.set(PK, projects[0].id);
    sel.value = project();
    sel.onchange = () => { store.set(PK, sel.value); projectListeners.forEach((cb) => cb(sel.value)); };
  }
  function projectInfo(id = project()) { return projects.find((p) => p.id === id); }

  function toast(msg, err = false, ms = 3800) {
    const box = document.querySelector(".toasts") || document.body.appendChild(h("div", { class: "toasts" }));
    const t = h("div", { class: "toast" + (err ? " err" : "") }, msg); box.append(t); setTimeout(() => t.remove(), ms);
  }
  function modal(title, body, actions = [], { wide = false } = {}) {
    const back = h("div", { class: "modal-back" });
    const close = () => back.remove();
    const foot = actions.length ? h("footer", {}, ...actions.map((a) => {
      const b = h("button", { class: "btn " + (a.kind || "") }, a.label);
      b.onclick = async () => { if (!a.run) return close(); b.disabled = true; try { if ((await a.run(close)) !== false) close(); } catch (e) { toast(e.message, true); } finally { b.disabled = false; } };
      return b;
    })) : null;
    back.append(h("div", { class: "modal" + (wide ? " wide" : "") },
      h("header", {}, h("h3", {}, title), h("button", { class: "btn ghost sm", onclick: close }, "关闭")),
      h("div", { class: "body" }, body), foot));
    back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
    document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); } });
    document.body.append(back);
    return close;
  }

  // 提交一个工具任务并等待结果（progress 回调显示状态）
  async function runTool(pid, tool, args = {}, { onProgress, timeoutSec = 3600 } = {}) {
    let job;
    try { job = await api(`/api/projects/${pid}/tools/${tool}`, { method: "POST", body: args }); }
    catch (e) {
      if (/Start the scenario/.test(e.message)) { await api(`/api/projects/${pid}/activate`, { method: "POST", body: { active: true } }); job = await api(`/api/projects/${pid}/tools/${tool}`, { method: "POST", body: args }); }
      else throw e;
    }
    const id = job.id || job.job_id, t0 = Date.now();
    while (Date.now() - t0 < timeoutSec * 1000) {
      await new Promise((r) => setTimeout(r, 1500));
      const j = await api(`/api/projects/${pid}/jobs/${id}`);
      onProgress && onProgress(j);
      if (j.status === "succeeded") return j.outputs?.result ?? j.outputs;
      if (["failed", "cancelled", "interrupted"].includes(j.status)) throw new Error(j.error || j.status);
    }
    throw new Error("任务还在运行，可稍后在“任务记录”里查看");
  }

  // 折线图（SVG，无依赖）：series = [{name, color, points:[[x标签, y]]}]，y 范围 0–1
  function lineChart(series, { height = 220, yMax = 1, pct = true } = {}) {
    const W = 760, H = height, L = 40, R = 14, T = 12, B = 34;
    const labels = series[0]?.points.map((p) => p[0]) || [];
    const n = Math.max(labels.length, 1), x = (i) => L + (n === 1 ? (W - L - R) / 2 : i * (W - L - R) / (n - 1)), y = (v) => T + (1 - v / yMax) * (H - T - B);
    const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "chart");
    const add = (tag, a, text) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(a)) e.setAttribute(k, v); if (text != null) e.textContent = text; svg.append(e); return e; };
    for (let g = 0; g <= 4; g++) { const v = yMax * g / 4; add("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "#e6ece9" }); add("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, pct ? Math.round(v * 100) + "%" : v.toFixed(2)); }
    labels.forEach((lb, i) => add("text", { x: x(i), y: H - 12, "text-anchor": "middle" }, String(lb).length > 14 ? String(lb).slice(-14) : lb));
    for (const s of series) {
      const pts = s.points.map((p, i) => [x(i), p[1]]).filter((p) => p[1] != null);
      if (!pts.length) continue;
      add("polyline", { points: pts.map(([a, v]) => `${a},${y(v)}`).join(" "), fill: "none", stroke: s.color, "stroke-width": 2.4, "stroke-linejoin": "round" });
      for (const [a, v] of pts) { const c = add("circle", { cx: a, cy: y(v), r: 4, fill: "#fff", stroke: s.color, "stroke-width": 2 }); const t = document.createElementNS(NS, "title"); t.textContent = `${s.name}：${pct ? (v * 100).toFixed(1) + "%" : v}`; c.append(t); }
    }
    const legend = h("div", { class: "legend" }, ...series.map((s) => h("span", {}, h("i", { style: { background: s.color } }), s.name)));
    return h("div", {}, svg, legend);
  }

  // 在 canvas 上画图片和框（boxes: [{xyxy|xywhn, label, color, dashed}]）
  async function drawBoxes(canvas, src, boxes = [], { natural } = {}) {
    const im = new Image(); im.src = src; await im.decode().catch(() => {});
    canvas.width = im.naturalWidth || 640; canvas.height = im.naturalHeight || 480;
    const g = canvas.getContext("2d"); g.drawImage(im, 0, 0);
    const lw = Math.max(2, canvas.width / 260); g.lineWidth = lw; g.font = `${Math.max(12, canvas.width / 45)}px sans-serif`;
    for (const b of boxes) {
      let [x1, y1, x2, y2] = b.xyxy || [];
      if (b.xywhn) { const [cx, cy, w, hh] = b.xywhn; x1 = (cx - w / 2) * canvas.width; x2 = (cx + w / 2) * canvas.width; y1 = (cy - hh / 2) * canvas.height; y2 = (cy + hh / 2) * canvas.height; }
      else if (natural) { const s = canvas.width / natural[0]; x1 *= s; x2 *= s; y1 *= s; y2 *= s; }
      g.strokeStyle = b.color || "#2f9e78"; g.setLineDash(b.dashed ? [lw * 3, lw * 2] : []);
      if (b.poly && b.poly.length > 2) {   // 分割：画轮廓并半透明填充
        const k = natural ? canvas.width / natural[0] : 1;
        g.beginPath(); b.poly.forEach(([px, py], i) => (i ? g.lineTo(px * k, py * k) : g.moveTo(px * k, py * k))); g.closePath();
        g.fillStyle = (b.color || "#2f9e78") + "55"; g.fill(); g.stroke();
      } else g.strokeRect(x1, y1, x2 - x1, y2 - y1);
      if (b.kpts) {   // 姿态关键点
        const k = natural ? canvas.width / natural[0] : 1;
        g.fillStyle = "#ffd43b";
        for (const [px, py] of b.kpts) if (px || py) { g.beginPath(); g.arc(px * k, py * k, lw * 1.6, 0, Math.PI * 2); g.fill(); g.stroke(); }
      }
      if (b.label && window.I18N) b.label = I18N.t(b.label);
      if (b.label) { g.setLineDash([]); const tw = g.measureText(b.label).width + 8, th = parseInt(g.font) + 6; g.fillStyle = b.color || "#2f9e78"; g.fillRect(x1, Math.max(0, y1 - th), tw, th); g.fillStyle = "#fff"; g.fillText(b.label, x1 + 4, Math.max(th - 6, y1 - 6)); }
    }
  }

  const pct = (v, d = 0) => (v == null || isNaN(v) ? "—" : (v * 100).toFixed(d) + "%");
  const when = (s) => { if (!s) return "—"; const d = new Date(typeof s === "number" ? s * 1000 : s); return isNaN(d) ? String(s) : d.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); };
  const bytes = (n) => n == null ? "—" : n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : (n / 1024).toFixed(0) + " KB";
  return { api, blobUrl, rawFetch, h, nav, project, projectInfo, onProject, toast, modal, runTool, lineChart, drawBoxes, pct, when, bytes, store };
})();
