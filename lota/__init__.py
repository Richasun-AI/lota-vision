"""Standalone LOTA workflow engine."""

__version__ = "0.1.0"

import os as _os

# 改名兼容（原名 AILabel）：旧的环境变量 AILABEL_* 自动当作 LOTA_* 用，新名字优先
for _k, _v in list(_os.environ.items()):
    if _k.startswith("AILABEL_"):
        _os.environ.setdefault("LOTA_" + _k[len("AILABEL_"):], _v)
