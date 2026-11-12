"use strict";

const $ = (selector) => document.querySelector(selector);
const root = $("#content");
const dialog = $("#dialog");
const state = {
  project: null, projects: [], tools: [], tab: "gallery", generation: 0,
  offset: 0, status: "", checkpoint: "", images: [], selections: new Map(),
  token: (()=>{try{return localStorage.getItem("lota-token")||sessionStorage.getItem("lota-token")||"";}catch{return "";}})(),
};
// 图库每页张数：可选，记在浏览器里（接口一次最多 200 张）
const PER_PAGE_OPTIONS = [36, 60, 100, 200];
let perPage = (() => { try { const n = +localStorage.getItem("lota-per-page"); return PER_PAGE_OPTIONS.includes(n) ? n : 36; } catch { return 36; } })();
const nav = [
  ["gallery", "原图与分拣", "M3 5h14v11H3z M4 14l4-5 3 3 2-2 3 4"],
  ["labels", "标注", "M4 4h8l4 4v9H4z M7 12l2 2 4-4"],
  ["datasets", "数据集", "M3 5h14v4H3z M4 9v7h12V9 M8 12h4"],
  ["models", "训练与模型", "M4 15h2m2 0V9m4 6V5m4 10v-3"],
  ["workflows", "工作流", "M3 4h5v4H3z M12 12h5v4h-5z M5 8v6h7 M12 5h5v4"],
  ["jobs", "任务记录", "M4 4h12v12H4z M7 7h6m-6 3h6m-6 3h6"],
];
// 左侧导航：按使用顺序分组。["tab", key] 是本页内的视图；["link", 地址, 名称, 图标, 标记] 是独立页面
const ICONS = {
  home: "M3 9l7-6 7 6v8H3z M8 17v-5h4v5",
  book: "M4 4h5a2 2 0 0 1 2 2v11a2 2 0 0 0-2-2H4z M16 4h-5 M16 4v11h-5",
  tag: "M3 3h7l7 7-7 7-7-7z M7 7h.01",
  flow: "M3 4h5v4H3z M12 12h5v4h-5z M5 8v6h7 M12 5h5v4",
  trophy: "M6 3h8v5a4 4 0 0 1-8 0z M6 5H3a3 3 0 0 0 3 3 M14 5h3a3 3 0 0 1-3 3 M10 12v3 M7 17h6",
  shield: "M10 2l7 3v5c0 4-3 7-7 8-4-1-7-4-7-8V5z M7 10l2 2 4-4",
  search: "M9 15A6 6 0 1 0 9 3a6 6 0 0 0 0 12z M13.5 13.5L17 17",
  camera: "M3 6h3l1.5-2h5L14 6h3v10H3z M10 14a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  tool: "M12 3a4 4 0 0 0 4 5l-8 8a2 2 0 0 1-3-3l8-8a4 4 0 0 0-1-2z",
  gear: "M10 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z M10 2v2m0 12v2M2 10h2m12 0h2M4.3 4.3l1.4 1.4m8.6 8.6 1.4 1.4M4.3 15.7l1.4-1.4m8.6-8.6 1.4-1.4",
  plug: "M7 2v4m6-4v4 M5 6h10v3a5 5 0 0 1-10 0z M10 14v4",
};
const SIDEBAR = [
  ["开始", [["link", "/overview", "总览", "home"], ["link", "/guide", "使用指南", "book"]]],
  ["数据", [["tab", "gallery"], ["link", "/types", "照片类型", "tag"], ["tab", "labels"], ["link", "/datasets", "数据集", "datasets"]]],
  ["训练与应用", [["link", "/flow", "工作流", "flow", "推荐"], ["link", "/models", "模型中心", "trophy"],
    ["link", "/inspect", "智能质检", "shield"], ["link", "/streams", "实时监控", "camera"], ["link", "/review", "结果复核", "search"], ["link", "/toolbox", "工具箱", "tool"]]],
  ["系统", [["tab", "jobs"], ["link", "/settings", "项目设置", "gear"], ["link", "/developers", "开发者 API", "plug"]]],
];
const LEGACY_TABS = ["datasets", "models", "workflows"];   // 已有新版独立页面，旧视图收进“旧版页面”
const fieldLabels = {
  image_ids: "图片 ID", image_id: "图片 ID", limit: "处理上限", stability_seconds: "文件稳定等待（秒）",
  decision: "复核结论", checkpoint: "检查点", site_id: "站点", note: "备注",
  max_distance: "感知哈希距离", tasks: "标注 JSON", version: "版本名",
  val_fraction: "验证集比例", test_fraction: "测试集比例", seed: "随机种子",
  dataset_version: "数据集版本", model_version: "模型版本", weights: "初始权重路径",
  alias: "发布别名", epochs: "训练轮次", imgsz: "输入尺寸", batch: "批大小", workers: "加载进程数",
  device: "运行设备", selector: "模型版本或别名", split: "评估数据集", confidence: "置信度阈值",
  predictions: "推理结果", min_confidence: "最低置信度", language: "识别语言",
  labels: "检测目标", destination: "目标文件夹", execute: "执行文件分流", hints_path: "历史分类文件", model_dir: "特征模型", batch_size: "批大小", tool: "工具",
};
const statusLabels = {
  accepted: "已确认", rejected: "已排除", pending: "待复核", queued: "待运行",
  running: "运行中", succeeded: "已完成", failed: "失败", paused: "已暂停",
  cancelled: "已取消", interrupted: "已中断",
};
const telemetryClasses = "transformer\nmeter\npole\ncabinet\nmanhole\ncable";
const logisticsClasses = "standard\nfragile\noversized\ndamaged";
const templates = {
  telecom: {id: "telecom-inspection", name: "通信巡检", task: "detect", classes: telemetryClasses, require_site: false},
  logistics: {id: "logistics-sorting", name: "物流分拣", task: "classify", classes: logisticsClasses, require_site: false},
  blank: {id: "", name: "", task: "detect", classes: "", require_site: false},
};

function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key in node && !key.startsWith("aria")) node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) if (child !== null && child !== undefined && child !== false) node.append(child);
  return node;
}

function icon(path) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("aria-hidden", "true");
  const line = document.createElementNS(namespace, "path");
  for (const [key, val] of Object.entries({d: path, fill: "none", stroke: "currentColor", "stroke-width": "1.25", "stroke-linecap": "round", "stroke-linejoin": "round"})) line.setAttribute(key, val);
  svg.append(line);
  return svg;
}

function button(title, action, variant = "", extra = {}) {
  return h("button", {type: "button", class: "btn " + variant, onclick: action, ...extra}, title);
}

function asyncClick(fn) {
  return async (event) => {
    const target = event?.currentTarget;
    if (target && target instanceof HTMLButtonElement) target.disabled = true;
    try { await fn(event); } catch (error) { notifyUser(error.message, true); }
    finally { if (target && target instanceof HTMLButtonElement) target.disabled = false; }
  };
}

function message(error) {
  if (typeof error === "string") return error;
  if (Array.isArray(error)) return error.map((item) => (item.loc || []).join(".") + " " + item.msg).join("；");
  return error ? JSON.stringify(error) : "";
}

async function request(path, method = "GET", body, mode = "json") {
  if(method==="GET" && state.annotationTask && /\/projects\/[^/]+\/(annotations|datasets|models)(\?|$)/.test(path)){
    path+=(path.includes("?")?"&":"?")+"annotation_task="+encodeURIComponent(state.annotationTask);
  }
  const headers = {};
  if (state.token) headers.Authorization = "Bearer " + state.token;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) {
    let error;
    try { error = await response.json(); } catch { error = null; }
    const text = message(error?.detail) || "请求失败 (" + response.status + ")";
    if (response.status === 401) $("#connection-dot").className = "indicator error";
    throw new Error(text);
  }
  return mode === "blob" ? response.blob() : response.status === 204 ? null : response.json();
}

function pathFor(project = state.project) {
  if (!project) throw new Error("请选择子项目场景");
  return "/api/projects/" + encodeURIComponent(project.id);
}

function notifyUser(text, failure = false) {
  const item = h("div", {class: "toast" + (failure ? " error" : "")}, text);
  $("#notifications").append(item);
  setTimeout(() => item.remove(), failure ? 7000 : 3600);
}

function selected(project = state.project) {
  if (!project) return new Set();
  if (!state.selections.has(project.id)) {
    let stored = [];
    try { stored = JSON.parse(sessionStorage.getItem("lota-selected-" + project.id) || "[]"); } catch {}
    state.selections.set(project.id, new Set(stored));
  }
  return state.selections.get(project.id);
}

// 当前子项目：所有页面共用（数据集、模型中心等页面读 localStorage，工作流读 sessionStorage），两处都写
function shareCurrentProject(id) { try { sessionStorage.setItem("lota-project", id); localStorage.setItem("lota-project", id); } catch {} }

function saveSelection(project = state.project) {
  if (project) sessionStorage.setItem("lota-selected-" + project.id, JSON.stringify([...selected(project)]));
}

function empty(text) { return h("div", {class: "empty"}, text); }
function panel(...children) { return h("section", {class: "panel"}, children); }
function code(data) { return h("pre", {class: "code"}, JSON.stringify(data, null, 2)); }
function pill(text, type = "") { return h("span", {class: "pill " + type}, text); }
function statusPill(value) {
  return pill(statusLabels[value] || value || "—",
    ["accepted", "succeeded"].includes(value) ? "good" :
    ["rejected", "failed", "cancelled"].includes(value) ? "bad" :
    ["running", "pending", "interrupted"].includes(value) ? "warn" : "");
}
function page(title, hint, actions) {
  return h("header", {class: "page-head"},
    h("div", {}, h("h1", {}, title), hint ? h("p", {}, hint) : null), actions || null);
}
function field(title, control, hint = "", wide = false) {
  return h("label", {class: "field" + (wide ? " wide" : "") + (control.type === "checkbox" ? " check" : "")},
    control.type === "checkbox" ? [control, h("span", {}, title)] : [h("span", {}, title), control],
    hint ? h("small", {}, hint) : null);
}
function option(value, title) { return h("option", {value}, title); }
function select(values, current, extra = {}) {
  const control = h("select", extra, values.map((entry) => Array.isArray(entry) ? option(...entry) : option(entry, entry)));
  if (current !== undefined) control.value = current;
  return control;
}
function textInput(value = "", extra = {}) { return h("input", {type: "text", value, ...extra}); }
function lines(value = "") { return h("textarea", {value}); }
function splitLines(value) { return value.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean); }

