// 模型中心：成绩一目了然、训练向导（大白话选项）、设为正式/最新、评估、试一试、导出到手机
"use strict";
const { h } = UI;
const M = (m, k) => m?.[`metrics/${k}(B)`] ?? m?.[`metrics/${k}`] ?? m?.[k];
const SCORE = (m) => M(m, "mAP50") ?? m?.["metrics/accuracy_top1"] ?? m?.["metrics/mIoU"];
const COLORS = { map: "#1d6d52", p: "#1d5fd1", r: "#c2410c" };
const ALIAS_TEXT = { production: "正式使用", latest: "最新", candidate: "候选" };
let S = null;   // summary
let jobsTimer = null;

function scoreBar(label, help, v) {
  const cls = v == null ? "" : v >= 0.7 ? "" : v >= 0.45 ? " warn" : " danger";
  return h("div", { class: "score", title: help },
    h("span", {}, label), h("div", { class: "meter" + cls }, h("i", { style: { width: (v == null ? 0 : Math.round(v * 100)) + "%" } })),
    h("span", { class: "v" }, UI.pct(v)));
}
function grade(v) {
  if (v == null) return h("span", { class: "badge" }, "未评估");
  if (v >= 0.8) return h("span", { class: "badge ok" }, "优秀");
  if (v >= 0.65) return h("span", { class: "badge brand" }, "良好");
  if (v >= 0.45) return h("span", { class: "badge warn" }, "一般");
  return h("span", { class: "badge danger" }, "待提升");
}

async function render() {
  const pid = UI.project(), app = document.getElementById("app");
  if (!pid) { app.replaceChildren(h("div", { class: "empty" }, "还没有子项目")); return; }
  const [sum, ms] = await Promise.all([UI.api(`/api/projects/${pid}/hub/summary`), UI.api(`/api/projects/${pid}/hub/models`)]);
  S = sum;
  const prod = ms.models.find((m) => m.aliases.includes("production")), latest = ms.models.find((m) => m.aliases.includes("latest"));
  const info = UI.projectInfo(pid);
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "模型中心"), h("p", {}, `「${info?.config?.name || pid}」的所有模型版本：成绩、正在使用哪个、导出到手机。分数越高越好；“正式使用”的模型会被预标注、找图和推理默认使用。`)),
      h("div", { class: "row" }, h("a", { class: "btn", href: "/datasets" }, "看数据集"), h("button", { class: "btn", onclick: compareDlg }, "A/B 对比"), h("button", { class: "btn", onclick: importDlg }, "导入外部模型"),
        h("button", { class: "btn primary lg", onclick: () => trainWizard() }, "＋ 训练新模型"))),
    h("div", { class: "grid k4" },
      kpi("正式使用中", prod ? prod.version : "还没有", prod ? `综合得分 ${UI.pct(SCORE(prod.metrics))}` : "训练后点“设为正式使用”"),
      kpi("最新一版", latest ? latest.version : (ms.models[0]?.version || "—"), latest ? `综合得分 ${UI.pct(SCORE(latest.metrics))}` : "主动学习每轮会更新"),
      kpi("已标注、可训练", sum.accepted_annotations + " 张", sum.new_since_last_dataset ? `比最新数据集多 ${sum.new_since_last_dataset} 张，可以再训一版` : "都已打包进数据集"),
      kpi("模型版本数", ms.models.length, `数据集 ${sum.datasets} 个`)),
    ...[ms.models.length ? trendCard(ms.models) : null].filter(Boolean),
    h("div", { class: "callout", style: { marginTop: "14px" } },
      h("b", {}, "怎么看分数："), " 「综合得分」(mAP50) 是总成绩；「框得准」(精确率) = 模型画的框里有多少是对的，低了说明误报多；",
      "「找得全」(召回率) = 该找的目标找到了多少，低了说明漏检多。一般 65% 以上可试用，80% 以上很好。"),
    h("div", { class: "section-title" }, h("h2", {}, "全部模型"), h("span", { class: "muted small" }, "最新的在前")),
    ms.models.length ? h("div", { class: "grid k3" }, ms.models.map((m) => modelCard(pid, m)))
      : h("div", { class: "empty" }, "还没有模型。先在 Label Studio 标注一些图片，再点右上角「训练新模型」。"),
    h("div", { class: "section-title" }, h("h2", {}, "最近的任务"), h("span", { class: "muted small" }, "训练、评估、导出都在后台跑，可以关掉页面")),
    h("div", { id: "jobs" }));
  loadJobs();
}
function kpi(label, value, hint) { return h("div", { class: "card kpi" }, h("span", { class: "label" }, label), h("span", { class: "value", style: { fontSize: String(value).length > 14 ? "18px" : "" } }, value), h("span", { class: "hint" }, hint)); }

function trendCard(models) {
  const asc = models.slice().reverse().filter((m) => m.metrics && SCORE(m.metrics) != null);
  if (!asc.length) return null;
  return h("div", { class: "card", style: { marginTop: "14px" } },
    h("div", { class: "row between" }, h("h3", {}, "每一版的成绩"), h("span", { class: "muted small" }, "看是不是一版比一版好")),
    UI.lineChart([
      { name: "综合得分 mAP50", color: COLORS.map, points: asc.map((m) => [m.version, SCORE(m.metrics)]) },
      { name: "框得准（精确率）", color: COLORS.p, points: asc.map((m) => [m.version, M(m.metrics, "precision")]) },
      { name: "找得全（召回率）", color: COLORS.r, points: asc.map((m) => [m.version, M(m.metrics, "recall")]) }]));
}

function modelCard(pid, m) {
  const met = m.metrics || {};
  const last = (m.evaluations || []).slice(-1)[0];
  const origin = m.continued_from ? `接着 ${m.continued_from} 继续训` : m.base && /^(Ultralytics|外部导入)/.test(m.base) ? `来源：${m.base}` : m.base ? `从 ${m.base} 开始训` : m.training?.initial_weights ? `底座 ${m.training.initial_weights}` : "外部导入";
  const ep = m.training?.parameters?.epochs;
  return h("div", { class: "card hover" + (m.aliases.includes("production") ? " accent" : "") },
    h("div", { class: "row between" }, h("h3", { class: "mono" }, m.version), grade(SCORE(met))),
    h("div", { class: "row", style: { margin: "4px 0 8px" } },
      ...m.aliases.map((a) => h("span", { class: "badge " + (a === "production" ? "ok" : a === "latest" ? "info" : "violet") }, ALIAS_TEXT[a] || a)),
      h("span", { class: "badge" }, m.task === "classify" ? "分类" : m.task === "segment" ? "分割" : "检测"),
      h("span", { class: "tiny muted", title: (m.classes || []).join("、") }, (m.classes || []).slice(0, 8).join("、") + ((m.classes || []).length > 8 ? ` 等 ${m.classes.length} 类` : ""))),
    ...(met["metrics/mIoU"] != null ? [scoreBar("平均交并比 mIoU", "语义分割：预测区域和标注区域重合的程度", met["metrics/mIoU"])] : m.task === "classify" ? [
      scoreBar("准确率", "Top-1：第一名就猜对的比例", met["metrics/accuracy_top1"]),
      scoreBar("前五名命中", "Top-5：正确答案在前 5 个候选里的比例", met["metrics/accuracy_top5"])] : [
      scoreBar("综合得分", "mAP50：总成绩", SCORE(met)),
      scoreBar("框得准", "精确率 Precision：画的框里有多少是对的", M(met, "precision")),
      scoreBar("找得全", "召回率 Recall：该找的找到了多少", M(met, "recall"))]),
    h("div", { class: "small muted", style: { marginTop: "8px" } },
      `数据集 ${m.dataset_version || "—"} · ${origin}${ep ? ` · ${ep} 轮` : ""} · ${UI.when(m.created_at)} · ${UI.bytes(m.size)}`),
    m.note ? h("div", { class: "small", style: { marginTop: "4px", color: "var(--ink-2)" } }, "📝 " + m.note) : null,
    last ? h("div", { class: "small", style: { marginTop: "4px" } }, `最近评估：${last.dataset_version}/${last.split === "test" ? "测试集" : "验证集"} 综合 ${UI.pct(SCORE(last.metrics))} · ${last.at}`) : null,
    m.exports.length ? h("div", { class: "row small", style: { marginTop: "6px" } }, "已导出：", ...m.exports.map((e) =>
      h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); download(pid, m.version, e.name); } }, `${e.name}（${UI.bytes(e.size)}）`))) : null,
    h("div", { class: "row", style: { marginTop: "12px" } },
      m.aliases.includes("production") ? null : h("button", { class: "btn sm primary", onclick: () => setAlias(pid, "production", m.version) }, "设为正式使用"),
      m.aliases.includes("latest") ? null : h("button", { class: "btn sm", onclick: () => setAlias(pid, "latest", m.version) }, "设为最新"),
      h("button", { class: "btn sm", onclick: () => tryIt(pid, m) }, "试一试"),
      h("button", { class: "btn sm", onclick: () => plotsDlg(pid, m) }, "看图表"),
      h("button", { class: "btn sm", onclick: () => evaluate(pid, m) }, "评估"),
      h("button", { class: "btn sm", onclick: () => exportDlg(pid, m) }, "导出到手机"),
      h("button", { class: "btn sm ghost", onclick: () => download(pid, m.version, "weights.pt") }, "下载 .pt")));
}

