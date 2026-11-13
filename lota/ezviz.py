"""海康·萤石云（萤石开放平台）取流：摄像头和服务器不在同一个网络时，经萤石云拉直播流。

流程：AppKey + AppSecret → accessToken（7 天有效，缓存）→ 按设备序列号 + 通道号取直播地址（FLV / HLS / RTMP，
缓存到快过期再换）→ 交给 OpenCV / FFmpeg 读帧，后面的看画面、AI 检测、报警截图和 RTSP 摄像头完全一样。

· 设备要先用“萤石云视频”App 添加到这个开放平台账号下
· 画面开了“视频加密”时，取到的流是加密的、解不出画面：在 App 里关掉；或者填设备验证码（机身标签上 6 位大写字母），
  服务器会调用接口把这台设备的视频加密关掉
· 接口地址默认国内 https://open.ys7.com，海外账号用环境变量 LOTA_EZVIZ_API 改成对应区域的地址
"""
import hashlib
import json
import os
import re
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = os.environ.get("LOTA_EZVIZ_API", "https://open.ys7.com").rstrip("/")
PROTOCOLS = {"flv": 4, "hls": 2, "rtmp": 3}   # FLV 延迟最低，默认用它
URL_TTL = 86400                                # 直播地址要 1 天有效期，提前 10 分钟换
SERIAL_RE = re.compile(r"^[A-Za-z0-9]{6,32}$")

# 常见错误码 → 中文说明（其余的直接显示萤石返回的 msg）
ERRORS = {
    "10001": "参数错误",
    "10002": "accessToken 过期或无效",
    "10005": "AppKey 被冻结",
    "10017": "AppKey 不存在：检查萤石开放平台“我的应用”里的 AppKey",
    "10030": "AppKey 和 AppSecret 对不上",
    "20002": "设备不存在：检查序列号（机身标签上 9 位左右的字母数字）",
    "20007": "设备不在线：检查摄像头电源和网络，在萤石云视频 App 里能不能看到",
    "20008": "设备响应超时",
    "20014": "序列号不合法",
    "20018": "这个账号下没有这台设备：先用萤石云视频 App 把设备添加到开放平台对应的账号",
    "49999": "萤石云数据异常，稍后再试",
    "60060": "这台设备的直播地址没开通",
}

_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}
_urls: dict[tuple, tuple[str, float]] = {}
_decrypted: set[str] = set()


class EzvizError(ValueError):
    def __init__(self, code, msg):
        self.code = str(code)
        super().__init__(f"萤石云：{ERRORS.get(self.code, msg or '未知错误')}（错误码 {self.code}）")


def _post(path, data, timeout=10):
    req = Request(API + path, data=urlencode(data).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError) as e:
        raise ConnectionError(f"连不上萤石云（{API}）：{getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise ConnectionError("萤石云返回的不是 JSON") from e
    if str(d.get("code")) != "200":
        raise EzvizError(d.get("code"), d.get("msg"))
    return d.get("data") or {}


def _key(cam):
    return cam.ez_app_key + ":" + hashlib.sha256(cam.ez_app_secret.encode()).hexdigest()[:16]


def check(cam):
    """保存前检查（不联网）"""
    if not cam.ez_app_key or not cam.ez_app_secret:
        raise ValueError("请填萤石开放平台的 AppKey 和 AppSecret（open.ys7.com → 我的应用）")
    if not SERIAL_RE.match(cam.ez_serial or ""):
        raise ValueError("请填设备序列号（机身标签或萤石云视频 App 设备信息里，字母数字）")
    if cam.ez_protocol not in PROTOCOLS:
        raise ValueError("取流协议只能是 flv / hls / rtmp")


def token(cam, force=False):
    k = _key(cam)
    with _lock:
        t = _tokens.get(k)
        if t and not force and t[1] - time.time() > 3600:
            return t[0]
    d = _post("/api/lapp/token/get", {"appKey": cam.ez_app_key, "appSecret": cam.ez_app_secret})
    tok, exp = d.get("accessToken"), (d.get("expireTime") or 0) / 1000 or time.time() + 6 * 86400
    if not tok:
        raise ConnectionError("萤石云没有返回 accessToken")
    with _lock:
        _tokens[k] = (tok, exp)
    return tok


def _decrypt(cam, tok):
    """填了验证码：把设备的视频加密关掉（每台设备只试一次；已经关着的报错忽略）"""
    serial = cam.ez_serial.upper()
    if not cam.ez_code or serial in _decrypted:
        return
    try:
        _post("/api/lapp/device/encrypt/off", {"accessToken": tok, "deviceSerial": serial, "validateCode": cam.ez_code})
    except EzvizError:
        pass
    _decrypted.add(serial)


def live_url(cam, force=False):
    """取直播地址（缓存，快过期再换）；force=True 不用缓存（画面断了重连时）"""
    check(cam)
    serial = cam.ez_serial.upper()
    ck = (_key(cam), serial, cam.channel, cam.substream, cam.ez_protocol)
    with _lock:
        u = _urls.get(ck)
        if u and not force and u[1] - time.time() > 600:
            return u[0]
    tok = token(cam)
    _decrypt(cam, tok)
    args = {"deviceSerial": serial, "channelNo": cam.channel, "protocol": PROTOCOLS[cam.ez_protocol],
            "quality": 2 if cam.substream else 1, "expireTime": URL_TTL}
    try:
        d = _post("/api/lapp/v2/live/address/get", {"accessToken": tok, **args})
    except EzvizError as e:
        if e.code != "10002":
            raise
        d = _post("/api/lapp/v2/live/address/get", {"accessToken": token(cam, force=True), **args})
    url = d.get("url")
    if not url:
        raise ConnectionError("萤石云没有返回直播地址")
    with _lock:
        _urls[ck] = (url, time.time() + URL_TTL)
    return url


def display(cam):
    """给网页看的地址（不含任何令牌）"""
    return f"ezviz://{(cam.ez_serial or '?').upper()}/{cam.channel}?{'sub' if cam.substream else 'main'}&{cam.ez_protocol}"
