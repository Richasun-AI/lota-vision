"""实时监控：接海康 / 大华 / 宇视 / 任意 RTSP 摄像头，网页实时看画面（MJPEG），
可选用某个子项目的模型抽帧检测、按质检规则报警并截图存证，截图可一键导入子项目图库做训练数据。

· 摄像头配置存在引擎数据目录 streams/cameras.json（含密码，只在服务器上；接口返回时一律打码）
· 每路摄像头一个读帧线程（断线自动重连），开了 AI 再加一个检测线程；没人看、也没开 AI 的 60 秒后自动断开
· 浏览器的 <img> 带不了口令头，所以看画面先用口令换一张短时票据，再用票据打开 /live/<id>.mjpg
"""
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, Field

# RTSP 默认走 TCP（UDP 在公网 / 跨网段经常花屏丢包）；5 秒连不上就算失败
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000|timeout;5000000")

MASK = "******"
IDLE_SEC = 60            # 没人看、也没开 AI：这么久后断开摄像头
TICKET_SEC = 600         # 看画面的票据有效期
EVENTS_KEEP = 2000       # 每路摄像头最多保留的报警截图
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
SCHEMES = ("rtsp", "rtsps", "rtmp", "http", "https", "srt")

# 各品牌的 RTSP 地址规则：{ch} 通道号；主码流 / 子码流
BRANDS = {
    "hikvision": {"name": "海康威视", "name_en": "Hikvision", "port": 554,
                  "path": lambda ch, sub: f"/Streaming/Channels/{ch}0{2 if sub else 1}"},
    "dahua": {"name": "大华", "name_en": "Dahua", "port": 554,
              "path": lambda ch, sub: f"/cam/realmonitor?channel={ch}&subtype={1 if sub else 0}"},
    "uniview": {"name": "宇视", "name_en": "Uniview", "port": 554,
                "path": lambda ch, sub: f"/unicast/c{ch}/s{1 if sub else 0}/live"},
    "ezviz": {"name": "海康·萤石云（不在同一网络时）", "name_en": "Hikvision EZVIZ cloud", "port": 0, "path": None},
    "url": {"name": "其他（直接填地址）", "name_en": "Other (stream URL)", "port": 0, "path": None},
}


class AiConfig(BaseModel):
    enabled: bool = False
    project: str = Field(default="", max_length=64)            # 用哪个子项目的模型和质检规则
    pipelines: list[str] = Field(default_factory=list)         # 跑哪几条质检规则；空 = 这个子项目全部启用的
    model: str = Field(default="production", max_length=100)   # 子项目没有质检规则时，只用这个模型画框（不报警）
    conf: float = Field(default=0.25, ge=0.01, le=1)
    every_sec: float = Field(default=1.0, ge=0.2, le=3600)     # 每隔几秒检测一帧
    cooldown_sec: int = Field(default=60, ge=0, le=86400)      # 同一路摄像头两次报警至少隔多久
    photo_type: str = Field(default="", max_length=64)         # 记录里的照片类型（可不填）


class Camera(BaseModel):
    id: str = Field(pattern=ID_RE.pattern)
    name: str = Field(min_length=1, max_length=80)
    brand: str = Field(default="hikvision", pattern="^(" + "|".join(BRANDS) + ")$")
    host: str = Field(default="", max_length=253)
    port: int = Field(default=554, ge=0, le=65535)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)
    channel: int = Field(default=1, ge=1, le=512)
    substream: bool = True                                     # 子码流：分辨率低、省带宽，监控够用；要看清细节用主码流
    url: str = Field(default="", max_length=1000)              # brand=url 时直接用这个地址
    # brand=ezviz（海康·萤石云）：开放平台应用的 AppKey / AppSecret + 设备序列号；通道号、码流沿用上面的
    ez_app_key: str = Field(default="", max_length=64)
    ez_app_secret: str = Field(default="", max_length=128)
    ez_serial: str = Field(default="", max_length=32)
    ez_code: str = Field(default="", max_length=32)            # 设备验证码：填了会自动关闭视频加密
    ez_protocol: str = Field(default="flv", pattern="^(flv|hls|rtmp)$")
    location: str = Field(default="", max_length=120)
    ai: AiConfig = Field(default_factory=AiConfig)