function openDialog(title, content, wide = false) {
  if (dialog._viewerCleanup) dialog._viewerCleanup();  // 打开别的对话框时退出大图查看器
  dialog.classList.toggle("wide", wide);
  dialog.replaceChildren(
    h("header", {class: "dialog-head"}, h("h2", {id: "dialog-title"}, title), button("关闭", () => dialog.close(), "small")),
    h("div", {class: "dialog-body"}, content),
  );
  if (!dialog.open) dialog.showModal();
}

function formContent(children, title, submit) {
  const errorBox = h("p", {class: "form-error", role: "alert"});
  const submitButton = h("button", {type: "submit", class: "btn primary"}, title);
  const form = h("form", {}, children, errorBox, h("div", {class: "actions"}, submitButton));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submitButton.disabled = true;
    errorBox.textContent = "";
    try { await submit(); }
    catch (error) { errorBox.textContent = error.message; }
    finally { submitButton.disabled = false; }
  });
  return form;
}

function renderShell() {
  const picker = $("#project-picker");
  picker.replaceChildren(option("", state.projects.length ? "请选择场景" : "尚无场景"),
    ...state.projects.map((project) => option(project.id, project.config.name)));
  picker.value = state.project?.id || "";
  $("#current-name").textContent = state.project ? state.project.config.name : "场景管理";
  const tabItem = (key, suffix = "") => {
    const [, title, shape] = nav.find((n) => n[0] === key);
    return h("button", {
      type: "button", class: "nav-item", "aria-current": state.tab === key ? "page" : "false",
      disabled: !state.project, onclick: () => navigate(key),
    }, h("span", {class: "nav-icon"}, icon(shape)), title + suffix);
  };
  const linkItem = (href, title, shape, badge) => h("a", {class: "nav-item", href},
    h("span", {class: "nav-icon"}, icon(ICONS[shape] || nav.find((n) => n[0] === shape)[2])), title,
    badge ? h("span", {class: "nav-badge"}, badge) : null);
  const legacyOpen = LEGACY_TABS.includes(state.tab);
  $("#navigation").replaceChildren(
    ...SIDEBAR.map(([group, items]) => h("div", {class: "nav-group"}, h("div", {class: "nav-title"}, group),
      ...items.map((it) => it[0] === "tab" ? tabItem(it[1]) : linkItem(...it.slice(1))))),
    h("details", {class: "nav-group nav-legacy", open: legacyOpen},
      h("summary", {class: "nav-title"}, "旧版页面"),
      ...LEGACY_TABS.map((key) => tabItem(key, "（旧版）"))));
  const project = state.project;
  $("#project-actions").replaceChildren(...(project ? [
    pill(project.active ? "运行中" : "已停用", project.active ? "good" : "warn"),
    button("配置", () => scenarioDialog(project), "small"),
    button(project.active ? "停用场景" : "启动场景", asyncClick(async () => {
      await request(pathFor(project) + "/activate", "POST", {active: !project.active});
      await initialize(project.id);
    }), project.active ? "small" : "small primary"),
  ] : []));
}

async function initialize(preferred) {
  const generation = ++state.generation;
  try {
    const [projects, tools] = await Promise.all([request("/api/projects"), request("/api/tools")]);
    if (generation !== state.generation) return;
    state.projects = projects;
    state.tools = tools;
    const chosen = preferred ?? state.project?.id ?? (()=>{try{return sessionStorage.getItem("lota-project")||localStorage.getItem("lota-project");}catch{return null;}})();
    const previousId=state.project?.id;
    state.project = projects.find((project) => project.id === chosen) || projects[0] || null;
    if(previousId && previousId!==state.project?.id)state.annotationTask="";
    if (state.project) shareCurrentProject(state.project.id);
    $("#connection-dot").className = "indicator ready";
    await navigate(state.tab);
  } catch (error) {
    $("#connection-dot").className = "indicator error";
    renderShell();
    root.replaceChildren(page("连接工作台", error.message), button("连接设置", connectionDialog, "primary"));
  }
}

async function navigate(tab) {
  state.tab = tab;
  const generation = ++state.generation;
  renderShell();
  if (!state.project) {
    root.replaceChildren(page("子项目场景", "从场景模板开始，配置原图目录、标签和工作流。"),
      panel(button("新建场景", () => scenarioDialog(), "primary")));
    return;
  }
  root.replaceChildren(empty("正在加载…"));
  const projects = {gallery: galleryPage, labels: labelsPage, datasets: datasetsPage,
    models: modelsPage, workflows: workflowsPage, jobs: jobsPage};
  try {
    const content = await projects[tab]();
    if (generation === state.generation) {
      disconnectThumbs();
      root.replaceChildren(content);
      if (tab === "gallery") observeThumbs();
    }
  } catch (error) {
    if (generation === state.generation) root.replaceChildren(empty(error.message),
      button("重试", () => navigate(tab)));
  }
}

function connectionDialog() {
  const token = textInput(state.token, {type: "password", autocomplete: "off", placeholder: "使用服务配置的访问令牌"});
  openDialog("连接设置", formContent(field("访问令牌", token, "仅保存在当前浏览器标签页的会话中。"), "保存并连接", async () => {
    state.token = token.value.trim();
    sessionStorage.setItem("lota-token", state.token); try{localStorage.setItem("lota-token", state.token);}catch{}
    dialog.close();
    await initialize();
  }));
}

$("#connection-button").addEventListener("click", connectionDialog);
$("#new-project").addEventListener("click", () => scenarioDialog());
$("#project-picker").addEventListener("change", (event) => {
  const project = state.projects.find((item) => item.id === event.target.value);
  if (!project) return renderShell();
  state.project = project;
  state.offset = 0;
  state.status = "";
  state.checkpoint = "";
  shareCurrentProject(project.id);
  navigate("gallery");
});

function scenarioDialog(project) {
  const initial = project?.config || {};
  const template = select([["telecom", "通信巡检"], ["logistics", "物流分拣"], ["blank", "自定义"]], "telecom");
  const identifier = textInput(project?.id || "", {required: true, pattern: "[a-z0-9][a-z0-9-]{0,62}", readOnly: Boolean(project)});
  const name = textInput(initial.name || "", {required: true, maxLength: 100});
  const task = select([["detect", "目标检测（画框：找出物体在哪）"], ["segment", "实例分割（画轮廓：裂纹、锈蚀面积、病灶）"], ["obb", "旋转框（框 + 角度：俯拍、零件、抓取）"], ["pose", "姿态关键点（人体、机械臂、车辆关键点）"], ["classify", "图像分类（整张图是什么）"]], initial.task || "detect");
  const classes = lines((initial.classes || []).join("\n"));
  classes.required = true;
  const requireSite = h("input", {type: "checkbox", checked: initial.require_site ?? false});
  const interval = h("input", {type: "number", min: 0, max: 86400, step: 1, value: initial.scan_interval_seconds ?? 0, required: true});
  const scanLimit = h("input", {type: "number", min: 1, max: 1000000, step: 1, value: initial.scan_limit ?? 5000, required: true});
  const studio = initial.label_studio || {};
  const studioUrl = textInput(studio.base_url || "", {type: "url", placeholder: "http://127.0.0.1:8080"});
  const studioId = h("input", {type: "number", min: 1, step: 1, value: studio.project_id ?? ""});
  const studioToken = textInput(studio.token_env || "", {placeholder: "LABEL_STUDIO_TOKEN"});
  const studioMedia = textInput(studio.media_base || "", {placeholder: "http://host/media/{image_id}"});
  const studioLocal = textInput(studio.local_files_root || "");
  const sourceRows = [];
  const sourceList = h("div");
  function addSource(source = {}) {
    const row = {
      name: textInput(source.name || "", {placeholder: "来源名称"}),
      path: textInput(source.path || "", {placeholder: "/path/to/originals"}),
      site_pattern: textInput(source.site_pattern || "", {placeholder: "(?P<site>...)"}),
      checkpoint: textInput(source.checkpoint || ""),
      manifest_path: textInput(source.manifest_path || ""),
      exclude: textInput((source.exclude || []).join(",")),
      recursive: h("input", {type: "checkbox", checked: source.recursive ?? true}),
    };
    const item = h("div", {class: "step"}, h("div", {class: "form-grid"},
      field("来源名称", row.name), field("服务器绝对路径", row.path),
      field("站点提取表达式", row.site_pattern), field("默认检查点", row.checkpoint),
      field("下载清单路径", row.manifest_path), field("排除模式（逗号分隔）", row.exclude),
      field("包含子目录", row.recursive),
      button("移除来源", () => { sourceRows.splice(sourceRows.indexOf(row), 1); item.remove(); }, "danger small")));
    sourceRows.push(row);
    sourceList.append(item);
  }
  for (const source of initial.sources || []) addSource(source);
  const routeRows = [];
  const routeList = h("div");
  function addRoute(label = "", destination = "") {
    const row = [textInput(label, {placeholder: "类别代码"}), textInput(destination, {placeholder: "目的地或队列"})];
    const item = h("div", {class: "step"}, h("div", {class: "form-grid"},
      field("类别代码", row[0]), field("分流去向", row[1]),
      button("移除路由", () => { routeRows.splice(routeRows.indexOf(row), 1); item.remove(); }, "danger small")));
    routeRows.push(row);
    routeList.append(item);
  }
  for (const [key, value] of Object.entries(initial.routes || {})) addRoute(key, value);
  function applyTemplate() {
    const data = templates[template.value];
    identifier.value = data.id;
    name.value = data.name;
    task.value = data.task;
    classes.value = data.classes;
    requireSite.checked = data.require_site;
  }
  if (!project) { applyTemplate(); template.addEventListener("change", applyTemplate); }
  const form = formContent([
    h("div", {class: "form-grid"},
      project ? null : field("场景模板", template, "", true),
      field("场景 ID", identifier), field("显示名称", name),
      field("旧版默认任务（兼容）", task), field("发布数据集时要求站点信息", requireSite),
      field("旧版默认标签（兼容）", classes, "检测和分类请在标注任务管理中分别配置。", true),
      field("自动扫描间隔（秒）", interval, "0 表示手动扫描。"),
      field("每次扫描处理上限", scanLimit)),
    h("fieldset", {}, h("legend", {}, "图片来源"), sourceList, button("添加来源", () => addSource(), "small")),
    h("fieldset", {}, h("legend", {}, "Label Studio"), h("div", {class: "form-grid"},
      field("服务 URL", studioUrl), field("标注项目 ID", studioId),
      field("令牌环境变量名称", studioToken),
      field("媒体 URL", studioMedia), field("本地文件根目录", studioLocal, "媒体 URL 或本地文件根目录至少填写一个。"))),
    h("fieldset", {}, h("legend", {}, "类别分流"), routeList, button("添加路由", () => addRoute(), "small")),
  ], "保存场景", async () => {
    const sources = sourceRows.map((row) => ({
      name: row.name.value.trim(), path: row.path.value.trim(), recursive: row.recursive.checked,
      ...Object.fromEntries(["site_pattern", "checkpoint", "manifest_path"]
        .filter((key) => row[key].value.trim()).map((key) => [key, row[key].value.trim()])),
      exclude: splitLines(row.exclude.value),
    })).filter((source) => source.path || source.name);
    const config = {
      name: name.value.trim(), task: task.value, classes: splitLines(classes.value), require_site: requireSite.checked, categories: initial.categories || {}, annotation_tasks: initial.annotation_tasks || {},
      sources, scan_interval_seconds: Number(interval.value), scan_limit: Number(scanLimit.value),
      routes: Object.fromEntries(routeRows.filter(([key]) => key.value.trim())
        .map(([key, value]) => [key.value.trim(), value.value.trim()])),
    };
    if (studioUrl.value.trim()) config.label_studio = {
      base_url: studioUrl.value.trim(), project_id: Number(studioId.value), token_env: studioToken.value.trim(),
      media_base: studioMedia.value.trim() || null, local_files_root: studioLocal.value.trim() || null,
    };
    const id = identifier.value.trim();
    await request("/api/projects/" + encodeURIComponent(id), "PUT", {
      config, ...(project ? {expected_revision: project.revision} : {}),
    });
    dialog.close();
    notifyUser("场景已保存");
    await initialize(id);
  });
  openDialog(project ? "编辑场景" : "新建子项目场景", form);
}