async function download(pid, version, name) {
  const url = await UI.blobUrl(`/api/projects/${pid}/models/${version}/files/${encodeURIComponent(name)}`);
  const a = h("a", { href: url, download: name === "weights.pt" ? version + ".pt" : name }); document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
async function setAlias(pid, alias, version) {
  await UI.runTool(pid, "model.alias", { alias, version });
  UI.toast(`已把 ${version} 设为「${ALIAS_TEXT[alias]}」`); render();
}

const SPLIT_C = { train: ["训练", "#1d6d52"], val: ["验证", "#1d5fd1"], test: ["测试", "#c2410c"] };
// ── 训练新模型（极简：参考 Ultralytics Platform 的“训练新模型”对话框）────────────────
const FAM_ZH = { yolo26: "YOLO26", yolov8: "YOLOv8" }, SIZE_ZH = { n: "极速", s: "均衡", m: "精准" };
const TV_ARCHS = [["tv:resnet50", "ResNet-50（非 YOLO，经典基线）"], ["tv:efficientnet_b0", "EfficientNet-B0（非 YOLO，小而准）"], ["tv:convnext_tiny", "ConvNeXt-Tiny（非 YOLO，精度高）"],
  ["tv:resnet18", "ResNet-18（非 YOLO，数据少时）"], ["tv:mobilenet_v3_large", "MobileNetV3（非 YOLO，手机友好）"], ["tv:vit_b_16", "ViT-B/16（非 YOLO，Transformer）"]];
const SEG_ARCHS = [["seg:deeplabv3_mobilenet_v3_large", "DeepLabV3-MobileNet（非 YOLO 语义分割）"], ["seg:deeplabv3_resnet50", "DeepLabV3-ResNet50（非 YOLO 语义分割）"],
  ["seg:lraspp_mobilenet_v3_large", "LR-ASPP（非 YOLO 语义分割，最快）"], ["seg:fcn_resnet50", "FCN-ResNet50（非 YOLO 语义分割）"]];
const TASK_SUFFIX = { detect: "", segment: "-seg", classify: "-cls", pose: "-pose", obb: "-obb" };

async function trainWizard(preDs) {
  const pid = UI.project(), task = UI.projectInfo(pid)?.config?.task || "detect";
  const [{ datasets }, bases, ms] = await Promise.all([UI.api(`/api/projects/${pid}/hub/datasets`), UI.api(`/api/projects/${pid}/hub/base-weights?task=${task}`), UI.api(`/api/projects/${pid}/hub/models`)]);
  const st = S.settings, tr = st.train, latest = ms.aliases.latest;
  const field = (label, el, help) => h("div", { class: "field" }, h("label", {}, label), el, help ? h("div", { class: "help" }, help) : null);
  const opt = (v, t) => h("option", { value: v }, t);

  // 1. 基础模型
  const baseSel = h("select", { class: "select" },
    latest ? h("optgroup", { label: "接着训（主动学习推荐）" }, opt("latest", `接着「最新」模型 ${latest} 训，学得快`)) : null,
    h("optgroup", { label: "从官方预训练底座开始" }, ...bases.filter((b) => b.ok).map((b) => opt(`base:${b.family}:${b.size}`, `${FAM_ZH[b.family]}${b.size} · ${SIZE_ZH[b.size]}${b.family === tr.family && b.size === tr.size ? "（推荐）" : ""}`))),
    task === "classify" ? h("optgroup", { label: "非 YOLO 经典网络" }, ...TV_ARCHS.map(([v, t]) => opt(v, t))) : null,
    task === "segment" ? h("optgroup", { label: "非 YOLO 语义分割" }, ...SEG_ARCHS.map(([v, t]) => opt(v, t))) : null);
  const defBase = `base:${tr.family}:${tr.size}`;
  baseSel.value = latest && tr.continue_from_latest ? "latest" : [...baseSel.options].some((o) => o.value === defBase) ? defBase : (baseSel.options[0]?.value || "");
  const baseHelp = h("div", { class: "help" });

  // 2. 数据集（下面带一张小卡片：多少图、多少类、怎么划分）
  const newCount = S.accepted_annotations;
  const dsSel = h("select", { class: "select" },
    opt("__new__", `打包最新标注为新数据集（${newCount} 张已复核${datasets.length ? `，比 ${datasets[0].version} 多 ${S.new_since_last_dataset} 张` : ""}）`),
    ...datasets.map((d) => opt(d.version, `${d.version}（${d.image_total} 张）`)));
  dsSel.value = preDs && datasets.some((d) => d.version === preDs) ? preDs : (S.new_since_last_dataset > 0 || !datasets.length ? "__new__" : datasets[0].version);
  const dsCardEl = h("div", { class: "card", style: { boxShadow: "none", padding: "10px 12px", background: "var(--panel-2)" } });

  // 3. 参数
  const epochs = h("input", { class: "input", type: "number", min: 1, max: 1000, value: st.presets[tr.preset] || 80 });
  const chips = h("div", { class: "row", style: { gap: "6px", marginTop: "4px" } });
  const drawChips = () => chips.replaceChildren(...[["quick", "试一下"], ["standard", "标准"], ["thorough", "充分"]].map(([k, t]) =>
    h("button", { class: "btn sm ghost", type: "button", onclick: () => { epochs.value = scaleEpochs(st.presets[k]); epochs.dataset.touched = "1"; update(); } }, `${t} ${scaleEpochs(st.presets[k])}`)));
  const batch = h("select", { class: "select" }, opt("-1", "自动（按显存挑，推荐）"), ...[4, 8, 16, 32, 64].map((n) => opt(String(n), String(n))));
  batch.value = "-1";
  const imgsz = h("select", { class: "select" }, ...[[320, "320 · 很快，大目标"], [480, "480"], [640, "640 · 常用（推荐）"], [960, "960 · 小目标"], [1280, "1280 · 很小的目标，慢"]].map(([v, t]) => opt(String(v), t)));
  imgsz.value = String([320, 480, 640, 960, 1280].includes(tr.imgsz) ? tr.imgsz : 640);
  const name = h("input", { class: "input mono" });
  const nameHelp = h("div", { class: "help" });

  function kind() { return baseSel.value.startsWith("tv:") ? "tv" : baseSel.value.startsWith("seg:") ? "seg" : "yolo"; }
  function scaleEpochs(n) { return kind() === "tv" ? Math.max(10, Math.round(n / 4)) : kind() === "seg" ? Math.max(10, Math.round(n / 3)) : n; }
  const sizeOf = () => baseSel.value.startsWith("base:") ? baseSel.value.split(":")[2] : tr.size;
  const defaultName = () => kind() === "yolo" ? st.naming.model.replace("{size}", sizeOf()) : kind() === "tv" ? st.naming.model.replace("{size}", baseSel.value.slice(3).replace(/_/g, "")) : "{dataset}-" + baseSel.value.slice(4).split("_")[0] + "-v{n}";
  const dsName = () => dsSel.value === "__new__" ? st.naming.dataset.replace("{project}", pid).replace("{n}", "N") : dsSel.value;

  // 在本地训练：一行命令（不含任何密钥）
  const cmd = h("pre", { class: "mono small", style: { whiteSpace: "pre-wrap", background: "#0e2620", color: "#d6e7df", padding: "12px", borderRadius: "10px", margin: 0 } });
  const localBox = h("div", { class: "form" });
  function update() {
    if (!epochs.dataset.touched) epochs.value = scaleEpochs(st.presets[tr.preset] || 80);
    drawChips();
    if (!name.dataset.touched) name.value = defaultName();
    const d = datasets.find((x) => x.version === dsSel.value);
    dsCardEl.replaceChildren(d
      ? h("div", { class: "row between" }, h("div", {}, h("b", { class: "mono" }, d.version), h("div", { class: "small muted" }, `${d.image_total} 张图片 · ${(d.classes || []).length} 个类别 · ${d.box_total} 个标注`)),
        h("div", { class: "row", style: { gap: "6px" } }, ...["train", "val", "test"].filter((k) => d.counts?.[k]).map((k) => h("span", { class: "badge", style: { background: SPLIT_C[k][1] + "22", color: SPLIT_C[k][1] } }, `${SPLIT_C[k][0]} ${d.counts[k]}`))))
      : h("div", { class: "small" }, `开始时先把 ${newCount} 张已复核的标注打包成新版本（验证 ${Math.round(st.split.val * 100)}%、测试 ${Math.round(st.split.test * 100)}%），再用它训练。`));
    baseHelp.textContent = baseSel.value === "latest" ? "在上一版的基础上继续学新标注，几十轮就能追上；数据变化很大时改用官方底座重新训。"
      : kind() === "yolo" ? "官方在 COCO 等大数据集上预训练过的模型，从它开始训比从零开始快得多。n 最快，s 均衡，m 最准。"
      : kind() === "tv" ? "torchvision 的 ImageNet 预训练网络，图片会缩放到 224；医学/工业分类常用作基线。" : "torchvision 语义分割网络，按像素分类，适合边界不规则的区域（锈蚀、积水、病灶）。";
    nameHelp.textContent = `训练出来的模型叫这个名字。{dataset} = 数据集名（现在是 ${dsName()}），{n} = 自动编号，重名会自动加 1。`;
    // 本地命令
    const ver = dsName(), run = name.value.replace("{dataset}", ver).replace("{n}", "1");
    if (kind() !== "yolo") {
      localBox.replaceChildren(h("div", { class: "callout" }, "非 YOLO 网络（torchvision）目前只支持在服务器上训练。想在本地训，选一个 YOLO 底座。"));
      return;
    }
    const model = baseSel.value === "latest" ? `${latest}.pt` : `${baseSel.value.split(":")[1]}${sizeOf()}${TASK_SUFFIX[task] || ""}.pt`;
    const data = task === "classify" ? ver : `${ver}/data.yaml`;
    cmd.textContent = `pip install -U ultralytics\n\nyolo train model=${model} data=${data} task=${task} epochs=${epochs.value} batch=${batch.value} imgsz=${imgsz.value} project=${pid} name=${run}`;
    const dlBtn = h("button", { class: "btn sm", type: "button", disabled: !d, onclick: async (e) => {
      e.target.disabled = true; e.target.textContent = "打包中…";
      try { const u = await UI.blobUrl(`/api/projects/${pid}/datasets/${d.version}/download`); const a = h("a", { href: u, download: d.version + ".zip" }); document.body.append(a); a.click(); a.remove(); }
      catch (err) { UI.toast(err.message, true); } finally { e.target.disabled = false; e.target.textContent = "下载数据集 zip"; } } }, "下载数据集 zip");
    localBox.replaceChildren(
      h("ol", { class: "small", style: { margin: 0, paddingLeft: "18px", display: "grid", gap: "6px" } },
        h("li", {}, d ? ["下载数据集并解压到当前目录（解压后是 ", h("span", { class: "mono" }, ver + "/"), " 文件夹）：", dlBtn] : "先选一个已有的数据集版本（“打包最新标注”要在服务器上先打包，再回来下载）。"),
        h("li", {}, baseSel.value === "latest" ? ["在「最新」模型卡片上点「下载 .pt」，放到同一目录（", h("span", { class: "mono" }, model), "）。"] : ["底座权重 ", h("span", { class: "mono" }, model), " 第一次运行时会自动下载。"]),
        h("li", {}, "在有显卡的电脑上运行下面的命令（Windows / macOS / Linux 都一样）：")),
      h("div", { style: { position: "relative" } }, cmd,
        h("button", { class: "btn sm", type: "button", style: { position: "absolute", top: "8px", right: "8px" }, onclick: () => { navigator.clipboard?.writeText(cmd.textContent).then(() => UI.toast("已复制")).catch(() => UI.toast("复制失败，请手动选中复制", true)); } }, "复制")),
      h("div", { class: "small muted" }, `训练完，结果在 ${pid}/${run}/weights/best.pt。回到模型中心点「导入外部模型」上传它，就能在这里评估、试一试、设为正式使用。`));
  }
  for (const el of [baseSel, dsSel, batch, imgsz]) el.onchange = update;
  epochs.oninput = () => { epochs.dataset.touched = "1"; update(); };
  name.oninput = () => { name.dataset.touched = "1"; update(); };
  baseSel.addEventListener("change", () => { delete name.dataset.touched; delete epochs.dataset.touched; update(); });

  const serverBox = h("div", { class: "callout" }, "在服务器显卡上跑，训练期间可以继续在 Label Studio 标注。训练完自动评估并设为「最新」，确认效果好再点「设为正式使用」。");
  const where = h("div", { class: "seg" },
    h("label", {}, h("input", { type: "radio", name: "where", value: "server", checked: true }), h("b", {}, "☁ 在服务器训练"), h("span", {}, "用服务器的显卡，实时看曲线（推荐）")),
    h("label", {}, h("input", { type: "radio", name: "where", value: "local" }), h("b", {}, "💻 在本地电脑训练"), h("span", {}, "给你一行 yolo 命令，训完再导入")));
  const whereBody = h("div");
  const showWhere = () => {
    const local = body.querySelector("input[name=where]:checked").value === "local";
    whereBody.replaceChildren(local ? localBox : serverBox);
    const go = body.closest(".modal")?.querySelector("footer .btn.primary");
    if (go) go.textContent = local ? "复制命令" : "开始训练";
  };
  const body = h("div", { class: "form" },
    field("基础模型", baseSel, null), baseHelp,
    field("数据集", dsSel), dsCardEl,
    h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" } },
      field("训练轮数 epochs", h("div", {}, epochs, chips), "一般 50–150；数据少就多训几轮"),
      field("批大小 batch", batch, "自动 = 按显存挑；报显存不足就选 8 或 4"),
      field("图像大小 imgsz", imgsz, "目标很小就选 960")),
    field("模型名称", name, null), nameHelp,
    field("在哪训练", where), whereBody,
    UI.pitfall([
      "训练完会自动设为“最新”，但不会自动上线。先在同一个考试集上和旧模型比一比，确实更好再点「设为正式使用」。",
      "这一版数据如果有新加的类别、或整批确认过的标注，先抽看几张，错的标注会被模型学进去。",
      "成绩连续两三轮涨不动了，就别再加同类的图，去补成绩差的类别或场景。"]),
    h("div", { class: "small muted" }, "学习率、提前停止、数据增强等专业参数：点左下角「详细设置」，或在 ", h("a", { href: "/settings" }, "项目设置"), " 里统一改。"));
  where.onchange = showWhere;
  update(); showWhere();

  const close = UI.modal("训练新模型", body, [
    { label: "详细设置…", run: () => { setTimeout(trainWizardAdvanced, 0); } },
    { label: "取消" },
    { label: "开始训练", kind: "primary", run: async () => {
      if (body.querySelector("input[name=where]:checked").value === "local") {
        await navigator.clipboard?.writeText(cmd.textContent).catch(() => {});
        UI.toast("命令已复制，去本地电脑运行吧"); return;
      }
      const ds = dsSel.value, n = Number(epochs.value), b = Number(batch.value), sz = Number(imgsz.value);
      if (!(n >= 1)) throw new Error("训练轮数要填正整数");
      if (ds === "__new__" && !S.accepted_annotations) throw new Error("还没有已复核的标注，先去 Label Studio 标注并回收");
      if (!/^[A-Za-z0-9{][A-Za-z0-9_.{}-]*$/.test(name.value)) throw new Error("模型名称只能用字母、数字、点、下划线、短横线（可以带 {dataset}、{n}）");
      const adv = { epochs: n, imgsz: sz, batch: b === -1 ? null : b, patience: null, lr0: null };
      if (kind() === "seg") return startSemseg(pid, { ds, arch: baseSel.value.slice(4), preset: tr.preset, adv, name: name.value });
      if (kind() === "tv") return startClassic(pid, { ds, arch: baseSel.value.slice(3), preset: tr.preset, adv: { ...adv, imgsz: null }, name: name.value });
      const fb = baseSel.value === "latest" ? bases.find((x) => x.ok && x.family === tr.family && x.size === tr.size) || bases.find((x) => x.ok) : bases.find((x) => `base:${x.family}:${x.size}` === baseSel.value);
      if (!fb?.ok) throw new Error("服务器上没有这个底座权重，换一个");
      startTraining(pid, { ds, size: sizeOf(), preset: tr.preset, from: baseSel.value === "latest" ? "latest" : "base", base: fb.path, name: name.value, adv: { ...adv, batch: b } });
    } }], { wide: true });
  showWhere();
  return close;
}

// ── 详细训练向导（专业参数）──────────────────────────────────────────────────
async function trainWizardAdvanced() {
  const pid = UI.project();
  const [{ datasets }, bases, ms] = await Promise.all([UI.api(`/api/projects/${pid}/hub/datasets`), UI.api(`/api/projects/${pid}/hub/base-weights?task=${UI.projectInfo(pid)?.config?.task || "detect"}`), UI.api(`/api/projects/${pid}/hub/models`)]);
  const st = S.settings, tr = st.train;
  const hasLatest = !!ms.aliases.latest, task = UI.projectInfo(pid)?.config?.task || "detect";
  const radio = (name, value, title, desc, checked) => h("label", {}, h("input", { type: "radio", name, value, checked }), h("b", {}, title), h("span", {}, desc));
  const newOne = S.new_since_last_dataset > 0 || !datasets.length;
  const dsBox = h("div", { class: "seg", style: { gridAutoFlow: "row" } },
    radio("ds", "__new__", "把最新的标注打包成新数据集（推荐）", `现在共 ${S.accepted_annotations} 张已标注${datasets.length ? `，比 ${datasets[0].version} 多 ${S.new_since_last_dataset} 张` : ""}；按“${st.naming.dataset}”自动命名`, newOne),
    ...datasets.slice(0, 4).map((d, i) => radio("ds", d.version, `用已有的 ${d.version}`, `${d.image_total} 张图 · ${d.box_total} 个框 · 训练 ${d.counts?.train || 0} / 验证 ${d.counts?.val || 0} / 测试 ${d.counts?.test || 0}`, !newOne && i === 0)));
  const sizeBox = h("div", { class: "seg" },
    radio("size", "n", "极速", "最小最快，适合手机实时", tr.size === "n"),
    radio("size", "s", "均衡（推荐）", "速度和准确度兼顾", tr.size === "s"),
    radio("size", "m", "精准", "最准，适合服务器批量", tr.size === "m"));
  const presetBox = h("div", { class: "seg" },
    radio("preset", "quick", "试一下", `${st.presets.quick} 轮，几分钟`, tr.preset === "quick"),
    radio("preset", "standard", "标准（推荐）", `${st.presets.standard} 轮`, tr.preset === "standard"),
    radio("preset", "thorough", "充分", `${st.presets.thorough} 轮，最慢最稳`, tr.preset === "thorough"));
  const fromBox = h("div", { class: "seg" },
    radio("from", "latest", "接着最新模型训", hasLatest ? `从 ${ms.aliases.latest} 继续，学得快（主动学习推荐）` : "还没有“最新”模型，会自动改为从底座开始", hasLatest && tr.continue_from_latest),
    radio("from", "base", "从通用底座重新训", "数据变化很大、或想要干净的对比时用", !(hasLatest && tr.continue_from_latest)));
  const TV = [["tv:resnet50", "ResNet-50（非 YOLO，经典基线，医学/工业常用）"], ["tv:efficientnet_b0", "EfficientNet-B0（非 YOLO，小而准）"],
    ["tv:convnext_tiny", "ConvNeXt-Tiny（非 YOLO，精度高）"], ["tv:resnet18", "ResNet-18（非 YOLO，数据少时）"],
    ["tv:mobilenet_v3_large", "MobileNetV3（非 YOLO，手机友好）"], ["tv:vit_b_16", "ViT-B/16（非 YOLO，Transformer）"]];
  const famSel = h("select", { class: "select" }, h("option", { value: "yolo26" }, "YOLO26（最新，推荐）"), h("option", { value: "yolov8" }, "YOLOv8"),
    ...(task === "classify" ? TV.map(([v, t]) => h("option", { value: v }, t)) : []),
    ...(task === "segment" ? [["seg:deeplabv3_mobilenet_v3_large", "DeepLabV3-MobileNet（非 YOLO 语义分割，均衡）"], ["seg:deeplabv3_resnet50", "DeepLabV3-ResNet50（非 YOLO 语义分割，医学常用）"],
      ["seg:lraspp_mobilenet_v3_large", "LR-ASPP（非 YOLO 语义分割，最快）"], ["seg:fcn_resnet50", "FCN-ResNet50（非 YOLO 语义分割）"]].map(([v, t]) => h("option", { value: v }, t)) : [])); famSel.value = tr.family;
  const num = (v, min, max, step = 1) => h("input", { class: "input", type: "number", value: v ?? "", min, max, step });
  const adv = { imgsz: num(tr.imgsz, 160, 1600, 32), batch: num(tr.batch, 1, 64), patience: num(tr.patience, 0, 300), lr0: num(tr.lr0, 0.0001, 0.1, 0.0001), epochs: num("", 1, 1000) };
  const field = (label, el, help) => h("div", { class: "field" }, h("label", {}, label), el, help ? h("div", { class: "help" }, help) : null);
  const body = h("div", { class: "form" },
    field("1. 用哪批数据", dsBox),
    field("2. 模型大小", sizeBox),
    field("3. 训练强度", presetBox),
    field("4. 从哪里开始", fromBox),
    h("details", { class: "adv" }, h("summary", {}, "高级设置（一般不用改，默认值来自「项目设置」）"),
      h("div", { class: "grid k2", style: { marginTop: "10px" } },
        field("模型系列", famSel), field("图片尺寸", adv.imgsz, "越大越能看清小目标，但更慢；640 常用"),
        field("每批张数", adv.batch, "显存 8G 用 8；报显存不足就改小"), field("提前停止（轮）", adv.patience, "这么多轮没进步就停"),
        field("学习率 lr0（可空）", adv.lr0, "空 = 自动；接着训时可设 0.001"), field("轮数（可空）", adv.epochs, "填了就覆盖上面的训练强度")),
      h("div", { class: "small muted" }, "数据增强、数据划分比例、命名规则在 ", h("a", { href: "/settings" }, "项目设置"), " 里统一改。")),
    h("div", { class: "callout" }, "训练在服务器显卡上跑，期间可以继续在 Label Studio 标注。训练完会自动评估并设为「最新」；确认效果好，再在卡片上点「设为正式使用」。"));
  UI.modal("训练新模型", body, [{ label: "取消" }, { label: "开始训练", kind: "primary", run: async () => {
    const val = (n) => body.querySelector(`input[name=${n}]:checked`)?.value;
    const ds = val("ds"), size = val("size"), preset = val("preset"), from = val("from");
    if (famSel.value.startsWith("seg:")) {   // 非 YOLO 语义分割
      startSemseg(pid, { ds, arch: famSel.value.slice(4), preset, adv: Object.fromEntries(Object.entries(adv).map(([k, el]) => [k, el.value === "" ? null : Number(el.value)])) });
      return;
    }
    if (famSel.value.startsWith("tv:")) {   // 非 YOLO 经典分类网络
      if (ds === "__new__" && !S.accepted_annotations) throw new Error("还没有已复核的标注");
      startClassic(pid, { ds, arch: famSel.value.slice(3), preset, adv: Object.fromEntries(Object.entries(adv).map(([k, el]) => [k, el.value === "" ? null : Number(el.value)])) });
      return;
    }
    const base = bases.find((b) => b.family === famSel.value && b.size === size);
    if (!base?.ok) throw new Error(`服务器上还没有 ${famSel.value}${size}（${task === "segment" ? "分割" : task === "classify" ? "分类" : "检测"}）底座权重；请换一个系列或大小（分割、分类目前有 YOLO26 n / s）`);
    startTraining(pid, { ds, size, preset, from, base: base.path, adv: Object.fromEntries(Object.entries(adv).map(([k, el]) => [k, el.value === "" ? null : Number(el.value)])) });
  } }], { wide: true });
}

async function startTraining(pid, o) {
  const st = S.settings, tr = st.train;
  const panel = progressPanel("训练新模型");
  try {
    let ds = o.ds;
    if (ds === "__new__") {
      panel.step("打包最新标注为新数据集…");
      const pr = await UI.runTool(pid, "dataset.publish", { version: st.naming.dataset.replace("{project}", pid), val_fraction: st.split.val, test_fraction: st.split.test, seed: st.split.seed });
      ds = pr.version; panel.step(`数据集 ${ds}：训练 ${pr.counts?.train} / 验证 ${pr.counts?.val} / 测试 ${pr.counts?.test || 0}`);
    }
    const args = {
      dataset_version: ds, model_version: o.name || st.naming.model.replace("{size}", o.size), weights: o.base,
      epochs: o.adv.epochs || st.presets[o.preset], imgsz: o.adv.imgsz || tr.imgsz, batch: o.adv.batch || tr.batch,
      device: tr.device, workers: tr.workers, patience: o.adv.patience ?? tr.patience, ...(o.adv.lr0 ? { lr0: o.adv.lr0 } : {}),
      ...(o.from === "latest" ? { continue_from: "latest" } : {}), ...st.augment };
    panel.step(`开始训练（${args.epochs} 轮，模型 ${o.size}）…训练期间可以关掉这个窗口`);
    const t0 = Date.now(); let last = 0;
    const out = await UI.runTool(pid, "model.train", args, { onProgress: async () => {
      const mins = Math.round((Date.now() - t0) / 60000);
      if (Date.now() - last < 5000) return; last = Date.now();
      const g = await UI.api(`/api/projects/${pid}/train-progress`).catch(() => ({}));
      if (g.points?.length && g.updated * 1000 > t0 - 5000) {
        const lastPt = g.points[g.points.length - 1];
        panel.note(`第 ${g.epochs_done} / ${g.epochs_total || args.epochs} 轮 · 当前综合得分 ${UI.pct(lastPt.map50)} · 已用 ${mins} 分钟`);
        panel.chart(UI.lineChart([{ name: "综合得分 mAP50", color: "#1d6d52", points: g.points.map((p) => [p.epoch, p.map50]) },
          { name: "框得准", color: "#1d5fd1", points: g.points.map((p) => [p.epoch, p.precision]) },
          { name: "找得全", color: "#c2410c", points: g.points.map((p) => [p.epoch, p.recall]) }], { height: 180 }));
      } else panel.note(`准备数据、加载模型中… 已用 ${mins} 分钟`);
    } });
    panel.step(`训练完成：${out.version}，综合得分 ${UI.pct(SCORE(out.metrics))}`);
    await UI.runTool(pid, "model.alias", { alias: "latest", version: out.version });
    panel.step("已设为「最新」。确认效果后可在卡片上点「设为正式使用」。");
    panel.done(); render();
  } catch (e) { panel.fail(e.message); }
}

async function startSemseg(pid, o) {
  const st = S.settings;
  const panel = progressPanel("训练语义分割网络（非 YOLO）");
  try {
    let ds = o.ds;
    if (ds === "__new__") {
      panel.step("打包最新标注为新数据集…");
      const pr = await UI.runTool(pid, "dataset.publish", { version: st.naming.dataset.replace("{project}", pid), val_fraction: st.split.val, test_fraction: st.split.test, seed: st.split.seed });
      ds = pr.version; panel.step(`数据集 ${ds}：训练 ${pr.counts?.train} / 验证 ${pr.counts?.val}`);
    }
    const epochs = o.adv.epochs || Math.max(10, Math.round(st.presets[o.preset] / 3));
    panel.step(`开始训练 ${o.arch}（预训练 + 微调，${epochs} 轮）…`);
    const out = await UI.runTool(pid, "model.train_semseg", { dataset_version: ds, model_version: o.name || "{dataset}-" + o.arch.split("_")[0] + "-v{n}", arch: o.arch,
      epochs, imgsz: o.adv.imgsz && o.adv.imgsz <= 1024 ? o.adv.imgsz : 512, batch: o.adv.batch || 8, device: st.train.device, workers: st.train.workers });
    panel.step(`训练完成：${out.version}，平均交并比 mIoU ${UI.pct(out.metrics?.["metrics/mIoU"])}`);
    await UI.runTool(pid, "model.alias", { alias: "latest", version: out.version });
    panel.step("已设为「最新」。"); panel.done(); render();
  } catch (e) { panel.fail(e.message); }
}

async function startClassic(pid, o) {
  const st = S.settings;
  const panel = progressPanel("训练经典分类网络（非 YOLO）");
  try {
    let ds = o.ds;
    if (ds === "__new__") {
      panel.step("打包最新标注为新数据集…");
      const pr = await UI.runTool(pid, "dataset.publish", { version: st.naming.dataset.replace("{project}", pid), val_fraction: st.split.val, test_fraction: st.split.test, seed: st.split.seed });
      ds = pr.version; panel.step(`数据集 ${ds}：训练 ${pr.counts?.train} / 验证 ${pr.counts?.val} / 测试 ${pr.counts?.test || 0}`);
    }
    const epochs = o.adv.epochs || Math.max(10, Math.round(st.presets[o.preset] / 4));
    panel.step(`开始训练 ${o.arch}（ImageNet 预训练 + 微调，${epochs} 轮）…`);
    const out = await UI.runTool(pid, "model.train_classic", { dataset_version: ds, model_version: o.name || st.naming.model.replace("{size}", o.arch.replace(/_/g, "")), arch: o.arch,
      epochs, imgsz: 224, batch: o.adv.batch || 32, device: st.train.device, workers: st.train.workers });
    panel.step(`训练完成：${out.version}，准确率 ${UI.pct(out.metrics?.["metrics/accuracy_top1"])}`);
    await UI.runTool(pid, "model.alias", { alias: "latest", version: out.version });
    panel.step("已设为「最新」。"); panel.done(); render();
  } catch (e) { panel.fail(e.message); }
}

function progressPanel(title) {
  const list = h("ol", { class: "small", style: { margin: 0, paddingLeft: "18px" } }), note = h("div", { class: "muted small" }), chart = h("div");
  const close = UI.modal(title, h("div", {}, list, note, chart), [{ label: "在后台继续" }], { wide: true });
  return {
    step: (t) => list.append(h("li", {}, t)), note: (t) => { note.textContent = t; }, chart: (el) => chart.replaceChildren(el),
    done: () => { note.textContent = "✅ 全部完成"; }, fail: (m) => { list.append(h("li", { style: { color: "var(--danger)" } }, "❌ " + m)); },
    close };
}

// ── 评估 ──────────────────────────────────────────────────────────────────
async function evaluate(pid, m) {
  const { datasets } = await UI.api(`/api/projects/${pid}/hub/datasets`);
  if (!datasets.length) return UI.toast("还没有数据集", true);
  const dsSel = h("select", { class: "select" }, ...datasets.map((d) => h("option", { value: d.version }, `${d.version}（验证 ${d.counts?.val || 0} · 测试 ${d.counts?.test || 0}）`)));
  const split = h("div", { class: "seg" },
    h("label", {}, h("input", { type: "radio", name: "split", value: "val", checked: true }), h("b", {}, "验证集"), h("span", {}, "训练时没用过的图")),
    h("label", {}, h("input", { type: "radio", name: "split", value: "test" }), h("b", {}, "测试集"), h("span", {}, "完全独立，最能代表真实效果")));
  const body = h("div", { class: "form" }, h("div", { class: "field" }, h("label", {}, "用哪个数据集考试"), dsSel), h("div", { class: "field" }, h("label", {}, "考哪部分"), split),
    UI.pitfall(["不要用训练过的图来考，否则分数会虚高。", "不同模型要用同一个数据集、同一部分（最好是固定的考试集）来比才公平；数据集页可以「📌 固定为考试集」。",
      "加类别前训练的旧模型和新数据集类别不一样，没法拿来比。"]));
  UI.modal(`评估 ${m.version}`, body, [{ label: "取消" }, { label: "开始评估", kind: "primary", run: async () => {
    const sp = body.querySelector("input[name=split]:checked").value;
    const d = datasets.find((x) => x.version === dsSel.value);
    if (sp === "test" && !(d.counts?.test > 0)) throw new Error("这个数据集没有测试集；换验证集，或在项目设置里把测试比例设为 0.1 后重新打包");
    UI.toast("评估中…");
    const out = await UI.runTool(pid, "model.evaluate", { selector: m.version, dataset_version: dsSel.value, split: sp, device: S.settings.train.device });
    UI.toast(`评估完成：综合得分 ${UI.pct(SCORE(out.metrics))}`); render();
  } }]);
}

// ── 试一试 ────────────────────────────────────────────────────────────────
// 图片：上传一张识别一张。视频：服务器按间隔抽帧逐帧识别（什么格式都行），结果一帧一帧流回来；
// 浏览器能播放就边播边叠框，播不了就看抽出来的帧；时间轴可以点着跳。
const IMG_RE = /\.(jpe?g|png|bmp|webp|tiff?)$/i, VID_RE = /\.(mp4|mov|avi|mkv|m4v|webm|flv|wmv|mpe?g|3gp|ts)$/i;
const mmss = (t) => `${String(Math.floor(t / 60)).padStart(2, "0")}:${(t % 60).toFixed(1).padStart(4, "0")}`;

function tryIt(pid, m) {
  const cv = h("canvas", { style: { width: "100%", borderRadius: "10px", background: "#eef2f0", display: "none" } });
  const res = h("div", { class: "small" }), file = h("input", { type: "file", accept: "image/*,video/*", multiple: true, style: { display: "none" } });
  const conf = h("input", { type: "range", min: 0.05, max: 0.95, step: 0.05, value: S.settings.predict.confidence });
  const confV = h("b", {}, conf.value);
  const tile = h("input", { type: "checkbox", title: "高清大图里的小目标（远处锈点、裂纹）：切成小块分别识别再合并，慢一些" });
  const every = h("select", { class: "select", style: { width: "auto" } }, ...[["0.5", "每 0.5 秒"], ["1", "每 1 秒"], ["2", "每 2 秒"], ["5", "每 5 秒"]].map(([v, t]) => h("option", { value: v }, t)));
  every.value = "1";
  const maxF = h("select", { class: "select", style: { width: "auto" } }, ...[["60", "最多 60 帧"], ["120", "最多 120 帧"], ["300", "最多 300 帧"], ["600", "最多 600 帧"]].map(([v, t]) => h("option", { value: v }, t)));
  maxF.value = "120";
  const vidOpts = h("span", { class: "row small", style: { gap: "6px", display: "none" } }, "视频抽帧", every, maxF);
  const strip = h("div", { class: "row", style: { gap: "6px", flexWrap: "wrap" } });
  const vbox = h("div", { style: { display: "none" } });
  let items = [], cur = null, busy = false, xhr = null, vstate = null;
  const zone = h("div", { tabindex: 0, style: { border: "2px dashed var(--line-2)", borderRadius: "12px", padding: "26px 12px", textAlign: "center", color: "var(--muted)", cursor: "pointer", background: "var(--panel-2)", transition: "background .12s, border-color .12s" } },
    h("div", { style: { fontSize: "28px", lineHeight: 1 } }, "🖼 🎬"),
    h("div", { style: { marginTop: "6px", color: "var(--ink-2)", fontWeight: 600 } }, "把图片或视频拖到这里，或点击选择"),
    h("div", { class: "tiny" }, "图片：jpg / png / bmp / webp；视频：mp4 / mov / avi / mkv / webm 等。也可以 Ctrl+V 粘贴截图；一次拖多个，点下面的小图切换"));

  // ── 图片 ──
  const runImage = async () => {
    if (!cur || cur.kind !== "image" || busy) return;
    busy = true; res.textContent = "识别中…";
    try {
      const fd = new FormData(); fd.append("file", cur.file); fd.append("conf", conf.value); fd.append("tile", tile.checked ? "true" : "false");
      const r = await UI.api(`/api/projects/${pid}/models/${m.version}/try`, { method: "POST", body: fd });
      cv.style.display = "";
      await UI.drawBoxes(cv, cur.url, r.detections.map((d) => ({ xyxy: d.xyxy, poly: d.polygon, kpts: d.keypoints, label: `${d.label} ${(d.confidence * 100).toFixed(0)}%` })), { natural: [r.width, r.height] });
      res.replaceChildren(summary(r.detections, r.top), h("span", { class: "muted" }, `（${cur.file.name || "粘贴的图片"} · ${r.width}×${r.height} · ${r.ms} 毫秒${r.tiled ? "，切片推理" : ""}）`));
    } catch (e) { res.textContent = "识别失败：" + e.message; }
    finally { busy = false; }
  };
  function summary(dets, top) {
    const counts = {};
    for (const d of dets) counts[d.label] = (counts[d.label] || 0) + 1;
    return dets.length ? `找到 ${dets.length} 个目标：` + Object.entries(counts).map(([k, n]) => `${k} × ${n}`).join("、")
      : top?.length ? "分类结果：" + top.map((t) => `${t.label} ${UI.pct(t.confidence)}`).join("、") : "没有找到目标";
  }

  // ── 视频 ──
  function runVideo() {
    if (!cur || cur.kind !== "video") return;
    xhr?.abort();
    cv.style.display = "none"; vbox.style.display = "";
    const video = h("video", { src: cur.url, controls: true, muted: true, playsInline: true, style: { width: "100%", display: "block", borderRadius: "10px", background: "#000" } });
    const overlay = h("canvas", { style: { position: "absolute", left: 0, top: 0, width: "100%", height: "100%", pointerEvents: "none" } });
    const still = h("canvas", { style: { width: "100%", borderRadius: "10px", background: "#eef2f0", display: "none" } });
    const cap = h("div", { class: "small", style: { marginTop: "4px" } });
    const wrap = h("div", { style: { position: "relative" } }, video, overlay);
    const tl = h("div", { title: "每根竖条是一帧，越高目标越多；点一下跳到那里", style: { position: "relative", height: "54px", marginTop: "8px", borderRadius: "8px", background: "var(--panel-2)", border: "1px solid var(--line)", cursor: "pointer", overflow: "hidden" } });
    const cursor = h("i", { style: { position: "absolute", top: 0, bottom: 0, width: "2px", background: "#c2410c", left: "0" } });
    const prog = h("div", { class: "meter", style: { marginTop: "8px" } }, h("i", { style: { width: "0%" } }));
    const info = h("div", { class: "small", style: { marginTop: "6px" } }, "上传视频中…");
    const hits = h("div", { class: "row", style: { gap: "6px", flexWrap: "wrap", marginTop: "8px" } });
    vbox.replaceChildren(wrap, still, cap, tl, prog, info, hits);
    const st = vstate = { frames: [], meta: null, playable: null, video, done: false };
    video.addEventListener("loadedmetadata", () => { st.playable = video.videoWidth > 0; if (!st.playable) fallback(); });
    video.addEventListener("error", () => { st.playable = false; fallback(); });
    function fallback() { if (still.style.display === "") return; wrap.style.display = "none"; still.style.display = ""; if (st.frames.length) showFrame(st.frames.find((f) => f.detections.length) || st.frames[0]); }
    const color = (d) => PAL_TRY[[...new Set(st.frames.flatMap((f) => f.detections.map((x) => x.label)))].indexOf(d.label) % PAL_TRY.length] || PAL_TRY[0];
    const near = (t) => { let best = null; for (const f of st.frames) { if (f.t <= t + 0.05) best = f; else break; } return best && t - best.t <= (st.meta?.every_sec || 1) * 1.5 ? best : null; };
    function drawOverlay() {
      if (!st.playable) return;
      const f = near(video.currentTime);
      overlay.width = video.videoWidth; overlay.height = video.videoHeight;
      const g = overlay.getContext("2d"); g.clearRect(0, 0, overlay.width, overlay.height);
      cursor.style.left = (st.meta?.duration ? video.currentTime / st.meta.duration * 100 : 0) + "%";
      if (!f) return;
      const k = overlay.width / f.width, lw = Math.max(2, overlay.width / 300);
      g.lineWidth = lw; g.font = `${Math.max(14, overlay.width / 50)}px sans-serif`;
      for (const d of f.detections) {
        const c = color(d); g.strokeStyle = c;
        if (d.polygon?.length > 2) { g.beginPath(); d.polygon.forEach(([x, y], i) => (i ? g.lineTo(x * k, y * k) : g.moveTo(x * k, y * k))); g.closePath(); g.fillStyle = c + "44"; g.fill(); g.stroke(); }
        else g.strokeRect(d.xyxy[0] * k, d.xyxy[1] * k, (d.xyxy[2] - d.xyxy[0]) * k, (d.xyxy[3] - d.xyxy[1]) * k);
        const lab = `${d.label} ${(d.confidence * 100).toFixed(0)}%`, tw = g.measureText(lab).width + 8, th = parseInt(g.font) + 6;
        g.fillStyle = c; g.fillRect(d.xyxy[0] * k, Math.max(0, d.xyxy[1] * k - th), tw, th); g.fillStyle = "#fff"; g.fillText(lab, d.xyxy[0] * k + 4, Math.max(th - 6, d.xyxy[1] * k - 6));
      }
      if (!f.detections.length && f.top?.length) { g.fillStyle = "rgba(14,38,32,.8)"; g.fillRect(8, 8, overlay.width * 0.5, parseInt(g.font) + 12); g.fillStyle = "#fff"; g.fillText(`${f.top[0].label} ${UI.pct(f.top[0].confidence)}`, 16, parseInt(g.font) + 12); }
    }
    let raf = 0;
    const loop = () => { drawOverlay(); if (!video.paused && !video.ended) raf = requestAnimationFrame(loop); };
    video.addEventListener("play", () => { cancelAnimationFrame(raf); loop(); });
    video.addEventListener("seeked", drawOverlay); video.addEventListener("pause", drawOverlay);
    async function showFrame(f) {
      if (!f) return;
      cursor.style.left = (st.meta?.duration ? f.t / st.meta.duration * 100 : 0) + "%";
      if (st.playable) { video.currentTime = f.t; return; }
      try {
        const u = await UI.blobUrl(`/api/projects/${pid}/try-video/${st.meta.id}/${f.i}.jpg`);
        await UI.drawBoxes(still, u, f.detections.map((d) => ({ xyxy: d.xyxy, poly: d.polygon, kpts: d.keypoints, color: color(d), label: `${d.label} ${(d.confidence * 100).toFixed(0)}%` })), { natural: [f.width, f.height] });
        cap.textContent = `${mmss(f.t)}：${summary(f.detections, f.top)}（浏览器播放不了这个格式，显示的是服务器抽出来的帧）`;
      } catch (e) { UI.toast(e.message, true); }
    }
    tl.onclick = (e) => {
      if (!st.frames.length) return;
      const t = (e.offsetX / tl.clientWidth) * (st.meta.duration || st.frames[st.frames.length - 1].t || 1);
      let best = st.frames[0]; for (const f of st.frames) if (Math.abs(f.t - t) < Math.abs(best.t - t)) best = f;
      showFrame(best);
    };
    function drawTimeline() {
      const dur = st.meta.duration || (st.frames[st.frames.length - 1]?.t || 1), mx = Math.max(1, ...st.frames.map((f) => f.detections.length));
      const w = Math.max(2, 100 / Math.max(1, st.meta.planned) * 0.8);
      tl.replaceChildren(...st.frames.map((f) => h("i", { title: `${mmss(f.t)} · ${f.detections.length ? f.detections.length + " 个目标" : f.top?.[0] ? f.top[0].label : "没有目标"}`, style: {
        position: "absolute", bottom: 0, left: (f.t / dur * 100) + "%", width: `max(2px, ${w}%)`, height: (f.detections.length ? 20 + f.detections.length / mx * 80 : 8) + "%",
        background: f.detections.length ? "var(--brand-600)" : "var(--line-2)", borderRadius: "2px 2px 0 0" } })), cursor);
    }
    function finish(analysed) {
      st.done = true;
      const withObj = st.frames.filter((f) => f.detections.length), per = {};
      for (const f of st.frames) {
        const c = {}; for (const d of f.detections) c[d.label] = (c[d.label] || 0) + 1;
        for (const [k, n] of Object.entries(c)) { per[k] ??= { frames: 0, max: 0, first: f.t }; per[k].frames++; per[k].max = Math.max(per[k].max, n); }
      }
      const ms = st.frames.reduce((a, f) => a + f.ms, 0) / Math.max(1, st.frames.length);
      info.replaceChildren(h("div", {}, h("b", {}, `分析完成：${analysed} 帧（每 ${st.meta.every_sec} 秒一帧），${withObj.length} 帧里有目标。`),
        h("span", { class: "muted" }, ` 视频 ${mmss(st.meta.duration)} · ${st.meta.width}×${st.meta.height} · ${st.meta.fps} fps · 平均每帧 ${Math.round(ms)} 毫秒`)),
        Object.keys(per).length ? h("table", { class: "table", style: { marginTop: "8px" } }, h("tr", {}, h("th", {}, "类别"), h("th", { class: "num" }, "出现在几帧"), h("th", { class: "num" }, "同一画面最多几个"), h("th", {}, "第一次出现")),
          ...Object.entries(per).sort((a, b) => b[1].frames - a[1].frames).map(([k, v]) => h("tr", {}, h("td", {}, h("i", { style: { display: "inline-block", width: "10px", height: "10px", borderRadius: "3px", background: color({ label: k }), marginRight: "6px" } }), k),
            h("td", { class: "num" }, v.frames), h("td", { class: "num" }, v.max), h("td", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); showFrame(st.frames.find((f) => f.t === v.first)); } }, mmss(v.first)))))) : null,
        st.meta.planned < Math.floor((st.meta.duration || 0) / st.meta.every_sec) ? h("div", { class: "small muted" }, `只分析了前 ${analysed} 帧；想看完整段，把“最多帧数”调大，或把间隔调长。`) : null);
      hits.replaceChildren(...withObj.slice(0, 24).map((f) => {
        const im = h("img", { title: `${mmss(f.t)} · ${f.detections.length} 个目标`, style: { width: "96px", height: "60px", objectFit: "cover", borderRadius: "6px", cursor: "pointer", background: "#ddd" } });
        UI.blobUrl(`/api/projects/${pid}/try-video/${st.meta.id}/${f.i}.jpg`).then((u) => { im.src = u; }).catch(() => {});
        im.onclick = () => showFrame(f);
        return h("div", { style: { position: "relative" } }, im, h("span", { class: "badge", style: { position: "absolute", left: "3px", bottom: "3px", height: "18px", fontSize: "11px" } }, mmss(f.t)));
      }));
      if (withObj.length) hits.prepend(h("div", { class: "small muted", style: { width: "100%" } }, "有目标的帧（点一下跳过去）："));
    }

    // ① 断点续传上传 → ② 流式读取结果（NDJSON：meta / frame / done / error），断了自动重连、已分析的帧不重做
    const ctl = new AbortController(); let live = null;
    xhr = { abort: () => { ctl.abort(); live?.abort(); } };
    const it = cur, fail = (t) => info.replaceChildren(h("span", { style: { color: "var(--danger)" } }, "❌ " + t));
    (async () => {
      try {
        if (!it.uid) {
          it.uid = await resumableUpload(pid, it.file, ctl.signal, {
            progress: (got, size, speed) => { prog.firstChild.style.width = (got / size * 30) + "%";
              info.textContent = `上传视频中… ${Math.round(got / size * 100)}%（${UI.bytes(got)} / ${UI.bytes(size)}${speed ? ` · ${UI.bytes(speed)}/秒 · 还要约 ${Math.max(1, Math.round((size - got) / speed))} 秒` : ""}）`; },
            note: (t) => { info.textContent = t; } });
        }
        prog.firstChild.style.width = "30%";
        await analyse(it.uid);
      } catch (e) { if (!ctl.signal.aborted) fail(e.message); }
    })();

    async function analyse(uid) {
      for (let attempt = 0; ; attempt++) {
        if (ctl.signal.aborted) return;
        const r = await analyseOnce(uid);
        if (r === "ok" || ctl.signal.aborted) return;
        if (r !== "net" || attempt >= 20) { if (r === "net") fail("和服务器的连接一直断开。网络恢复后重新拖入同一个视频，已上传和已分析的部分都不会重做"); return; }
        const wait = Math.min(30, 2 ** Math.min(attempt + 1, 5));
        info.textContent = `连接断了，${wait} 秒后自动重连（第 ${attempt + 1} 次）… 已分析的 ${st.frames.length} 帧不用重做`;
        await sleepOrOnline(wait * 1000, ctl.signal);
      }
    }
    function analyseOnce(uid) {
      return new Promise((resolve) => {
        const fd = new FormData();
        fd.append("upload_id", uid); fd.append("conf", conf.value); fd.append("every_sec", every.value); fd.append("max_frames", maxF.value); fd.append("tile", tile.checked ? "true" : "false");
        const x = live = new XMLHttpRequest();
        x.open("POST", `/api/projects/${pid}/models/${m.version}/try-video`);
        const tk = UI.store.get("lota-token"); if (tk) x.setRequestHeader("Authorization", "Bearer " + tk);
        let seen = 0, gotDone = false, gotErr = false;
        const consume = () => {
          const txt = x.responseText, end = txt.lastIndexOf("\n");
          if (end < seen) return;
          for (const line of txt.slice(seen, end).split("\n")) {
            if (!line.trim()) continue;
            let o; try { o = JSON.parse(line); } catch { continue; }
            if (o.type === "meta") {
              st.meta = o; st.frames = [];   // 重连时服务器会先重放已分析的帧
              info.textContent = o.resumed ? `接着上次的进度：已分析 ${o.resumed} 帧，继续…` : `视频 ${mmss(o.duration)} · ${o.width}×${o.height}，准备分析 ${o.planned} 帧…`;
            } else if (o.type === "frame") {
              st.frames.push(o);
              prog.firstChild.style.width = (30 + st.frames.length / Math.max(1, st.meta.planned) * 70) + "%";
              if (!o.cached) info.textContent = `已分析 ${st.frames.length} / ${st.meta.planned} 帧（${mmss(o.t)}）· 这一帧：${summary(o.detections, o.top)}`;
              if (st.playable === false && st.frames.length === 1) showFrame(o);
              if (st.playable && video.paused) drawOverlay();
            } else if (o.type === "done") { gotDone = true; prog.firstChild.style.width = "100%"; drawTimeline(); finish(o.analysed); }
            else if (o.type === "error") { gotErr = true; fail(o.detail); }
          }
          if (st.meta) drawTimeline();
          seen = end + 1;
        };
        x.onprogress = consume;
        x.onload = () => {
          if (x.status >= 300) {
            let d = x.responseText; try { d = JSON.parse(d).detail; } catch {}
            if (x.status >= 500 || x.status === 0) return resolve("net");
            if (/上传|传完/.test(String(d))) it.uid = null;   // 服务器上的视频过期了：下次重新上传
            fail(x.status === 401 ? "口令无效，请刷新页面重新输入" : d); return resolve("fatal");
          }
          consume();
          resolve(gotDone || gotErr ? "ok" : "net");   // 流没走完就断了 → 重连续跑
        };
        x.onerror = () => resolve("net");
        x.onabort = () => resolve("ok");
        x.send(fd);
      });
    }
  }

  // 断点续传：同一个文件（名称 + 大小 + 修改时间）得到同一个上传编号；每块 4MB，失败自动重试，
  // 页面关了再拖同一个文件进来，也会从服务器已收到的地方继续
  async function resumableUpload(pid, f, signal, { progress, note }) {
    const CH = 4 * 1024 * 1024;
    const s = await UI.api(`/api/projects/${pid}/uploads`, { method: "POST", body: { name: f.name || "video.mp4", size: f.size, fingerprint: `${f.name}|${f.size}|${f.lastModified}` } });
    let off = s.received, fails = 0, t0 = Date.now(), b0 = off;
    if (s.complete) { note("这个视频之前已经传完，直接分析"); return s.id; }
    if (off > 0) note(`上次传到 ${Math.round(off / f.size * 100)}%，从断点继续…`);
    while (off < f.size) {
      if (signal.aborted) throw new Error("已取消");
      try {
        const r = await UI.rawFetch(`/api/projects/${pid}/uploads/${s.id}?offset=${off}`, { method: "PUT", body: f.slice(off, off + CH), signal, headers: { "Content-Type": "application/octet-stream" } });
        const d = await r.json().catch(() => ({}));
        if (r.status === 409) { off = d.detail?.received ?? off; continue; }      // 服务器收到的和我们以为的不一样：以服务器为准
        if (r.status >= 400 && r.status < 500) { const e = new Error(typeof d.detail === "string" ? d.detail : "上传被拒绝"); e.fatal = true; throw e; }
        if (!r.ok) throw new Error("服务器暂时出错");
        off = d.received; fails = 0;
        const sec = (Date.now() - t0) / 1000;
        progress(off, f.size, sec > 1 ? (off - b0) / sec : 0);
      } catch (e) {
        if (signal.aborted || e.fatal) throw e;
        fails++;
        if (fails > 60) throw new Error(`网络一直连不上，上传已暂停（已传 ${UI.bytes(off)}，不会丢）。网络恢复后重新拖入同一个视频，会从断点继续`);
        const wait = Math.min(30, 2 ** Math.min(fails, 5));
        note(`网络中断，${wait} 秒后自动重试（第 ${fails} 次）… 已传的 ${UI.bytes(off)}（${Math.round(off / f.size * 100)}%）不会丢，网络一恢复会马上继续`);
        await sleepOrOnline(wait * 1000, signal);
        try { off = (await UI.api(`/api/projects/${pid}/uploads/${s.id}`)).received; t0 = Date.now(); b0 = off; } catch {}
      }
    }
    return s.id;
  }
  function sleepOrOnline(ms, signal) {   // 等一会儿；网络一恢复（online 事件）就提前醒
    return new Promise((res) => {
      const done = () => { clearTimeout(t); window.removeEventListener("online", done); signal?.removeEventListener("abort", done); res(); };
      const t = setTimeout(done, ms); window.addEventListener("online", done); signal?.addEventListener("abort", done);
    });
  }

  const pick = (it) => {
    cur = it;
    [...strip.children].forEach((x) => { x.style.outline = x._it === it ? "2px solid var(--brand-600)" : "none"; });
    vidOpts.style.display = items.some((x) => x.kind === "video") ? "" : "none";
    if (it.kind === "video") { res.textContent = ""; runVideo(); }
    else { xhr?.abort(); vbox.style.display = "none"; vbox.replaceChildren(); runImage(); }
  };
  const add = (list) => {
    const got = [...list].map((f) => ({ file: f, url: URL.createObjectURL(f),
      kind: /^video\//.test(f.type) || VID_RE.test(f.name || "") ? "video" : /^image\//.test(f.type) || IMG_RE.test(f.name || "") ? "image" : "" })).filter((x) => x.kind);
    if (!got.length) return UI.toast("只支持图片（jpg / png / bmp / webp）和视频（mp4 / mov / avi / mkv / webm 等）", true);
    items = [...items, ...got].slice(-24);
    strip.replaceChildren(...(items.length > 1 ? items.map((it) => {
      const el = it.kind === "image" ? h("img", { src: it.url, title: it.file.name, style: { width: "56px", height: "56px", objectFit: "cover", borderRadius: "8px", cursor: "pointer" } })
        : h("div", { title: it.file.name, style: { width: "56px", height: "56px", borderRadius: "8px", cursor: "pointer", background: "var(--brand-900)", color: "#fff", display: "grid", placeItems: "center", fontSize: "10px", textAlign: "center", overflow: "hidden", padding: "2px" } },
          h("div", {}, h("div", { style: { fontSize: "18px" } }, "🎬"), (it.file.name || "").slice(0, 10)));
      el._it = it; el.onclick = () => pick(it); return el;
    }) : []));
    pick(got[0]);
  };
  zone.onclick = () => file.click();
  zone.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") file.click(); };
  file.onchange = () => { add(file.files); file.value = ""; };
  const hot = (on) => { zone.style.background = on ? "var(--brand-50)" : "var(--panel-2)"; zone.style.borderColor = on ? "var(--brand-500)" : "var(--line-2)"; };
  const body = h("div", { class: "form" },
    h("div", { class: "row" }, h("span", { class: "small muted" }, "最低置信度"), conf, confV, h("label", { class: "row small" }, tile, "切片推理（小目标）"), vidOpts),
    zone, file, strip, cv, vbox, res);
  body.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; hot(true); });
  body.addEventListener("dragleave", (e) => { if (!body.contains(e.relatedTarget)) hot(false); });
  body.addEventListener("drop", (e) => { e.preventDefault(); hot(false); if (e.dataTransfer.files.length) add(e.dataTransfer.files); });
  const paste = (e) => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); add(fs); } };
  const block = (e) => e.preventDefault();   // 拖到对话框外面也不要让浏览器把文件打开
  document.addEventListener("paste", paste); document.addEventListener("dragover", block); document.addEventListener("drop", block);
  const rerun = () => { if (cur?.kind === "video") runVideo(); else runImage(); };
  conf.oninput = () => { confV.textContent = conf.value; }; conf.onchange = rerun; tile.onchange = rerun; every.onchange = rerun; maxF.onchange = rerun;
  const close = UI.modal(`试一试 ${m.version}`, body, [], { wide: true });
  new MutationObserver((_, ob) => {
    if (!body.isConnected) {
      xhr?.abort(); vstate?.video?.pause();
      document.removeEventListener("paste", paste); document.removeEventListener("dragover", block); document.removeEventListener("drop", block); ob.disconnect();
    }
  }).observe(document.body, { childList: true });
  return close;
}
const PAL_TRY = ["#2f9e78", "#1d5fd1", "#e8590c", "#7c3aed", "#d9922b", "#0e7490", "#be185d", "#4d7c0f"];

