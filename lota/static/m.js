// 拍照即检（手机网页）：选检查点 → 拍照 → 几秒内出结论（合格 / 需注意 / 不合格 + 原因 + 框）→ 不合格当场重拍
"use strict";
const { h } = UI;
const V = { fail: ["不合格", "❌"], warn: ["需注意", "⚠️"], pass: ["合格", "✅"], skip: ["这类照片还没有质检规则", "·"] };
const store = { get: (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } }, set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} } };
let TYPES = [], hist = store.get("m-hist", []);

async function init() {
  // 质检规则里写了“适用照片”的类型 → 可选检查点
  const ov = await UI.api("/api/overview").catch(() => []);
  const seen = new Map();
  for (const p of ov) {
    const d = await UI.api(`/api/projects/${p.id}/pipelines`).catch(() => ({ pipelines: [] }));
    for (const pl of d.pipelines || []) if (pl.enabled !== false && (pl.match || []).length) seen.set(pl.match[0], pl.name || pl.match[0]);
  }
  TYPES = [...seen.entries()];
  render();
}

function render(result) {
  const app = document.getElementById("app");
  const last = store.get("m-type", TYPES[0]?.[0] || "");
  const file = h("input", { type: "file", accept: "image/*", capture: "environment", style: { display: "none" } });
  const order = h("input", { class: "input", placeholder: "工单号（可不填），如 WO2026…", value: store.get("m-order", "") });
  const site = h("input", { class: "input", placeholder: "站点（可不填），如 PH1700300", value: store.get("m-site", "") });
  const types = h("div", { class: "types" }, ...TYPES.map(([k, n]) => h("label", {}, h("input", { type: "radio", name: "t", value: k, checked: k === last }), n)));
  const shot = h("div", { class: "shot", onclick: () => file.click() }, h("b", {}, "📷"), "点这里拍照（或选相册里的照片）");
  file.onchange = () => file.files[0] && check(file.files[0], types.querySelector("input:checked")?.value || "", order.value.trim(), site.value.trim());
  app.replaceChildren(
    h("div", { class: "form", style: { gap: "12px" } },
      h("div", {}, h("div", { class: "small muted", style: { marginBottom: "6px" } }, "1. 拍的是哪个检查点"),
        TYPES.length ? types : h("div", { class: "callout warn" }, "还没有配置质检规则（智能质检页里给规则写上“适用照片”）")),
      h("details", { class: "adv" }, h("summary", {}, "工单 / 站点（可不填，填了会进审核报告）"), h("div", { class: "form", style: { marginTop: "8px", gap: "8px" } }, order, site)),
      h("div", {}, h("div", { class: "small muted", style: { marginBottom: "6px" } }, "2. 拍照"), shot, file),
      h("div", { id: "result" }, result || null),
      hist.length ? h("div", {}, h("div", { class: "small muted", style: { margin: "8px 0 6px" } }, "最近检查"),
        h("div", { class: "hist" }, ...hist.slice(0, 8).map((x) => h("div", {}, h("img", { src: x.thumb }), h("span", { class: "badge " + (x.v === "fail" ? "danger" : x.v === "warn" ? "warn" : x.v === "pass" ? "ok" : "") }, V[x.v]?.[0] || x.v))))) : null));
}

async function shrink(f, max = 1600) {   // 手机照片先压缩再传（快、省流量），质检够用
  const im = new Image(); im.src = URL.createObjectURL(f); await im.decode();
  const k = Math.min(1, max / Math.max(im.naturalWidth, im.naturalHeight));
  const c = document.createElement("canvas"); c.width = Math.round(im.naturalWidth * k); c.height = Math.round(im.naturalHeight * k);
  c.getContext("2d").drawImage(im, 0, 0, c.width, c.height);
  return new Promise((r) => c.toBlob((b) => r({ blob: b, url: c.toDataURL("image/jpeg", 0.5) }), "image/jpeg", 0.88));
}

async function check(f, ptype, order, site) {
  store.set("m-type", ptype); store.set("m-order", order); store.set("m-site", site);
  const box = document.getElementById("result");
  box.replaceChildren(h("div", { class: "verdict skip" }, "AI 审核中…"));
  try {
    const { blob, url } = await shrink(f);
    const fd = new FormData(); fd.append("file", blob, f.name || "photo.jpg"); fd.append("photo_type", ptype);
    fd.append("meta", JSON.stringify({ file: f.name || "phone.jpg", order, site, photo_type: ptype, source: "mobile" }));
    const t0 = Date.now();
    const r = await UI.api("/api/agent/inspect-all/upload", { method: "POST", body: fd });
    const v = V[r.verdict] || [r.verdict, ""];
    const res = (r.projects || []).flatMap((p) => p.results || []);
    const why = res.flatMap((x) => x.findings.filter((q) => q.hit && q.message).map((q) => q.message + (q.metric === "coverage" ? `（覆盖率 ${(q.value * 100).toFixed(1)}%）` : "")));
    const vlm = res.map((x) => x.vlm).filter(Boolean);
    const cv = h("canvas");
    const dets = res.flatMap((x) => x.detections.map((d) => ({ xyxy: d.xyxy, poly: d.polygon, label: `${d.label} ${(d.confidence * 100).toFixed(0)}%` })));
    UI.drawBoxes(cv, URL.createObjectURL(blob), dets, { natural: res[0] ? [res[0].width, res[0].height] : null });
    box.replaceChildren(h("div", { class: "form", style: { gap: "10px" } },
      h("div", { class: "verdict " + r.verdict }, `${v[1]} ${v[0]}`),
      why.length ? h("ul", { style: { margin: 0, paddingLeft: "20px" } }, ...why.map((w) => h("li", {}, w))) : null,
      ...vlm.map((t) => h("div", { class: "callout" }, "🧠 " + t)),
      cv, h("div", { class: "tiny muted" }, `用时 ${((Date.now() - t0) / 1000).toFixed(1)} 秒 · ${res.map((x) => x.model).join("、")}`),
      r.verdict === "fail" ? h("div", { class: "callout warn" }, "不合格：请按原因处理后，再拍一张确认。") : null));
    hist.unshift({ v: r.verdict, thumb: url, at: Date.now() }); hist = hist.slice(0, 12); store.set("m-hist", hist);
  } catch (e) { box.replaceChildren(h("div", { class: "callout warn" }, "检查失败：" + e.message)); }
}

init().catch((e) => { document.getElementById("app").replaceChildren(h("div", { class: "callout warn" }, e.message)); });