let thumbObserver;
function disconnectThumbs() {
  if (thumbObserver) { thumbObserver.disconnect(); thumbObserver = null; }
}

async function loadPhoto(target) {
  if (target.dataset.started) return;
  target.dataset.started = "true";
  try {
    const blob = await request(target.dataset.path, "GET", undefined, "blob");
    if (!target.isConnected) return;
    const url = URL.createObjectURL(blob);
    const image = h("img", {alt: target.dataset.alt || "图片预览", decoding: "async"});
    image.addEventListener("load", () => URL.revokeObjectURL(url), {once: true});
    image.addEventListener("error", () => {
      URL.revokeObjectURL(url);
      if (target.isConnected) target.replaceChildren(h("span", {}, "无法显示图片"));
    }, {once: true});
    image.src = url;
    target.replaceChildren(image);
  } catch {
    if (target.isConnected) target.replaceChildren(h("span", {}, "预览不可用"));
  }
}

function observeThumbs() {
  const targets = root.querySelectorAll(".photo-media");
  if (!("IntersectionObserver" in window)) { targets.forEach(loadPhoto); return; }
  thumbObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) {
      thumbObserver?.unobserve(entry.target);
      loadPhoto(entry.target);
    }
  }, {rootMargin: "160px"});
  targets.forEach((target) => thumbObserver.observe(target));
}

function fileName(photo) {
  return (photo.source_path || photo.path || photo.image_id).split(/[\\/]/).pop();
}

function previewPath(project, photo) {
  return pathFor(project) + "/images/" + encodeURIComponent(photo.image_id) + "/preview";
}

function updateSelectionUI() {
  const values = selected();
  const count = $("#selection-count");
  if (count) count.textContent = "已选 " + values.size + " 张";
  for (const card of root.querySelectorAll(".photo[data-image-id]")) {
    const marked = values.has(card.dataset.imageId);
    card.classList.toggle("is-selected", marked);
    card.querySelector('input[type="checkbox"]').checked = marked;
  }
  for (const action of root.querySelectorAll("[data-needs-selection]")) {
    action.disabled = !values.size || !state.project?.active;
  }
}

function changeSelection(photo, checked, project = state.project) {
  const values = selected(project);
  if (checked) values.add(photo.image_id); else values.delete(photo.image_id);
  saveSelection(project);
  if (project?.id === state.project?.id) updateSelectionUI();
}

function setVisible(checked) {
  const values = selected();
  for (const photo of state.images) {
    if (checked) values.add(photo.image_id); else values.delete(photo.image_id);
  }
  saveSelection();
  updateSelectionUI();
}

// 选中当前筛选范围里的全部图片（跨页）。后台工具一次最多处理 1000 张，所以最多选 1000 张
const SELECT_ALL_MAX = 1000;
async function selectAllInRange(project = state.project) {
  const r = state.galleryRange || {}, values = selected(project), ids = [];
  for (let offset = 0; ids.length < SELECT_ALL_MAX; offset += 200) {
    const q = new URLSearchParams({limit: "200", offset: String(offset)});
    let rows;
    if (r.usingSorting) { if (r.category) q.set("category", r.category); if (!r.status) q.set("exclude_rejected", "true"); rows = (await request(pathFor(project) + "/sorting?" + q)).items || []; }
    else { q.set("status", !r.status ? "active" : r.status === "all" ? "" : r.status); if (!q.get("status")) q.delete("status"); if (r.checkpoint) q.set("checkpoint", r.checkpoint); rows = await request(pathFor(project) + "/images?" + q); }
    ids.push(...rows.map((x) => x.image_id));
    if (rows.length < 200) break;
  }
  const chosen = ids.slice(0, SELECT_ALL_MAX);
  for (const id of chosen) values.add(id);
  saveSelection(project); updateSelectionUI();
  notifyUser("已选中 " + chosen.length + " 张" + (ids.length > SELECT_ALL_MAX || (r.total || 0) > SELECT_ALL_MAX ? "（筛选范围超过 " + SELECT_ALL_MAX + " 张，只选了前 " + SELECT_ALL_MAX + " 张；处理完再选下一批）" : ""));
}

function clearSelection() {
  selected().clear();
  saveSelection();
  updateSelectionUI();
}

function stat(label, value) {
  return h("div", {class: "metric"}, h("span", {}, label), h("strong", {}, String(value ?? 0)));
}

function flags(photo) {
  const bits = [statusPill(photo.status)];
  if (photo.raw_path) bits.push(pill("已抽取", "good"));
  if ((photo.aliases || []).length > 1) bits.push(pill("同内容 ×" + photo.aliases.length));
  if (photo.site_conflict?.length || photo.checkpoint_conflict?.length) bits.push(pill("来源冲突", "warn"));
  if (!photo.site_id && state.project?.config.require_site) bits.push(pill("缺站点", "warn"));
  if (photo.quality?.flags?.length) bits.push(pill("画质提醒", "warn"));
  return bits;
}

function imageCard(project, photo) {
  const name = fileName(photo);
  const thumbnail = h("button", {type: "button", class: "photo-media",
    "aria-label": "预览 " + name, dataset: {path: previewPath(project, photo), alt: name},
    onclick: () => previewDialog(project, photo),
  }, h("span", {}, "图片预览"));
  const check = h("input", {type: "checkbox", checked: selected(project).has(photo.image_id),
    onchange: (event) => changeSelection(photo, event.target.checked, project)});
  const meta = [photo.site_id, photo.checkpoint, photo.width && photo.height ? photo.width + " × " + photo.height : null].filter(Boolean).join(" · ");
  return h("article", {class: "photo" + (check.checked ? " is-selected" : ""), dataset: {imageId: photo.image_id}}, thumbnail,
    h("div", {class: "photo-body"}, h("strong", {class: "photo-name", title: name}, name),
      h("div", {class: "actions"}, flags(photo)), h("p", {class: "photo-meta", title: meta}, meta),
      h("div", {class: "rail"}, h("label", {class: "photo-select"}, check, "选取"),
        button("复核", () => reviewDialog(project, photo), "small"))));
}

async function queueTool(tool, args, project = state.project, announce = true) {
  const takesTask = !!state.tools.find((t) => t.id === tool)?.schema?.properties?.annotation_task;
  if(state.annotationTask && takesTask && /^(labels|dataset|model)\./.test(tool))args={...args,annotation_task:state.annotationTask};
  const job = await request(pathFor(project) + "/tools/" + encodeURIComponent(tool), "POST", args);
  if (announce) notifyUser("已提交任务 " + String(job.id || "").slice(0, 8));
  return job;
}

async function waitJob(project, job, rounds = 14) {
  if (!job.id) return job;
  for (let i = 0; i < rounds; i++) {
    if (i) await new Promise((resolve) => setTimeout(resolve, 650));
    const current = await request(pathFor(project) + "/jobs/" + encodeURIComponent(job.id));
    if (current.status === "succeeded") return current;
    if (["failed", "interrupted", "cancelled"].includes(current.status)) throw new Error(current.error || statusLabels[current.status]);
  }
  return null;
}

