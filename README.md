# LOTA — Label Once, Train Always

> LOTA: Label Once, Train Always.**
> 开源的端到端视觉 AI 平台：标注一次，模型自己越练越好。

**数据 → 标注 → 训练 → 模型 → 推理 / 质检** 的一体化视觉 AI 平台。第一个落地场景是通信巡检照片的 **AI 自动审核**，已在菲律宾落地使用；平台按“子项目 = 一个识别场景”组织，可扩展到工业检测、医学影像、机器人、自动驾驶、遥感等商业场景。

- 页面：`http://<服务器>:8795/overview`（首次打开会问访问口令 = 引擎目录 `.env` 里的 `LOTA_API_TOKEN`）
- 标注：Label Studio `http://<服务器>:8081`（引擎自动建项目、送任务、带模型预框、回收标注）
- 接口：`/docs`（OpenAPI），Agent 接口 `/api/agent/*`，MCP 服务器 `mcp/lota_mcp.py`

## 页面地图

| 页面       | 地址          | 做什么                                                                                                                    |
| ---------- | ------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 总览       | `/overview`   | 所有子项目的进度、Label Studio 协同进度、模型成绩、质检统计；**下一步建议**；「新建子项目」向导（6 个行业 16 个场景模板） |
| 工作台     | `/`           | 原图与分拣（大图查看器：原图、滚轮缩放、排除/删除、批量复核）、类别、任务记录                                             |
| 工作流     | `/flow`       | 节点式编辑器 + 简单模式；12 个工作流模板；拖线推荐下一个节点；断点续跑                                                    |
| 数据集     | `/datasets`   | 版本、划分、类别分布、带框样例；一键回收并打包；**找标注错误**（FiftyOne mistakenness 思路）                              |
| 模型中心   | `/models`     | 成绩（大白话）+ 趋势图；训练向导（4 个选项）+ **实时训练曲线**；设为正式/最新；试一试；评估；**A/B 对比**；导出到手机     |
| 智能质检   | `/inspect`    | 检测管线（规则：数量 / 覆盖率 / 置信度 / 分类结果 → 合格/需注意/不合格 + 视觉大模型说明）；拖图试跑；实时质检记录         |
| 工具箱     | `/toolbox`    | 图像增强（7 种，前后对比）；视频上传 / 抽帧 / Label Studio 视频跟踪标注                                                   |
| 实时监控   | `/streams`    | 接海康 / 大华 / 宇视 / 任意 RTSP 摄像头：网页实时看画面；按子项目的模型和质检规则抽帧检测，命中就报警截图；截图一键导入图库做训练数据 |
| 结果复核   | `/review`     | 覆盖率判定的人工复核单（阈值 vs 人工一致率）                                                                              |
| 项目设置   | `/settings`   | 专业参数统一管理：训练默认值、轮数档位、数据划分、命名规范、数据增强（5 个预设）、找图、导出、Label Studio                |
| 使用指南   | `/guide`      | 按“我想做什么”组织的步骤 + 常见问题                                                                                       |
| 开发者 API | `/developers` | 鉴权、Agent 接口、流式质检、MCP 配置、全部工具（实时）                                                                    |

## 能力一览

- **任务类型**：目标检测、实例分割（多边形）、旋转框 OBB、图像分类；模型：YOLO26 / YOLOv8（n/s/m 底座已下载）+ **非 YOLO** 经典分类网络（torchvision：ResNet、EfficientNet、ConvNeXt、MobileNet、ViT）
- **数据**：文件夹导入、按照片类型抽图、**用权重找图**（断点续扫；挑图策略 confident / uncertain / mixed + 每文件夹上限 + 感知哈希去重）、视频抽帧、**DICOM 医学影像**（窗宽窗位，去隐私）、图像增强副本
- **标注协同**：Label Studio 自动建项目（检测框 / 多边形 / 可旋转框 / 分类 / 视频跟踪）、带模型预框送任务、**webhook 自动回收**、ML 后端按项目自动用引擎最新模型
- **主动学习**：回收 → 打包 → 接着上一轮训练 → 用新模型挑最值得标的新图 → 带预框送标注（模板「主动学习一轮」）
- **质量**：找标注错误（漏标 / 多标 / 框不准）、找重复、画质标记、批量复核、排除 / 删除（删掉的照片再次导入同一文件夹时自动跳过，不会重新冒出来）
- **模型管理**：版本自动编号、别名（production / latest）、评估记录、A/B 对比、导出 TFLite INT8 / TFLite / NCNN / ONNX
- **智能质检**：规则引擎 + 可选视觉大模型（本机 Ollama qwen2.5vl，命中才调用）；单张接口、批量节点、流式客户端（接照片下载器）
- **实时视频流**：海康 / 大华 / 宇视自动拼 RTSP 地址，也支持任意 rtsp / rtmp / http / srt 地址；网页 MJPEG 实时播放（短时票据，不暴露口令）；断线自动重连；AI 抽帧检测 + 报警截图 + 写入质检记录；视觉大模型只在报警时调用
- **给 Agent**：`GET /api/agent/manifest`（能力清单）、`POST /api/agent/run`、`/api/agent/template`、`/api/agent/inspect`；零依赖 MCP 服务器（10 个工具）

