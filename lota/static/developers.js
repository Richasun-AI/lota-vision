// 开发者 API：给 Agent / MCP / 脚本 / 手机 App 的接口说明（示例可直接复制），能力清单实时读取
"use strict";
const { h } = UI;
const BASE = location.origin;

function code(text) {
  const pre = h("pre", { class: "code" }, text);
  const btn = h("button", { class: "btn sm", style: { float: "right", marginTop: "-4px" }, onclick: () => { navigator.clipboard?.writeText(text); UI.toast("已复制"); } }, "复制");
  return h("div", {}, btn, pre);
}
function sec(id, title, desc, ...kids) {
  return h("div", { class: "card", id, style: { marginBottom: "14px" } }, h("h3", {}, title), desc ? h("div", { class: "sub", style: { marginBottom: "8px" } }, desc) : null, ...kids);
}

async function render() {
  const app = document.getElementById("app"), pid = UI.project() || "demo";
  let man = null; try { man = await UI.api("/api/agent/manifest"); } catch (e) { UI.toast(e.message, true); }
  const search = h("input", { class: "input", placeholder: "搜索工具，如 train、找图、export…", style: { maxWidth: "360px" } });
  const list = h("div");
  const drawTools = () => {
    const q = search.value.toLowerCase();
    list.replaceChildren(...(man?.tools || []).filter((t) => !q || (t.id + t.title + t.description).toLowerCase().includes(q)).map((t) =>
      h("details", { class: "adv", style: { margin: "6px 0", background: "#fff" } },
        h("summary", {}, h("code", {}, t.id), "  ", t.title),
        t.description ? h("p", { class: "small" }, t.description) : null,
        Object.keys(t.params || {}).length ? h("table", { class: "table", style: { margin: "6px 0" } },
          h("tr", {}, h("th", {}, "参数"), h("th", {}, "说明"), h("th", {}, "默认")),
          ...Object.entries(t.params).map(([k, p]) => h("tr", {}, h("td", {}, h("code", {}, k), h("div", { class: "tiny muted" }, p.label)), h("td", { class: "small" }, p.help), h("td", { class: "small mono" }, JSON.stringify(p.default ?? null)))))
          : h("pre", { class: "code" }, JSON.stringify(t.schema?.properties || {}, null, 1)))));
  };
  search.oninput = drawTools;
  app.replaceChildren(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "开发者 API"),
      h("p", {}, "平台的每一个按钮背后都是 HTTP 接口：Agent、MCP、脚本、手机 App 都能调用。完整的交互式文档在 ", h("a", { href: "/docs", target: "_blank" }, "/docs（OpenAPI）"), "。"))),
    h("div", { class: "row", style: { marginBottom: "12px" } }, ...[["auth", "鉴权"], ["agent", "Agent 接口"], ["inspect", "流式质检"], ["mcp", "MCP"], ["stream", "接下载器"], ["tools", "全部工具"]].map(([k, t]) =>
      h("a", { class: "badge brand", href: "#" + k }, t))),
    sec("auth", "1. 鉴权 / Authentication", "所有 /api 接口都要带访问口令（服务器引擎目录 .env 里的 LOTA_API_TOKEN）。",
      code(`Authorization: Bearer <LOTA_API_TOKEN>\n\n# PowerShell\n$env:LOTA_API_TOKEN="你的口令"\n# bash\nexport LOTA_API_TOKEN=你的口令`)),
    sec("agent", "2. Agent 接口 / Agent endpoints", "给大模型用：先取能力清单，再运行工具或模板。返回都是 JSON。",
      h("table", { class: "table" },
        h("tr", {}, h("th", {}, "做什么"), h("th", {}, "接口")),
        ...[["能力清单：子项目、模型、工具（含中文参数说明）、模板", "GET /api/agent/manifest"],
          ["运行一个工具（默认等它完成）", "POST /api/agent/run {project, tool, args, wait}"],
          ["运行工作流模板（可覆盖节点参数）", "POST /api/agent/template {project, template, overrides, wait}"],
          ["查任务", "GET /api/agent/jobs/{project}/{job_id}"],
          ["子项目概况（图片、标注、模型成绩、设置）", "GET /api/projects/{project}/hub/summary"],
          ["模型列表 / 试一试 / 下载", "GET /api/projects/{p}/hub/models · POST /api/projects/{p}/models/{v}/try · GET …/files/{name}"],
          ["项目设置（统一配置）", "GET/PUT /api/projects/{project}/settings"]].map(([a, b]) => h("tr", {}, h("td", {}, a), h("td", {}, h("code", {}, b))))),
      code(`curl -H "Authorization: Bearer $LOTA_API_TOKEN" ${BASE}/api/agent/manifest\n\n# 训练一版（等待完成）\ncurl -X POST -H "Authorization: Bearer $LOTA_API_TOKEN" -H "Content-Type: application/json" \\\n  ${BASE}/api/agent/run -d '{"project":"${pid}","tool":"model.train","args":{"dataset_version":"${pid}-r1","model_version":"{dataset}-s","weights":"/data/lota/runtime/weights/yolo26s.pt","epochs":80,"device":"0"}}'\n\n# 跑“主动学习一轮”模板（不等待，返回任务 ID）\ncurl -X POST -H "Authorization: Bearer $LOTA_API_TOKEN" -H "Content-Type: application/json" \\\n  ${BASE}/api/agent/template -d '{"project":"mcb","template":"mcb-loop","overrides":{"find":{"max_hits":20}}}'`)),
    sec("inspect", "3. 流式质检 / Inspect an image", "送一张图，按“智能质检”页配置的规则判定合格/不合格，返回原因、框、（可选）视觉大模型说明；同时记入质检记录。",
      code(`# 方式一：JSON（base64 / 服务器路径 / URL 三选一）\ncurl -X POST -H "Authorization: Bearer $LOTA_API_TOKEN" -H "Content-Type: application/json" \\\n  ${BASE}/api/agent/inspect -d '{"project":"${pid}","photo_type":"site_ground","path":"/data/images/xxx.jpg"}'\n\n# 方式二：上传文件（手机 App、网页常用）\ncurl -H "Authorization: Bearer $LOTA_API_TOKEN" -F project=${pid} -F photo_type=site_ground -F file=@photo.jpg ${BASE}/api/agent/inspect/upload`),
      code(`# Python\nimport base64, json, urllib.request\nbody = {"project": "${pid}", "photo_type": "site_ground",\n        "image_base64": base64.b64encode(open("photo.jpg","rb").read()).decode()}\nreq = urllib.request.Request("${BASE}/api/agent/inspect", json.dumps(body).encode(),\n      {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})\nr = json.load(urllib.request.urlopen(req))\nprint(r["verdict"], [f["message"] for x in r["results"] for f in x["findings"] if f["hit"]])`),
      h("div", { class: "small muted" }, "返回示例：", h("code", {}, `{"verdict":"fail","results":[{"pipeline":"weeds-check","model":"demo-y8m-v1","findings":[{"metric":"coverage","value":0.12,"hit":true,"message":"站内杂草较多，需要清理"}],"detections":[…]}],"ms":180}`))),
    sec("mcp", "4. MCP（Claude Code / Claude Desktop / 其他 Agent）", "零依赖的 MCP 服务器：mcp/lota_mcp.py。工具：list_projects、get_capabilities、project_status、list_models、run_tool、run_template、job_status、inspect_image、list_inspections、set_production。",
      code(`// .mcp.json（Claude Code 项目根目录）或 Claude Desktop 配置\n{\n  "mcpServers": {\n    "lota": {\n      "command": "python",\n      "args": ["/path/to/lota/mcp/lota_mcp.py"],\n      "env": { "LOTA_URL": "${BASE}", "LOTA_API_TOKEN": "你的口令" }\n    }\n  }\n}`),
      h("div", { class: "small" }, "装好后可以直接对 Agent 说：“看看 demo 现在的正式模型成绩”“用 mcb-loop 跑一轮主动学习”“质检这张照片 C:/xx.jpg”。")),
    sec("stream", "5. 接照片下载器（流式找问题）", "下载器把巡检照片存成 downloads/<工单>/<站点>/<图片类型>/<附件>.jpg；下面的脚本盯住这个目录，新照片一落地就推给引擎质检，断点续推。",
      code(`$env:LOTA_API_TOKEN="你的口令"\npython scripts/stream_inspect.py --project demo \`\n  --watch D:/photos/downloads --only-fail`)),
    sec("tools", "6. 全部工具（实时读取）", `共 ${(man?.tools || []).length} 个工具、${Object.keys(man?.templates || {}).length} 个模板。工具 ID 用在 /api/agent/run 的 tool 字段。`,
      h("div", { class: "row", style: { marginBottom: "8px" } }, search), list,
      h("h3", { style: { marginTop: "14px" } }, "模板"),
      h("table", { class: "table" }, h("tr", {}, h("th", {}, "模板 ID"), h("th", {}, "说明"), h("th", {}, "节点")),
        ...Object.entries(man?.templates || {}).map(([k, t]) => h("tr", {}, h("td", {}, h("code", {}, k)), h("td", { class: "small" }, t.name), h("td", { class: "small mono" }, t.nodes.map((n) => n.id).join(" → ")))))));
  drawTools();
}

(async () => { await UI.nav("developers"); render().catch((e) => UI.toast(e.message, true)); })();