function confirmExtract(project = state.project) {
  const ids = [...selected(project)];
  if (!ids.length) return;
  const node = formContent(h("p", {}, "把选中的 " + ids.length + " 张原图复制到当前场景的原图库。"), "确认抽取", async () => {
    const job = await queueTool("catalog.extract", {image_ids: ids}, project);
    dialog.close();
    const result = await waitJob(project, job);
    if (result && project.id === state.project?.id && state.tab === "gallery") navigate("gallery");
  });
  openDialog("确认抽取样本", node);
}

async function galleryPage() {
  const project = state.project;
  const query = new URLSearchParams({limit: String(perPage), offset: String(state.offset)});
  // 复核状态：空 = 默认不显示已排除的；all = 全部（含已排除）
  if (state.status && state.status !== "all") query.set("status", state.status);
  else if (!state.status) query.set("status", "active");
  if (state.checkpoint) query.set("checkpoint", state.checkpoint);
  const sortQuery=new URLSearchParams({limit:String(perPage),offset:String(state.offset)});
  if(state.category)sortQuery.set("category",state.category);
  if(!state.status)sortQuery.set("exclude_rejected","true");
  const [stats, originalImages, sorting] = await Promise.all([request(pathFor(project) + "/stats"), request(pathFor(project) + "/images?" + query),request(pathFor(project)+"/sorting?"+sortQuery)]);
  // 选了某个自动分类才按分拣结果列；默认列全部原图（没有分拣结果的图也要看得到）
  const usingSorting=!!state.category&&(!state.status||state.status==="all")&&!state.checkpoint;
  const images=usingSorting?sorting.items:originalImages;
  // 当前筛选范围一共多少张（按检查点筛选时算不出总数，只能逐页翻）
  let total=null;
  if(usingSorting)total=state.category?(sorting.categories[state.category]||0):Object.values(sorting.categories).reduce((a,b)=>a+b,0);
  else if(!state.checkpoint)total=state.status==="all"?stats.total:!state.status?stats.total-((stats.review||{}).rejected||0):state.status==="extracted"?stats.extracted:(stats.review||{})[state.status]??null;
  state.galleryRange={usingSorting,category:state.category||"",status:state.status||"",checkpoint:state.checkpoint||"",total};
  if (project !== state.project) return document.createDocumentFragment();
  state.images = images;
  const status = select([["", "未排除的（默认）"], ["all", "全部（含已排除）"], ["pending", "待复核"], ["accepted", "已确认"],
    ["rejected", "已排除"], ["extracted", "已抽取"]], state.status, {class: "filter", "aria-label": "复核状态"});
  const checkpoint = textInput(state.checkpoint, {class: "filter", placeholder: "输入检查点", "aria-label": "检查点筛选"});
  const category=select([["","全部自动分类"],...Object.entries(sorting.categories).map(([key,count])=>[key,categoryLabel(key,project.config)+" ("+count+")"])],state.category||"");
  category.onchange=()=>{state.category=category.value;state.offset=0;navigate("gallery");};
  const filter = h("form", {class: "gallery-filter"}, category, status, checkpoint,
    h("button", {type: "submit", class: "btn"}, "筛选"));
  filter.addEventListener("submit", (event) => {
    event.preventDefault(); state.status = status.value; state.checkpoint = checkpoint.value.trim(); state.offset = 0; navigate("gallery");
  });
  const hasSelection = selected(project).size > 0 && project.active;
  const selection = h("section", {class: "panel tight selection-bar"}, h("div", {class: "rail"},
    h("div", {class: "actions"}, h("strong", {id: "selection-count"}, "已选 " + selected(project).size + " 张"),
      button("选中本页", () => setVisible(true), "small", {disabled: !images.length}),
      button("取消本页", () => setVisible(false), "small", {disabled: !images.length}),
      button("选中全部筛选结果" + (total != null ? "（" + Math.min(total, SELECT_ALL_MAX) + " 张）" : ""), asyncClick(() => selectAllInRange(project)), "small", {disabled: !images.length, title: "把当前筛选范围里的图全部勾上（跨页），一次最多 " + SELECT_ALL_MAX + " 张"}),
      button("清空", clearSelection, "small")),
    h("div", {class: "actions"},
      button("批量复核所选", () => batchReviewDialog(project), "small", {disabled: !hasSelection, dataset: {needsSelection: "true"}}),
      button("提交标注", () => toolDialog("labels.push", {image_ids: [...selected(project)]}), "small", {
        disabled: !hasSelection, dataset: {needsSelection: "true"}}),
      button("抽取所选", () => confirmExtract(project), "small primary", {disabled: !hasSelection, dataset: {needsSelection: "true"}}))));
  const pageNo = Math.floor(state.offset / perPage) + 1, pages = total == null ? null : Math.max(1, Math.ceil(total / perPage));
  const go = (n) => { n = Math.max(1, pages ? Math.min(pages, n) : n); state.offset = (n - 1) * perPage; navigate("gallery"); };
  const hasNext = pages ? pageNo < pages : images.length >= perPage;
  const pager = () => {
    const jump = h("input", {type: "number", min: 1, max: pages || undefined, value: pageNo, class: "page-jump", "aria-label": "页码"});
    jump.onkeydown = (e) => { if (e.key === "Enter") go(parseInt(jump.value, 10) || 1); };
    const size = select(PER_PAGE_OPTIONS.map((n) => [String(n), "每页 " + n + " 张"]), String(perPage), {"aria-label": "每页张数"});
    size.onchange = () => { const first = state.offset; perPage = +size.value; try { localStorage.setItem("lota-per-page", size.value); } catch {}
      state.offset = Math.floor(first / perPage) * perPage; navigate("gallery"); };   // 换每页张数后，仍停在当前看到的第一张所在的页
    return h("div", {class: "panel tight rail pagination"}, h("span", {class: "small muted"},
      images.length ? "显示第 " + (state.offset + 1) + "–" + (state.offset + images.length) + " 张" + (total != null ? "，共 " + total + " 张" : "") : "没有结果"),
      h("div", {class: "actions"}, size,
        button("首页", () => go(1), "small", {disabled: pageNo <= 1}),
        button("上一页", () => go(pageNo - 1), "small", {disabled: pageNo <= 1}),
        jump, pages ? h("span", {class: "small"}, "/ " + pages) : "",
        button("跳转", () => go(parseInt(jump.value, 10) || 1), "small"),
        button("下一页", () => go(pageNo + 1), "small", {disabled: !hasNext}),
        button("末页", () => go(pages), "small", {disabled: !pages || pageNo >= pages})));
  };
  return h("div", {},
    page("原图与分拣", "按站点和检查点复核样本，选中后抽取到当前场景。", h("div", {class: "actions"},
      button("刷新", () => navigate("gallery")), button("扫描原图", () => toolDialog("catalog.scan"), "primary", {disabled: !project.active}))),
    h("div", {class: "metrics"}, stat("内容去重后", stats.total), stat("待复核", stats.review?.pending), stat("已排除（默认不显示）", stats.review?.rejected),
      stat("已抽取", stats.extracted), stat("来源路径", Object.values(stats.sources || {}).reduce((sum, x) => sum + x, 0))),
    panel(h("strong", {}, "选图步骤"),
      h("p", {}, "先运行自动分拣，再按类型复核并抽取图片。"),
      h("p", {class:"small muted"}, "点击编辑工作流，可添加检测指定目标和目录分流节点。")),
    panel(button("\u7c7b\u522b\u7f16\u8f91\u5668",()=>categoryEditor()),button("\u4ece\u7167\u7247\u7c7b\u578b\u62bd\u56fe",()=>toolDialog("curation.pick_type",{only_confirmed:true}),"",{title:"\u6309\u201c\u7167\u7247\u7c7b\u578b\u201d\u9875\u7684\u5f52\u7c7b\u7ed3\u679c\u628a\u67d0\u7c7b\u7167\u7247\u590d\u5236\u8fdb\u672c\u5b50\u9879\u76ee\uff0c\u7c7b\u522b\u4e00\u8d77\u5e26\u8fc7\u6765\uff0c\u4e0d\u7528\u91cd\u65b0\u5206\u62e3"}),button("按照片类型补类别",()=>toolDialog("curation.backfill_types",{dry_run:true}),"",{title:"给本子项目已有的图按“照片类型”页的结果补上类别"}),button("\u786e\u8ba4\u6240\u9009\u56fe\u7247\u7c7b\u522b",()=>toolDialog("curation.confirm",{image_ids:[...selected(project)],category:state.category||""})),
 button("\u8bad\u7ec3\u7167\u7247\u5206\u7c7b\u5668",()=>toolDialog("curation.train")),
 button("\u91cd\u65b0\u5f52\u7c7b\u6240\u9009\u56fe\u7247",()=>toolDialog("curation.reclassify",{image_ids:[...selected(project)]})),
 button("\u79fb\u9664\u6240\u9009\u56fe\u7247\u7684\u7c7b\u522b",()=>cleanupDialog(project,"unconfirm")),
 button("\u6392\u9664\u6240\u9009\uff08\u53ef\u6062\u590d\uff09",()=>cleanupDialog(project,"reject")),
 button("\u6062\u590d\u6240\u9009\u4e3a\u5f85\u590d\u6838",()=>cleanupDialog(project,"restore")),
 button("\u5220\u9664\u6240\u9009\u56fe\u7247",()=>cleanupDialog(project,"delete"),"danger"),filter), selection, images.length ? pager() : "",
    images.length ? h("div", {class: "gallery"}, images.map((photo) => imageCard(project, photo))) :
      empty(project.config.sources.length ? "当前筛选范围没有图片。可扫描原图或清除筛选。" : "在场景配置中添加图片来源，保存后扫描原图。"),
    pager());
}

