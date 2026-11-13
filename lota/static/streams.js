// 实时监控：海康 / 大华 / 宇视 / 任意 RTSP 摄像头 —— 实时看画面、AI 抽帧检测、报警截图、截图导入图库做训练数据
"use strict";
const { h } = UI;
let BRANDS = {}, CAMS = [], PROJECTS = [], timer = null;
const STATUS = { live: ["ok", "直播中"], connecting: ["info", "连接中"], error: ["danger", "出错"], idle: ["", "未连接"], stopped: ["", "已断开"] };
const VERDICT = { fail: ["danger", "不合格"], warn: ["warn", "需注意"], pass: ["ok", "正常"] };

async function render() {
  const app = document.getElementById("app");
  [BRANDS, CAMS, PROJECTS] = await Promise.all([UI.api("/api/streams/brands"), UI.api("/api/streams"), UI.api("/api/projects").catch(() => [])]);
  const add = h("button", { class: "btn primary", onclick: () => editCamera() }, "＋ 添加摄像头");
  app.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "📹 实时监控"),
      h("p", {}, "接入海康、大华、宇视或任意 RTSP 摄像头 / 录像机：网页实时看画面；开启 AI 后按子项目的模型和质检规则抽帧检测，命中就报警并截图存证，截图可以一键导入图库做训练数据。")), add),
    CAMS.length ? h("div", { class: "cams" }, ...CAMS.map(tile))
      : h("div", { class: "card empty" }, h("p", {}, "还没有摄像头。"), h("p", { class: "small muted" }, "点右上角「添加摄像头」，填 IP、账号密码即可；海康、大华会自动拼好取流地址。"), add.cloneNode(true)),
    h("div", { class: "section-title", style: { marginTop: "22px" } }, "报警截图"),
    eventsCard());
  if (!CAMS.length) app.querySelector(".card.empty .btn").onclick = () => editCamera();
  clearInterval(timer);
  timer = setInterval(refreshStatus, 4000);
}

// ── 摄像头卡片 ────────────────────────────────────────────────────────────
function tile(c) {
  const screen = h("div", { class: "screen", title: "点击放大" }, "点击「看画面」开始播放");
  const badges = h("div", { class: "over" });
  screen.append(badges);
  const st = h("div", { class: "small muted", "data-status": c.id });
  const play = h("button", { class: "btn sm primary" }, "▶ 看画面");
  play.onclick = () => { if (screen.querySelector("img")) stop(screen, play); else start(c, screen, play, 6, 640); };
  screen.onclick = () => bigView(c);
  const card = h("div", { class: "card cam", "data-cam": c.id }, screen,
    h("div", { class: "meta" },
      h("div", { class: "t" }, h("h3", {}, c.name), h("span", { class: "badge" }, BRANDS[c.brand]?.name || c.brand)),
      h("div", { class: "small muted mono", style: { wordBreak: "break-all" } }, c.stream_url || c.url_error || ""),
      c.location ? h("div", { class: "small" }, "📍 " + c.location) : null,
      h("div", { class: "small" }, c.ai.enabled ? `🤖 AI 检测：子项目 ${c.ai.project}，每 ${c.ai.every_sec} 秒一帧` : "🤖 AI 检测：未开启"),
      st,
      h("div", { class: "row" }, play,
        h("button", { class: "btn sm", onclick: () => snapshot(c) }, "📷 截图"),
        h("button", { class: "btn sm", onclick: () => editCamera(c) }, "设置"),
        h("button", { class: "btn sm ghost", onclick: () => removeCamera(c) }, "删除"))));
  paintStatus(card, c.state);
  if (c.ai.enabled) start(c, screen, play, 4, 640);   // 开了 AI 的摄像头本来就连着，直接播
  return card;
}