## 快速开始

1. 总览 →「＋ 新建子项目」→ 选行业模板 → 创建
2. 工作流 → 选模板（如「空开·开始」）→ 运行：抽图、连 Label Studio、开启自动回收、送标注
3. 在 Label Studio 标注（每张选 Targets reviewed / No target）
4. 模型中心 →「训练新模型」；满意后「设为正式使用」
5. 智能质检配规则 → 自动审核；或「导出到手机」

## 给 Agent / MCP

```jsonc
// Claude Code 的 .mcp.json
{ "mcpServers": { "lota": { "command": "python",
    "args": ["/path/to/lota/mcp/lota_mcp.py"],
    "env": { "LOTA_URL": "http://<服务器>:8795", "LOTA_API_TOKEN": "<口令>" } } } }
```

流式质检（盯住照片下载目录，新照片一落地就送去质检）：

```powershell
$env:LOTA_API_TOKEN="<口令>"
python scripts/stream_inspect.py --project demo --watch <下载目录> --only-fail
```

## 部署

服务器上的引擎目录（例如 `/opt/lota`）：

| 项          | 位置                                                                                                                                                                          |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 代码        | `app/lota/`（本仓库的 `lota/`）、`app/pyproject.toml`                                                                                                                         |
| 启停        | `engine.sh`（本仓库的 `scripts/engine.sh`）：`start` / `stop` / `restart` / `status`，或 `run` 前台运行给进程管理器用                                                         |
| 部署配置    | `deploy.env`：数据目录、允许读的原图 / 权重目录、Label Studio 地址，模板见 `deploy.env.example`                                                                               |
| 机密        | `.env`：`LOTA_API_TOKEN`（网页和接口的访问口令）、`LABEL_STUDIO_TOKEN`                                                                                                        |
| Python 环境 | `.venv/`                                                                                                                                                                      |
| 引擎数据    | `runtime/`（`LOTA_HOME`）：`projects/<子项目>/`（catalog、annotations、datasets、models、runs、reviews、qa、inspections、settings.json、pipelines.json）、`weights/` 底座权重 |

```bash
python -m venv .venv && .venv/bin/pip install -e app        # 装依赖
cp deploy.env.example deploy.env && vi deploy.env           # 改成自己的目录和地址
echo "LOTA_API_TOKEN=$(openssl rand -hex 16)" >> .env       # 设了口令才对外监听
bash engine.sh start
```

Label Studio 要开本地文件服务，并把根目录设成 `LOTA_DATA_ROOT`：
`LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED=true`、`LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT=<LOTA_DATA_ROOT>`。

## 代码结构

```
lota/
  engine.py store.py tools.py api.py      引擎核心：子项目、任务队列、worker、HTTP API
  catalog.py labels.py models.py          原图目录、标注（LS 对接/转换）、数据集与模型注册/训练/推理
  flow.py        工作流节点目录、模板、新工具（导入、找图、覆盖率判定、LS 对接…）、版本自动编号
  hub.py         模型中心 / 数据集 / 项目设置 / 导出 / 试一试 / 总览 / 训练进度 / LS webhook
  agent.py       检测管线（智能质检）、Agent 接口、模板转工作流、批量质检
  qa.py          找标注错误
  media.py       图像增强、视频抽帧、LS 视频标注、DICOM 导入
  streams.py     实时监控：摄像头接入、MJPEG 播放、AI 抽帧检测与报警
  cleanup.py     移除类别、批量复核、删除照片
  backends_tv.py 非 YOLO 分类网络（torchvision）
  static/        页面：ui.css/ui.js（设计规范与共享组件）+ 各页面
mcp/lota_mcp.py        MCP 服务器（零依赖）
scripts/stream_inspect.py 流式质检客户端；engine.sh 服务器启停
docs/                    使用说明与设计文档
```

## 路线

- 已完成：合并工程；首个场景端到端；节点式工作流；产品化页面（总览、模型中心、数据集、质检、工具箱、设置、指南、开发者）；分割 / OBB / 非 YOLO / DICOM / 视频；主动学习；Agent + MCP
- 下一步：姿态关键点（pose）任务；把 ⑤ 类型归类（SigLIP 相似图、种子确认）完整移植进工作台；大图切片（遥感 / PCB）；按类别的评估明细；多用户与权限
