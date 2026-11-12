// 工具箱：图像增强（前后对比）、视频（上传、抽帧、Label Studio 视频标注）、常用入口
"use strict";
const { h } = UI;
let METHODS = {}, file = null, method = "auto";

async function render() {
  const pid = UI.project(), app = document.getElementById("app");
  METHODS = await UI.api("/api/media/methods");
  app.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "工具箱"),
      h("p", {}, "常用的图像与视频工具。所有工具也都能在“工作流”里当节点用、用接口调用。"))),
    h("div", { class: "grid k4", style: { marginBottom: "14px" } },
      link("🖼️", "图像增强", "暗光、雾天、偏色、模糊照片一键变清楚", "#enhance"),
      link("🎬", "视频", "抽帧做图片数据集，或建视频跟踪标注", "#video"),
      link("📱", "导出到手机", "TFLite / NCNN / ONNX", "/models"),
      link("🛡️", "智能质检", "规则 + 模型 + 大模型自动审核", "/inspect")),
    enhanceCard(), videoCard(pid));
  loadVideos();
}
function link(icon, t, d, href) { return h("a", { class: "card hover", href, style: { textDecoration: "none", color: "inherit" } }, h("div", { style: { fontSize: "24px" } }, icon), h("h3", {}, t), h("div", { class: "sub" }, d)); }

// ── 图像增强 ──────────────────────────────────────────────────────────────
function enhanceCard() {
  const before = h("img"), after = h("img", { class: "after" }), bar = h("div", { class: "bar" });
  const cmp = h("div", { class: "compare" }, before, after, bar, h("span", { class: "tag", style: { left: "10px" } }, "原图"), h("span", { class: "tag", style: { right: "10px" } }, "增强后"));
  const setPos = (x) => { const r = cmp.getBoundingClientRect(), p = Math.max(0, Math.min(1, (x - r.left) / r.width)) * 100; after.style.clipPath = `inset(0 0 0 ${p}%)`; bar.style.left = p + "%"; };
  cmp.addEventListener("pointerdown", (e) => { cmp.setPointerCapture(e.pointerId); setPos(e.clientX); cmp.onpointermove = (m) => setPos(m.clientX); });
  cmp.addEventListener("pointerup", () => { cmp.onpointermove = null; });
  const inp = h("input", { type: "file", accept: "image/*" });
  const strength = h("input", { type: "range", min: 0, max: 1, step: 0.05, value: 0.6, style: { flex: 1 } }), sv = h("b", { class: "mono" }, "0.6");
  const info = h("span", { class: "small muted" }, "先选一张照片");
  const run = async () => {
    if (!file) return;
    info.textContent = "处理中…";
    const fd = new FormData(); fd.append("file", file); fd.append("method", method); fd.append("strength", strength.value);
    const r = await UI.rawFetch("/api/media/enhance/preview", { method: "POST", body: fd });
    if (!r.ok) { info.textContent = "失败：" + (await r.text()); return; }
    after.src = URL.createObjectURL(await r.blob());
    info.textContent = `${METHODS[method].name} · 强度 ${strength.value} · ${r.headers.get("X-Elapsed-Ms") || "?"} 毫秒（预览按长边 1600 处理）`;
  };
  inp.onchange = () => { file = inp.files[0]; if (file) { before.src = URL.createObjectURL(file); run(); } };
  strength.oninput = () => { sv.textContent = strength.value; }; strength.onchange = run;
  const seg = h("div", { class: "seg", style: { gridAutoFlow: "row", gridTemplateColumns: "repeat(auto-fill,minmax(150px,1fr))" } },
    ...Object.entries(METHODS).map(([k, m]) => { const l = h("label", {}, h("input", { type: "radio", name: "m", checked: k === method }), h("b", {}, m.name), h("span", {}, m.desc));
      l.onclick = () => { method = k; run(); }; return l; }));
  return h("div", { class: "card", id: "enhance", style: { marginBottom: "14px" } },
    h("div", { class: "row between" }, h("h3", {}, "图像增强 / Image enhancement"), h("span", { class: "small muted" }, "拖动中间的分隔条对比前后")),
    h("div", { class: "grid k2", style: { marginTop: "10px" } },
      h("div", { class: "form" }, h("div", { class: "field" }, h("label", {}, "1. 选一张照片"), inp),
        h("div", { class: "field" }, h("label", {}, "2. 增强方法"), seg),
        h("div", { class: "field" }, h("label", {}, "3. 强度"), h("div", { class: "row" }, strength, sv)), info,
        h("div", { class: "callout" }, "批量处理：在“工作流”里用节点「图像增强（生成增强副本）」，接在“导入图片 / 用权重找图”后面；原图不动，增强后的副本作为新图加进子项目，可用于标注、训练（扩充数据）或质检前预处理。")),
      cmp));
}

