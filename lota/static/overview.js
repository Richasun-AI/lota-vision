// 总览：所有子项目一眼看清（图片、标注、Label Studio 进度、模型成绩、质检），并给出“下一步该做什么”
"use strict";
const { h } = UI;
const TASK = { detect: "目标检测", segment: "实例分割", classify: "图像分类" };

async function render() {
  const app = document.getElementById("app");
  const list = await UI.api("/api/overview");
  const total = (k) => list.reduce((a, p) => a + (p[k] || 0), 0);
  const fails = list.reduce((a, p) => a + (p.inspections?.fail || 0), 0);
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "总览"), h("p", {}, "所有子项目的进度一眼看清，每个项目都给出“下一步该做什么”。一个子项目 = 一个识别场景（如杂草、空开）。")),
      h("div", { class: "row" }, h("a", { class: "btn", href: "/developers" }, "开发者 API"), h("a", { class: "btn", href: "/flow" }, "打开工作流"),
        h("button", { class: "btn primary lg", onclick: newProject }, "＋ 新建子项目"))),
    h("div", { class: "card", style: { marginBottom: "14px" } },
      h("div", { class: "small muted", style: { marginBottom: "6px" } }, "完整流程（每一步都可以在工作流里一键跑）"),
      h("div", { class: "flowline" }, ...["原始照片", "抽图分类 / 用权重找图", "Label Studio 标注", "回收打包数据集", "训练模型", "模型中心：评估 / 设为正式", "智能质检 / 导出到手机"]
        .flatMap((t, i) => i ? ["→", h("b", {}, t)] : [h("b", {}, t)]))),
    h("div", { id: "sys", class: "card", style: { marginBottom: "14px", padding: "10px 16px" } }, h("span", { class: "small muted" }, "读取系统状态…")),
    h("div", { class: "grid k4", style: { marginBottom: "14px" } },
      kpi("子项目", list.length, `${list.filter((p) => p.active).length} 个已启用`),
      kpi("图片", total("images").toLocaleString(), "已登记进各子项目"),
      kpi("已复核标注", total("accepted_annotations").toLocaleString(), "可用于训练"),
      kpi("质检不合格", fails, "最近 2000 条质检记录里")),
    h("div", { class: "grid k2" }, list.map(projCard)));
}
async function loadSys() {
  const box = document.getElementById("sys"); if (!box) return;
  const s = await UI.api("/api/system").catch(() => null); if (!s) return;
  const g = s.gpu[0];
  const pct = (a, b) => Math.round(a / b * 100);
  const meter = (p) => h("div", { class: "meter" + (p > 90 ? " danger" : p > 75 ? " warn" : ""), style: { width: "90px", display: "inline-block", verticalAlign: "middle", margin: "0 6px" } }, h("i", { style: { width: p + "%" } }));
  box.replaceChildren(h("div", { class: "row", style: { gap: "18px" } },
    h("b", { class: "small" }, "系统状态"),
    g ? h("span", { class: "small" }, `🎮 ${g.name.replace("NVIDIA GeForce ", "")} 显存`, meter(pct(g.mem_used, g.mem_total)), `${(g.mem_used / 1024).toFixed(1)}/${(g.mem_total / 1024).toFixed(0)} GB · 占用 ${g.util}% · ${g.temp}℃`) : h("span", { class: "small muted" }, "没有检测到显卡"),
    ...s.disks.map((d) => h("span", { class: "small" }, `💾 ${d.label}`, meter(pct(d.used, d.total)), `剩 ${(d.free / 1e9).toFixed(0)} GB`)),
    h("span", { class: "small" }, s.jobs.length ? h("span", { class: "badge info" }, `⏳ ${s.jobs.length} 个任务在跑：` + s.jobs.slice(0, 3).map((j) => `${j.project}/${j.kind}`).join("、")) : h("span", { class: "badge ok" }, "空闲")),
    h("a", { class: "small", href: "/m", target: "_blank" }, "📱 手机拍照即检 →")));
  setTimeout(loadSys, 15000);
}

function kpi(label, value, hint) { return h("div", { class: "card kpi" }, h("span", { class: "label" }, label), h("span", { class: "value" }, value), h("span", { class: "hint" }, hint)); }