async function start(c, screen, play, fps, width) {
  try {
    const t = await UI.api(`/api/streams/${c.id}/ticket`, { method: "POST" });
    const img = h("img", { alt: c.name, src: `/live/${c.id}.mjpg?ticket=${encodeURIComponent(t.ticket)}&fps=${fps}&width=${width}` });
    img.onerror = () => { if (!img.dataset.retry) { img.dataset.retry = 1; setTimeout(() => { if (img.isConnected) { img.remove(); start(c, screen, play, fps, width); } }, 3000); } };
    [...screen.childNodes].forEach((n) => { if (n.nodeType === 3) n.remove(); });
    screen.querySelector("img")?.remove();
    screen.prepend(img);
    if (play) play.textContent = "■ 停止";
  } catch (e) { UI.toast(e.message, true); }
}
function stop(screen, play) {
  const img = screen.querySelector("img");
  if (img) { img.src = ""; img.remove(); }
  if (!screen.firstChild || screen.firstChild.nodeType !== 3) screen.prepend("已停止播放");
  if (play) play.textContent = "▶ 看画面";
}

function paintStatus(card, s = {}) {
  const [cls, txt] = STATUS[s.status] || ["", s.status || "未连接"];
  const over = card.querySelector(".over");
  over.replaceChildren(h("span", { class: "badge " + cls }, txt),
    s.fps ? h("span", { class: "badge" }, `${s.fps} fps`) : null,
    s.size ? h("span", { class: "badge" }, `${s.size[0]}×${s.size[1]}`) : null,
    s.verdict && VERDICT[s.verdict] ? h("span", { class: "badge " + VERDICT[s.verdict][0] }, VERDICT[s.verdict][1]) : null);
  const line = card.querySelector("[data-status]");
  line.replaceChildren(...[
    s.error ? h("div", { style: { color: "var(--danger)" } }, "⚠ " + s.error) : null,
    s.ai_error ? h("div", { style: { color: "var(--warn)" } }, "AI：" + s.ai_error) : null,
    s.last_alarm ? h("div", {}, "上次报警：" + UI.when(s.last_alarm)) : null].filter(Boolean));
}
async function refreshStatus() {
  if (document.hidden) return;
  try {
    const list = await UI.api("/api/streams");
    for (const c of list) { const card = document.querySelector(`[data-cam="${c.id}"]`); if (card) paintStatus(card, c.state); }
  } catch {}
}

function bigView(c) {
  const box = h("div", {}, h("div", { class: "empty" }, "连接中…"));
  const screen = h("div", { style: { position: "relative" } });
  box.replaceChildren(screen);
  const close = UI.modal(`${c.name} · 实时画面`, box, [
    { label: "📷 截图", run: () => { snapshot(c); return false; } },
    { label: "关闭" }], { wide: true });
  UI.api(`/api/streams/${c.id}/ticket`, { method: "POST" }).then((t) => {
    screen.append(h("img", { class: "big", src: `/live/${c.id}.mjpg?ticket=${encodeURIComponent(t.ticket)}&fps=12&width=1600` }));
  }).catch((e) => { UI.toast(e.message, true); close(); });
}

async function snapshot(c) {
  try {
    const u = await UI.blobUrl(`/api/streams/${c.id}/snapshot.jpg?width=1920`);
    const a = h("a", { class: "btn primary", href: u, download: `${c.id}-${Date.now()}.jpg` }, "下载截图");
    UI.modal(`${c.name} · 截图`, h("div", {}, h("img", { class: "big", src: u }), h("p", { class: "small muted" }, "画面上的框是最近一次 AI 检测的结果。")), [{ label: "关闭" }], { wide: true });
    a.click();
  } catch (e) { UI.toast(e.message, true); }
}

async function removeCamera(c) {
  if (!confirm(`删除摄像头「${c.name}」？已有的报警截图会保留。`)) return;
  try { await UI.api(`/api/streams/${c.id}`, { method: "DELETE" }); UI.toast("已删除"); render(); } catch (e) { UI.toast(e.message, true); }
}

// ── 添加 / 设置摄像头 ──────────────────────────────────────────────────────
function field(label, el, help) { return h("div", { class: "field" }, h("label", {}, label), el, help ? h("div", { class: "help" }, help) : null); }
function inp(v, attrs = {}) { return h("input", { class: "input", value: v ?? "", ...attrs }); }

