// 巡检审核报告：按工单 / 站点汇总所有子项目的质检结果；可打印（存 PDF）、导出 CSV
"use strict";
const { h } = UI;
const V = { fail: ["不合格", "danger"], warn: ["需注意", "warn"], pass: ["合格", "ok"], skip: ["无适用规则", ""] };

async function listView() {
  const app = document.getElementById("app");
  const orders = await UI.api("/api/inspections/orders");
  app.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "巡检审核报告"),
      h("p", {}, "按工单 / 站点汇总所有子项目的 AI 质检结果。照片来自流式质检（scripts/stream_inspect.py --project all 会带上工单号和站点）或接口推送（meta.order / meta.site）。"))),
    orders.length ? h("table", { class: "table" },
      h("tr", {}, h("th", {}, "工单"), h("th", {}, "站点"), h("th", { class: "num" }, "不合格"), h("th", { class: "num" }, "需注意"), h("th", { class: "num" }, "合格"), h("th", {}, "最近"), h("th", {}, "")),
      ...orders.map((o) => h("tr", {}, h("td", { class: "mono" }, o.order || "—"), h("td", { class: "mono" }, o.site || "—"),
        h("td", { class: "num" }, h("b", { style: { color: o.fail ? "var(--danger)" : "" } }, o.fail)), h("td", { class: "num" }, o.warn), h("td", { class: "num" }, o.pass),
        h("td", { class: "small muted" }, o.at || o.last), h("td", {}, h("a", { class: "btn sm primary", href: `/report?${o.order ? "order=" + encodeURIComponent(o.order) : "site=" + encodeURIComponent(o.site)}` }, "查看报告")))))
      : h("div", { class: "empty" }, "还没有带工单号的质检记录。运行：python scripts/stream_inspect.py --project all --watch <下载目录>"));
}

async function reportView(order, site) {
  const app = document.getElementById("app");
  const r = await UI.api(`/api/inspections/report?order=${encodeURIComponent(order)}&site=${encodeURIComponent(site)}`);
  const v = V[r.overall] || [r.overall, ""];
  const csv = async () => {
    const u = await UI.blobUrl(`/api/inspections/report.csv?order=${encodeURIComponent(order)}&site=${encodeURIComponent(site)}`);
    const a = h("a", { href: u, download: `巡检审核报告-${order || site}.csv` }); document.body.append(a); a.click(); a.remove();
  };
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "巡检审核报告"), h("p", {}, `工单 ${order || (r.orders || []).join("、") || "—"} · 站点 ${site || (r.sites || []).join("、") || "—"} · 生成于 ${new Date().toLocaleString("zh-CN", { hour12: false })}`)),
      h("div", { class: "row noprint" }, h("a", { class: "btn", href: "/report" }, "← 全部工单"), h("button", { class: "btn", onclick: csv }, "导出 CSV"),
        h("button", { class: "btn primary", onclick: () => print() }, "打印 / 存 PDF"))),
    h("div", { class: "verdict-big " + r.overall, style: { marginBottom: "14px" } },
      h("span", { class: "v" }, v[0]), h("span", {}, `共审核 ${r.total} 张照片：不合格 ${r.stats.fail || 0}、需注意 ${r.stats.warn || 0}、合格 ${r.stats.pass || 0}、无适用规则 ${r.stats.skip || 0}`)),
    h("div", { class: "card", style: { marginBottom: "14px" } }, h("h3", {}, "按照片类型"),
      h("table", { class: "table", style: { marginTop: "8px" } },
        h("tr", {}, h("th", {}, "照片类型 / 检查点"), h("th", { class: "num" }, "不合格"), h("th", { class: "num" }, "需注意"), h("th", { class: "num" }, "合格"), h("th", {}, "结论")),
        ...r.by_type.map((b) => { const vv = b.fail ? "fail" : b.warn ? "warn" : b.pass ? "pass" : "skip";
          return h("tr", {}, h("td", {}, b.photo_type), h("td", { class: "num" }, b.fail), h("td", { class: "num" }, b.warn), h("td", { class: "num" }, b.pass),
            h("td", {}, h("span", { class: "badge " + V[vv][1] }, V[vv][0]))); }))),
    h("div", { class: "section-title" }, h("h2", {}, `问题清单（${r.issues.length}）`)),
    r.issues.length ? h("div", { class: "grid", style: { gap: "10px" } }, ...r.issues.map((i) => {
      const cv = h("canvas");
      UI.blobUrl(`/api/projects/${i.project}/inspections/image/${i.sha}`).then((u) => UI.drawBoxes(cv, u, [])).catch(() => {});
      return h("div", { class: "issue" }, cv, h("div", {},
        h("div", { class: "row between" }, h("b", {}, i.photo_type || "未知类型"), h("span", { class: "badge " + V[i.verdict][1] }, V[i.verdict][0])),
        h("div", { class: "small muted" }, `${i.file || ""} · 子项目 ${i.project} · ${i.at}`),
        h("ul", { class: "small", style: { margin: "6px 0", paddingLeft: "18px" } }, ...i.why.map((w) => h("li", {}, w))),
        ...i.vlm.map((t) => h("div", { class: "callout small" }, "🧠 " + t))));
    })) : h("div", { class: "empty" }, "没有问题 🎉"),
    h("p", { class: "small muted", style: { marginTop: "16px" } }, "说明：结论由 AI 模型 + 质检规则自动给出，供人工复核参考；规则可在“智能质检”页调整。"));
}

(async () => {
  await UI.nav("inspect", { picker: false });
  const q = new URLSearchParams(location.search);
  (q.get("order") || q.get("site") ? reportView(q.get("order") || "", q.get("site") || "") : listView()).catch((e) => UI.toast(e.message, true));
})();
