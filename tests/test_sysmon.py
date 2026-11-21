"""机器资源小窗口：nvidia-smi 解析（功率读不到的显卡）、按 since 取新采样"""
import unittest
from unittest import mock

from lota import sysmon


class SysmonTest(unittest.TestCase):
    def test_gpu_parse_with_missing_power(self):
        out = "0, NVIDIA GeForce RTX 4060, 91, 2381, 8188, [N/A], 115.00, 65\n"
        with mock.patch.object(sysmon.subprocess, "check_output", lambda *a, **k: out):
            g = sysmon._gpus()[0]
        self.assertEqual((g["util"], g["mem_used"], g["power"], g["power_limit"], g["temp"]), (91, 2381, None, 115.0, 65))

    def test_no_nvidia_smi(self):
        def boom(*a, **k):
            raise FileNotFoundError("nvidia-smi")
        with mock.patch.object(sysmon.subprocess, "check_output", boom):
            self.assertEqual(sysmon._gpus(), [])

    def test_history_since_and_starts_sampler(self):
        with mock.patch.object(sysmon.threading, "Thread") as T:
            sysmon._samples.clear()
            sysmon._state["thread"] = None
            sysmon._samples.extend([{"t": 1.0}, {"t": 2.0}, {"t": 3.0}])
            self.assertEqual([s["t"] for s in sysmon.history(1.5)["samples"]], [2.0, 3.0])
            T.return_value.start.assert_called_once()
            sysmon.history(0)
            T.return_value.start.assert_called_once()     # 已经在采样：不重复起线程
        sysmon._state["thread"] = None
        sysmon._samples.clear()


if __name__ == "__main__":
    unittest.main()