// 大图查看器：先秒开 1800px 预览，后台换成原图；滚轮缩放、拖动平移、双击 适应/100%；
// ← → 切换，X 排除，Delete 删除；排除/删除后自动跳到下一张
function previewDialog(project, photo) {
  if (dialog._viewerCleanup) dialog._viewerCleanup();  // 从“近重复”里再打开时，先清掉上一个查看器
  let list = (state.images || []).slice();
  let idx = list.findIndex((x) => x.image_id === photo.image_id);
  if (idx < 0) { list = [photo]; idx = 0; }
  let changed = false, view = {s: 1, x: 0, y: 0, w: 1, h: 1}, token = 0;
  const img = h("img", {class: "pv-img", alt: "", draggable: false});
  const stage = h("div", {class: "pv-stage"}, img);
  const info = h("span", {class: "small"}), zoomText = h("span", {class: "small muted"});
  const flagsBox = h("div", {class: "actions"}), nearResult = h("div"), detailBox = h("div");
  const pick = h("input", {type: "checkbox"});
  const withSource = h("input", {type: "checkbox"});
  const apply = () => { img.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.s})`; zoomText.textContent = Math.round(view.s * 100) + "%"; };
  const fit = () => {
    const W = stage.clientWidth, H = stage.clientHeight;
    view.s = Math.min(W / view.w, H / view.h); view.x = (W - view.w * view.s) / 2; view.y = (H - view.h * view.s) / 2; apply();
  };
  const actual = (cx = stage.clientWidth / 2, cy = stage.clientHeight / 2) => zoomAt(1 / view.s, cx, cy);
  function zoomAt(f, cx, cy) {
    const ns = Math.min(8, Math.max(0.03, view.s * f)); f = ns / view.s;
    view.x = cx - (cx - view.x) * f; view.y = cy - (cy - view.y) * f; view.s = ns; apply();
  }
  stage.addEventListener("wheel", (e) => { e.preventDefault(); const r = stage.getBoundingClientRect(); zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top); }, {passive: false});
  stage.addEventListener("pointerdown", (e) => {
    stage.setPointerCapture(e.pointerId); const sx = e.clientX, sy = e.clientY, ox = view.x, oy = view.y; stage.classList.add("dragging");
    stage.onpointermove = (m) => { view.x = ox + m.clientX - sx; view.y = oy + m.clientY - sy; apply(); };
    stage.onpointerup = () => { stage.onpointermove = null; stage.classList.remove("dragging"); };
  });
  stage.addEventListener("dblclick", (e) => { const r = stage.getBoundingClientRect(); Math.abs(view.s - 1) < 0.01 ? fit() : actual(e.clientX - r.left, e.clientY - r.top); });

  async function show() {
    const p = list[idx], my = ++token;
    dialog.querySelector("#dialog-title").textContent = `${fileName(p)}（${idx + 1} / ${list.length}）`;
    pick.checked = selected(project).has(p.image_id);
    flagsBox.replaceChildren(...[].concat(flags(p)), h("span", {class: "small muted"}, [p.site_id, p.checkpoint].filter(Boolean).join(" · ")));
    nearResult.replaceChildren();
    detailBox.replaceChildren(h("details", {}, h("summary", {}, "来源与画质记录"), code({source: p.source_path, size: [p.width, p.height],
      quality: p.quality, aliases: p.aliases, note: p.review_note})));
    info.textContent = "加载预览…"; img.style.opacity = ".35";
    try {  // 1) 快速预览
      const b = await request(previewPath(project, p) + "?size=1800", "GET", undefined, "blob");
      if (my !== token) return;
      await setSrc(b, true);
      info.textContent = `预览 · 正在加载原图…`;
    } catch { info.textContent = "预览不可用"; }
    try {  // 2) 原图
      const b = await request(pathFor(project) + "/images/" + encodeURIComponent(p.image_id) + "/original", "GET", undefined, "blob");
      if (my !== token) return;
      await setSrc(b, false);
      info.textContent = `原图 ${view.w} × ${view.h} · ${(b.size / 1048576).toFixed(1)} MB`;
    } catch { if (my === token) info.textContent += "（原图加载失败，显示的是预览）"; }
  }
  function setSrc(blob, first) {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob), probe = new Image();
      probe.onload = () => {
        const ow = view.w;
        img.src = url; img.style.opacity = "1";
        view.w = probe.naturalWidth; view.h = probe.naturalHeight;
        if (first) fit(); else { view.s *= ow / view.w; apply(); }  // 换原图时保持当前视野
        setTimeout(() => URL.revokeObjectURL(url), 2000); resolve();
      };
      probe.onerror = () => { URL.revokeObjectURL(url); resolve(); };
      probe.src = url;
    });
  }
  const go = (d) => { if (!list.length) return; idx = (idx + d + list.length) % list.length; show(); };
  const after = () => { if (!list.length) { dialog.close(); return; } if (idx >= list.length) idx = 0; show(); };
  let busy = false;
  async function run(tool, args, mutates = true) {
    if (busy) throw Error("上一个操作还没完成");
    busy = true;
    try {
      const job = await queueTool(tool, args, project, false);
      const done = await waitJob(project, job, 40);
      if (!done) throw Error("任务还在排队/运行，稍后到“任务记录”查看");
      if (mutates) changed = true;
      return done.outputs?.result || {};
    } finally { busy = false; }
  }
  const exclude = asyncClick(async () => {
    const p = list[idx];
    await run("catalog.reject", {image_ids: [p.image_id], decision: "rejected"});
    p.status = "rejected"; notifyUser("已排除：" + fileName(p)); go(1);
  });
  const restore = asyncClick(async () => {
    const p = list[idx];
    await run("catalog.reject", {image_ids: [p.image_id], decision: "pending"});
    p.status = "pending"; notifyUser("已恢复为待复核"); show();
  });
  const remove = asyncClick(async () => {
    const p = list[idx];
    if (!confirm(`删除这张照片？\n${fileName(p)}\n\n` + (withSource.checked ? "⚠️ 服务器上的原图文件也会被删掉，不可恢复！" : "（只从本场景删除，原图文件保留）"))) return;
    const r = await run("catalog.delete", {image_ids: [p.image_id], delete_source: withSource.checked});
    selected(project).delete(p.image_id); saveSelection();
    list.splice(idx, 1); notifyUser("已删除" + (r.deleted_sources?.length ? `，连同 ${r.deleted_sources.length} 个原图文件` : "")); after();
  });
  pick.onchange = () => { changeSelection(list[idx], pick.checked, project); };
  const keys = (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) && e.target.type !== "checkbox") return;
    if (e.key === "ArrowRight") { e.preventDefault(); go(1); } else if (e.key === "ArrowLeft") { e.preventDefault(); go(-1); }
    else if (e.key === "x" || e.key === "X") exclude(); else if (e.key === "Delete") remove();
    else if (e.key === "f" || e.key === "F") fit(); else if (e.key === "1") actual();
  };
  openDialog("", h("div", {class: "pv"},
    h("div", {class: "rail"}, info, zoomText),
    stage,
    h("div", {class: "pv-bar"},
      h("div", {class: "actions"},
        button("‹ 上一张", () => go(-1), "small"), button("下一张 ›", () => go(1), "small"),
        button("适应窗口 (F)", fit, "small"), button("100% (1)", () => actual(), "small"),
        h("label", {class: "photo-select"}, pick, "选取")),
      h("div", {class: "actions"},
        button("复核", () => reviewDialog(project, list[idx]), "small"),
        button("查找近重复", asyncClick(async () => {
          const r = await run("catalog.near", {image_id: list[idx].image_id}, false);
          const m = r.items || [];
          nearResult.replaceChildren(h("div", {class: "actions"}, m.length ? m.map((it) => button(fileName(it) + " · 距离 " + it.distance, () => previewDialog(project, it), "small"))
            : h("span", {class: "small muted"}, "没有近重复候选")));
        }), "small", {disabled: !project.active}),
        button("恢复待复核", restore, "small"),
        button("排除这张 (X)", exclude, "small"),
        h("label", {class: "photo-select", title: "勾选后删除时连服务器上的原图文件一起删（不可恢复）"}, withSource, "连原图文件"),
        button("删除这张 (Del)", remove, "small danger"))),
    flagsBox, nearResult, detailBox,
    h("p", {class: "small muted"}, "滚轮缩放（以鼠标位置为中心）· 按住拖动 · 双击切换 适应/100% · ← → 上一张/下一张 · X 排除并跳到下一张 · Delete 删除")), true);
  dialog.classList.add("viewer");
  const cleanup = () => {
    dialog.classList.remove("viewer"); dialog.removeEventListener("keydown", keys);
    dialog.removeEventListener("close", onClose); dialog._viewerCleanup = null;
    if (changed && state.tab === "gallery") navigate("gallery");
  };
  const onClose = () => cleanup();
  dialog._viewerCleanup = cleanup;
  dialog.addEventListener("keydown", keys);
  dialog.addEventListener("close", onClose);
  requestAnimationFrame(show);
}

function reviewDialog(project, photo) {
  const decision = select([["pending", "待复核"], ["accepted", "已确认"], ["rejected", "已排除"]], photo.status || "pending");
  const checkpoint = textInput(photo.checkpoint || "");
  const site = textInput(photo.site_id || "");
  const note = lines(photo.review_note || "");
  const form = formContent(h("div", {class: "form-grid"}, field("复核结论", decision), field("站点", site),
    field("检查点", checkpoint), field("备注", note, "", true)), "提交复核", async () => {
    const job = await queueTool("catalog.review", {
      image_id: photo.image_id, decision: decision.value, checkpoint: checkpoint.value.trim() || null,
      site_id: site.value.trim() || null, note: note.value.trim() || null,
    }, project);
    dialog.close();
    const result = await waitJob(project, job);
    if (result && project.id === state.project?.id && state.tab === "gallery") navigate("gallery");
  });
  openDialog("复核 " + fileName(photo), form);
}

// 每个参数的中文说明（键：工具.参数 或 参数）；没写的就不显示
const FIELD_HELP = {
  "image_ids": "在图库里勾选的图片，这里只显示张数",
  "curation.confirm.category": "把所选照片人工确认为这个类别；分类器只从人工确认过的照片学习",
  "curation.train.version": "给这次训练出的分类器起个名字，已自动填好（日期时间），一般不用改",
  "curation.train.model_dir": "把照片变成特征向量的视觉模型（SigLIP2），服务器上已有，选默认即可",
  "curation.reclassify.version": "用哪个已训练好的分类器来归类，默认最新的",
  "curation.reclassify.min_confidence": "把握不到这个分数的照片记为“未知”，留给人工；0.7 是稳妥值，调低归得多但错得也多",
  "sorting.classify.hints_path": "可选：以前的分类结果文件，留空即可",
  "sorting.classify.model_dir": "可选：留空就用当前生效的照片分类器",
  annotation_task: "同一场景有多套标注任务时选用哪一套；只有一套时不显示", limit: "最多处理多少张", seed: "随机种子：相同种子每次抽到同样的图，换数字就换一批",
  confidence: "模型把握不到这个分数的框不要；0.25 适合预标注（多给框让人删），0.5 适合正式判定",
  device: "0 = 用显卡（快），cpu = 用处理器（慢）", epochs: "训练轮数，60 左右；数据少时加 patience 提前停",
  imgsz: "训练/推理时把图缩放到的边长，640 是常用值", batch: "每批几张，显存 8G 用 8", workers: "读图进程数，2 即可",
  val_fraction: "拿多少比例的图做验证（0.15 = 15%）", test_fraction: "拿多少比例的图做最终测试，可以是 0",
  dataset_version: "已发布的数据集版本名", model_version: "给新模型起名，如 demo-y8m-v2", selector: "模型版本名，或 production 这样的别名",
  weights: "初始权重 .pt 文件（在权重目录里）", alias: "发布别名，通常用 production", split: "在哪个子集上评估：val 验证集 / test 测试集",
};
const helpFor = (id, key) => FIELD_HELP[id + "." + key] || FIELD_HELP[key] || "";
const stamp = () => { const d = new Date(), z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${z(d.getMonth() + 1)}${z(d.getDate())}-${z(d.getHours())}${z(d.getMinutes())}`; };

