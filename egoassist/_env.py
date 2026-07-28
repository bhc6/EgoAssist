"""加载 .env（可复现地管理密钥/配置，密钥不入代码）。

优先级：进程已有的环境变量 > .env 文件。未安装 python-dotenv 时静默跳过，
仍从真实环境变量读取，不影响离线/CI。
"""

from __future__ import annotations

import os

_LOADED = False


def load_env(path: str | None = None) -> None:
    """幂等加载 .env（从当前工作目录向上查找）。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        from dotenv import find_dotenv, load_dotenv
        load_dotenv(path or find_dotenv(usecwd=True), override=False)
    except Exception:
        pass  # 没装 python-dotenv 也能跑，只是不读 .env


def env(key: str, default: str | None = None) -> str | None:
    load_env()
    return os.environ.get(key, default)
