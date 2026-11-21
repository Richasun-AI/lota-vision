"""长任务的进度：工具里调用 report(...)，引擎把它写进任务表，任务记录页显示进度条、已用时间和预计还要多久。

不在任务里跑（命令行、测试）时 report 什么都不做。训练 / 调参这类卡在 Ultralytics 里的，用 watch(...) 起一个后台线程定时读结果文件再上报。
"""
from __future__ import annotations

import contextvars
import threading
import time

_reporter: contextvars.ContextVar = contextvars.ContextVar("lota_progress", default=None)


class Reporter:
    def __init__(self, write, min_interval=2.0, should_stop=None):
        self.write, self.min_interval, self.should_stop = write, min_interval, should_stop
        self.started = time.time()
        self.last = 0.0
        self.lock = threading.Lock()

    def report(self, done=None, total=None, unit="", message="", force=False, **extra):
        now = time.time()
        with self.lock:
            if not force and now - self.last < self.min_interval:
                return
            self.last = now
        data = {"started": round(self.started, 1), "at": round(now, 1), "message": str(message)[:300]}
        if done is not None:
            data["done"] = round(float(done), 3)
        if total:
            data["total"] = float(total)
        if unit:
            data["unit"] = unit
        data.update({k: v for k, v in extra.items() if isinstance(v, (int, float, str, bool)) or v is None})
        try:
            self.write(data)
        except Exception:   # 进度写不进去不能影响任务本身
            pass


def bind(reporter):
    """引擎在调用工具前绑定；返回 token，结束后 unbind(token)"""
    return _reporter.set(reporter)


def unbind(token):
    _reporter.reset(token)


def report(done=None, total=None, unit="", message="", force=False, **extra):
    r = _reporter.get()
    if r is not None:
        r.report(done, total, unit, message, force, **extra)


def stop_requested():
    """用户在任务记录里点了“提前结束”：支持的工具在合适的时候（比如调参这一组跑完）收尾"""
    r = _reporter.get()
    try:
        return bool(r is not None and r.should_stop and r.should_stop())
    except Exception:
        return False


def watch(poll, interval=10.0):
    """后台线程每 interval 秒调一次 poll()，poll 返回 report 的参数字典（或 None 跳过）。
    用法：with watch(fn): 长时间阻塞的调用"""
    return _Watch(poll, interval, _reporter.get())


class _Watch:
    def __init__(self, poll, interval, reporter):
        self.poll, self.interval, self.reporter = poll, interval, reporter
        self.stop = threading.Event()
        self.thread = None

    def _run(self):
        while not self.stop.wait(self.interval):
            self._once()

    def _once(self):
        try:
            data = self.poll()
        except Exception:
            return
        if data:
            self.reporter.report(force=True, **data)

    def __enter__(self):
        if self.reporter is not None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self._once()
        return False
