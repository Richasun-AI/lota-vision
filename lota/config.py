"""部署相关的路径和地址：全部从环境变量读，默认值是通用的单机配置。

自己的服务器把实际值写进项目根目录的 deploy.env（不进仓库，模板见 deploy.env.example），
scripts/engine.sh 启动时会加载它。代码里不要再写死 IP 或绝对路径。
"""
import os
from pathlib import Path


def _path(name, default):
    return Path(os.environ.get(name) or default).expanduser()


# 数据盘：原图、导入、回收站都在它下面；也是 Label Studio 的本地文件根目录（LOCAL_FILES_DOCUMENT_ROOT）
DATA_ROOT = _path("LOTA_DATA_ROOT", "~/lota-data")
# 引擎数据目录（--home）：数据库、子项目、权重
HOME = _path("LOTA_HOME", DATA_ROOT / "lota" / "runtime")
WEIGHTS_DIR = _path("LOTA_WEIGHTS_DIR", HOME / "weights")
# 默认的找图目录（全部原图）
IMAGES_DIR = _path("LOTA_IMAGES_DIR", DATA_ROOT / "images")
# 抽图、找图、视频抽帧等结果复制到这里
IMPORTS_DIR = _path("LOTA_IMPORTS_DIR", DATA_ROOT / "imports")
# 可选：照片类型归类的旧流水线（emb_index / hq_filter 模块和 work/ 数据），没有就不用这部分功能
LEGACY_PIPELINE = _path("LOTA_LEGACY_PIPELINE", DATA_ROOT / "pipeline")
# 浏览器打开 Label Studio 用的地址（引擎自己连 Label Studio 用 127.0.0.1）
LS_PUBLIC_URL = os.environ.get("LOTA_LS_PUBLIC_URL") or "http://localhost:8081"
# 私有扩展（不进仓库）：自己业务专用的工作流模板、质检管线、视觉大模型提示词等，放在这个目录
PRIVATE_DIR = _path("LOTA_PRIVATE_DIR", HOME.parent / "private")

_PLACEHOLDERS = {"{DATA_ROOT}": DATA_ROOT, "{HOME}": HOME, "{WEIGHTS_DIR}": WEIGHTS_DIR, "{IMAGES_DIR}": IMAGES_DIR,
                 "{IMPORTS_DIR}": IMPORTS_DIR, "{LEGACY_PIPELINE}": LEGACY_PIPELINE}


def _expand(v):
    if isinstance(v, str):
        for k, p in _PLACEHOLDERS.items():
            v = v.replace(k, p.as_posix())
        return v
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def private_json(name, default=None):
    """读私有目录里的 JSON；字符串里的 {WEIGHTS_DIR} 等占位符换成实际路径。没有文件就返回 default。"""
    import json
    f = PRIVATE_DIR / name
    if not f.is_file():
        return default
    return _expand(json.loads(f.read_text(encoding="utf-8")))


def private_text(name, default=""):
    f = PRIVATE_DIR / name
    return f.read_text(encoding="utf-8") if f.is_file() else default