async function toolDialog(id, preset = {}) {
  const tool = state.tools.find(x => x.id === id);
  const project = state.project, config = project.config;
  const controls = {}, notes = [];
  // 工具特有的选项
  let extra = {};
  if (/^labels\.(push|pull)$/.test(id) && !config.label_studio) {
    openDialog(tool.title, h("div", {},
      h("p", {}, "当前场景「" + config.name + "」还没连接 Label Studio，所以没法提交/回收标注。"),
      h("p", {class: "small muted"}, "点下面的按钮会自动在 Label Studio（8081）里新建一个项目（标签就是本场景的类别：" + config.classes.join("、") +
        "），挂好服务器图片目录，并把连接存进场景配置。需要服务器引擎目录的 .env 里已经填了 LABEL_STUDIO_TOKEN。"),
      h("div", {class: "actions"}, button("一键连接 Label Studio", asyncClick(async () => {
        const job = await queueTool("labels.ls_setup", {}, project);
        const done = await waitJob(project, job, 40);
        if (!done) { notifyUser("还在连接中，稍后到“任务记录”查看"); return; }
        dialog.close(); await initialize(project.id);
        notifyUser("已连接 Label Studio 项目 #" + (done.outputs?.result?.project_id ?? ""));
        toolDialog(id, preset);
      }), "primary"))));
    return;
  }
  if (id === "curation.confirm") {
    const enabled = Object.keys(config.categories || {}).filter(k => config.categories[k].enabled);
    // 本场景的类别在前，44 个标准照片类型在后（选了标准类型，提交时自动加进场景）
    const std = await fetch("/static/standard_categories.json").then(r => r.json()).catch(() => ({}));
    const options = [...enabled.map(k => [k, categoryLabel(k)]),
      ...Object.entries(std).filter(([k]) => !enabled.includes(k)).map(([k, c]) => [k, c.name + " / " + c.name_en + "（标准类型）"])];
    if (!enabled.length) notes.push(h("p", {class: "small muted"}, "本场景还没有自己的照片类别，下面列的是标准照片类型；选一个提交时会自动加进本场景。也可以用“类别编辑器”自定义。"));
    extra.category = select(options, preset.category && options.some(o => o[0] === preset.category) ? preset.category : options[0]?.[0]);
  }
  if (id === "curation.train" || id === "curation.reclassify") {
    const info = await request(pathFor(project) + "/classifiers");
    const total = Object.values(info.confirmed).reduce((a, b) => a + b, 0);
    notes.push(h("p", {class: "small muted"}, "人工已确认 " + total + " 张：" +
      (Object.entries(info.confirmed).map(([k, n]) => categoryLabel(k) + " " + n).join("，") || "无") +
      (id === "curation.train" ? "。训练至少要 2 个类别、每类最好 ≥5 张且来自不同站点。" : "")));
    if (id === "curation.train" && (total < 4 || Object.keys(info.confirmed).length < 2)) {
      openDialog(tool.title, h("div", {}, ...notes,
        h("p", {class: "error"}, "现在还不能训练：已确认的照片不够。"),
        h("ol", {}, h("li", {}, "在图库勾选几张同一类型的照片（如都是“大门”）"),
          h("li", {}, "点“确认所选图片类别”，选对应类别"),
          h("li", {}, "再对另一个类型重复一次；至少 2 个类别、每类 5 张以上，再来训练"))));
      return;
    }
    if (id === "curation.train") {
      const fm = (await request("/api/feature_models")).models;
      extra.version = textInput("cls-" + stamp());
      extra.model_dir = select(fm.length ? fm.map(m => [m.path, m.name + "（" + m.type + "）"]) : [["", "（服务器上没找到 SigLIP 模型）"]], fm[0]?.path || "");
    } else {
      if (!info.classifiers.length) {
        openDialog(tool.title, h("div", {}, ...notes, h("p", {}, "还没有训练好的照片分类器。先点“训练照片分类器”，训练完再来归类。")));
        return;
      }
      extra.version = select(info.classifiers.map(c => [c.version, c.version + " · " + c.classes.length + " 类" +
        (c.accuracy != null ? " · 验证准确率 " + (c.accuracy * 100).toFixed(0) + "%" : "")]), info.classifiers[0].version);
    }
  }
  const tasks = Object.keys(config.annotation_tasks || {});
  // 和新版工作流共用同一套参数定义：中文名、说明、下拉选项、推荐默认值（/api/nodes）
  const catalog = await nodeCatalog();
  const node = catalog.nodes[id] || {};
  if (node.desc) notes.unshift(h("p", {class: "small muted"}, node.desc.replace(/\*\*/g, "")));
  const fields = Object.entries(tool.schema.properties || {}).filter(([key]) => key !== "annotation_task" || tasks.length).map(([key, spec]) => {
    const p = (node.params || {})[key];
    let value = preset[key] ?? p?.default ?? spec.default ?? (key === "image_ids" ? [...selected()] : "");
    if (Array.isArray(value) && key !== "image_ids" && p && !p.json) value = value.join(",");
    let input;
    if (key === "image_ids") {
      input = h("input", {type: "text", readOnly: true, value: value.length + " 张"});
      input._value = value;
      if (!value.length) notes.push(h("p", {class: "error"}, "还没有勾选图片：先在图库里勾选要处理的照片。"));
      if (value.length > SELECT_ALL_MAX) notes.push(h("p", {class: "error"}, "已勾选 " + value.length + " 张，一次最多处理 " + SELECT_ALL_MAX + " 张：请先“清空”再分批选。"));
    } else if (extra[key]) input = extra[key];
    else if (key === "annotation_task") input = select(Object.keys(config.annotation_tasks || {}).map(x => [x, x]), value);
    else if (p?.kind === "combo") input = select(p.values.map(v => [v, v === "" ? "（不填）" : v]), value);
    else if (p?.kind === "weights") input = select(catalog.weights.map(w => [w, w.split("/").pop()]), value || catalog.weights[0]);
    else if (p?.kind === "model") input = select(catalog.models.length ? catalog.models : [["production", "production（还没有模型）"]], value || "production");
    else if (p?.kind === "phototype") input = select([["", "（不限）"], ...catalog.phototypes], value || "");
    else if (p?.kind === "toggle" || spec.type === "boolean") input = h("input", {type: "checkbox", checked: !!value});
    else if (p?.kind === "number") input = h("input", {type: "number", value: value ?? "", min: p.min, max: p.max, step: p.step || "any"});
    else input = spec.enum ? select(spec.enum.map(x => [x, x]), value) :
      (spec.type === "array" || spec.type === "object") && !p ? lines(JSON.stringify(value || (spec.type === "array" ? [] : {}), null, 2)) :
      textInput(value ?? "");
    controls[key] = {input, spec, p};
    return field(p?.label || fieldLabels[key] || key, input, p?.help || helpFor(id, key));
  });
  openDialog(tool.title, formContent(h("div", {}, ...notes, h("div", {class: "form-grid"}, ...fields)), "提交任务", async () => {
    const args = {};
    for (const [key, {input, spec, p}] of Object.entries(controls)) {
      if (input._value) args[key] = input._value;
      else if (input.type === "checkbox") args[key] = input.checked;
      else if (input.value !== "") args[key] = (p?.array || (spec.type === "array" && p)) ? input.value.replace(/，/g, ",").split(",").map(x => x.trim()).filter(Boolean) :
        spec.type === "array" || spec.type === "object" || p?.json ? JSON.parse(input.value) :
        spec.type === "integer" || spec.type === "number" || p?.kind === "number" ? Number(input.value) : input.value;
    }
    // 不跳到“任务记录”：留在当前页，后台跟踪，完成后提示并刷新当前页
    const tab = state.tab;
    const job = await queueTool(id, args, project, false);
    dialog.close();
    toastWithAction("已提交「" + tool.title + "」，完成后会提示", "查看进度", () => navigate("jobs"));
    trackJob(project, job, tool.title, (current) => {
      if (id === "curation.confirm") { selected(project).clear(); saveSelection(project); }   // 确认完清空勾选，方便选下一批
      if (id === "curation.backfill_types") backfillReport(project, current?.outputs?.result, args);
      if (project.id === state.project?.id && state.tab === tab) navigate(tab);
    });
  }), true);
}

