// 覆盖率判定的复核单：看带框图片、拖阈值即时重算、人工确认（草多/不多/不确定），并算阈值与人工的一致率。
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
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
};
const REGION = { full: 1, lower_half: 0.5, lower_third: 1 / 3 };
const PAGE = 48;
let R = null, page = 1, imgCache = {};

function coverage(it, conf) {  // 和服务器 judge.coverage 同一个算法
  const frac = REGION[R.params.region] || 1, y0 = it.height * (1 - frac);
  let a = 0;
  for (const d of it.detections) {
    if (d.confidence < conf) continue;
    let [x1, y1, x2, y2] = d.xyxy; y1 = Math.max(y1, y0);
    if (y2 > y1 && x2 > x1) a += (x2 - x1) * (y2 - y1);
  }
  return Math.min(1, a / (it.width * it.height * frac));
}
const thr = () => +$("#thr").value, conf = () => +$("#conf").value;
const dec = (id) => R.decisions[id]?.decision || "";

function view() {
  const t = thr(), c = conf();
  R.items.forEach((it) => { it._cov = coverage(it, c); it._flag = it._cov > t; });
  // 一致率：人工明确判了“草多/不多”的图里，阈值判定和人工一致的比例
  const judged = R.items.filter((it) => ["many", "ok"].includes(dec(it.image_id)));
  const agree = judged.filter((it) => (dec(it.image_id) === "many") === it._flag).length;
  const flagged = R.items.filter((it) => it._flag).length;
  const counts = { many: 0, ok: 0, unsure: 0 }; R.items.forEach((it) => { const d = dec(it.image_id); if (d) counts[d]++; });
  // 帮忙找阈值：在人工确认过的图上，试一遍阈值，找一致率最高的
  let best = null;
  if (judged.length >= 5) for (let x = 0.005; x <= 0.3001; x += 0.005) {
    const a = judged.filter((it) => (dec(it.image_id) === "many") === (coverage(it, c) > x)).length;
    if (!best || a > best.a) best = { x, a };
  }
  $("#stats").innerHTML = `共 <b>${R.items.length}</b> 张，按当前阈值 <b>${(t * 100).toFixed(1)}%</b> 判为超标 <b style="color:var(--bad)">${flagged}</b> 张（${(flagged / R.items.length * 100).toFixed(0)}%）。
    人工已确认：草多 ${counts.many}、不多 ${counts.ok}、不确定 ${counts.unsure}。` +
    (judged.length ? ` 当前阈值与人工一致率 <b>${(agree / judged.length * 100).toFixed(0)}%</b>（${agree}/${judged.length}）。` : " 确认几张后，这里会显示阈值和你的判断一致率。") +
    (best ? ` 💡 在你确认过的图上，阈值 <b>${(best.x * 100).toFixed(1)}%</b> 一致率最高（${(best.a / judged.length * 100).toFixed(0)}%）<button id="use-best">用这个阈值</button>` : "");
  const ub = $("#use-best"); if (ub) ub.onclick = () => { $("#thr").value = best.x; setLabels(); view(); };

  let list = R.items.slice();
  const f = $("#filter").value;
  if (f === "flag") list = list.filter((it) => it._flag);
  if (f === "pass") list = list.filter((it) => !it._flag);
  if (f === "undecided") list = list.filter((it) => !dec(it.image_id));
  if (f === "decided") list = list.filter((it) => dec(it.image_id));
  if (f === "disagree") list = list.filter((it) => ["many", "ok"].includes(dec(it.image_id)) && (dec(it.image_id) === "many") !== it._flag);
  list.sort((a, b) => $("#sort").value === "cov" ? b._cov - a._cov : a._cov - b._cov);
  const pages = Math.max(1, Math.ceil(list.length / PAGE)); page = Math.min(Math.max(1, page), pages);
  $("#page").textContent = `第 ${page} / ${pages} 页（${list.length} 张）`;
  const grid = $("#grid"); grid.innerHTML = "";
  for (const it of list.slice((page - 1) * PAGE, page * PAGE)) grid.append(card(it));
}

