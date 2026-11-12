"""把 Ultralytics 官方预训练模型（YOLO26 全家 + YOLOv8 n/s/m）登记进“官方模型库”子项目。

每种任务一个子项目（类别和官方数据集一致）：检测/分割 = COCO 80 类，分类 = ImageNet 1000 类，
姿态 = COCO 人体 17 关键点，旋转框 = DOTA 15 类。登记后在模型中心可试一试、评估、导出手机、设别名，
质检规则也能直接用（比如 COCO 的 person 检测人员闯入）。重复运行是安全的（已登记的跳过）。

用法（服务器）：cd app && ../.venv/bin/python ../scripts/register_official.py
"""
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("YOLO_AUTOINSTALL", "False")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from lota.engine import Engine          # noqa: E402
from lota.store import Scenario          # noqa: E402
from lota.models import register, set_alias, _read_aliases   # noqa: E402
from lota.hub import _card_update, save_settings, load_settings   # noqa: E402

RT = Path(os.environ.get("LOTA_HOME") or Path.home() / "lota-data/lota/runtime")
W = RT / "weights"
COCO_KP = ["nose", "left_eye", "right_eye", "left_ear", "right_ear", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
           "left_wrist", "right_wrist", "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle"]
LIBS = [
    ("official-detect", "官方模型库 · 检测（COCO 80 类）", "detect", "", ["yolov8n", "yolov8s", "yolov8m"], "COCO"),
    ("official-segment", "官方模型库 · 实例分割（COCO 80 类）", "segment", "-seg", [], "COCO"),
    ("official-classify", "官方模型库 · 分类（ImageNet 1000 类）", "classify", "-cls", [], "ImageNet"),
    ("official-pose", "官方模型库 · 人体姿态（COCO 17 关键点）", "pose", "-pose", [], "COCO-Pose"),
    ("official-obb", "官方模型库 · 旋转框（DOTA 15 类）", "obb", "-obb", [], "DOTA v1"),
]
SIZE = {"n": "极速", "s": "均衡", "m": "精准", "l": "大", "x": "特大"}


def code(name):
    c = re.sub(r"[^a-z0-9_.-]+", "_", name.lower()).strip("_.-")
    return c if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", c or "") else "c_" + str(abs(hash(name)) % 10**6)


def main():
    from ultralytics import YOLO
    e = Engine(RT, [], [])
    for pid, title, task, suffix, extra, data in LIBS:
        files = [f"yolo26{s}{suffix}.pt" for s in "nsmlx"] + [f"{x}.pt" for x in extra]
        files = [f for f in files if (W / f).is_file()]
        if not files:
            print("跳过（没有权重）", pid)
            continue
        names = YOLO(str(W / files[0])).names
        names = [names[i] for i in sorted(names)] if isinstance(names, dict) else list(names)
        if task == "classify":
            sc_classes = ["imagenet"]          # 1000 类超过子项目上限，子项目里放占位；模型自己的 1000 类照常用
        else:
            sc_classes, seen = [], set()
            for n in names:
                c = code(n)
                while c in seen:
                    c += "_"
                seen.add(c)
                sc_classes.append(c)
        try:
            e.store.project(pid)
        except FileNotFoundError:
            e.save(pid, Scenario.model_validate({"name": title, "task": task, "classes": sc_classes}))
            e.store.activate(pid, True)
            print("新建子项目", pid)
        d = e.store.project_dir(pid)
        st = {"meta": {"kind": "official", "dataset": data},
              "class_names": {c: {"zh": n, "en": n} for c, n in zip(sc_classes, names)} if task != "classify" else {}}
        if task == "pose":
            st["pose"] = {"keypoints": COCO_KP}
        save_settings(d, {**{k: v for k, v in load_settings(d).items()}, **st})
        have = set(_read_aliases(d / "models").values()) | {p.name for p in (d / "models").glob("*") if (p / "model.json").is_file()} if (d / "models").is_dir() else set()
        for f in files:
            ver = f[:-3]
            if ver in have:
                print("  已登记", ver)
                continue
            m = YOLO(str(W / f))
            mn = [m.names[i] for i in sorted(m.names)] if isinstance(m.names, dict) else list(m.names)
            register(d, ver, W / f, classes=mn, task=task)
            size = re.match(r"yolo(?:26|v8)([nsmlx])", ver).group(1)
            _card_update(d, ver, base=f"Ultralytics 官方预训练（{data}）",
                         note=f"官方{SIZE[size]}版（{size}），在 {data} 上预训练，未用你的数据训练；可直接试一试、导出到手机，或当作训练底座。")
            print("  登记", ver, task, len(mn), "类")
        prod = f"yolo26s{suffix}"
        if (d / "models" / prod / "model.json").is_file():
            set_alias(d, "production", prod)
            set_alias(d, "latest", f"yolo26n{suffix}" if (d / "models" / f"yolo26n{suffix}").is_dir() else prod)
            print("  production =", prod)
    print("完成")


if __name__ == "__main__":
    main()
