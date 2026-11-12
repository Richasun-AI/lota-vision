"""图像增强 + 视频（抽帧、Label Studio 视频标注）。

图像增强（OpenCV）：自动 / 去雾 / 暗光 / 对比度(CLAHE) / 去噪 / 锐化 / 白平衡，强度 0–1。
用途：① 看不清的照片先增强再人工复核；② 生成增强副本扩充训练集；③ 现场暗光、逆光、雾天照片的预处理。
视频：按间隔抽帧（跳过几乎一样的帧）→ 图片集，接标注、训练；或直接建 Label Studio 视频跟踪标注项目。
"""
import json
import shutil
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from . import config as deploy
from .store import Spec
from .tools import TOOLS, Tool

METHODS = {
    "auto": ("自动增强", "白平衡 + 局部对比度 + 轻度锐化，适合大多数灰暗、偏色照片"),
    "dehaze": ("去雾", "雾天、灰蒙蒙、隔着脏玻璃拍的照片"),
    "low_light": ("暗光增强", "夜间、机柜内部、背光拍摄的暗照片"),
    "clahe": ("提升对比度", "局部对比度增强（CLAHE），看清阴影里的细节，如铭牌、端子"),
    "denoise": ("去噪", "高 ISO 夜拍噪点多（较慢）"),
    "sharpen": ("锐化", "轻微模糊、对焦不准，看清边缘、裂纹"),
    "white_balance": ("白平衡", "整体偏黄、偏蓝的照片"),
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}