function draw(cv, it, size) {
  const img = imgCache[it.image_id + size];
  const go = (im) => {
    const s = im.naturalWidth / it.width;
    cv.width = im.naturalWidth; cv.height = im.naturalHeight;
    const g = cv.getContext("2d"); g.drawImage(im, 0, 0);
    const frac = REGION[R.params.region] || 1;
    g.fillStyle = "rgba(255,255,0,.07)"; g.fillRect(0, cv.height * (1 - frac), cv.width, cv.height * frac);  // 统计区域
    g.lineWidth = Math.max(2, cv.width / 220);
    for (const d of it.detections) {
      const [x1, y1, x2, y2] = d.xyxy.map((v) => v * s);
      g.strokeStyle = d.confidence >= conf() ? "#2f9e44" : "rgba(255,200,0,.7)";
      g.strokeRect(x1, y1, x2 - x1, y2 - y1);
    }
  };
  if (img) return go(img);
  const im = new Image(); im.onload = () => { imgCache[it.image_id + size] = im; go(im); };
  authFetch(`/api/projects/${$("#project").value}/images/${it.image_id}/preview?size=${size}`)
    .then((r) => r.ok ? r.blob() : null).then((b) => { if (b) im.src = URL.createObjectURL(b); });
}

function card(it) {
  const el = document.createElement("div"); el.className = "card" + (it._flag ? " flag" : "");
  const cv = document.createElement("canvas"); draw(cv, it, 480);
  cv.onclick = () => { const b = $("#big"); b.style.display = "flex"; draw(b.querySelector("canvas"), it, 1600); };
  const info = document.createElement("div"); info.className = "info";
  info.innerHTML = `<span class="cov ${it._flag ? "flag" : ""}">${(it._cov * 100).toFixed(1)}% ${it._flag ? "超标" : ""}</span>`;
  const btns = document.createElement("span"); btns.className = "btns";
  for (const [k, label] of [["many", "草多"], ["ok", "不多"], ["unsure", "不确定"]]) {
    const b = document.createElement("button"); b.textContent = label;
    b.className = k + (dec(it.image_id) === k ? " on" : "");
    b.onclick = async () => {
      const v = dec(it.image_id) === k ? "" : k;  // 再点一次撤销
      await api(`/projects/${$("#project").value}/reviews/${R.review_id}/decisions`, { method: "POST", body: JSON.stringify({ decisions: { [it.image_id]: v } }) });
      if (v) R.decisions[it.image_id] = { decision: v }; else delete R.decisions[it.image_id];
      view();
    };
    btns.append(b);
  }
  info.append(btns); el.append(cv, info); return el;
}

function setLabels() { $("#thr-v").textContent = (thr() * 100).toFixed(1) + "%"; $("#conf-v").textContent = conf().toFixed(2); }
async function openReview(id) {
  R = await api(`/projects/${$("#project").value}/reviews/${id}`);
  $("#thr").value = R.params.threshold; $("#conf").value = R.params.min_confidence; setLabels(); page = 1; view();
}
async function loadReviews(select) {
  const list = await api(`/projects/${$("#project").value}/reviews`);
  const s = $("#review"); s.innerHTML = "";
  for (const r of list) s.add(new Option(`${r.created} · ${r.label} · 超标 ${r.flagged}/${r.total}`, r.review_id));
  if (select) s.value = select;
  if (s.value) await openReview(s.value); else $("#stats").textContent = "这个子项目还没有复核单：在工作流里运行“覆盖率判定”节点会生成。";
}
async function init() {
  const q = new URLSearchParams(location.search);
  const ps = await api("/projects");
  for (const p of (Array.isArray(ps) ? ps : ps.projects || [])) $("#project").add(new Option(`${p.config?.name || p.id}（${p.id}）`, p.id));
  $("#project").value = q.get("project") || (window.UI && UI.project ? UI.project() : "") || "";
  $("#project").onchange = () => loadReviews();
  $("#review").onchange = () => openReview($("#review").value);
  for (const id of ["#thr", "#conf"]) $(id).oninput = () => { setLabels(); view(); };
  for (const id of ["#filter", "#sort"]) $(id).onchange = () => { page = 1; view(); };
  $("#prev").onclick = () => { page--; view(); scrollTo(0, 0); };
  $("#next").onclick = () => { page++; view(); scrollTo(0, 0); };
  $("#big").onclick = () => ($("#big").style.display = "none");
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#big").style.display = "none"; });
  await loadReviews(q.get("id"));
}
init().catch((e) => ($("#stats").textContent = "⚠️ " + e.message));