// ── 导出 ──────────────────────────────────────────────────────────────────
function exportDlg(pid, m) {
  const radio = (v, t, d, c) => h("label", {}, h("input", { type: "radio", name: "fmt", value: v, checked: c }), h("b", {}, t), h("span", {}, d));
  const box = h("div", { class: "seg", style: { gridAutoFlow: "row" } },
    radio("tflite_int8", "安卓手机（推荐）", "LiteRT / TFLite INT8 量化，体积约 1/4，用 Ultralytics 官方 Flutter 插件或 LiteRT 直接跑", true),
    radio("tflite", "安卓手机（不量化）", "TFLite FP32，精度和电脑上一致，体积大一些"),
    radio("ncnn", "安卓 / ARM 设备（NCNN）", "腾讯 NCNN，打包为 zip，适合自己写安卓 App"),
    radio("onnx", "通用格式（ONNX）", "电脑、服务器、其他推理框架通用"));
  UI.modal(`导出 ${m.version}`, h("div", { class: "form" }, box,
    h("div", { class: "callout warn" }, "量化（INT8）后精度会略降，导出后建议在手机上用几张真实照片核对一下。")),
    [{ label: "取消" }, { label: "开始导出", kind: "primary", run: async () => {
      const fmt = document.querySelector("input[name=fmt]:checked").value;
      UI.toast("导出中，约 10–60 秒…");
      const out = await UI.runTool(pid, "model.export", { version: m.version, format: fmt, imgsz: S.settings.export.imgsz });
      UI.toast(`导出完成：${out.file}（${UI.bytes(out.size)}）`); download(pid, m.version, out.file); render();
    } }]);
}

