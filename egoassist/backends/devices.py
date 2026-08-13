"""Portable accelerator selection without importing torch at module load time."""

from __future__ import annotations

from typing import Any


def _torch(torch_module: Any | None = None):
    if torch_module is not None:
        return torch_module
    import torch

    return torch


def _available(torch_module: Any, kind: str) -> bool:
    backend = getattr(torch_module, kind, None)
    check = getattr(backend, "is_available", None)
    return bool(check and check())


def resolve_device(requested: str | None = None, *, torch_module: Any | None = None) -> str:
    """Resolve ``auto`` to the best supported torch device."""
    torch = _torch(torch_module)
    value = str(requested or "auto").strip().lower()
    if value == "auto":
        if _available(torch, "cuda"):
            return "cuda"
        backends = getattr(torch, "backends", None)
        if backends is not None and _available(backends, "mps"):
            return "mps"
        if _available(torch, "xpu"):
            return "xpu"
        return "cpu"

    if value == "cpu":
        return value
    if value == "cuda" or value.startswith("cuda:"):
        if not _available(torch, "cuda"):
            raise RuntimeError(f"Requested device '{value}', but CUDA is unavailable")
        if value.startswith("cuda:"):
            try:
                index = int(value.partition(":")[2])
            except ValueError as exc:
                raise ValueError(f"Invalid CUDA device: {value!r}") from exc
            count = getattr(torch.cuda, "device_count", lambda: 0)()
            if index < 0 or index >= count:
                raise RuntimeError(
                    f"Requested device '{value}', but only {count} CUDA device(s) are available"
                )
        return value
    if value == "mps":
        backends = getattr(torch, "backends", None)
        if backends is None or not _available(backends, "mps"):
            raise RuntimeError("Requested device 'mps', but Apple MPS is unavailable")
        return value
    if value == "xpu" or value.startswith("xpu:"):
        if not _available(torch, "xpu"):
            raise RuntimeError(f"Requested device '{value}', but Intel XPU is unavailable")
        return value
    raise ValueError(f"Unsupported device {value!r}; use auto, cpu, cuda[:N], mps, or xpu[:N]")


def preferred_dtype(torch_module: Any, device: str):
    """Choose a conservative inference dtype for a resolved device."""
    kind = device.split(":", 1)[0]
    if kind == "cuda":
        bf16_supported = getattr(torch_module.cuda, "is_bf16_supported", lambda: False)
        return torch_module.bfloat16 if bf16_supported() else torch_module.float16
    if kind in {"mps", "xpu"}:
        return torch_module.float16
    return torch_module.float32
