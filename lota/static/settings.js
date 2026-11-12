// 项目设置：专业参数统一放这里（训练、划分、命名、数据增强、找图、导出、Label Studio）。每项都有大白话说明和推荐值
"use strict";
const { h } = UI;

// 表单定义：[分组, 标题, 说明, [键, 名称, 说明, 类型, 选项/范围]]
const SCHEMA = [
  ["train", "训练默认值", "训练向导里预先选好的值；在向导里还能临时改", [
    ["family", "模型系列", "YOLO26 是 Ultralytics 2026 年的新一代，端侧（手机）更快，推荐；YOLOv8 是老牌、资料多", "select", [["yolo26", "YOLO26（推荐）"], ["yolov8", "YOLOv8"]]],
    ["size", "模型大小", "n 极速（手机实时）/ s 均衡 / m 精准（服务器）。数据少于 500 张时 n 或 s 就够", "select", [["n", "n · 极速"], ["s", "s · 均衡（推荐）"], ["m", "m · 精准"]]],
    ["preset", "训练强度", "对应下面“训练轮数”的三档", "select", [["quick", "试一下"], ["standard", "标准（推荐）"], ["thorough", "充分"]]],
    ["imgsz", "图片尺寸", "训练和推理时把图缩放到的边长。目标很小（远处的螺丝、裂纹）时用 960 或 1280，但更慢、更占显存", "number", [320, 1600, 32]],
    ["batch", "每批张数", "显存 8G：640 尺寸用 8，960 用 4。报“显存不足 CUDA out of memory”就减半", "number", [1, 64, 1]],
    ["device", "用什么跑", "0 = 第一块显卡（快）；cpu = 处理器（很慢，只在没有显卡时用）", "select", [["0", "显卡 0"], ["cpu", "CPU"]]],
    ["workers", "读图进程数", "2 即可；服务器 CPU 多可以设 4–8", "number", [0, 16, 1]],
    ["patience", "提前停止（轮）", "连续这么多轮成绩不进步就停，省时间、防过拟合。20–50 合适", "number", [0, 300, 1]],
    ["lr0", "学习率", "空 = 自动（推荐）。接着上一版训练时可设 0.001，防止“学了新的忘了旧的”", "number", [0.0001, 0.1, 0.0001]],
    ["continue_from_latest", "默认接着最新模型训", "主动学习推荐打开：每轮接着上一轮，学得快", "toggle"]]],
  ["presets", "训练轮数", "三档“训练强度”分别训练多少轮（一轮 = 把训练集完整学一遍）", [
    ["quick", "试一下", "快速看看数据和流程有没有问题", "number", [5, 500, 5]],
    ["standard", "标准", "一般 60–100 轮", "number", [10, 1000, 10]],
    ["thorough", "充分", "数据多、要上线的模型用", "number", [10, 2000, 10]]]],
  ["split", "数据集划分", "打包数据集时，多少图拿去验证、测试（同一站点/重复图只会在同一部分）", [
    ["val", "验证集比例", "训练时用来挑最好的一轮。0.15–0.2 合适", "range", [0.05, 0.4, 0.05]],
    ["test", "测试集比例", "完全不参与训练，用来公平比较不同模型。图少于 100 张时可设 0", "range", [0, 0.3, 0.05]],
    ["seed", "随机种子", "固定它，同一张图每次都分到同一部分，新老版本可比", "number", [0, 99999, 1]]]],
  ["naming", "命名规范", "版本名自动生成，团队统一。可用：{project} 子项目，{n} 自动编号，{dataset} 数据集版本，{size} 模型大小", [
    ["dataset", "数据集版本名", "例：{project}-r{n} → demo-r1、demo-r2", "text"],
    ["model", "模型版本名", "例：{dataset}-{size}-v{n} → demo-r3-s-v1（同一数据集再训一次会自动变成 v2）", "text"]]],
  ["augment", "数据增强（训练时自动生成有变化的图）", "让模型见过更多光照、角度、位置的变化，数据少时尤其有用。不懂就用下面的预设", [
    ["hsv_h", "色调变化", "照片偏色（白平衡不准）时调大。默认 0.015", "range", [0, 0.1, 0.005]],
    ["hsv_s", "饱和度变化", "鲜艳程度变化。默认 0.7", "range", [0, 1, 0.05]],
    ["hsv_v", "亮度变化", "逆光、阴天、夜间多时调大。默认 0.4", "range", [0, 1, 0.05]],
    ["degrees", "旋转角度（度）", "照片常歪着拍时设 5–15；仪表、文字类设 0", "range", [0, 90, 1]],
    ["translate", "平移", "目标位置变化。默认 0.1", "range", [0, 0.5, 0.05]],
    ["scale", "缩放", "远近变化大时调大。默认 0.5", "range", [0, 0.9, 0.05]],
    ["fliplr", "左右翻转概率", "左右对称的目标用 0.5；有方向/文字的（标签、指示灯排列）设 0", "range", [0, 1, 0.1]],
    ["flipud", "上下翻转概率", "俯拍、显微镜、医学切片可设 0.5；正常拍照设 0", "range", [0, 1, 0.1]],
    ["mosaic", "拼图增强", "把 4 张图拼成 1 张，对小目标、数据少很有用。默认 1.0", "range", [0, 1, 0.1]],
    ["mixup", "叠图增强", "两张图半透明叠加，数据多时可设 0.1", "range", [0, 0.5, 0.05]]]],
  ["find", "找图 / 主动学习默认值", "“用权重找图”“主动学习一轮”的默认挑图方式", [
    ["strategy", "挑图策略", "mixed = 拿不准的 + 有把握的交替（主动学习推荐）；uncertain = 只挑拿不准的；confident = 只挑有把握的", "select", [["mixed", "混合（推荐）"], ["uncertain", "最拿不准的"], ["confident", "最有把握的"]]],
    ["per_folder", "每个文件夹最多几张", "多样性：同一站点不要挑太多。0 = 不限", "number", [0, 100, 1]],
    ["max_hits", "每轮找多少张", "一轮标得完的量，20–50", "number", [1, 1000, 5]],
    ["min_confidence", "最低置信度", "低于它不算找到。主动学习用 0.25", "range", [0.05, 0.9, 0.05]]]],
  ["predict", "推理 / 预标注", "模型给图画框时的默认值", [
    ["confidence", "最低置信度", "预标注用 0.25（宁多勿少，人删框比画框快）；正式判定用 0.5", "range", [0.05, 0.9, 0.05]],
    ["imgsz", "推理尺寸", "一般和训练尺寸一样", "number", [320, 1600, 32]]]],
  ["export", "导出到手机", "模型中心“导出到手机”的默认值", [
    ["format", "默认格式", "安卓首选 TFLite INT8（体积约 1/4）", "select", [["tflite_int8", "安卓 TFLite INT8（推荐）"], ["tflite", "安卓 TFLite FP32"], ["ncnn", "NCNN"], ["onnx", "ONNX"]]],
    ["imgsz", "导出尺寸", "手机上 640 常用；要更快可 480/320", "number", [160, 1280, 32]]]],
  ["guide", "标注规范", "写清楚“怎么标”，同步到 Label Studio 后标注员每次打开任务都会先看到。标注标准统一，是模型分数上去的关键", [
    ["text", "标注规范内容", "第一行是标题，下面每行一条（可写“1. …”）。不会写就点下面的“生成模板”", "textarea"]]],
  ["pose", "姿态关键点（只有“姿态”任务用）", "关键点的名称和顺序；建好后不要改顺序。名称里带 left/right 或 左/右 的会自动配对，左右翻转增强时自动对调", [
    ["keypoints", "关键点名称", "逗号隔开，如 nose, left_eye, right_eye, left_shoulder, right_shoulder（COCO 人体 17 点）或 grip_left, grip_right, center", "list"]]],
  ["label_studio", "Label Studio", "标注工具连接", [
    ["public_url", "浏览器访问地址", "页面上“去 Label Studio 标注”链接用的地址", "text"]]],
];
const AUG_PRESETS = {
  off: { label: "关闭", desc: "不做增强（只在调试时用）", v: { hsv_h: 0, hsv_s: 0, hsv_v: 0, degrees: 0, translate: 0, scale: 0, fliplr: 0, flipud: 0, mosaic: 0, mixup: 0 } },
  light: { label: "轻度", desc: "数据多、场景固定", v: { hsv_h: 0.01, hsv_s: 0.4, hsv_v: 0.25, degrees: 0, translate: 0.05, scale: 0.3, fliplr: 0.5, flipud: 0, mosaic: 0.5, mixup: 0 } },
  std: { label: "标准（推荐）", desc: "Ultralytics 默认", v: { hsv_h: 0.015, hsv_s: 0.7, hsv_v: 0.4, degrees: 0, translate: 0.1, scale: 0.5, fliplr: 0.5, flipud: 0, mosaic: 1, mixup: 0 } },
  strong: { label: "强", desc: "数据很少、现场光照角度变化大", v: { hsv_h: 0.02, hsv_s: 0.8, hsv_v: 0.5, degrees: 10, translate: 0.2, scale: 0.7, fliplr: 0.5, flipud: 0, mosaic: 1, mixup: 0.1 } },
  topdown: { label: "俯拍 / 显微 / 医学", desc: "没有上下左右之分的图", v: { hsv_h: 0.01, hsv_s: 0.5, hsv_v: 0.3, degrees: 90, translate: 0.1, scale: 0.5, fliplr: 0.5, flipud: 0.5, mosaic: 1, mixup: 0 } },
};