// ── 最近的任务 ────────────────────────────────────────────────────────────
const KIND = { "model.train": "训练", "model.evaluate": "评估", "model.export": "导出", "model.alias": "切换别名", "dataset.publish": "打包数据集", "model.predict": "批量推理" };
let TITLES = null;
async function loadJobs() {
  clearTimeout(jobsTimer);
  if (!TITLES) { TITLES = {}; try { for (const t of await UI.api("/api/tools")) TITLES[t.id] = t.title; } catch {} }
  const box = document.getElementById("jobs"); if (!box) return;
  const pid = UI.project();
  let jobs = await UI.api(`/api/projects/${pid}/jobs`).catch(() => []);
  jobs = (Array.isArray(jobs) ? jobs : jobs.jobs || []).slice(0, 8);
  const st = { queued: ["排队", "badge"], running: ["运行中", "badge info"], succeeded: ["完成", "badge ok"], failed: ["失败", "badge danger"], interrupted: ["中断", "badge warn"], cancelled: ["取消", "badge"], paused: ["暂停", "badge"] };
  box.replaceChildren(jobs.length ? h("table", { class: "table" },
    h("tr", {}, h("th", {}, "任务"), h("th", {}, "状态"), h("th", {}, "时间"), h("th", {}, "说明")),
    ...jobs.map((j) => h("tr", {}, h("td", {}, KIND[j.kind] || TITLES[j.kind] || (j.kind.startsWith("workflow:") ? "工作流 " + j.kind.slice(9) : j.kind)),
      h("td", {}, h("span", { class: (st[j.status] || ["", "badge"])[1] }, (st[j.status] || [j.status])[0])),
      h("td", { class: "small muted nowrap" }, UI.when(j.updated || j.created)), h("td", { class: "small" }, j.error ? h("span", { style: { color: "var(--danger)" } }, j.error.slice(0, 160)) : ""))))
    : h("div", { class: "empty" }, "还没有任务"));
  if (jobs.some((j) => ["queued", "running"].includes(j.status))) jobsTimer = setTimeout(() => { loadJobs(); }, 5000);
}