// ── 视频 ──────────────────────────────────────────────────────────────────
function videoCard(pid) {
  const inp = h("input", { type: "file", accept: "video/*" }), prog = h("span", { class: "small muted" });
  inp.onchange = async () => {
    const f = inp.files[0]; if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    prog.textContent = `上传中… ${UI.bytes(f.size)}`;
    const xhr = new XMLHttpRequest(); xhr.open("POST", `/api/projects/${pid}/videos`);
    const t = UI.store.get("lota-token"); if (t) xhr.setRequestHeader("Authorization", "Bearer " + t);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) prog.textContent = `上传中 ${Math.round(e.loaded / e.total * 100)}%`; };
    xhr.onload = () => { prog.textContent = xhr.status < 300 ? "上传完成" : "上传失败：" + xhr.responseText; loadVideos(); };
    xhr.send(fd);
  };
  return h("div", { class: "card", id: "video" },
    h("h3", {}, "视频 / Video"), h("div", { class: "sub" }, "两种用法：① 按间隔抽帧 → 变成图片，走原来的标注、训练流程（推荐，最简单）；② 建 Label Studio 视频跟踪项目，在关键帧画框，中间帧自动插值。"),
    h("div", { class: "row", style: { margin: "10px 0" } }, h("b", {}, "上传视频"), inp, prog, h("span", { class: "small muted" }, "支持 mp4 / mov / avi / mkv；也可以把视频放到服务器上引擎允许读的任意目录")),
    h("div", { id: "videos" }));
}
async function loadVideos() {
  const pid = UI.project(), box = document.getElementById("videos"); if (!box) return;
  const list = await UI.api(`/api/projects/${pid}/videos`).catch(() => []);
  box.replaceChildren(list.length ? h("table", { class: "table" }, h("tr", {}, h("th", {}, "视频"), h("th", {}, "大小"), h("th", {}, "上传时间"), h("th", {}, "操作")),
    ...list.map((v) => h("tr", {}, h("td", { class: "mono small" }, v.name), h("td", { class: "small" }, UI.bytes(v.size)), h("td", { class: "small" }, UI.when(v.mtime)),
      h("td", {}, h("div", { class: "row" }, h("button", { class: "btn sm primary", onclick: () => framesDlg(v) }, "抽帧"),
        h("button", { class: "btn sm", onclick: () => lsVideo(v) }, "建视频标注项目"))))))
    : h("div", { class: "empty" }, "还没有上传视频"));
}
function framesDlg(v) {
  const every = h("input", { class: "input", type: "number", value: 1, min: 0.1, step: 0.5 }), max = h("input", { class: "input", type: "number", value: 300, min: 1, max: 1000 });
  const skip = h("input", { type: "checkbox", checked: true });
  UI.modal(`视频抽帧：${v.name}`, h("div", { class: "form" },
    h("div", { class: "grid k2" }, h("div", { class: "field" }, h("label", {}, "每隔几秒抽一帧"), every, h("div", { class: "help" }, "画面变化慢（固定机位）用 2–5 秒；巡检走动拍摄用 0.5–1 秒")),
      h("div", { class: "field" }, h("label", {}, "最多抽几帧"), max)),
    h("label", { class: "row" }, skip, "跳过和上一张几乎一样的帧（推荐）")),
    [{ label: "取消" }, { label: "开始抽帧", kind: "primary", run: async () => {
      UI.toast("抽帧中…");
      const r = await UI.runTool(UI.project(), "video.frames", { video: v.path, every_sec: Number(every.value), max_frames: Number(max.value), skip_similar: skip.checked });
      UI.modal("抽帧完成", h("div", {}, h("p", {}, `视频 ${r.duration_sec} 秒，抽出 ${r.frames} 帧，跳过相似帧 ${r.skipped_similar} 张，已加进子项目（${r.count} 张，按内容去重）。`),
        h("p", {}, "下一步：到 ", h("a", { href: "/" }, "工作台 · 原图与分拣"), " 看图，或在 ", h("a", { href: "/flow" }, "工作流"), " 里接“送 Label Studio”。")));
    } }]);
}
async function lsVideo(v) {
  UI.toast("创建 Label Studio 视频项目…");
  try {
    const r = await UI.runTool(UI.project(), "labels.ls_video", { videos: [v.path] });
    const pub = String(r.url).replace(/^https?:\/\/(127\.0\.0\.1|localhost):8081/, `${location.protocol}//${location.hostname}:8081`);
    UI.modal("视频标注项目已建好", h("div", {}, h("p", {}, `Label Studio 项目 #${r.project_id}，类别：${r.classes.join("、")}。在关键帧画框，中间帧会自动插值。`),
      h("a", { class: "btn primary", href: pub, target: "_blank" }, "去 Label Studio 标注 →")));
  } catch (e) { UI.toast(e.message, true); }
}

(async () => { await UI.nav("toolbox"); UI.onProject(render); render().catch((e) => UI.toast(e.message, true)); })();