let state = null;
async function render() {
  const pid = UI.project(), app = document.getElementById("app");
  const { settings, defaults } = await UI.api(`/api/projects/${pid}/settings`);
  state = JSON.parse(JSON.stringify(settings));
  const info = UI.projectInfo(pid);
  const sections = SCHEMA.map(([group, title, desc, fields]) => h("div", { class: "card", id: "g-" + group },
    h("h3", {}, title), h("div", { class: "sub", style: { marginBottom: "12px" } }, desc),
    group === "augment" ? augPresets() : null,
    h("div", { class: "grid k2" }, fields.map((f) => fieldEl(group, f, defaults[group]?.[f[0]])))));
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "项目设置"), h("p", {}, `「${info?.config?.name || pid}」的统一配置。专业参数都集中在这里，训练向导、工作流模板、找图、导出都默认用这些值。不确定就保持默认。`)),
      h("div", { class: "row" }, h("button", { class: "btn", onclick: reset }, "恢复默认"), h("button", { class: "btn primary lg", onclick: save }, "保存设置"))),
    h("div", { class: "row", style: { marginBottom: "12px" } }, ...SCHEMA.map(([g, t]) => h("a", { class: "badge brand", href: "#g-" + g }, t.replace(/（.*）/, "")))),
    h("div", { class: "grid", style: { gap: "14px" } }, sections));
}
function fieldEl(group, [key, label, help, kind, opt], def) {
  const val = state[group]?.[key];
  const set = (v) => { state[group][key] = v; };
  let input;
  if (kind === "select") { input = h("select", { class: "select" }, ...opt.map(([v, t]) => h("option", { value: v }, t))); input.value = val ?? ""; input.onchange = () => set(input.value); }
  else if (kind === "toggle") { input = h("label", { class: "row" }, h("input", { type: "checkbox", checked: !!val, onchange: (e) => set(e.target.checked) }), "打开"); }
  else if (kind === "range") {
    const out = h("b", { class: "mono", style: { minWidth: "48px", textAlign: "right" } }, String(val));
    const r = h("input", { type: "range", min: opt[0], max: opt[1], step: opt[2], value: val ?? 0, style: { flex: 1 } });
    r.oninput = () => { out.textContent = r.value; set(Number(r.value)); };
    input = h("div", { class: "row", "data-key": group + "." + key }, r, out);
  } else if (kind === "number") {
    input = h("input", { class: "input", type: "number", min: opt[0], max: opt[1], step: opt[2], value: val ?? "", placeholder: val == null ? "自动" : "" });
    input.onchange = () => set(input.value === "" ? null : Number(input.value));
  } else if (kind === "list") {
    input = h("input", { class: "input", value: (val || []).join(", ") });
    input.onchange = () => set(input.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean));
  } else if (kind === "textarea") {
    input = h("textarea", { class: "input", style: { minHeight: "220px", lineHeight: "1.6" } }, val ?? "");
    input.oninput = () => set(input.value);
    const gen = h("button", { class: "btn sm", onclick: async () => {
      const t = await UI.api(`/api/projects/${UI.project()}/guide-template`);
      if (input.value.trim() && !confirm("用模板替换现在的内容？")) return;
      input.value = t.text; set(t.text); UI.toast("已生成模板，按需修改后点保存");
    } }, "生成模板");
    const sync = h("button", { class: "btn sm primary", onclick: async (e) => {
      e.target.disabled = true;
      try { await save(); const r = await UI.runTool(UI.project(), "labels.ls_guide", {}); UI.toast(`已同步到 Label Studio 项目 #${r.ls_project}`); }
      catch (err) { UI.toast(err.message, true); } finally { e.target.disabled = false; }
    } }, "保存并同步到 Label Studio");
    input = h("div", { class: "form", style: { gap: "8px" } }, input, h("div", { class: "row" }, gen, sync));
  } else {
    input = h("input", { class: "input", value: val ?? "" }); input.onchange = () => set(input.value.trim());
  }
  return h("div", { class: "field", style: kind === "textarea" ? { gridColumn: "1 / -1" } : null }, h("label", {}, label), input,
    h("div", { class: "help" }, help, def !== undefined && def !== null ? ` · 默认 ${def}` : ""));
}
function augPresets() {
  return h("div", { class: "seg", style: { marginBottom: "12px" } }, ...Object.entries(AUG_PRESETS).map(([k, p]) => {
    const l = h("label", {}, h("input", { type: "radio", name: "aug" }), h("b", {}, p.label), h("span", {}, p.desc));
    l.onclick = () => {
      Object.assign(state.augment, p.v);
      for (const [kk, v] of Object.entries(p.v)) { const row = document.querySelector(`[data-key="augment.${kk}"]`); if (row) { row.querySelector("input").value = v; row.querySelector("b").textContent = v; } }
      UI.toast(`已套用“${p.label}”，记得点保存`);
    };
    return l;
  }));
}
async function save() {
  await UI.api(`/api/projects/${UI.project()}/settings`, { method: "PUT", body: { settings: state } });
  UI.toast("设置已保存");
}
async function reset() {
  const { defaults } = await UI.api(`/api/projects/${UI.project()}/settings`);
  UI.modal("恢复默认设置", h("p", {}, "把这个子项目的所有设置恢复成推荐的默认值？"), [{ label: "取消" }, { label: "恢复默认", kind: "danger", run: async () => {
    await UI.api(`/api/projects/${UI.project()}/settings`, { method: "PUT", body: { settings: defaults } }); UI.toast("已恢复默认"); render();
  } }]);
}

(async () => { await UI.nav("settings"); UI.onProject(render); render().catch((e) => UI.toast(e.message, true)); })();