// ── 模型 A/B 对比：同一个数据集、同一部分上考试，并排看成绩和差距 ─────────────────────────
async function compareDlg() {
  const pid = UI.project();
  const [ms, { datasets }] = await Promise.all([UI.api(`/api/projects/${pid}/hub/models`), UI.api(`/api/projects/${pid}/hub/datasets`)]);
  if (ms.models.length < 2) return UI.toast("至少要有两个模型才能对比", true);
  if (!datasets.length) return UI.toast("还没有数据集", true);
  const opt = (m) => h("option", { value: m.version }, m.version + (m.aliases.length ? `（${m.aliases.map((a) => ALIAS_TEXT[a] || a).join("、")}）` : ""));
  const a = h("select", { class: "select" }, ...ms.models.map(opt)), b = h("select", { class: "select" }, ...ms.models.map(opt));
  const prod = ms.models.find((m) => m.aliases.includes("production"));
  a.value = prod ? prod.version : ms.models[1].version; b.value = ms.models.find((m) => m.version !== a.value).version;
  const ds = h("select", { class: "select" }, ...datasets.map((d) => h("option", { value: d.version }, `${d.version}（验证 ${d.counts?.val || 0} · 测试 ${d.counts?.test || 0}）`)));
  const sp = h("select", { class: "select" }, h("option", { value: "test" }, "测试集（推荐，最公平）"), h("option", { value: "val" }, "验证集"));
  if (!(datasets[0].counts?.test > 0)) sp.value = "val";
  const out = h("div");
  const f = (l, el) => h("div", { class: "field" }, h("label", {}, l), el);
  UI.modal("模型 A/B 对比", h("div", { class: "form" },
    h("div", { class: "grid k2" }, f("模型 A（一般选正式使用的）", a), f("模型 B（新训练的）", b)),
    h("div", { class: "grid k2" }, f("用哪个数据集考", ds), f("考哪部分", sp)),
    h("div", { class: "callout" }, "两个模型在同一份数据上考试才公平；不要用其中一个模型训练过的数据（训练集）来比。"), out),
    [{ label: "关闭" }, { label: "开始对比", kind: "primary", run: async () => {
      if (a.value === b.value) throw new Error("请选两个不同的模型");
      const d = datasets.find((x) => x.version === ds.value);
      if (sp.value === "test" && !(d.counts?.test > 0)) throw new Error("这个数据集没有测试集，改选验证集");
      out.replaceChildren(h("div", { class: "muted small" }, "考试中…（两个模型依次评估）"));
      const res = {};
      for (const v of [a.value, b.value]) res[v] = (await UI.runTool(pid, "model.evaluate", { selector: v, dataset_version: ds.value, split: sp.value, device: S.settings.train.device })).metrics || {};
      const cls = (ms.models.find((m) => m.version === a.value) || {}).task === "classify";
      const rows = cls ? [["准确率 Top-1", "metrics/accuracy_top1"], ["前五名命中 Top-5", "metrics/accuracy_top5"]]
        : [["综合得分 mAP50", "metrics/mAP50(B)"], ["严格得分 mAP50-95", "metrics/mAP50-95(B)"], ["框得准（精确率）", "metrics/precision(B)"], ["找得全（召回率）", "metrics/recall(B)"]];
      const main = rows[0][1], diff = (res[b.value][main] ?? 0) - (res[a.value][main] ?? 0);
      out.replaceChildren(h("table", { class: "table" },
        h("tr", {}, h("th", {}, "指标"), h("th", { class: "num" }, "A · " + a.value), h("th", { class: "num" }, "B · " + b.value), h("th", { class: "num" }, "B − A")),
        ...rows.map(([t, k]) => { const va = res[a.value][k], vb = res[b.value][k], dd = vb != null && va != null ? vb - va : null;
          return h("tr", {}, h("td", {}, t), h("td", { class: "num" }, UI.pct(va, 1)), h("td", { class: "num" }, UI.pct(vb, 1)),
            h("td", { class: "num", style: { color: dd > 0 ? "var(--ok)" : dd < 0 ? "var(--danger)" : "" } }, dd == null ? "—" : (dd > 0 ? "+" : "") + (dd * 100).toFixed(1))); })),
        h("div", { class: "callout " + (diff > 0.02 ? "ok" : diff < -0.02 ? "warn" : ""), style: { marginTop: "10px" } },
          diff > 0.02 ? `B 比 A 高 ${(diff * 100).toFixed(1)} 分，建议把 ${b.value} 设为正式使用。` :
          diff < -0.02 ? `B 比 A 低 ${(-diff * 100).toFixed(1)} 分，建议继续用 ${a.value}。` : "两个模型差不多（相差不到 2 分）；可以看速度、大小再决定。"),
        diff > 0.02 && !ms.models.find((m) => m.version === b.value).aliases.includes("production")
          ? h("button", { class: "btn primary", style: { marginTop: "8px" }, onclick: () => setAlias(pid, "production", b.value) }, `把 ${b.value} 设为正式使用`) : null);
      render();
      return false;   // 保持窗口打开看结果
    } }], { wide: true });
}