def enhance_array(img, method="auto", strength=0.6):
    import cv2
    import numpy as np
    s = float(max(0.0, min(1.0, strength)))
    if s == 0:
        return img

    def wb(x):
        f = x.astype(np.float32)
        means = f.reshape(-1, 3).mean(0) + 1e-6
        f *= means.mean() / means
        return np.clip(f, 0, 255).astype(np.uint8)

    def clahe(x, clip):
        lab = cv2.cvtColor(x, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8)).apply(l)
        return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

    def sharpen(x, amt):
        blur = cv2.GaussianBlur(x, (0, 0), 2.0)
        return cv2.addWeighted(x, 1 + amt, blur, -amt, 0)

    def gamma(x, g):
        lut = np.array([((i / 255.0) ** g) * 255 for i in range(256)], dtype=np.uint8)
        return cv2.LUT(x, lut)

    def dehaze(x, w):   # 暗通道先验（简化版）
        f = x.astype(np.float32) / 255.0
        dark = cv2.erode(f.min(axis=2), np.ones((15, 15), np.uint8))
        flat = dark.ravel()
        idx = flat.argsort()[-max(1, flat.size // 1000):]
        A = f.reshape(-1, 3)[idx].max(axis=0)
        t = 1 - w * cv2.erode((f / np.maximum(A, 1e-3)).min(axis=2), np.ones((15, 15), np.uint8))
        t = cv2.max(cv2.GaussianBlur(t, (0, 0), 5), 0.1)
        out = (f - A) / t[..., None] + A
        return np.clip(out * 255, 0, 255).astype(np.uint8)

    if method == "auto":
        out = clahe(wb(img), 1.0 + 2.0 * s)
        return sharpen(out, 0.6 * s)
    if method == "dehaze":
        return dehaze(img, 0.5 + 0.45 * s)
    if method == "low_light":
        return clahe(gamma(img, 1.0 - 0.55 * s), 1.0 + 2.0 * s)
    if method == "clahe":
        return clahe(img, 1.0 + 4.0 * s)
    if method == "denoise":
        h = 3 + 12 * s
        return cv2.fastNlMeansDenoisingColored(img, None, h, h, 7, 21)
    if method == "sharpen":
        return sharpen(img, 1.5 * s)
    if method == "white_balance":
        return cv2.addWeighted(img, 1 - s, wb(img), s, 0)
    raise ValueError(f"不认识的增强方法 {method}；可选：{list(METHODS)}")


def enhance_file(src, dst, method, strength, max_side=0):
    import cv2
    img = cv2.imread(str(src), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"读不了图片 {src}")
    if max_side and max(img.shape[:2]) > max_side:
        k = max_side / max(img.shape[:2])
        img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    out = enhance_array(img, method, strength)
    cv2.imwrite(str(dst), out, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return dst


class Enhance(Spec):
    image_ids: list[str] | None = None          # 上游节点连来的图片
    folder: str = ""                            # 或者：服务器上的一个文件夹
    method: Literal["auto", "dehaze", "low_light", "clahe", "denoise", "sharpen", "white_balance"] = "auto"
    strength: float = Field(default=0.6, ge=0, le=1)
    max_side: int = Field(default=0, ge=0, le=8000)   # 0 = 保持原尺寸
    limit: int = Field(default=500, ge=1, le=1000)


def enhance_images(c, image_ids=None, folder="", method="auto", strength=0.6, max_side=0, limit=500):
    """生成增强后的副本（原图不动），作为新图片加进本子项目，可接标注、训练或质检"""
    from .flow import IMPORTS, _images, import_folder
    if image_ids:
        from .catalog import get_image
        srcs = [c.engine.image_path(c.directory, get_image(c.directory, i), False) for i in image_ids[:limit]]
    elif folder:
        srcs = _images(c.engine.allowed_source(folder, directory=True), True)[:limit]
    else:
        raise ValueError("请连上游图片，或填一个文件夹")
    dest = IMPORTS / Path(c.directory).name / f"enhanced-{method}-{time.strftime('%Y%m%d-%H%M%S')}"
    from .flow import _fresh_dir
    dest = _fresh_dir(dest)
    done = 0
    for s in srcs:
        try:
            enhance_file(s, dest / (Path(s).stem + f"_{method}.jpg"), method, strength, max_side)
            done += 1
        except Exception:
            pass
    out = import_folder(c, str(dest), recursive=False)
    out.update({"method": method, "strength": strength, "enhanced": done, "folder": str(dest)})
    return out


# ── 视频 ─────────────────────────────────────────────────────────────────
class Frames(Spec):
    video: str = Field(min_length=1)            # 服务器上的视频文件（上传的视频在 imports/<子项目>/videos/）
    every_sec: float = Field(default=1.0, gt=0, le=600)
    max_frames: int = Field(default=300, ge=1, le=1000)
    skip_similar: bool = True                   # 跳过和上一张几乎一样的帧
    start_sec: float = Field(default=0, ge=0)
    end_sec: float = Field(default=0, ge=0)      # 0 = 到结尾


def extract_frames(c, video, every_sec=1.0, max_frames=300, skip_similar=True, start_sec=0, end_sec=0):
    """视频按间隔抽帧 → 图片（文件名带时间点）→ 加进本子项目"""
    import cv2
    from .flow import IMPORTS, import_folder
    path = c.engine.allowed_source(video)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"打不开视频 {path.name}（格式不支持或文件损坏）")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if total else 0
    dest = IMPORTS / Path(c.directory).name / f"video-{path.stem[:40]}-{time.strftime('%Y%m%d-%H%M%S')}"
    from .flow import _fresh_dir
    dest = _fresh_dir(dest)
    t, saved, skipped, last = float(start_sec), 0, 0, None
    stop = end_sec if end_sec and end_sec > start_sec else (duration or 1e9)
    while t <= stop and saved < max_frames:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (9, 8))
        sig = (small[:, 1:] > small[:, :-1]).flatten()
        if skip_similar and last is not None and int((sig != last).sum()) <= 3:
            skipped += 1
        else:
            cv2.imwrite(str(dest / f"{path.stem[:40]}_{int(t // 60):02d}m{t % 60:05.2f}s.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            saved += 1
            last = sig
        t += every_sec
    cap.release()
    if not saved:
        raise ValueError("没有抽到帧（时间范围不对或视频是空的）")
    out = import_folder(c, str(dest), recursive=False)
    out.update({"video": path.name, "fps": round(fps, 2), "duration_sec": round(duration, 1), "frames": saved, "skipped_similar": skipped, "folder": str(dest)})
    return out


class LsVideo(Spec):
    videos: list[str] = Field(min_length=1, max_length=200)   # 服务器上的视频路径
    title: str | None = None
    base_url: str = "http://127.0.0.1:8081"
    token_env: str = "LABEL_STUDIO_TOKEN"
    local_files_root: str = str(deploy.DATA_ROOT)


def ls_video_setup(c, videos, title=None, base_url="http://127.0.0.1:8081", token_env="LABEL_STUDIO_TOKEN",
                   local_files_root=str(deploy.DATA_ROOT)):
    """建一个 Label Studio 视频跟踪标注项目：在关键帧画框，中间帧自动插值；类别 = 本子项目的类别"""
    import os
    from urllib.parse import quote
    from .labels import LabelStudioClient
    token = os.environ.get(token_env)
    if not token:
        raise ValueError(f"没有 Label Studio 令牌：请在引擎 .env 里设置 {token_env}")
    cli = LabelStudioClient(base_url, token)
    classes = c.scenario.classes
    config = ('<View><Header value="在关键帧画框，Label Studio 会自动插值中间帧；目标消失时关掉该轨迹"/>'
              '<Video name="video" value="$video" framerate="25"/>'
              '<VideoRectangle name="box" toName="video"/>'
              '<Labels name="videoLabels" toName="video" allowEmpty="true">'
              + "".join(f'<Label value="{x}"/>' for x in classes) + '</Labels></View>')
    proj = cli._request("POST", "/api/projects", payload={"title": (title or f"LOTA 视频 · {c.scenario.name}")[:50], "label_config": config})
    root = Path(local_files_root).resolve()
    tasks = []
    for v in videos:
        p = c.engine.allowed_source(v).resolve()
        if not p.is_relative_to(root):
            raise ValueError(f"{p} 不在 Label Studio 本地文件根目录 {root} 下")
        tasks.append({"data": {"video": "/data/local-files/?d=" + quote(str(p.relative_to(root)), safe="/"), "ailabel_project": Path(c.directory).name}})
    try:
        cli._request("POST", "/api/storages/localfiles", payload={"project": proj["id"], "title": "videos",
                     "path": str(Path(c.engine.allowed_source(videos[0])).parent), "use_blob_urls": True})
    except Exception:
        pass
    cli._request("POST", f"/api/projects/{proj['id']}/import", payload=tasks)
    return {"project_id": proj["id"], "url": f"{base_url}/projects/{proj['id']}/data", "videos": len(tasks), "classes": classes}


TOOLS.update({
    "image.enhance": Tool("图像增强（生成增强副本）", Enhance, enhance_images),
    "video.frames": Tool("视频抽帧", Frames, extract_frames),
    "labels.ls_video": Tool("建 Label Studio 视频标注项目", LsVideo, ls_video_setup),
})


def register_routes(router, engine, directory):
    import io
    import tempfile
    from fastapi import File, Form, UploadFile
    from fastapi.responses import Response

    @router.get("/media/methods", tags=["media"])
    def methods():
        return {k: {"name": v[0], "desc": v[1]} for k, v in METHODS.items()}

    @router.post("/media/enhance/preview", tags=["media"])
    async def preview(file: UploadFile = File(...), method: str = Form("auto"), strength: float = Form(0.6), max_side: int = Form(1600)):
        import cv2
        import numpy as np
        data = await file.read()
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("读不了这张图片")
        if max_side and max(img.shape[:2]) > max_side:
            k = max_side / max(img.shape[:2])
            img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        t0 = time.time()
        out = enhance_array(img, method, strength)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return Response(buf.tobytes(), media_type="image/jpeg", headers={"X-Elapsed-Ms": str(round((time.time() - t0) * 1000))})

    @router.post("/projects/{project_id}/videos", tags=["media"])
    async def upload_video(project_id: str, file: UploadFile = File(...)):
        from .flow import IMPORTS
        directory(project_id)
        name = Path(file.filename or "video.mp4").name
        if Path(name).suffix.lower() not in VIDEO_EXT:
            raise ValueError(f"只支持视频：{sorted(VIDEO_EXT)}")
        folder = IMPORTS / project_id / "videos"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        with open(target, "wb") as f:
            while chunk := await file.read(8 * 1024 * 1024):
                f.write(chunk)
        return {"path": str(target), "size": target.stat().st_size}

    @router.get("/projects/{project_id}/videos", tags=["media"])
    def list_videos(project_id: str):
        from .flow import IMPORTS
        directory(project_id)
        folder = IMPORTS / project_id / "videos"
        return [{"path": str(p), "name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
                for p in sorted(folder.iterdir(), key=lambda x: -x.stat().st_mtime) if p.suffix.lower() in VIDEO_EXT] if folder.is_dir() else []


# ── 医学影像：DICOM 导入（窗宽窗位 → 普通图片，默认不保留病人信息）──────────────────────
class DicomImport(Spec):
    folder: str = Field(min_length=1)
    window: Literal["auto", "dicom", "lung", "mediastinum", "bone", "brain", "abdomen"] = "auto"
    frames: Literal["middle", "all"] = "middle"         # 多帧（CT/MR 序列、超声动态）取中间一帧或全部
    max_frames: int = Field(default=200, ge=1, le=1000)
    recursive: bool = True


WINDOWS = {"lung": (-600, 1500), "mediastinum": (40, 400), "bone": (400, 1800), "brain": (40, 80), "abdomen": (60, 400)}


def _dicom_to_uint8(ds, arr, window):
    import numpy as np
    a = arr.astype(np.float32) * float(getattr(ds, "RescaleSlope", 1) or 1) + float(getattr(ds, "RescaleIntercept", 0) or 0)
    if window in WINDOWS:
        c, w = WINDOWS[window]
    elif window == "dicom" and getattr(ds, "WindowCenter", None) is not None:
        wc, ww = ds.WindowCenter, ds.WindowWidth
        c = float(wc[0] if hasattr(wc, "__len__") and not isinstance(wc, str) else wc)
        w = float(ww[0] if hasattr(ww, "__len__") and not isinstance(ww, str) else ww)
    else:                                            # 自动：按 1%–99% 分位拉伸
        lo, hi = np.percentile(a, [1, 99])
        c, w = (lo + hi) / 2, max(1.0, hi - lo)
    out = np.clip((a - (c - w / 2)) / w, 0, 1) * 255
    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        out = 255 - out
    return out.astype(np.uint8)


def import_dicom(c, folder, window="auto", frames="middle", max_frames=200, recursive=True):
    """把 DICOM（CT / MR / X 光 / 超声 等）按窗宽窗位转成普通 PNG 加进子项目；不复制病人姓名等隐私字段"""
    import cv2
    import numpy as np
    import pydicom
    from .flow import IMPORTS, import_folder
    root = c.engine.allowed_source(folder, directory=True)
    files = [p for p in (root.rglob("*") if recursive else root.glob("*")) if p.is_file() and (p.suffix.lower() in (".dcm", ".dicom") or not p.suffix)]
    dest = IMPORTS / Path(c.directory).name / f"dicom-{time.strftime('%Y%m%d-%H%M%S')}"
    from .flow import _fresh_dir
    dest = _fresh_dir(dest)
    ok, bad, meta = 0, 0, []
    for f in files:
        if ok >= max_frames:
            break
        try:
            ds = pydicom.dcmread(str(f))
            arr = ds.pixel_array
        except Exception:
            bad += 1
            continue
        if arr.ndim == 3 and arr.shape[-1] not in (3, 4):          # 多帧灰度
            idx = [arr.shape[0] // 2] if frames == "middle" else range(arr.shape[0])
            slices = [(i, arr[i]) for i in idx]
        else:
            slices = [(0, arr)]
        for i, a in slices:
            if ok >= max_frames:
                break
            img = _dicom_to_uint8(ds, a, window) if a.ndim == 2 else a.astype(np.uint8)
            name = f"{f.stem[:50]}_f{i:03d}.png"
            cv2.imwrite(str(dest / name), img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
            meta.append({"file": name, "source": f.name, "frame": i, "modality": str(getattr(ds, "Modality", "")),
                         "body_part": str(getattr(ds, "BodyPartExamined", "")), "rows": int(getattr(ds, "Rows", 0) or 0)})
            ok += 1
    if not ok:
        raise ValueError(f"没有读到 DICOM 图像（共 {len(files)} 个文件，{bad} 个读不了；压缩格式的 DICOM 需要另装解码库）")
    (dest / "_dicom_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    out = import_folder(c, str(dest), recursive=False)
    out.update({"converted": ok, "unreadable": bad, "window": window, "folder": str(dest),
                "modalities": sorted({m["modality"] for m in meta if m["modality"]})})
    return out


TOOLS["catalog.import_dicom"] = Tool("导入医学影像（DICOM）", DicomImport, import_dicom)