async function editCamera(c) {
  const isNew = !c;
  c = c || { id: "", name: "", brand: "hikvision", host: "", port: 554, username: "admin", password: "", channel: 1, substream: true, url: "", location: "",
    ez_app_key: "", ez_app_secret: "", ez_serial: "", ez_code: "", ez_protocol: "flv",
    ai: { enabled: false, project: UI.project(), pipelines: [], model: "production", conf: 0.25, every_sec: 1, cooldown_sec: 60, photo_type: "" } };
  const f = {
    name: inp(c.name, { placeholder: "如：仓库大门" }),
    id: inp(c.id, { placeholder: "如：gate-1（小写字母、数字、- _）", disabled: !isNew }),
    brand: h("select", { class: "select" }, ...Object.entries(BRANDS).map(([k, b]) => h("option", { value: k, selected: k === c.brand }, b.name))),
    host: inp(c.host, { placeholder: "192.168.1.64" }), port: inp(c.port, { type: "number", min: 0, max: 65535 }),
    username: inp(c.username, { autocomplete: "off" }), password: inp(c.password, { type: "password", autocomplete: "new-password", placeholder: isNew ? "" : "不改就留着 ******" }),
    channel: inp(c.channel, { type: "number", min: 1, max: 512 }),
    substream: h("select", { class: "select" }, h("option", { value: "1", selected: c.substream }, "子码流（省带宽，推荐）"), h("option", { value: "0", selected: !c.substream }, "主码流（高清）")),
    url: inp(c.url, { placeholder: "rtsp://192.168.1.10:554/stream1 · rtmp://… · http://…/video.mjpg" }),
    location: inp(c.location, { placeholder: "可不填，如：A 区 3 号楼" }),
    ezKey: inp(c.ez_app_key, { autocomplete: "off", placeholder: "open.ys7.com → 我的应用" }),
    ezSecret: inp(c.ez_app_secret, { type: "password", autocomplete: "new-password", placeholder: isNew ? "" : "不改就留着 ******" }),
    ezSerial: inp(c.ez_serial, { placeholder: "如：J12345678（机身标签上）" }),
    ezCode: inp(c.ez_code, { type: "password", autocomplete: "new-password", placeholder: "可不填；6 位大写字母" }),
    ezChannel: inp(c.channel, { type: "number", min: 1, max: 512 }),
    ezSub: h("select", { class: "select" }, h("option", { value: "1", selected: c.substream }, "流畅（省流量，推荐）"), h("option", { value: "0", selected: !c.substream }, "高清")),
    ezProto: h("select", { class: "select" }, ...[["flv", "FLV（延迟低，推荐）"], ["hls", "HLS（兼容好，延迟高）"], ["rtmp", "RTMP"]].map(([v, t]) => h("option", { value: v, selected: v === (c.ez_protocol || "flv") }, t))),
    aiOn: h("input", { type: "checkbox", checked: c.ai.enabled }),
    project: h("select", { class: "select" }, h("option", { value: "" }, "— 选子项目 —"), ...PROJECTS.map((p) => h("option", { value: p.id, selected: p.id === c.ai.project }, p.name ? `${p.name}（${p.id}）` : p.id))),
    pipes: h("div", { class: "row" }),
    model: inp(c.ai.model, { placeholder: "production" }),
    conf: inp(c.ai.conf, { type: "number", min: 0.01, max: 1, step: 0.01 }),
    every: inp(c.ai.every_sec, { type: "number", min: 0.2, step: 0.1 }),
    cooldown: inp(c.ai.cooldown_sec, { type: "number", min: 0, step: 10 }),
    ptype: inp(c.ai.photo_type, { placeholder: "可不填" }),
  };
  const preview = h("div", { class: "small muted" });
  const deviceBox = h("div", { class: "form" },
    h("div", { class: "three" }, field("IP / 域名", f.host), field("端口", f.port, "RTSP 默认 554"), field("通道号", f.channel, "直连摄像头填 1；录像机填第几路")),
    h("div", { class: "three" }, field("用户名", f.username), field("密码", f.password), field("码流", f.substream)));
  const urlBox = field("取流地址", f.url, "支持 rtsp:// rtsps:// rtmp:// http(s):// srt://；账号密码可以写在地址里，也可以填在下面两格");
  const urlAuth = h("div", { class: "two" }, field("用户名（可不填）", f.username.cloneNode()), field("密码（可不填）", f.password.cloneNode()));
  const ezBox = h("div", { class: "form" },
    h("div", { class: "help" }, "摄像头和服务器不在同一个网络时用：服务器经萤石云拉画面（延迟 2–10 秒，占萤石账号的流量额度）。先用“萤石云视频”App 把设备添加到萤石开放平台（open.ys7.com）对应的账号，再到“我的应用”里拿 AppKey 和 AppSecret。"),
    h("div", { class: "two" }, field("AppKey", f.ezKey), field("AppSecret", f.ezSecret)),
    h("div", { class: "three" }, field("设备序列号", f.ezSerial, "机身标签或 App 设备信息里"), field("通道号", f.ezChannel, "摄像头填 1；录像机填第几路"), field("清晰度", f.ezSub)),
    h("div", { class: "two" }, field("取流协议", f.ezProto), field("设备验证码", f.ezCode, "画面加密时才需要：填了会自动关闭这台设备的“视频加密”（否则拉到的画面解不出来）；也可以在 App 里自己关")));
  const swap = () => {
    const kind = f.brand.value, isUrl = kind === "url", isEz = kind === "ezviz";
    deviceBox.style.display = isUrl || isEz ? "none" : "";
    urlBox.style.display = urlAuth.style.display = isUrl ? "" : "none";
    ezBox.style.display = isEz ? "" : "none";
    if (!isUrl && !isEz && !f.port.value) f.port.value = BRANDS[kind].port;
  };
  f.brand.onchange = swap;
  const [uUser, uPass] = urlAuth.querySelectorAll("input");
  uUser.value = c.username || ""; uPass.value = c.password || "";
  const loadPipes = async () => {
    f.pipes.replaceChildren();
    if (!f.project.value) return;
    try {
      const d = await UI.api(`/api/projects/${f.project.value}/pipelines`);
      const list = (d.pipelines || []).filter((p) => p.enabled !== false);
      if (!list.length) { f.pipes.append(h("span", { class: "small muted" }, "这个子项目还没有质检规则：只用下面的模型画框，不会报警。规则在「智能质检」页配置。")); return; }
      for (const p of list) f.pipes.append(h("label", { class: "row", style: { gap: "4px" } }, h("input", { type: "checkbox", value: p.id, checked: !c.ai.pipelines.length || c.ai.pipelines.includes(p.id) }), p.name || p.id));
    } catch (e) { f.pipes.append(h("span", { class: "small", style: { color: "var(--danger)" } }, e.message)); }
  };
  f.project.onchange = loadPipes;
  const collect = () => {
    const isUrl = f.brand.value === "url", isEz = f.brand.value === "ezviz";
    const chosen = [...f.pipes.querySelectorAll("input:checked")].map((x) => x.value), all = f.pipes.querySelectorAll("input").length;
    return {
      id: f.id.value.trim().toLowerCase(), name: f.name.value.trim() || f.id.value.trim(), brand: f.brand.value,
      host: f.host.value.trim(), port: Number(f.port.value) || 0,
      channel: Number((isEz ? f.ezChannel : f.channel).value) || 1, substream: (isEz ? f.ezSub : f.substream).value === "1",
      ez_app_key: f.ezKey.value.trim(), ez_app_secret: f.ezSecret.value.trim(), ez_serial: f.ezSerial.value.trim().toUpperCase(),
      ez_code: f.ezCode.value.trim(), ez_protocol: f.ezProto.value,
      username: (isUrl ? uUser.value : f.username.value).trim(), password: isUrl ? uPass.value : f.password.value,
      url: f.url.value.trim(), location: f.location.value.trim(),
      ai: { enabled: f.aiOn.checked, project: f.project.value, pipelines: chosen.length === all ? [] : chosen, model: f.model.value.trim() || "production",
        conf: Number(f.conf.value) || 0.25, every_sec: Number(f.every.value) || 1, cooldown_sec: Number(f.cooldown.value) || 0, photo_type: f.ptype.value.trim() },
    };
  };
  const test = h("button", { class: "btn" }, "🔌 测试连接");
  test.onclick = async () => {
    test.disabled = true; preview.replaceChildren(f.brand.value === "ezviz" ? "经萤石云连接中…（最多 30 秒）" : "连接中…（最多 12 秒）");
    try {
      const body = collect(); if (!body.id) body.id = "test";
      const r = await UI.api("/api/streams/test", { method: "POST", body });
      preview.replaceChildren(h("div", { style: { color: "var(--ok)" } }, `✅ 连上了：${r.width}×${r.height}，用时 ${r.ms} 毫秒`),
        h("div", { class: "mono" }, r.stream_url), h("img", { src: r.snapshot, style: { width: "100%", borderRadius: "8px", marginTop: "6px" } }));
    } catch (e) { preview.replaceChildren(h("div", { style: { color: "var(--danger)" } }, "❌ " + e.message)); }
    finally { test.disabled = false; }
  };
  const body = h("div", { class: "form" },
    h("div", { class: "two" }, field("名称", f.name), field("编号", f.id, isNew ? "创建后不能改，报警截图按它归档" : "编号不能改")),
    h("div", { class: "two" }, field("品牌 / 接入方式", f.brand, "海康、大华、宇视按规则自动拼地址（服务器要能连到摄像头）；不在同一个网络的海康摄像头选“萤石云”"), field("位置", f.location)),
    deviceBox, urlBox, urlAuth, ezBox,
    h("div", { class: "row" }, test), preview,
    h("div", { class: "section-title" }, "AI 检测与报警"),
    h("label", { class: "row" }, f.aiOn, h("b", {}, "开启 AI 检测"), h("span", { class: "small muted" }, "开启后服务器一直连着这路摄像头，不看画面也会检测、报警")),
    h("div", { class: "two" }, field("用哪个子项目", f.project, "用它的正式模型和质检规则"), field("照片类型", f.ptype, "可不填；只影响记录里的标注")),
    field("质检规则", f.pipes, "命中规则（不合格 / 需注意）就报警截图"),
    h("div", { class: "two" }, field("没有规则时用的模型", f.model, "版本名或别名，如 production"), field("置信度下限", f.conf, "没有规则时画框用")),
    h("div", { class: "two" }, field("每隔几秒检测一帧", f.every, "越小越及时，也越占显卡；一般 1–2 秒"), field("报警间隔（秒）", f.cooldown, "同一路摄像头两次报警至少隔多久，防止刷屏")));
  swap(); loadPipes();
  UI.modal(isNew ? "添加摄像头" : `设置 · ${c.name}`, body, [
    { label: "取消" },
    { label: isNew ? "添加" : "保存", kind: "primary", run: async () => {
      const b = collect();
      if (!b.id) throw new Error("请填编号");
      await UI.api(isNew ? "/api/streams" : `/api/streams/${c.id}`, { method: isNew ? "POST" : "PUT", body: b });
      UI.toast(isNew ? "已添加" : "已保存"); render();
    } }], { wide: true });
}