// ── 训练图表 + 按类别成绩 ───────────────────────────────────────────────────
async function plotsDlg(pid, m) {
  const d = await UI.api(`/api/projects/${pid}/models/${m.version}/plots`);
  const view = h("div", { style: { minHeight: "320px" } }), tabs = h("div", { class: "tabs" });
  const show = async (it) => {
    if (it === "per_class") {
      const rows = d.per_class;
      view.replaceChildren(rows.length ? h("div", {},
        h("p", { class: "small muted" }, "从低到高排：最上面的类别最弱，优先给它多标数据、检查标注是否一致。"),
        h("table", { class: "table" }, h("tr", {}, h("th", {}, "类别"), h("th", {}, "综合得分 AP50"), h("th", { class: "num" }, "框得准"), h("th", { class: "num" }, "找得全")),
          ...rows.map((r) => h("tr", {}, h("td", { class: "mono" }, r.class),
            h("td", {}, h("div", { class: "score", style: { gridTemplateColumns: "1fr 54px", margin: 0 } },
              h("div", { class: "meter" + (r.ap50 >= 0.7 ? "" : r.ap50 >= 0.45 ? " warn" : " danger") }, h("i", { style: { width: Math.round((r.ap50 || 0) * 100) + "%" } })),
              h("span", { class: "v" }, UI.pct(r.ap50)))),
            h("td", { class: "num" }, UI.pct(r.precision)), h("td", { class: "num" }, UI.pct(r.recall))))))
        : h("div", { class: "empty" }, "这个模型没有按类别的成绩（训练于本功能上线前，或是分类模型）。重新评估一次就会有。"));
      return;
    }
    view.replaceChildren(h("div", { class: "muted small" }, "加载中…"));
    const u = await UI.blobUrl(`/api/projects/${pid}/models/${m.version}/plots/${it.name}`);
    view.replaceChildren(h("img", { src: u, style: { width: "100%", borderRadius: "10px", border: "1px solid var(--line)" } }));
  };
  const items = [...d.plots, { name: "per_class", title: "按类别成绩" }];
  items.forEach((it, i) => {
    const b = h("button", { class: i === 0 ? "on" : "" }, it.title.replace(/（.*）/, ""));
    b.title = it.title;
    b.onclick = () => { [...tabs.children].forEach((x) => x.classList.toggle("on", x === b)); show(it.name === "per_class" ? "per_class" : it); };
    tabs.append(b);
  });
  UI.modal(`${m.version} 的训练图表`, h("div", {}, tabs, view,
    h("div", { class: "callout small", style: { marginTop: "10px" } }, "怎么看：混淆矩阵对角线越亮越好，非对角线亮的格子 = 容易认错的类别；PR 曲线越靠右上越好；F1 曲线最高点对应的置信度是推荐阈值；验证集预测和人工标注对照着看，能直接看出漏检和误检。")), [], { wide: true });
  if (items.length) show(items[0].name === "per_class" ? "per_class" : items[0]);
}


