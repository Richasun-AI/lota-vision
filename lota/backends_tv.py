"""非 YOLO 模型后端：torchvision 经典分类网络（ResNet / EfficientNet / ConvNeXt / MobileNet / ViT）。

医学影像、工业外观分类常用这些“经典骨干网络”做基线。数据集沿用引擎的分类目录结构（split/类别/图片），
训练好的模型和 YOLO 模型一样登记进模型库（training.backend = "torchvision"），推理、评估、试一试、质检自动分派。
"""
import json
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from .store import Spec
from .tools import TOOLS, Tool

ARCHS = {
    "resnet18": ("ResNet-18", "小而快，数据少时不易过拟合"),
    "resnet50": ("ResNet-50", "最常用的基线，医学/工业分类论文里最常见"),
    "efficientnet_b0": ("EfficientNet-B0", "参数少、精度高，适合部署"),
    "convnext_tiny": ("ConvNeXt-Tiny", "较新的卷积网络，精度高"),
    "mobilenet_v3_large": ("MobileNetV3-Large", "手机端友好"),
    "vit_b_16": ("ViT-B/16", "视觉 Transformer，数据多时效果好，较慢"),
}
MEAN, STD = [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]


def _build(arch, num_classes, pretrained=True):
    import torch.nn as nn
    import torchvision.models as tvm
    weights = "DEFAULT" if pretrained else None
    m = getattr(tvm, arch)(weights=weights)
    if arch.startswith("resnet"):
        m.fc = nn.Linear(m.fc.in_features, num_classes)
    elif arch.startswith(("efficientnet", "mobilenet")):
        m.classifier[-1] = nn.Linear(m.classifier[-1].in_features, num_classes)
    elif arch.startswith("convnext"):
        m.classifier[-1] = nn.Linear(m.classifier[-1].in_features, num_classes)
    elif arch.startswith("vit"):
        m.heads.head = nn.Linear(m.heads.head.in_features, num_classes)
    else:
        raise ValueError(f"不支持的网络 {arch}")
    return m


def _tfms(imgsz, train):
    from torchvision import transforms as T
    if train:
        return T.Compose([T.RandomResizedCrop(imgsz, scale=(0.6, 1.0)), T.RandomHorizontalFlip(),
                          T.ColorJitter(0.2, 0.2, 0.2), T.ToTensor(), T.Normalize(MEAN, STD)])
    return T.Compose([T.Resize(int(imgsz * 1.14)), T.CenterCrop(imgsz), T.ToTensor(), T.Normalize(MEAN, STD)])


def is_tv(metadata):
    return (metadata.get("training") or {}).get("backend") == "torchvision"


_CACHE = {}


def load(weights, device="cpu"):
    import torch
    key = (str(weights), Path(weights).stat().st_mtime, device)
    if key not in _CACHE:
        ck = torch.load(str(weights), map_location="cpu", weights_only=False)
        m = _build(ck["arch"], len(ck["classes"]), pretrained=False)
        m.load_state_dict(ck["state_dict"])
        m.eval().to(device)
        _CACHE.clear()
        _CACHE[key] = (m, ck)
    return _CACHE[key]


def predict_paths(weights, paths, device="cpu", topk=5):
    import torch
    from PIL import Image, ImageOps
    dev = device if device == "cpu" or torch.cuda.is_available() else "cpu"
    dev = "cuda:0" if dev in ("0", "cuda") else dev
    m, ck = load(weights, dev)
    tf = _tfms(ck["imgsz"], False)
    out = []
    with torch.inference_mode():
        for p in paths:
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                w, h = im.size
                x = tf(im).unsqueeze(0).to(dev)
            prob = torch.softmax(m(x), dim=1)[0].cpu()
            vals, idx = prob.topk(min(topk, len(ck["classes"])))
            out.append({"path": str(p), "width": w, "height": h,
                        "classifications": [{"class_id": int(i), "label": ck["classes"][int(i)], "confidence": float(v)} for v, i in zip(vals, idx)]})
    return out


