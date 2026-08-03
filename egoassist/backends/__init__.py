from .base import Answer, VLMBackend
from .mock import MockBackend
from .real import (
    DashScopeQwenBackend, GeminiBackend, LocalQwenBackend,
    OpenAIBackend,
)

__all__ = [
    "Answer", "VLMBackend", "MockBackend",
    "OpenAIBackend", "GeminiBackend", "DashScopeQwenBackend",
    "LocalQwenBackend",
]


def build_backend(name: str | None = None, **kw):
    """按名字构建后端：mock / local / gemini / openai。

    name 缺省时读环境变量 EGOASSIST_BACKEND（默认 mock）；模型/设备也可由
    .env 覆盖（EGOASSIST_LOCAL_MODEL / EGOASSIST_GEMINI_MODEL / EGOASSIST_OPENAI_MODEL /
    EGOASSIST_DEVICE）。真实后端 lazy import，不装依赖时 mock 仍可用。
    """
    from .._env import env

    name = (name or env("EGOASSIST_BACKEND", "mock")).lower()
    device = kw.pop("device", None) or env("EGOASSIST_DEVICE", "cuda")

    if name == "mock":
        return MockBackend(**kw)
    if name in ("local", "qwen", "local_qwen"):
        kw.setdefault("model_id", env("EGOASSIST_LOCAL_MODEL", "Qwen/Qwen3-VL-2B-Instruct"))
        return LocalQwenBackend(device=device, **kw)
    if name == "gemini":
        kw.setdefault("model", env("EGOASSIST_GEMINI_MODEL", "gemini-2.5-flash"))
        return GeminiBackend(device=device, **kw)
    if name == "openai":
        kw.setdefault("model", env("EGOASSIST_OPENAI_MODEL", "gpt-4o"))
        return OpenAIBackend(device=device, **kw)
    if name in ("dashscope", "qwen_api", "qwen-cloud", "omni"):
        kw.setdefault("model", env("EGOASSIST_QWEN_API_MODEL", "qwen3-vl-plus"))
        return DashScopeQwenBackend(device=device, **kw)
    raise ValueError(f"未知后端: {name}（可选 mock/local/gemini/openai）")