function projCard(p) {
  const score = p.production?.metrics?.["metrics/mAP50(B)"];
  const ls = p.ls;
  const go = (path) => `${path}?project=${encodeURIComponent(p.id)}`;
  const pick = () => { UI.store.set("lota-project", p.id); };
  const lsLine = !ls ? h("span", { class: "muted" }, "未连接")
    : ls.error ? h("span", { style: { color: "var(--danger)" } }, "连不上：" + ls.error)
    : h("span", {}, `${ls.annotated}/${ls.tasks} 已标，已回收 ${ls.pulled ?? 0}`, " · ", h("a", { href: ls.url, target: "_blank" }, "打开 →"), " · ",
        ls.auto_pull ? h("span", { class: "badge ok", title: "在 Label Studio 一提交标注，引擎就自动回收" }, "⚡ 自动回收已开启")
          : h("button", { class: "btn sm", onclick: async (e) => { e.target.disabled = true; try { await UI.runTool(p.id, "labels.ls_webhook", { enabled: true }); UI.toast("已开启自动回收"); render(); } catch (err) { UI.toast(err.message, true); e.target.disabled = false; } } }, "开启自动回收"));
  return h("div", { class: "card proj hover" },
    h("div", { class: "head" },
      h("div", {}, h("h3", {}, p.name || p.id), h("div", { class: "sub" }, h("span", { class: "mono" }, p.id), " · ", TASK[p.task] || p.task, " · 类别：", (p.classes || []).slice(0, 6).join("、"), (p.classes || []).length > 6 ? "…" : "")),
      p.kind === "official" ? h("span", { class: "badge violet" }, "官方模型库") : h("span", { class: "badge " + (p.active ? "ok" : "") }, p.active ? "已启用" : "未启用")),
    h("div", { class: "stats" },
      st(p.images.toLocaleString(), "图片"), st(p.accepted_annotations, "已复核标注"), st(p.datasets, "数据集版本"),
      st(score != null ? UI.pct(score) : "—", p.production ? "正式模型 " + p.production.version : "还没有正式模型")),
    h("div", { class: "small" }, h("b", {}, "Label Studio："), lsLine),
    h("div", { class: "small" }, h("b", {}, "质检："), p.pipelines ? `${p.pipelines} 条规则` : "没有规则",
      Object.keys(p.inspections || {}).length ? ` · 不合格 ${p.inspections.fail || 0}、需注意 ${p.inspections.warn || 0}、合格 ${p.inspections.pass || 0}` : ""),
    h("div", {}, h("div", { class: "small muted", style: { marginBottom: "6px" } }, "下一步建议"),
      h("div", { class: "grid", style: { gap: "6px" } }, ...p.suggestions.map((t) =>
        h("div", { class: "tip " + t.level }, h("span", {}, t.text), t.href ? h("a", { class: "btn sm", href: t.href, target: t.href.startsWith("http") ? "_blank" : null, onclick: pick }, t.label + " →") : null)))),
    h("div", { class: "row", style: { borderTop: "1px solid var(--line)", paddingTop: "10px" } },
      ...[["/flow", "工作流"], ["/datasets", "数据集"], ["/models", "模型"], ["/inspect", "质检"], ["/settings", "设置"]].map(([u, t]) =>
        h("a", { class: "btn sm ghost", href: go(u), onclick: pick }, t))));
}
function st(v, l) { return h("div", { class: "stat" }, h("b", {}, v), h("span", {}, l)); }