class TrainTV(Spec):
    dataset_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)          # 可用 {dataset}、{n}
    arch: Literal["resnet18", "resnet50", "efficientnet_b0", "convnext_tiny", "mobilenet_v3_large", "vit_b_16"] = "resnet50"
    epochs: int = Field(default=20, ge=1, le=500)
    imgsz: int = Field(default=224, ge=64, le=1024)
    batch: int = Field(default=32, ge=1, le=512)
    lr: float = Field(default=0.0005, gt=0, le=0.1)
    device: str = "0"
    workers: int = Field(default=2, ge=0, le=16)
    pretrained: bool = True
    patience: int = Field(default=8, ge=0, le=200)


def train_tv(c, dataset_version, model_version, arch="resnet50", epochs=20, imgsz=224, batch=32, lr=0.0005, device="0",
             workers=2, pretrained=True, patience=8):
    """用 torchvision 经典网络训练图像分类（ImageNet 预训练 + 微调），最好的一轮登记进模型库"""
    import tempfile
    import torch
    from torch.utils.data import DataLoader
    from torchvision.datasets import ImageFolder
    from .models import _register
    from .flow import _next_name
    root = Path(c.directory)
    ds_dir = root / "datasets" / Path(dataset_version).name
    man = json.loads((ds_dir / "manifest.json").read_text(encoding="utf-8"))
    if man["task"] != "classify":
        raise ValueError("torchvision 经典网络目前只用于图像分类数据集")
    classes = man["classes"]
    model_version = _next_name(root / "models", model_version.replace("{dataset}", dataset_version))
    dev = "cuda:0" if device in ("0", "cuda") and torch.cuda.is_available() else "cpu"
    tr = ImageFolder(str(ds_dir / "train"), transform=_tfms(imgsz, True))
    va = ImageFolder(str(ds_dir / "val"), transform=_tfms(imgsz, False))
    if tr.classes != sorted(tr.classes) or any(cl not in classes for cl in tr.classes):
        raise ValueError("数据集类别目录和清单不一致")
    remap = torch.tensor([classes.index(cl) for cl in tr.classes])
    remap_v = torch.tensor([classes.index(cl) for cl in va.classes])
    dl = DataLoader(tr, batch_size=batch, shuffle=True, num_workers=workers)
    dv = DataLoader(va, batch_size=batch, shuffle=False, num_workers=workers)
    model = _build(arch, len(classes), pretrained).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.05)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    lossf = torch.nn.CrossEntropyLoss(label_smoothing=0.1)
    best, best_state, history, bad, t0 = -1.0, None, [], 0, time.time()
    for ep in range(epochs):
        model.train()
        for x, y in dl:
            x, y = x.to(dev), remap[y].to(dev)
            opt.zero_grad()
            lossf(model(x), y).backward()
            opt.step()
        sched.step()
        model.eval()
        correct = top5 = n = 0
        with torch.inference_mode():
            for x, y in dv:
                x, y = x.to(dev), remap_v[y].to(dev)
                logits = model(x)
                correct += (logits.argmax(1) == y).sum().item()
                k = min(5, len(classes))
                top5 += (logits.topk(k, 1).indices == y[:, None]).any(1).sum().item()
                n += len(y)
        acc = correct / max(1, n)
        history.append({"epoch": ep + 1, "top1": round(acc, 4), "top5": round(top5 / max(1, n), 4)})
        if acc > best:
            best, best_state, bad = acc, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}, 0
            best_top5 = top5 / max(1, n)
        else:
            bad += 1
            if patience and bad >= patience:
                break
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "weights.pt"
        torch.save({"arch": arch, "classes": classes, "imgsz": imgsz, "state_dict": best_state, "mean": MEAN, "std": STD}, f)
        meta = _register(root, model_version, f, classes=classes, task="classify", dataset_version=dataset_version,
                         metrics={"metrics/accuracy_top1": best, "metrics/accuracy_top5": best_top5},
                         training={"backend": "torchvision", "arch": arch, "imgsz": imgsz, "initial_weights": f"{arch} (ImageNet)" if pretrained else arch,
                                   "parameters": {"epochs": len(history), "batch": batch, "lr": lr, "device": device},
                                   "history": history, "seconds": round(time.time() - t0)})
    try:
        from .hub import _card_update
        _card_update(root, model_version, base=f"{ARCHS[arch][0]}（torchvision，ImageNet 预训练）")
    except Exception:
        pass
    return meta