def build_url(cam: Camera) -> str:
    """拼出取流地址（含账号密码，只在服务器内部用）。萤石云返回给网页看的占位地址；真正取流用 resolve_url"""
    if cam.brand == "ezviz":
        from . import ezviz
        ezviz.check(cam)
        return ezviz.display(cam)
    if cam.brand == "url":
        u = urlsplit(cam.url.strip())
        if u.scheme.lower() not in SCHEMES or not u.hostname:
            raise ValueError(f"地址要以 {' / '.join(s + '://' for s in SCHEMES)} 开头，例如 rtsp://192.168.1.64:554/stream1")
        if cam.username and "@" not in u.netloc:     # 账号密码单独填的，拼进地址
            auth = quote(cam.username, safe="") + (":" + quote(cam.password, safe="") if cam.password else "")
            u = u._replace(netloc=f"{auth}@{u.netloc}")
        return urlunsplit(u)
    if not cam.host or not HOST_RE.match(cam.host):
        raise ValueError("请填摄像头 / 录像机的 IP 地址或域名")
    b = BRANDS[cam.brand]
    auth = (quote(cam.username, safe="") + ":" + quote(cam.password, safe="") + "@") if cam.username else ""
    return f"rtsp://{auth}{cam.host}:{cam.port or b['port']}{b['path'](cam.channel, cam.substream)}"


def resolve_url(cam: Camera, fresh=False) -> str:
    """真正用来取流的地址：萤石云要联网换临时直播地址，其他品牌和 build_url 一样"""
    if cam.brand == "ezviz":
        from . import ezviz
        return ezviz.live_url(cam, force=fresh)
    return build_url(cam)


def redact(url: str) -> str:
    """地址里的密码换成 ******，可以给网页显示"""
    try:
        u = urlsplit(url)
        if u.password:
            host = u.hostname + (f":{u.port}" if u.port else "")
            return urlunsplit(u._replace(netloc=f"{u.username}:{MASK}@{host}"))
    except ValueError:
        pass
    return url


def public(cam: Camera) -> dict:
    d = cam.model_dump()
    d["password"] = MASK if cam.password else ""
    d["ez_app_secret"] = MASK if cam.ez_app_secret else ""
    d["ez_code"] = MASK if cam.ez_code else ""
    try:
        d["stream_url"] = redact(build_url(cam))
    except ValueError as e:
        d["stream_url"], d["url_error"] = "", str(e)
    if cam.brand == "url":
        d["url"] = redact(cam.url)
    return d


# ── 画框 ──────────────────────────────────────────────────────────────────
COLORS = {"fail": (40, 40, 230), "warn": (0, 160, 255), "pass": (60, 200, 80), "skip": (200, 200, 200)}