// 添加检测类别：只在末尾追加，类别代码用小写字母、数字、下划线（Label Studio 里写成 “grounding bar” 也能自动对上 grounding_bar）
function addClassDialog(project = state.project) {
  const code = textInput("", {placeholder: "例如 grounding_bar"});
  openDialog("添加类别", formContent(h("div", {},
    h("p", {}, "现有类别：" + (project.config.classes || []).join("、")),
    field("新类别代码", code, "小写字母开头，只用小写字母、数字、下划线；Label Studio 里的标签写成 “grounding bar” 也能自动对应到 grounding_bar"),
    h("p", {class: "small muted"}, "加了以后：①以前的标注没检查过新类别，要在“标注系统”里“送回复核”；②当前 Label Studio 项目里没有这个标签的话，页面会提示新建一个。")),
    "添加", async () => {
      const v = code.value.trim().toLowerCase().replace(/[\s-]+/g, "_");
      if (!/^[a-z0-9][a-z0-9_.-]{0,63}$/.test(v)) throw Error("类别代码只能用小写字母、数字、下划线、点、短横线");
      const config = structuredClone(project.config);
      if (config.classes.includes(v)) throw Error("已经有这个类别：" + v);
      config.classes = [...config.classes, v];
      await request(pathFor(project), "PUT", {config, expected_revision: project.revision});
      dialog.close(); await initialize(project.id); notifyUser("已添加类别 " + v);
    }), true);
}

// “按照片类型补类别”的结果：试运行时先给统计，确认后再真正写入
function backfillReport(project, r, args) {
  if (!r) return;
  const rows = Object.entries(r.by_type || {}).map(([name, n]) => h("tr", {}, h("td", {}, name), h("td", {}, String(n.confirmed)), h("td", {}, String(n.predicted))));
  const skipped = Object.entries(r.skipped || {}).map(([k, n]) => k + " " + n).join("，");
  openDialog("按照片类型补类别" + (r.dry_run ? "（试运行）" : ""), h("div", {},
    h("p", {}, "子项目共 " + r.images + " 张，对上“照片类型”页的 " + r.matched + " 张：" +
      (r.dry_run ? "可补为已确认 " : "已补为已确认 ") + r.confirmed + " 张，" + (r.dry_run ? "可归类待复核 " : "已归类待复核 ") + r.predicted + " 张。"),
    rows.length ? h("table", {class: "table"}, h("thead", {}, h("tr", {}, h("th", {}, "类型"), h("th", {}, "已确认"), h("th", {}, "待复核"))), h("tbody", {}, ...rows)) :
      h("p", {class: "muted"}, "没有可补的图。"),
    skipped ? h("p", {class: "small muted"}, "跳过：" + skipped) : "",
    r.picked_missing ? h("p", {class: "small muted"}, "以前抽过的原图里有 " + r.picked_missing + " 张已经找不到（删了或移走了），没法对上。") : "",
    r.dry_run && (r.confirmed || r.predicted) ? h("div", {class: "actions"}, button("确认写入", asyncClick(async () => {
      const job = await queueTool("curation.backfill_types", {...args, dry_run: false}, project, false);
      dialog.close();
      trackJob(project, job, "按照片类型补类别", (done) => { backfillReport(project, done?.outputs?.result, {...args, dry_run: false}); if (state.tab === "gallery") navigate("gallery"); });
    }), "primary")) : ""));
}

function toastWithAction(text, label, action, failure = false) {
  const item = h("div", {class: "toast has-action" + (failure ? " error" : "")}, h("span", {}, text),
    h("button", {type: "button", class: "toast-action", onclick: () => { item.remove(); action(); }}, label));
  $("#notifications").append(item);
  setTimeout(() => item.remove(), failure ? 10000 : 4500);
}

// 后台轮询任务状态（间隔逐步拉长到 5 秒），成功调 onDone，失败给出可点的提示
async function trackJob(project, job, title, onDone) {
  if (!job?.id) { onDone?.(job); return; }
  let delay = 700, errors = 0;
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 1.4, 5000);
    let current;
    try { current = await request(pathFor(project) + "/jobs/" + encodeURIComponent(job.id)); errors = 0; }
    catch { if (++errors >= 10) return; continue; }
    if (current.status === "succeeded") { notifyUser("✓「" + title + "」已完成"); onDone?.(current); return; }
    if (["failed", "interrupted", "cancelled"].includes(current.status)) {
      toastWithAction("「" + title + "」" + (statusLabels[current.status] || "失败") + (current.error ? "：" + current.error : ""),
        "查看任务记录", () => navigate("jobs"), true);
      return;
    }
  }
}
async function labelsPage(){
  const project=state.project;
  const [ls,annotations]=await Promise.all([request(pathFor(project)+"/hub/ls-status").catch(e=>({connected:false,error:e.message})),request(pathFor(project)+"/annotations")]);
  // 直接排“回收标注”任务（不弹参数框），完成后刷新本页
  const pullNow=asyncClick(async()=>{const job=await queueTool("labels.pull",{},project,false);notifyUser("正在从 Label Studio 回收标注…");
    trackJob(project,job,"回收标注",()=>{if(state.tab==="labels"&&state.project?.id===project.id)navigate("labels");});});
  const autoPull=(on)=>asyncClick(async()=>{const job=await request(pathFor(project)+"/tools/labels.ls_webhook","POST",{enabled:on,engine_url:"http://127.0.0.1:8795"});
    trackJob(project,job,on?"开启自动回收":"关闭自动回收",()=>{if(state.tab==="labels"&&state.project?.id===project.id)navigate("labels");});});
  const runAndRefresh=(tool,args,title,then)=>asyncClick(async()=>{
    const job=await request(pathFor(project)+"/tools/"+encodeURIComponent(tool),"POST",args);notifyUser("已提交「"+title+"」");
    trackJob(project,job,title,async(done)=>{if(then)await then(done);if(state.tab==="labels"&&state.project?.id===project.id)navigate("labels");});});
  // 类别：只能在末尾追加（删改会让已有标注的类别编号错位）；加了以后旧标注要“送回复核”检查新类别
  const classesPanel=panel(h("div",{class:"rail"},h("strong",{},"检测类别"),button("添加类别",()=>addClassDialog(project),"small")),
    h("p",{},(project.config.classes||[]).map((c,i)=>i+". "+c).join("　")),
    h("p",{class:"small muted"},"一个模型可以识别多个类别，以后还能接着加。新类别只加在末尾；加了以后，以前的标注没检查过新类别，要先“送回复核”再进数据集。"));
  const cfg=ls.config||{},cfgBad=ls.connected&&!ls.error&&((cfg.missing_classes||[]).length||cfg.has_review_choice===false);
  const cfgPanel=cfgBad?h("section",{class:"panel callout"},h("strong",{},"Label Studio 项目 #"+ls.project_id+" 的标注配置不完整"),
    (cfg.missing_classes||[]).length?h("p",{},"缺少类别："+cfg.missing_classes.join("、")):"",
    cfg.has_review_choice===false?h("p",{},"没有 “Targets reviewed / No target” 选项：在这个项目里标的图，引擎没法确认框已画全，所以收不进来。"):"",
    h("p",{class:"small muted"},"点下面的按钮，按现在的类别新建一个 Label Studio 项目并切换过去（自动开启自动回收）；旧项目留在 Label Studio 里不动，里面已画的框可以用“送回复核”带到新项目。"),
    h("div",{class:"actions"},button("按现在的类别新建 Label Studio 项目",runAndRefresh("labels.ls_setup",{new:true},"新建 Label Studio 项目",
      async()=>{await request(pathFor(project)+"/tools/labels.ls_webhook","POST",{enabled:true,engine_url:"http://127.0.0.1:8795"});await initialize(project.id);}),"primary"))):"";
  const nr=ls.needs_review||{},nrTotal=(nr.stale||0)+(nr.rejected||0);
  const reviewPanel=nrTotal?panel(h("strong",{},"需要复核："+nrTotal+" 张"),
    nr.stale?h("p",{},"加新类别前已通过的旧标注 "+nr.stale+" 张：没检查过新类别，暂时不能进数据集。"):"",
    nr.rejected?h("p",{},"以前回收没通过、但已经画了框的 "+nr.rejected+" 张（类别名对不上、没选 Targets reviewed 等）。"):"",
    h("p",{class:"small muted"},"“送回复核”会把这些图带着已有的框送进当前 Label Studio 项目，打开就有框：补画新类别、改错框，选 Targets reviewed 提交即可。有了新类别的模型后，可以选它给旧图预画新类别的框，补查更快。"),
    h("div",{class:"actions"},button("送回 Label Studio 复核",()=>toolDialog("labels.rereview"),"primary"))):"";
  let status;
  if(!ls.connected)status=panel(h("strong",{},"Label Studio"),h("p",{},"这个子项目还没连接 Label Studio。点“提交所选图片”时会提示一键连接。"));
  else if(ls.error)status=panel(h("strong",{},"Label Studio 项目 #"+ls.project_id),h("p",{class:"error"},"连不上 Label Studio："+ls.error));
  else{
    const behind=Math.max(0,(ls.annotated||0)-(ls.pulled||0));
    status=panel(h("div",{class:"rail"},h("strong",{},"Label Studio 项目 #"+ls.project_id+(ls.title?" · "+ls.title:"")),
        ls.url?h("a",{href:ls.url,target:"_blank",rel:"noopener"},"打开 Label Studio ↗"):""),
      h("div",{class:"metrics"},stat("任务",ls.tasks),stat("已标注",ls.annotated),stat("已回收到引擎",ls.pulled),stat("自动回收",ls.auto_pull?"已开启":"未开启")),
      behind?h("p",{},"Label Studio 里有 "+behind+" 个已标注任务还没回收到引擎（只有选了 Targets reviewed / No target 的才算已复核）。"):h("p",{class:"small muted"},"标注都已回收。"),
      h("p",{class:"small muted"},ls.auto_pull?"自动回收已开启：在 Label Studio 提交或修改标注后，引擎会自动回收，不用手动操作。":"开启自动回收后，在 Label Studio 提交或修改标注，引擎会自动回收。"),
      h("div",{class:"actions"},button("立即回收标注",pullNow,"primary"),
        ls.auto_pull?button("关闭自动回收",autoPull(false)):button("开启自动回收",autoPull(true))));
  }
  return h("div",{},page("标注系统","从已抽取图片提交任务，保留人工复核结果。"),
    annotationTaskPicker(),classesPanel,status,cfgPanel,reviewPanel,panel(button("提交所选图片",()=>toolDialog("labels.push")),button("回收标注（选参数）",()=>toolDialog("labels.pull")),
    button("导入标注 JSON",()=>toolDialog("labels.import"))),
    h("details",{},h("summary",{},"标注明细（原始数据）"),code(annotations)));
}
async function datasetsPage(){
 return h("div",{},page("数据集版本","发布时核对标注、站点划分和文件哈希。"),
 annotationTaskPicker(),panel(button("发布新版本",()=>toolDialog("dataset.publish"))),code(await request(pathFor()+"/datasets")));
}
async function modelsPage(){
 return h("div",{},page("训练与模型","每个场景独立管理模型版本与发布别名。"),
 annotationTaskPicker(),panel(...["model.register","model.train","model.evaluate","model.alias","model.predict"].map(id=>button(state.tools.find(t=>t.id===id).title,()=>toolDialog(id)))),
 code(await request(pathFor()+"/models")));
}
async function jobsPage(){
 const jobs=await request(pathFor()+"/jobs");
 return h("div",{},page("任务记录","查看执行步骤与结果，失败任务可从已完成步骤后续跑。"),
 button("刷新",()=>navigate("jobs")), ...jobs.map(job=>panel(h("strong",{},job.kind),statusPill(job.status),
 button("查看结果",asyncClick(async()=>openDialog(job.id,code(await request(pathFor()+"/jobs/"+job.id)),true))),
 ...(["queued","paused","failed","interrupted"].includes(job.status)?[button(job.status==="queued"?"暂停":"续跑",asyncClick(async()=>{
 await request(pathFor()+"/jobs/"+job.id+"/control","POST",{command:job.status==="queued"?"pause":"resume"});navigate("jobs");}))]:[]),
 job.error?h("p",{class:"error"},job.error):null)));
}
$("#project-picker").addEventListener("change",()=>initialize($("#project-picker").value));
$("#new-project").onclick=()=>scenarioDialog();
$("#connection-button").onclick=connectionDialog;
initialize();