TOOLS["model.train_classic"] = Tool("训练经典分类网络（非 YOLO）", TrainTV, train_tv)


def evaluate_tv(directory, metadata, weights, dataset_version, split="val", device="0"):
    from .models import _dataset
    man, ds_dir = _dataset(Path(directory), dataset_version)
    items = [i for i in man["images"] if i["split"] == split]
    if not items:
        raise ValueError(f"数据集 {dataset_version} 没有 {split} 部分")
    preds = predict_paths(weights, [ds_dir / i["path"] for i in items], device)
    top1 = sum(1 for i, p in zip(items, preds) if p["classifications"][0]["label"] == i["labels"][0]) / len(items)
    top5 = sum(1 for i, p in zip(items, preds) if i["labels"][0] in [x["label"] for x in p["classifications"][:5]]) / len(items)
    return {"metrics": {"metrics/accuracy_top1": top1, "metrics/accuracy_top5": top5}, "count": len(items), "split": split}


# ── 语义分割（非 YOLO）：torchvision DeepLabV3 / LRASPP / FCN，逐像素分类 ─────────────────────
# 医学（器官、病灶勾画）、自动驾驶（可行驶区域）的经典做法。复用“分割”子项目的多边形标注（栅格化成掩膜）训练；
# 预测的掩膜转成轮廓多边形，所以质检覆盖率规则、试一试画轮廓都能直接用。
SEG_ARCHS = {
    "deeplabv3_resnet50": ("DeepLabV3-ResNet50", "精度高，医学/遥感常用"),
    "deeplabv3_mobilenet_v3_large": ("DeepLabV3-MobileNetV3", "速度和精度兼顾"),
    "lraspp_mobilenet_v3_large": ("LR-ASPP-MobileNetV3", "最快，手机/边缘设备"),
    "fcn_resnet50": ("FCN-ResNet50", "经典全卷积网络"),
}


def is_semseg(metadata):
    return (metadata.get("training") or {}).get("backend") == "torchvision-seg"


def _build_seg(arch, num_classes, pretrained=True):
    import torch.nn as nn
    import torchvision.models.segmentation as tvs
    m = getattr(tvs, arch)(weights="DEFAULT" if pretrained else None)
    if arch.startswith("deeplabv3") or arch.startswith("fcn"):
        m.classifier[-1] = nn.Conv2d(m.classifier[-1].in_channels, num_classes, 1)
        if getattr(m, "aux_classifier", None) is not None:
            m.aux_classifier = None
    elif arch.startswith("lraspp"):
        m.classifier.low_classifier = nn.Conv2d(m.classifier.low_classifier.in_channels, num_classes, 1)
        m.classifier.high_classifier = nn.Conv2d(m.classifier.high_classifier.in_channels, num_classes, 1)
    return m


def _mask_for(item, classes, h, w):
    import cv2
    import numpy as np
    mask = np.zeros((h, w), np.uint8)
    for lab in item.get("labels") or []:
        nums = [float(x) for x in lab[1:]]
        if len(nums) < 6:
            continue
        pts = np.array([[nums[i] * w, nums[i + 1] * h] for i in range(0, len(nums) - 1, 2)], np.int32)
        cv2.fillPoly(mask, [pts], classes.index(lab[0]) + 1)
    return mask


class _SegSet:
    def __init__(self, ds_dir, items, classes, imgsz, train):
        self.d, self.items, self.classes, self.s, self.train = ds_dir, items, classes, imgsz, train

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        import cv2
        import numpy as np
        import torch
        it = self.items[i]
        img = cv2.cvtColor(cv2.imread(str(self.d / it["path"])), cv2.COLOR_BGR2RGB)
        h, w = img.shape[:2]
        mask = _mask_for(it, self.classes, h, w)
        img = cv2.resize(img, (self.s, self.s), interpolation=cv2.INTER_AREA)
        mask = cv2.resize(mask, (self.s, self.s), interpolation=cv2.INTER_NEAREST)
        if self.train and np.random.rand() < 0.5:
            img, mask = img[:, ::-1].copy(), mask[:, ::-1].copy()
        x = (torch.from_numpy(img).permute(2, 0, 1).float() / 255 - torch.tensor(MEAN)[:, None, None]) / torch.tensor(STD)[:, None, None]
        return x, torch.from_numpy(mask).long()