def annotate(frame, overlay, label=""):
    import cv2
    import numpy as np
    img = frame.copy()
    verdict = (overlay or {}).get("verdict", "")
    color = COLORS.get(verdict, (60, 200, 80))
    for d in (overlay or {}).get("detections", []):
        x1, y1, x2, y2 = (int(v) for v in d["xyxy"])
        if d.get("polygon"):
            cv2.polylines(img, [np.array(d["polygon"], np.int32)], True, color, 2)
        else:
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        txt = f"{d['label']} {d['confidence']:.2f}"
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(img, (x1, max(0, y1 - th - 8)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(img, txt, (x1 + 3, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S") + (f"  {label}" if label else "") + (f"  [{verdict.upper()}]" if verdict else "")
    (sw, _), _ = cv2.getTextSize(stamp, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
    cv2.rectangle(img, (0, 0), (min(img.shape[1], sw + 16), 28), (0, 0, 0), -1)
    cv2.putText(img, stamp, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def _jpeg(img, width=0, quality=80):
    import cv2
    if width and img.shape[1] > width:
        img = cv2.resize(img, (width, int(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("画面编码失败")
    return buf.tobytes()


def probe(url, timeout=12):
    """连一下摄像头，取一帧：返回 (frame, 毫秒)；连不上抛错"""
    import cv2
    box = {}

    def run():
        t0 = time.time()
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        try:
            if not cap.isOpened():
                box["err"] = "连不上：检查 IP、端口、账号密码，以及摄像头是否开了 RTSP"
                return
            for _ in range(30):            # 头几帧可能是坏帧
                ok, frame = cap.read()
                if ok and frame is not None:
                    box["frame"], box["ms"] = frame, round((time.time() - t0) * 1000)
                    return
            box["err"] = "连上了但读不到画面：试试换主码流 / 子码流或通道号"
        finally:
            cap.release()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"{timeout} 秒内没连上：检查网络是否通（服务器能不能 ping 到摄像头）")
    if "err" in box:
        raise ConnectionError(box["err"])
    return box["frame"], box["ms"]


# ── 每路摄像头的运行状态 ──────────────────────────────────────────────────
class Live:
    def __init__(self, mgr, cam: Camera):
        self.mgr, self.cam = mgr, cam
        self.frame, self.frame_at, self.size = None, 0.0, None
        self.status, self.error, self.fps = "connecting", "", 0.0
        self.overlay, self.last_alarm, self.ai_error = {}, 0.0, ""
        self.seen = time.time()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True, name=f"cam-{cam.id}").start()
        if cam.ai.enabled:
            threading.Thread(target=self._detect_loop, daemon=True, name=f"ai-{cam.id}").start()

    def touch(self):
        self.seen = time.time()

    def info(self):
        return {"status": self.status, "error": self.error, "fps": round(self.fps, 1), "size": self.size,
                "frame_age": round(time.time() - self.frame_at, 1) if self.frame_at else None,
                "verdict": self.overlay.get("verdict"), "detections": len(self.overlay.get("detections", [])),
                "ai_error": self.ai_error, "last_alarm": self.last_alarm or None}

    def _read(self):
        import cv2
        backoff, fresh = 2, False
        while not self.stop.is_set():
            try:
                url = resolve_url(self.cam, fresh)   # 萤石云：重连时换一个新的直播地址
            except (ValueError, ConnectionError) as e:
                self.status, self.error = "error", str(e)
                if self.cam.brand != "ezviz":       # 本地摄像头：配置错了，重试也没用
                    return
                self.error += f"，{backoff} 秒后重试"   # 萤石云：设备离线、网络抖动等，过一会儿再试
                if self.stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)
                continue
            fresh = True
            self.status = "connecting"
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                cap.release()
                self.status, self.error = "error", f"连不上摄像头，{backoff} 秒后重试"
                if self.stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30)
                continue
            backoff, n, t0, fails = 2, 0, time.time(), 0
            while not self.stop.is_set():
                ok, frame = cap.read()        # 一直读，不让缓冲区积压（看到的永远是最新画面）
                if not ok or frame is None:
                    fails += 1
                    if fails > 50:
                        break
                    continue
                fails = 0
                with self.lock:
                    self.frame, self.frame_at = frame, time.time()
                self.size = [int(frame.shape[1]), int(frame.shape[0])]
                self.status, self.error = "live", ""
                n += 1
                if time.time() - t0 >= 2:
                    self.fps, n, t0 = n / (time.time() - t0), 0, time.time()
                if not self.cam.ai.enabled and time.time() - self.seen > IDLE_SEC:
                    self.stop.set()           # 没人看了：断开，下次有人看再连
            cap.release()
            if not self.stop.is_set():
                self.status, self.error = "error", "画面断了，正在重连…"
                self.stop.wait(1)
        self.status = "stopped"
        self.mgr._gone(self)

    def latest(self):
        with self.lock:
            return self.frame, self.frame_at

    # ── AI：抽帧检测 + 报警 ──
    def _pipes(self):
        from .agent import load_pipelines
        ai = self.cam.ai
        d = self.mgr.directory(ai.project)
        pipes = [p for p in load_pipelines(d) if p.get("enabled", True) and (not ai.pipelines or p["id"] in ai.pipelines)]
        if not pipes:                         # 没有质检规则：只用模型画框，不报警
            pipes = [{"id": "live", "name": "实时检测", "model": ai.model or "production", "conf": ai.conf, "rules": []}]
        return d, pipes

    def _detect_loop(self):
        import cv2
        from .agent import inspect_image
        tmp = None
        while not self.stop.is_set():
            t0 = time.time()
            frame, at = self.latest()
            if frame is not None and time.time() - at < 10:
                try:
                    d, pipes = self._pipes()
                    live_pipes = [{**p, "vlm": {**(p.get("vlm") or {}), "enabled": False}} for p in pipes]   # 视觉大模型只在报警时调
                    tmp = Path(d) / ".streams" / f"{self.cam.id}.jpg"
                    tmp.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(tmp), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
                    out = inspect_image(d, tmp, photo_type=self.cam.ai.photo_type, save=False, pipes=live_pipes)
                    dets = [x for r in out["results"] for x in r["detections"]]
                    self.overlay = {"verdict": out["verdict"] if any(r["findings"] for r in out["results"]) else "",
                                    "detections": dets, "at": time.time()}
                    self.ai_error = ""
                    if out["verdict"] in ("warn", "fail") and time.time() - self.last_alarm >= self.cam.ai.cooldown_sec:
                        self.last_alarm = time.time()
                        self.mgr.save_event(self, frame, out, pipes, d)
                except Exception as e:        # 模型没训好、子项目被删等：记下原因，过一会儿再试
                    self.ai_error = str(e)[:300]
                    self.overlay = {}
                    if self.stop.wait(10):
                        break
            self.stop.wait(max(0.05, self.cam.ai.every_sec - (time.time() - t0)))

    def mjpeg(self, fps=8, width=960):
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n"
        gap = 1.0 / max(1, min(fps, 25))
        last = 0
        while not self.stop.is_set():
            self.touch()
            frame, at = self.latest()
            if frame is None or at == last:
                time.sleep(0.05 if frame is not None else 0.3)
                continue
            last = at
            ov = self.overlay if time.time() - self.overlay.get("at", 0) < max(3.0, self.cam.ai.every_sec * 2.5) else {}
            jpg = _jpeg(annotate(frame, ov, self.cam.id), width, 75)
            yield boundary + f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n"
            time.sleep(gap)


# ── 管理：配置、运行中的摄像头、票据、报警记录 ─────────────────────────────
class Manager:
    def __init__(self, engine, directory):
        self.engine, self.directory = engine, directory
        self.root = Path(engine.store.root) / "streams"
        (self.root / "events").mkdir(parents=True, exist_ok=True)
        self.file = self.root / "cameras.json"
        self.lock = threading.RLock()
        self.live: dict[str, Live] = {}
        self.tickets: dict[str, tuple[str, float]] = {}

    def cameras(self) -> dict[str, Camera]:
        if not self.file.is_file():
            return {}
        return {k: Camera.model_validate(v) for k, v in json.loads(self.file.read_text(encoding="utf-8")).items()}

    def _write(self, cams):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: c.model_dump() for k, c in cams.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)
        try:
            os.chmod(self.file, 0o600)        # 里面有摄像头密码
        except OSError:
            pass

    def camera(self, cid) -> Camera:
        c = self.cameras().get(cid)
        if not c:
            raise FileNotFoundError(f"没有这个摄像头：{cid}")
        return c

    def merge_secret(self, cam: Camera, old: Camera | None) -> Camera:
        """网页提交的是打码的密码（******）时，沿用原来的密码"""
        if old and cam.password == MASK:
            cam = cam.model_copy(update={"password": old.password})
        if old and cam.brand == "url" and MASK in cam.url and old.url:
            cam = cam.model_copy(update={"url": old.url})
        for k in ("ez_app_secret", "ez_code"):
            if old and getattr(cam, k) == MASK:
                cam = cam.model_copy(update={k: getattr(old, k)})
        return cam

    def save(self, cam: Camera, create=False):
        with self.lock:
            cams = self.cameras()
            if create and cam.id in cams:
                raise ValueError(f"摄像头编号 {cam.id} 已存在，换一个")
            if not create and cam.id not in cams:
                raise FileNotFoundError(f"没有这个摄像头：{cam.id}")
            cam = self.merge_secret(cam, cams.get(cam.id))
            build_url(cam)                     # 地址拼不出来就不保存
            if cam.ai.enabled:
                if not cam.ai.project:
                    raise ValueError("开了 AI 检测就要选一个子项目（用它的模型和质检规则）")
                self.directory(cam.ai.project)
            cams[cam.id] = cam
            self._write(cams)
            self.restart(cam.id)
            return cam

    def delete(self, cid):
        with self.lock:
            cams = self.cameras()
            if cams.pop(cid, None) is None:
                raise FileNotFoundError(f"没有这个摄像头：{cid}")
            self._write(cams)
            self.halt(cid)

    # 运行
    def get_live(self, cid) -> Live:
        with self.lock:
            lv = self.live.get(cid)
            if lv is None or lv.stop.is_set():
                lv = self.live[cid] = Live(self, self.camera(cid))
            lv.touch()
            return lv

    def halt(self, cid):
        with self.lock:
            lv = self.live.pop(cid, None)
        if lv:
            lv.stop.set()

    def restart(self, cid):
        self.halt(cid)
        c = self.cameras().get(cid)
        if c and c.ai.enabled:
            self.get_live(cid)

    def _gone(self, lv):
        with self.lock:
            if self.live.get(lv.cam.id) is lv:
                self.live.pop(lv.cam.id, None)

    def autostart(self):
        for cid, c in self.cameras().items():
            if c.ai.enabled:
                try:
                    self.get_live(cid)
                except Exception:
                    pass

    def status(self, cid):
        lv = self.live.get(cid)
        return lv.info() if lv and not lv.stop.is_set() else {"status": "idle"}

    # 票据
    def ticket(self, cid):
        self.camera(cid)
        now = time.time()
        self.tickets = {k: v for k, v in self.tickets.items() if v[1] > now}
        t = secrets.token_urlsafe(24)
        self.tickets[t] = (cid, now + TICKET_SEC)
        return {"ticket": t, "expires_in": TICKET_SEC}

    def check_ticket(self, cid, t):
        v = self.tickets.get(t or "")
        return bool(v and v[0] == cid and v[1] > time.time())

    # 报警记录
    def save_event(self, lv: Live, frame, out, pipes, directory):
        import cv2
        cam = lv.cam
        folder = self.root / "events" / cam.id
        folder.mkdir(parents=True, exist_ok=True)
        eid = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        raw = folder / f"{eid}.raw.jpg"
        cv2.imwrite(str(raw), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        dets = [x for r in out["results"] for x in r["detections"]]
        (folder / f"{eid}.jpg").write_bytes(_jpeg(annotate(frame, {"verdict": out["verdict"], "detections": dets}, cam.id), 1280, 85))
        notes = []
        for r, p in zip(out["results"], pipes):   # 视觉大模型：报警时才补一句说明
            v = p.get("vlm") or {}
            if v.get("enabled") and r["verdict"] in ("warn", "fail"):
                try:
                    from .agent import _vlm
                    r["vlm"] = _vlm(raw, v)
                    notes.append(r["vlm"])
                except Exception as e:
                    r["vlm"] = f"（视觉大模型没回应：{e}）"
        hits = [{**f, "pipeline": r["pipeline"]} for r in out["results"] for f in r["findings"] if f["hit"]]
        rec = {"id": eid, "camera": cam.id, "camera_name": cam.name, "location": cam.location, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "ts": time.time(), "verdict": out["verdict"], "project": cam.ai.project, "findings": hits,
               "messages": [f.get("message") for f in hits if f.get("message")], "vlm": notes, "detections": dets,
               "size": [int(frame.shape[1]), int(frame.shape[0])]}
        (folder / f"{eid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        try:                                   # 同时记进子项目的质检记录（智能质检页能看到）
            from .agent import _record
            _record(directory, raw, {**out, "meta": {"source": "stream", "camera": cam.id, "camera_name": cam.name, "event": eid}})
        except Exception:
            pass
        files = sorted(folder.glob("*.json"))
        for old in files[:-EVENTS_KEEP]:       # 超过上限删最早的
            for f in (old, old.with_suffix(".jpg"), old.with_name(old.stem + ".raw.jpg")):
                f.unlink(missing_ok=True)

    def events(self, camera="", limit=100, verdict=""):
        base = self.root / "events"
        dirs = [base / camera] if camera else [p for p in base.iterdir() if p.is_dir()]
        files = []
        for d in dirs:
            if d.is_dir():
                files += list(d.glob("*.json"))
        files.sort(key=lambda f: f.name, reverse=True)
        out = []
        for f in files:
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if verdict and rec.get("verdict") != verdict:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def event_file(self, camera, eid, raw=False):
        if not ID_RE.match(camera) or not re.match(r"^[0-9]{8}-[0-9]{6}-[0-9]{3}$", eid):
            raise FileNotFoundError("没有这条报警")
        f = self.root / "events" / camera / (f"{eid}.raw.jpg" if raw else f"{eid}.jpg")
        if not f.is_file():
            raise FileNotFoundError("截图已被清理")
        return f

    def delete_events(self, items):
        n = 0
        for it in items:
            try:
                f = self.event_file(it["camera"], it["id"])
            except (FileNotFoundError, KeyError, TypeError):
                continue
            for x in (f, f.with_suffix(".json"), f.with_name(f.stem + ".raw.jpg")):
                x.unlink(missing_ok=True)
            n += 1
        return n

    def import_events(self, project, items):
        """报警截图（原图，不带框）复制到子项目的导入目录；之后登记进图库，照常送标注、训练"""
        from . import config as deploy
        self.directory(project)
        dest = deploy.IMPORTS_DIR / project / f"stream-{time.strftime('%Y%m%d-%H%M%S')}"
        n = 0
        for it in items:
            try:
                src = self.event_file(it["camera"], it["id"], raw=True)
            except (FileNotFoundError, KeyError, TypeError):
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / f"{it['camera']}-{it['id']}.jpg")
            n += 1
        if not n:
            raise ValueError("选中的报警截图都已被清理，没有可导入的")
        return {"copied": n, "folder": str(dest)}   # 网页接着用 catalog.import_folder 登记进图库


_MANAGERS = {}


def manager(engine, directory) -> Manager:
    key = id(engine)
    if key not in _MANAGERS:
        _MANAGERS[key] = Manager(engine, directory)
    return _MANAGERS[key]


def register_routes(router, engine, directory, app):
    import base64
    from fastapi import Body, HTTPException, Query
    from fastapi.responses import FileResponse, Response, StreamingResponse
    mgr = manager(engine, directory)

    @router.get("/streams/brands", tags=["streams"])
    def brands():
        return {k: {"name": v["name"], "name_en": v["name_en"], "port": v["port"]} for k, v in BRANDS.items()}

    @router.get("/streams", tags=["streams"])
    def list_cameras():
        return [{**public(c), "state": mgr.status(cid)} for cid, c in mgr.cameras().items()]

    @router.post("/streams", tags=["streams"])
    def add_camera(cam: Camera):
        return public(mgr.save(cam, create=True))

    @router.put("/streams/{cid}", tags=["streams"])
    def update_camera(cid: str, cam: Camera):
        if cam.id != cid:
            raise ValueError("摄像头编号不能改；要换编号请删掉重建")
        return public(mgr.save(cam))

    @router.delete("/streams/{cid}", tags=["streams"])
    def delete_camera(cid: str):
        mgr.delete(cid)
        return {"deleted": cid}

    @router.post("/streams/test", tags=["streams"])
    def test_camera(cam: Camera):
        """试连：取一帧回来（不保存配置）；密码填 ****** 时用已保存的"""
        cam = mgr.merge_secret(cam, mgr.cameras().get(cam.id))
        shown = redact(build_url(cam))
        frame, ms = probe(resolve_url(cam, fresh=True), timeout=30 if cam.brand == "ezviz" else 12)   # 云端首帧慢
        return {"ok": True, "ms": ms, "width": int(frame.shape[1]), "height": int(frame.shape[0]), "stream_url": shown,
                "snapshot": "data:image/jpeg;base64," + base64.b64encode(_jpeg(frame, 800, 80)).decode()}

    @router.get("/streams/{cid}/status", tags=["streams"])
    def camera_status(cid: str):
        mgr.camera(cid)
        return mgr.status(cid)

    @router.post("/streams/{cid}/ticket", tags=["streams"])
    def camera_ticket(cid: str):
        mgr.get_live(cid)              # 顺便开始连摄像头
        return mgr.ticket(cid)

    @router.get("/streams/{cid}/snapshot.jpg", tags=["streams"])
    def snapshot(cid: str, boxes: bool = True, width: int = Query(1280, ge=160, le=4096)):
        lv = mgr.get_live(cid)
        for _ in range(60):            # 刚开始连：最多等 6 秒
            frame, _at = lv.latest()
            if frame is not None:
                break
            time.sleep(0.1)
        else:
            raise HTTPException(503, lv.error or "还没拿到画面，稍后再试")
        img = annotate(frame, lv.overlay, cid) if boxes else frame
        return Response(_jpeg(img, width, 85), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @router.get("/streams-events", tags=["streams"])
    def list_events(camera: str = "", limit: int = Query(100, ge=1, le=1000), verdict: str = ""):
        return mgr.events(camera, limit, verdict)

    @router.get("/streams-events/{camera}/{eid}.jpg", tags=["streams"])
    def event_image(camera: str, eid: str, raw: bool = False):
        return FileResponse(mgr.event_file(camera, eid, raw), media_type="image/jpeg")

    @router.post("/streams-events/delete", tags=["streams"])
    def delete_events(payload: dict = Body(...)):
        return {"deleted": mgr.delete_events(payload.get("items") or [])}

    @router.post("/streams-events/import", tags=["streams"])
    def import_events(payload: dict = Body(...)):
        return mgr.import_events(str(payload.get("project") or ""), payload.get("items") or [])

    # 看画面：<img src="/live/<id>.mjpg?ticket=…">（票据代替口令，见 /api/streams/<id>/ticket）
    @app.get("/live/{cid}.mjpg", include_in_schema=False)
    def live_mjpeg(cid: str, ticket: str = "", fps: int = Query(8, ge=1, le=25), width: int = Query(960, ge=160, le=3840)):
        if not mgr.check_ticket(cid, ticket):
            raise HTTPException(401, "票据无效或已过期，刷新页面重试")
        lv = mgr.get_live(cid)
        return StreamingResponse(lv.mjpeg(fps, width), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    t = threading.Timer(5, mgr.autostart)   # 引擎启动后：开了 AI 的摄像头自动接上
    t.daemon = True
    t.start()