// ── 新建子项目向导（行业场景模板）────────────────────────────────────────────
function newProject() {
  let domain = DOMAINS[0][0], picked = null;
  const tabs = h("div", { class: "tabs" }), grid = h("div", { class: "grid k3" }), form = h("div", { class: "form", style: { marginTop: "12px" } });
  const draw = () => {
    const cards = SCENARIOS.filter((s) => s.domain === domain).map((s) => {
      const card = h("div", { class: "card hover", style: { cursor: "pointer", boxShadow: "none", borderColor: picked === s ? "var(--brand-600)" : "" } },
        h("div", { class: "row between" }, h("b", {}, s.name), h("span", { class: "badge brand" }, TASK_TEXT[s.task].split("（")[0])),
        h("div", { class: "small muted", style: { margin: "4px 0" } }, s.classes.map((c) => c[1]).join("、")), h("div", { class: "tiny muted" }, s.tip));
      card.onclick = () => { picked = s; draw(); fill(s); };
      return card;
    });
    const blank = h("div", { class: "card hover", style: { cursor: "pointer", boxShadow: "none", borderStyle: "dashed" } }, h("b", {}, "＋ 空白子项目"), h("div", { class: "small muted" }, "自己定任务类型和类别"));
    blank.onclick = () => { picked = { id: "", name: "", task: "detect", classes: [], aug: "std", size: "s", tip: "" }; draw(); fill(picked); };
    grid.replaceChildren(...cards, blank);
  };
  const fill = (s) => {
    const id = h("input", { class: "input", value: s.id, placeholder: "小写字母、数字、短横线，如 pcb-defect" });
    const name = h("input", { class: "input", value: s.name, placeholder: "如：PCB 缺陷检测" });
    const task = h("select", { class: "select" }, ...Object.entries(TASK_TEXT).map(([k, t]) => h("option", { value: k }, t))); task.value = s.task;
    const cls = h("textarea", { class: "input", style: { minHeight: "110px", fontFamily: "var(--mono)" } }, s.classes.map((c) => c.join(", ")).join("\n"));
    form.replaceChildren(
      h("div", { class: "grid k2" }, h("div", { class: "field" }, h("label", {}, "子项目代码"), id, h("div", { class: "help" }, "建好后不能改；用在网址和接口里")),
        h("div", { class: "field" }, h("label", {}, "名称"), name)),
      h("div", { class: "field" }, h("label", {}, "任务类型"), task, h("div", { class: "help" }, "检测 = 画框找位置；分割 = 画轮廓（能算面积）；分类 = 整张图是什么；旋转框 = 框 + 角度（俯拍、零件、抓取）")),
      h("div", { class: "field" }, h("label", {}, "类别（每行一个：代码, 中文, English）"), cls, h("div", { class: "help" }, "代码用小写英文（标注、训练里用）；中文和英文用于显示")),
      s.rules && s.rules.length ? h("div", { class: "callout ok" }, "会同时建好质检规则：", s.rules.map((r) => r.message).join("；")) : null,
      s.tip ? h("div", { class: "callout" }, s.tip) : null);
    form._get = () => ({ id: id.value.trim(), name: name.value.trim() || id.value.trim(), task: task.value,
      classes: cls.value.split("\n").map((l) => l.split(/[,，]/).map((x) => x.trim())).filter((x) => x[0]) });
  };
  for (const [k, icon, t] of DOMAINS) {
    const b = h("button", { class: k === domain ? "on" : "" }, icon + " " + t);
    b.onclick = () => { domain = k; picked = null; form.replaceChildren(); [...tabs.children].forEach((x) => x.classList.toggle("on", x === b)); draw(); };
    tabs.append(b);
  }
  draw();
  UI.modal("新建子项目", h("div", {}, h("p", { class: "small muted" }, "先选一个行业场景模板（会自动配好任务类型、类别、数据增强、推荐模型、质检规则），再按需修改。"), tabs, grid, form),
    [{ label: "取消" }, { label: "创建", kind: "primary", run: async () => {
      if (!picked || !form._get) throw new Error("先选一个模板（或空白子项目）");
      const v = form._get();
      if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(v.id)) throw new Error("子项目代码只能用小写字母、数字和短横线，如 pcb-defect");
      if (!v.classes.length) throw new Error("至少要有一个类别");
      const bad = v.classes.find((c) => !/^[a-z0-9][a-z0-9_.-]{0,63}$/.test(c[0]));
      if (bad) throw new Error("类别代码“" + bad[0] + "”不合规：用小写英文、数字、下划线");
      const all = await UI.api("/api/projects");
      if ((Array.isArray(all) ? all : all.projects || []).some((p) => p.id === v.id)) throw new Error("子项目 " + v.id + " 已存在");
      await UI.api("/api/projects/" + v.id, { method: "PUT", body: { config: { name: v.name, task: v.task, classes: v.classes.map((c) => c[0]) }, expected_revision: null } });
      await UI.api("/api/projects/" + v.id + "/activate", { method: "POST", body: { active: true } });
      const fam = picked.family && !picked.family.startsWith("tv:") ? picked.family : "yolo26";
      await UI.api("/api/projects/" + v.id + "/settings", { method: "PUT", body: { settings: {
        train: { size: picked.size || "s", family: fam }, augment: AUG[picked.aug || "std"],
        class_names: Object.fromEntries(v.classes.map((c) => [c[0], { zh: c[1] || c[0], en: c[2] || c[0] }])),
        ...(picked.keypoints ? { pose: { keypoints: picked.keypoints } } : {}) } } });
      if (picked.rules && picked.rules.length) {
        await UI.api("/api/projects/" + v.id + "/pipelines", { method: "PUT", body: { pipelines: [{ id: v.id + "-check", name: v.name + "检查", enabled: true,
          match: picked.photo ? [picked.photo] : [], model: "production", conf: 0.25, imgsz: 640, rules: picked.rules,
          vlm: { enabled: false, model: "qwen2.5vl:7b", prompt: "这是一张" + v.name + "相关的照片。请用一句中文和一句英文描述发现的问题，不要编造。" } }] } });
      }
      UI.store.set("lota-project", v.id);
      UI.modal("子项目已创建", h("div", {}, h("p", {}, "「" + v.name + "」（" + v.id + "）已创建并启用：" + TASK_TEXT[v.task] + "，" + v.classes.length + " 个类别。"),
        h("ol", { class: "small" },
          h("li", {}, "导入图片：工作流里用「导入图片文件夹」「用权重找图」「按照片类型抽图」「视频抽帧」或「导入医学影像（DICOM）」"),
          h("li", {}, "标注：工作流里接「连接 Label Studio」→「送 Label Studio」；建议同时「开启自动回收」"),
          h("li", {}, "训练：数据集页「打包最新标注」→ 模型中心「训练新模型」" + (picked.family && picked.family.startsWith("tv:") ? "（推荐选非 YOLO 的 ResNet-50）" : "")),
          h("li", {}, "上线：模型中心「设为正式使用」→ 智能质检 / 导出到手机")),
        h("div", { class: "row" }, h("a", { class: "btn primary", href: "/flow?project=" + v.id }, "去工作流导入图片 →"), h("a", { class: "btn", href: "/settings?project=" + v.id }, "看项目设置"))));
      render();
    } }], { wide: true });
}

(async () => { await UI.nav("overview", { picker: false }); await render().catch((e) => UI.toast(e.message, true)); loadSys(); if (new URLSearchParams(location.search).get("new")) newProject(); })();