class TrainSeg(Spec):
    dataset_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    arch: Literal["deeplabv3_resnet50", "deeplabv3_mobilenet_v3_large", "lraspp_mobilenet_v3_large", "fcn_resnet50"] = "deeplabv3_mobilenet_v3_large"
    epochs: int = Field(default=30, ge=1, le=500)
    imgsz: int = Field(default=512, ge=128, le=1024)
    batch: int = Field(default=8, ge=1, le=128)
    lr: float = Field(default=0.0003, gt=0, le=0.1)
    device: str = "0"
    workers: int = Field(default=2, ge=0, le=16)
    patience: int = Field(default=10, ge=0, le=200)


def train_seg(c, dataset_version, model_version, arch="deeplabv3_mobilenet_v3_large", epochs=30, imgsz=512, batch=8, lr=0.0003,
              device="0", workers=2, patience=10):
    """用 torchvision 语义分割网络训练（ImageNet/COCO 预训练 + 微调），以验证集 mIoU 选最好的一轮登记进模型库"""
    import tempfile
    import torch
    from torch.utils.data import DataLoader
    from .models import _register
    from .flow import _next_name
    root = Path(c.directory)
    ds_dir = root / "datasets" / Path(dataset_version).name
    man = json.loads((ds_dir / "manifest.json").read_text(encoding="utf-8"))
    if man["task"] != "segment":
        raise ValueError("语义分割需要“分割”任务的数据集（多边形标注）")
    classes = man["classes"]
    model_version = _next_name(root / "models", model_version.replace("{dataset}", dataset_version))
    dev = "cuda:0" if device in ("0", "cuda") and torch.cuda.is_available() else "cpu"
    tr = [i for i in man["images"] if i["split"] == "train"]
    va = [i for i in man["images"] if i["split"] == "val"] or tr[: max(1, len(tr) // 5)]
    dl = DataLoader(_SegSet(ds_dir, tr, classes, imgsz, True), batch_size=batch, shuffle=True, num_workers=workers, drop_last=len(tr) > batch)
    dv = DataLoader(_SegSet(ds_dir, va, classes, imgsz, False), batch_size=batch, shuffle=False, num_workers=workers)
    K = len(classes) + 1
    model = _build_seg(arch, K).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    lossf = torch.nn.CrossEntropyLoss()
    best, best_state, best_per, history, bad, t0 = -1.0, None, {}, [], 0, time.time()
    for ep in range(epochs):
        model.train()
        for x, y in dl:
            x, y = x.to(dev), y.to(dev)
            opt.zero_grad()
            lossf(model(x)["out"], y).backward()
            opt.step()
        sched.step()
        model.eval()
        inter, union = torch.zeros(K), torch.zeros(K)
        with torch.inference_mode():
            for x, y in dv:
                p = model(x.to(dev))["out"].argmax(1).cpu()
                for k in range(1, K):
                    inter[k] += ((p == k) & (y == k)).sum()
                    union[k] += ((p == k) | (y == k)).sum()
        ious = {classes[k - 1]: float(inter[k] / union[k]) for k in range(1, K) if union[k] > 0}
        miou = sum(ious.values()) / len(ious) if ious else 0.0
        history.append({"epoch": ep + 1, "mIoU": round(miou, 4)})
        if miou > best:
            best, best_per, bad = miou, ious, 0
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if patience and bad >= patience:
                break
    metrics = {"metrics/mIoU": best, **{f"class/{k}/iou": v for k, v in best_per.items()}}
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "weights.pt"
        torch.save({"kind": "semseg", "arch": arch, "classes": classes, "imgsz": imgsz, "state_dict": best_state, "mean": MEAN, "std": STD}, f)
        meta = _register(root, model_version, f, classes=classes, task="segment", dataset_version=dataset_version, metrics=metrics,
                         training={"backend": "torchvision-seg", "arch": arch, "imgsz": imgsz, "initial_weights": f"{arch}（预训练）",
                                   "parameters": {"epochs": len(history), "batch": batch, "lr": lr, "device": device},
                                   "history": history, "seconds": round(time.time() - t0)})
    try:
        from .hub import _card_update
        _card_update(root, model_version, base=f"{SEG_ARCHS[arch][0]}（torchvision 语义分割）")
    except Exception:
        pass
    return meta


TOOLS["model.train_semseg"] = Tool("训练语义分割网络（非 YOLO）", TrainSeg, train_seg)


_SEG_CACHE = {}


def predict_seg(weights, paths, device="cpu", min_area=0.0005):
    """掩膜 → 每个类别的轮廓多边形（像素坐标），格式和检测/分割结果一致：label、confidence、xyxy、polygon"""
    import cv2
    import numpy as np
    import torch
    dev = "cuda:0" if device in ("0", "cuda") and torch.cuda.is_available() else "cpu"
    key = (str(weights), Path(weights).stat().st_mtime, dev)
    if key not in _SEG_CACHE:
        ck = torch.load(str(weights), map_location="cpu", weights_only=False)
        m = _build_seg(ck["arch"], len(ck["classes"]) + 1, pretrained=False)
        m.load_state_dict(ck["state_dict"])
        _SEG_CACHE.clear()
        _SEG_CACHE[key] = (m.eval().to(dev), ck)
    m, ck = _SEG_CACHE[key]
    s, out = ck["imgsz"], []
    for pth in paths:
        img = cv2.cvtColor(cv2.imread(str(pth)), cv2.COLOR_BGR2RGB)
        h, w = img.shape[:2]
        x = cv2.resize(img, (s, s), interpolation=cv2.INTER_AREA)
        x = (torch.from_numpy(x).permute(2, 0, 1).float() / 255 - torch.tensor(MEAN)[:, None, None]) / torch.tensor(STD)[:, None, None]
        with torch.inference_mode():
            prob = torch.softmax(m(x[None].to(dev))["out"][0], 0).cpu().numpy()
        lab = prob.argmax(0).astype(np.uint8)
        lab = cv2.resize(lab, (w, h), interpolation=cv2.INTER_NEAREST)
        dets = []
        for k, name in enumerate(ck["classes"], start=1):
            cnts, _ = cv2.findContours((lab == k).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for cnt in cnts:
                if cv2.contourArea(cnt) < min_area * w * h:
                    continue
                approx = cv2.approxPolyDP(cnt, 0.002 * cv2.arcLength(cnt, True), True)[:, 0, :]
                bx, by, bw, bh = cv2.boundingRect(cnt)
                region = cv2.resize((prob[k] * 255).astype(np.uint8), (w, h))[by:by + bh, bx:bx + bw]
                dets.append({"label": name, "confidence": round(float(region.mean()) / 255, 4), "xyxy": [bx, by, bx + bw, by + bh],
                             "polygon": [[int(px), int(py)] for px, py in approx]})
        out.append({"path": str(pth), "width": w, "height": h, "detections": dets})
    return out


def evaluate_seg(directory, metadata, weights, dataset_version, split="val", device="0"):
    import cv2
    import numpy as np
    from .models import _dataset
    man, ds_dir = _dataset(Path(directory), dataset_version)
    items = [i for i in man["images"] if i["split"] == split]
    if not items:
        raise ValueError(f"数据集 {dataset_version} 没有 {split} 部分")
    classes = metadata["classes"]
    inter, union = np.zeros(len(classes) + 1), np.zeros(len(classes) + 1)
    for it, pr in zip(items, predict_seg(weights, [ds_dir / i["path"] for i in items], device, min_area=0)):
        h, w = pr["height"], pr["width"]
        gt = _mask_for(it, classes, h, w)
        pm = np.zeros((h, w), np.uint8)
        for d in pr["detections"]:
            cv2.fillPoly(pm, [np.array(d["polygon"], np.int32)], classes.index(d["label"]) + 1)
        for k in range(1, len(classes) + 1):
            inter[k] += ((pm == k) & (gt == k)).sum()
            union[k] += ((pm == k) | (gt == k)).sum()
    ious = {classes[k - 1]: float(inter[k] / union[k]) for k in range(1, len(classes) + 1) if union[k] > 0}
    return {"metrics": {"metrics/mIoU": sum(ious.values()) / max(1, len(ious)), **{f"class/{k}/iou": v for k, v in ious.items()}},
            "count": len(items), "split": split}
