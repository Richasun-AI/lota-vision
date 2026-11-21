"""机器资源实时曲线（训练时的小窗口）：GPU 占用 / 显存 / 温度 / 功率、CPU 占用、内存。

后台线程每 2 秒采一次，保留最近 10 分钟；有人在看（最近 2 分钟请求过）才采，没人看就停。
读 Linux 的 /proc 和 nvidia-smi，不依赖 psutil。有的显卡（常见于笔记本）不报告功率，那时功率为空。
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque

INTERVAL = 2.0
KEEP = 300            # 300 × 2 秒 = 10 分钟
IDLE_STOP = 120       # 这么久没人看就停止采样

_lock = threading.Lock()
_samples: deque = deque(maxlen=KEEP)
_state = {"thread": None, "last_read": 0.0, "cpu": None}


def _cpu_times():
    with open("/proc/stat", encoding="ascii") as f:
        parts = [float(x) for x in f.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
    return sum(parts), idle


def _cpu_percent():
    try:
        total, idle = _cpu_times()
    except (OSError, ValueError, IndexError):
        return None
    prev, _state["cpu"] = _state["cpu"], (total, idle)
    if not prev or total <= prev[0]:
        return None
    return round(100.0 * (1 - (idle - prev[1]) / (total - prev[0])), 1)


def _memory():
    try:
        info = {}
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                key, value = line.split(":", 1)
                info[key] = int(value.split()[0]) * 1024
        total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return {"used": total - avail, "total": total, "percent": round(100.0 * (total - avail) / total, 1)}
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        return None


def _num(text):
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return None          # [N/A]、[Not Supported]


def _gpus():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,power.draw,power.limit,temperature.gpu",
             "--format=csv,noheader,nounits"], text=True, timeout=5)
    except Exception:
        return []
    gpus = []
    for line in out.strip().splitlines():
        cols = line.split(",")
        if len(cols) < 8:
            continue
        gpus.append({"index": int(_num(cols[0]) or 0), "name": cols[1].strip(), "util": _num(cols[2]),
                     "mem_used": _num(cols[3]), "mem_total": _num(cols[4]), "power": _num(cols[5]),
                     "power_limit": _num(cols[6]), "temp": _num(cols[7])})
    return gpus


def sample():
    return {"t": round(time.time(), 1), "cpu": _cpu_percent(), "mem": _memory(), "gpu": _gpus()}


def _loop():
    while time.time() - _state["last_read"] < IDLE_STOP:
        s = sample()
        with _lock:
            _samples.append(s)
        time.sleep(INTERVAL)
    with _lock:
        _state["thread"] = None


def history(since: float = 0.0) -> dict:
    """since 之后的采样（前端第一次拿全部，之后只拿新的）"""
    with _lock:
        _state["last_read"] = time.time()
        if _state["thread"] is None:
            _state["thread"] = threading.Thread(target=_loop, daemon=True, name="lota-sysmon")
            _state["thread"].start()
        rows = [s for s in _samples if s["t"] > since]
    return {"interval": INTERVAL, "samples": rows}
