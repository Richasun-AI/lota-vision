// 行业场景模板库：新建子项目时一键带出任务类型、类别（中英）、数据增强、推荐模型、质检规则
"use strict";
const AUG = {
  std: { hsv_h: 0.015, hsv_s: 0.7, hsv_v: 0.4, degrees: 0, translate: 0.1, scale: 0.5, fliplr: 0.5, flipud: 0, mosaic: 1, mixup: 0 },
  strong: { hsv_h: 0.02, hsv_s: 0.8, hsv_v: 0.5, degrees: 10, translate: 0.2, scale: 0.7, fliplr: 0.5, flipud: 0, mosaic: 1, mixup: 0.1 },
  topdown: { hsv_h: 0.01, hsv_s: 0.5, hsv_v: 0.3, degrees: 90, translate: 0.1, scale: 0.5, fliplr: 0.5, flipud: 0.5, mosaic: 1, mixup: 0 },
  medical: { hsv_h: 0, hsv_s: 0, hsv_v: 0.25, degrees: 10, translate: 0.1, scale: 0.3, fliplr: 0.5, flipud: 0, mosaic: 0.5, mixup: 0 },
  text: { hsv_h: 0.015, hsv_s: 0.6, hsv_v: 0.4, degrees: 0, translate: 0.1, scale: 0.4, fliplr: 0, flipud: 0, mosaic: 1, mixup: 0 },
};
const DOMAINS = [
  ["telecom", "📡", "通信巡检"], ["industry", "🏭", "工业检测"], ["medical", "🩺", "医学影像"],
  ["robot", "🤖", "机器人"], ["driving", "🚗", "自动驾驶"], ["remote", "🛰️", "遥感 / 无人机"],
];
// classes: [代码, 中文, English]
const SCENARIOS = [
  { domain: "telecom", id: "site-weeds", name: "站内杂草", task: "detect", classes: [["weeds", "杂草", "Weeds"]], aug: "std", size: "s",
    tip: "地面照片里框出杂草；按地面区域杂草覆盖率判定“草多”。", photo: "site_ground",
    rules: [{ label: "weeds", metric: "coverage", region: "lower_half", min_conf: 0.5, op: ">", value: 0.05, verdict: "fail", message: "站内杂草较多，需要清理", message_en: "Overgrown weeds on site" }] },
  { domain: "telecom", id: "breaker", name: "空开识别", task: "detect", classes: [["mcb", "小型空开", "MCB"]], aug: "std", size: "s", tip: "交流配电箱照片里框出空开本体。", photo: "ac_distribution" },
  { domain: "telecom", id: "tower-rust", name: "铁塔锈蚀", task: "segment", classes: [["rust", "锈蚀", "Rust"]], aug: "strong", size: "s",
    tip: "用多边形描出锈蚀区域；按锈蚀面积占比判定。", rules: [{ label: "rust", metric: "coverage", region: "full", min_conf: 0.4, op: ">", value: 0.03, verdict: "fail", message: "铁塔锈蚀明显，需要除锈防腐", message_en: "Visible rust on tower" }] },
  { domain: "telecom", id: "fence", name: "围栏 / 铁丝网破损", task: "detect", classes: [["fence_broken", "围栏破损", "Broken fence"], ["barbed_wire_broken", "铁丝网破损", "Broken barbed wire"]], aug: "std", size: "s",
    tip: "框出破损处。", rules: [{ label: "", metric: "count", min_conf: 0.5, op: ">", value: 0, verdict: "fail", message: "围栏或铁丝网有破损", message_en: "Fence or barbed wire damaged" }] },
  { domain: "telecom", id: "room-clean", name: "机房卫生", task: "classify", classes: [["clean", "整洁", "Clean"], ["messy", "杂乱", "Messy"]], aug: "std", size: "s", family: "tv:resnet50",
    tip: "整张照片判断整洁 / 杂乱（分类）。", rules: [{ label: "", metric: "top1", op: "==", value: "messy", verdict: "warn", message: "机房杂乱，需要整理", message_en: "Equipment room is messy" }] },
  { domain: "industry", id: "surface-defect", name: "表面缺陷（分割）", task: "segment",
    classes: [["scratch", "划痕", "Scratch"], ["dent", "凹坑", "Dent"], ["rust", "锈斑", "Rust"], ["stain", "污渍", "Stain"]], aug: "strong", size: "s",
    tip: "金属、玻璃、喷涂件表面缺陷，用多边形描轮廓；小缺陷建议图片尺寸 960 以上。", rules: [{ label: "", metric: "count", min_conf: 0.5, op: ">", value: 0, verdict: "fail", message: "发现表面缺陷", message_en: "Surface defect found" }] },
  { domain: "industry", id: "pcb-defect", name: "PCB 缺陷", task: "detect",
    classes: [["missing_hole", "漏孔", "Missing hole"], ["mouse_bite", "鼠咬", "Mouse bite"], ["open_circuit", "开路", "Open circuit"], ["short", "短路", "Short"], ["spur", "毛刺", "Spur"], ["spurious_copper", "残铜", "Spurious copper"]],
    aug: "topdown", size: "s", tip: "类别同北京大学 PCB 缺陷公开数据集；缺陷很小，建议图片尺寸 1280。", rules: [{ label: "", metric: "count", min_conf: 0.4, op: ">", value: 0, verdict: "fail", message: "PCB 有缺陷", message_en: "PCB defect found" }] },
  { domain: "industry", id: "part-pose", name: "零件定位（旋转框）", task: "obb", classes: [["part", "零件", "Part"]], aug: "topdown", size: "n", tip: "零件摆放方向不一，用可旋转的框（OBB）给出位置和角度，供机械臂抓取或计数。" },
  { domain: "medical", id: "lung-nodule", name: "肺结节检测", task: "detect", classes: [["nodule", "结节", "Nodule"]], aug: "medical", size: "s",
    tip: "CT 用“导入医学影像（DICOM）”节点、肺窗导入；结节小，建议尺寸 640–1024。仅供科研辅助，不能替代医生诊断。" },
  { domain: "medical", id: "lesion-seg", name: "病灶 / 器官分割", task: "segment", classes: [["lesion", "病灶", "Lesion"]], aug: "medical", size: "s", tip: "用多边形描出病灶或器官轮廓；可算面积占比。仅供科研辅助。" },
  { domain: "medical", id: "skin-cls", name: "皮肤病变分类", task: "classify", classes: [["benign", "良性", "Benign"], ["malignant", "恶性", "Malignant"]], aug: "medical", size: "s", family: "tv:resnet50",
    tip: "整张图分类；推荐非 YOLO 的 ResNet-50 / EfficientNet（医学分类常用基线）。仅供科研辅助。" },
  { domain: "robot", id: "grasp", name: "抓取目标（旋转框）", task: "obb", classes: [["object", "物体", "Object"]], aug: "topdown", size: "n", tip: "给出物体位置和朝向，机械臂按角度抓取。" },
  { domain: "robot", id: "human-pose", name: "人体姿态（安全监测）", task: "pose", classes: [["person", "人", "Person"]], aug: "std", size: "n",
    keypoints: ["nose", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist", "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"],
    tip: "人框 + 13 个关键点（COCO 子集）；判断弯腰、摔倒、攀爬等危险动作。关键点名称已写进项目设置。" },
  { domain: "robot", id: "bin-pick", name: "料箱物体识别", task: "detect", classes: [["box", "纸箱", "Box"], ["bag", "袋子", "Bag"], ["bottle", "瓶子", "Bottle"]], aug: "std", size: "n", tip: "手机/工控机实时识别，推荐 n 极速模型。" },
  { domain: "driving", id: "road-objects", name: "道路目标", task: "detect",
    classes: [["car", "轿车", "Car"], ["truck", "卡车", "Truck"], ["bus", "公交", "Bus"], ["pedestrian", "行人", "Pedestrian"], ["cyclist", "骑行者", "Cyclist"], ["traffic_light", "红绿灯", "Traffic light"], ["traffic_sign", "交通标志", "Traffic sign"]],
    aug: "text", size: "s", tip: "车载视频可用“视频抽帧”导入；交通标志有方向，已关闭左右翻转。" },
  { domain: "driving", id: "drivable", name: "可行驶区域 / 车道分割", task: "segment", classes: [["drivable", "可行驶区域", "Drivable area"], ["lane", "车道线", "Lane"]], aug: "text", size: "s", tip: "用多边形描区域；车道线较细，建议尺寸 960。" },
  { domain: "remote", id: "aerial-obb", name: "航拍旋转目标", task: "obb",
    classes: [["plane", "飞机", "Plane"], ["ship", "船", "Ship"], ["vehicle", "车辆", "Vehicle"], ["storage_tank", "储罐", "Storage tank"]], aug: "topdown", size: "s", tip: "俯拍目标方向任意，用旋转框（同 DOTA 数据集做法）；大图建议先切片。" },
];
const TASK_TEXT = { detect: "目标检测（画框）", segment: "实例分割（画轮廓）", classify: "图像分类（整张图）", obb: "旋转框（框 + 角度）", pose: "姿态关键点（框 + 点）" };