// 图库清理：移除类别 / 排除 / 恢复 / 删除（删除要二次确认，原图文件默认不删）
function cleanupDialog(project, action) {
  const ids = [...selected(project)];
  const titles = {unconfirm: "移除所选图片的类别", reject: "排除所选图片", restore: "恢复所选图片为待复核", delete: "删除所选图片"};
  if (!ids.length) { openDialog(titles[action], h("p", {}, "还没有勾选图片：先在下面图库里勾选要处理的照片。")); return; }
  const tip = {
    unconfirm: "撤销这些照片的人工确认类别：照片分类器不再从它们学习，分拣结果回到“unknown（待复核）”。照片本身不动。确认错了的话，也可以直接重新“确认所选图片类别”选对的类别，会覆盖旧的。",
    reject: "把这些照片标成“已排除”：不进数据集、不参与训练，但照片还在，随时可以“恢复所选为待复核”。适合拍糊、拍错、和巡检无关的照片。",
    restore: "把这些照片的状态改回“待复核”（撤销排除）。",
    delete: "从本场景彻底删除这些照片：登记、复制件、缩略图、类别、分拣结果、当前标注都会删掉；以后扫描到同样内容的图片也不会再登记回来。已经发布的数据集版本不受影响。",
  }[action];
  const source = h("input", {type: "checkbox"});
  const confirmBox = h("input", {type: "checkbox"});
  const body = h("div", {}, h("p", {}, "已选 " + ids.length + " 张。"), h("p", {class: "small muted"}, tip),
    action === "delete" ? h("div", {},
      h("label", {class: "photo-select"}, source, " 同时删除服务器上的原图文件（不可恢复！原图目录里的文件会被真正删掉）"),
      h("label", {class: "photo-select"}, confirmBox, " 我确认要删除这 " + ids.length + " 张照片")) : null);
  openDialog(titles[action], formContent(body, action === "delete" ? "删除" : "确定", async () => {
    if (action === "delete" && !confirmBox.checked) throw Error("请先勾选“我确认要删除”");
    const [tool, args] = {
      unconfirm: ["curation.unconfirm", {image_ids: ids}],
      reject: ["catalog.reject", {image_ids: ids, decision: "rejected"}],
      restore: ["catalog.reject", {image_ids: ids, decision: "pending"}],
      delete: ["catalog.delete", {image_ids: ids, delete_source: source.checked}],
    }[action];
    const job = await queueTool(tool, args, project);
    const done = await waitJob(project, job, 40);
    dialog.close();
    if (action === "delete") clearSelection();
    const r = done?.outputs?.result || {};
    notifyUser(action === "delete" ? `已删除 ${r.count ?? ids.length} 张` + (r.deleted_sources?.length ? `，连同 ${r.deleted_sources.length} 个原图文件` : "") :
      action === "unconfirm" ? `已移除 ${r.removed ?? 0} 张的类别` : `已处理 ${ids.length} 张`);
    navigate("gallery");
  }));
}

// 批量复核：多选后一次性下结论（确认 / 排除 / 待复核），可统一填检查点、站点、备注；不填的保留每张原来的值
function batchReviewDialog(project) {
  const ids = [...selected(project)];
  if (!ids.length) { openDialog("批量复核", h("p", {}, "还没有勾选图片：先在图库里勾选要复核的照片。")); return; }
  const decision = select([["accepted", "已确认（合格）"], ["rejected", "已排除（不合格）"], ["pending", "待复核（撤销结论）"]], "accepted");
  const checkpoint = textInput("", {placeholder: "不填 = 每张保留原来的检查点"});
  const site = textInput("", {placeholder: "不填 = 每张保留原来的站点"});
  const note = lines("");
  const form = formContent(h("div", {},
    h("p", {}, "已选 " + ids.length + " 张，一次性下同样的复核结论。"),
    h("div", {class: "form-grid"},
      field("复核结论", decision, "已确认 = 合格可用；已排除 = 不进数据集（照片还在，可恢复）；待复核 = 撤销之前的结论"),
      field("检查点（可不填）", checkpoint, "统一改成同一个检查点，如 Site Ground"),
      field("站点（可不填）", site, "统一改成同一个站点编号"),
      field("备注（可不填）", note, "如：拍糊了、角度不对", true))), "提交复核", async () => {
    const job = await queueTool("catalog.reject", {image_ids: ids, decision: decision.value,
      checkpoint: checkpoint.value.trim() || null, site_id: site.value.trim() || null, note: note.value.trim() || null}, project, false);
    const done = await waitJob(project, job, 60);
    dialog.close();
    notifyUser(done ? `已复核 ${ids.length} 张：${{accepted: "已确认", rejected: "已排除", pending: "待复核"}[decision.value]}` : "还在处理中，稍后到“任务记录”查看");
    navigate("gallery");
  });
  openDialog("批量复核所选（" + ids.length + " 张）", form);
}

// 参数定义目录（和新版工作流共用）+ 权重、模型下拉选项；按子项目缓存
const _catalogCache = {};
async function nodeCatalog() {
  const pid = state.project?.id || "";
  if (_catalogCache[pid]) return _catalogCache[pid];
  const [nd, w, ms] = await Promise.all([request("/api/nodes"), request("/api/weights").catch(() => ({weights: []})),
    pid ? request(pathFor() + "/models").catch(() => ({})) : {}]);
  const al = Object.keys(ms.aliases || {}), vs = (ms.models || []).map(m => m.version);
  const models = [...new Set(["production", ...al, ...vs])].map(v => [v, al.includes(v) ? v + "（别名 → " + ms.aliases[v] + "）" : v === "production" ? "production（还没设置）" : v]);
  let phototypes = [];
  try { const t = await request("/api/types/summary"); phototypes = t.types.map(x => [x.key, x.name + " · " + x.key + "（" + x.count + " 张）"]); } catch {}
  return (_catalogCache[pid] = {nodes: nd.nodes || {}, weights: w.weights || [], models, phototypes});
}

// 数据集、训练与模型：迁移到可视化的新页面（大白话、向导、图表）
function _moved(title, desc, href, label, extra = []) {
  return h("div", {}, page(title, desc),
    panel(h("p", {}, desc),
      h("div", {class: "actions"}, h("a", {class: "btn primary", href, style: "text-decoration:none;padding:.55rem 1rem"}, label), ...extra)));
}
async function datasetsPage() {
  return _moved("数据集版本", "数据集管理已升级到可视化页面：每个版本的图片数、框数、训练/验证/测试划分、类别分布、带框样例，一键打包最新标注。", "/datasets", "打开数据集页 →");
}
async function modelsPage() {
  return _moved("训练与模型", "训练与模型已升级为“模型中心”：成绩用大白话和趋势图展示，训练向导只需选 4 个选项，一键设为正式使用、试一试、评估、导出到手机。专业参数在“项目设置”里统一管理。", "/models", "打开模型中心 →",
    [h("a", {class: "btn", href: "/settings", style: "text-decoration:none;padding:.55rem 1rem"}, "项目设置")]);
}