// ── 导入外部模型（别处训练好的 Ultralytics .pt）──────────────────────────────────
function importDlg() {
  const pid = UI.project(), info = UI.projectInfo(pid)?.config || {};
  const file = h("input", { type: "file", accept: ".pt" });
  const ver = h("input", { class: "input", value: "import-" + new Date().toISOString().slice(0, 10).replace(/-/g, "") + "-v1" });
  const note = h("input", { class: "input", placeholder: "如：旧版检测模型 yolov8s-old" });
  const prog = h("div", { class: "small muted" });
  UI.modal("导入外部模型", h("div", { class: "form" },
    h("div", { class: "field" }, h("label", {}, "权重文件（.pt）"), file, h("div", { class: "help" }, `必须是 Ultralytics 模型，任务类型 = ${info.task}，类别 = ${(info.classes || []).join("、")}（顺序也要一致）`)),
    h("div", { class: "grid k2" }, h("div", { class: "field" }, h("label", {}, "版本名"), ver), h("div", { class: "field" }, h("label", {}, "备注"), note)), prog,
    h("div", { class: "callout" }, "导入后在模型卡片上「评估」一次，就能和其他模型公平比较；满意再「设为正式使用」。")),
    [{ label: "取消" }, { label: "导入", kind: "primary", run: () => new Promise((resolve, reject) => {
      if (!file.files[0]) return reject(new Error("先选择 .pt 文件"));
      const fd = new FormData(); fd.append("file", file.files[0]); fd.append("version", ver.value.trim()); fd.append("note", note.value);
      const xhr = new XMLHttpRequest(); xhr.open("POST", `/api/projects/${pid}/models/import`);
      const t = UI.store.get("lota-token"); if (t) xhr.setRequestHeader("Authorization", "Bearer " + t);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) prog.textContent = `上传中 ${Math.round(e.loaded / e.total * 100)}%（之后会检查任务类型和类别）`; };
      xhr.onload = () => { if (xhr.status < 300) { UI.toast("已导入 " + ver.value); render(); resolve(); } else { let m = xhr.responseText; try { m = JSON.parse(m).detail; } catch {} reject(new Error(m)); } };
      xhr.onerror = () => reject(new Error("上传失败"));
      xhr.send(fd);
    }) }]);
}

(async () => {
  await UI.nav("models"); UI.onProject(render);
  await render().catch((e) => UI.toast(e.message, true));
  const ds = new URLSearchParams(location.search).get("train");   // 从数据集页「用它训练」跳过来
  if (ds && S) trainWizard(ds).catch((e) => UI.toast(e.message, true));
})();