// ── 报警截图 ──────────────────────────────────────────────────────────────
function eventsCard() {
  const camSel = h("select", { class: "select", style: { width: "auto" } }, h("option", { value: "" }, "全部摄像头"), ...CAMS.map((c) => h("option", { value: c.id }, c.name)));
  const vSel = h("select", { class: "select", style: { width: "auto" } }, h("option", { value: "" }, "全部"), h("option", { value: "fail" }, "不合格"), h("option", { value: "warn" }, "需注意"));
  const proj = h("select", { class: "select", style: { width: "auto" } }, ...PROJECTS.map((p) => h("option", { value: p.id, selected: p.id === UI.project() }, p.name || p.id)));
  const grid = h("div", { class: "events" }, h("div", { class: "empty" }, "加载中…"));
  const count = h("span", { class: "small muted" });
  const picked = () => [...grid.querySelectorAll("input:checked")].map((x) => JSON.parse(x.value));
  const load = async () => {
    try {
      const list = await UI.api(`/api/streams-events?limit=200&camera=${camSel.value}&verdict=${vSel.value}`);
      count.textContent = `共 ${list.length} 条（最近 200 条）`;
      grid.replaceChildren(...(list.length ? list.map(evCard) : [h("div", { class: "empty" }, "还没有报警。开启 AI 检测、并在「智能质检」页配好规则后，命中的画面会出现在这里。")]));
    } catch (e) { grid.replaceChildren(h("div", { class: "empty" }, e.message)); }
  };
  camSel.onchange = vSel.onchange = load;
  const importBtn = h("button", { class: "btn primary" }, "导入到子项目图库");
  importBtn.onclick = async () => {
    const items = picked();
    if (!items.length) return UI.toast("先勾选报警截图", true);
    importBtn.disabled = true;
    try {
      const r = await UI.api("/api/streams-events/import", { method: "POST", body: { project: proj.value, items } });
      UI.toast(`已复制 ${r.copied} 张，正在登记进图库…`);
      const out = await UI.runTool(proj.value, "catalog.import_folder", { folder: r.folder, recursive: false });
      UI.toast(`已导入 ${out?.count ?? r.copied} 张到「${proj.value}」，可以去工作台送标注了`);
    } catch (e) { UI.toast(e.message, true); } finally { importBtn.disabled = false; }
  };
  const delBtn = h("button", { class: "btn ghost" }, "删除选中");
  delBtn.onclick = async () => {
    const items = picked();
    if (!items.length || !confirm(`删除 ${items.length} 条报警截图？`)) return;
    try { const r = await UI.api("/api/streams-events/delete", { method: "POST", body: { items } }); UI.toast(`已删除 ${r.deleted} 条`); load(); } catch (e) { UI.toast(e.message, true); }
  };
  const all = h("button", { class: "btn ghost sm", onclick: () => { const b = grid.querySelectorAll("input[type=checkbox]"); const on = [...b].some((x) => !x.checked); b.forEach((x) => { x.checked = on; }); } }, "全选 / 取消");
  load();
  return h("div", { class: "card" },
    h("div", { class: "row between", style: { marginBottom: "10px" } },
      h("div", { class: "row" }, camSel, vSel, h("button", { class: "btn sm", onclick: load }, "刷新"), count),
      h("div", { class: "row" }, all, delBtn, h("span", { class: "small muted" }, "导入到"), proj, importBtn)),
    h("p", { class: "small muted", style: { marginTop: 0 } }, "导入的是不带框的原图，进图库后照常送 Label Studio 标注、训练，模型会越来越准。"),
    grid);
}
function evCard(e) {
  const img = h("img", { alt: e.id });
  UI.blobUrl(`/api/streams-events/${e.camera}/${e.id}.jpg`).then((u) => { img.src = u; }).catch(() => {});
  img.onclick = () => UI.modal(`${e.camera_name} · ${e.at}`, h("div", {}, h("img", { class: "big", src: img.src }),
    ...(e.messages || []).map((m) => h("p", {}, "⚠ " + m)), ...(e.vlm || []).map((m) => h("p", { class: "small" }, "🤖 " + m)),
    h("p", { class: "small muted" }, `检出 ${(e.detections || []).length} 个目标 · 子项目 ${e.project}`)), [{ label: "关闭" }], { wide: true });
  const [cls, txt] = VERDICT[e.verdict] || ["", e.verdict];
  return h("div", { class: "ev" }, img, h("input", { type: "checkbox", value: JSON.stringify({ camera: e.camera, id: e.id }) }),
    h("div", { class: "cap" }, h("div", { class: "row between" }, h("b", {}, e.camera_name), h("span", { class: "badge " + cls }, txt)),
      h("div", { class: "muted" }, e.at), (e.messages || []).length ? h("div", {}, e.messages[0]) : null));
}

(async () => { await UI.nav("streams", { picker: false }); render().catch((e) => UI.toast(e.message, true)); })();
