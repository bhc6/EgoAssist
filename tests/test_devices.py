from types import SimpleNamespace

import pytest

from egoassist.backends.devices import preferred_dtype, resolve_device


def fake_torch(*, cuda=False, mps=False, xpu=False, count=1, bf16=False):
    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda, device_count=lambda: count,
                             is_bf16_supported=lambda: bf16),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)),
        xpu=SimpleNamespace(is_available=lambda: xpu),
        bfloat16="bf16", float16="fp16", float32="fp32",
    )


def test_auto_prefers_cuda_then_mps_then_xpu_then_cpu():
    assert resolve_device(torch_module=fake_torch(cuda=True, mps=True, xpu=True)) == "cuda"
    assert resolve_device(torch_module=fake_torch(mps=True, xpu=True)) == "mps"
    assert resolve_device(torch_module=fake_torch(xpu=True)) == "xpu"
    assert resolve_device(torch_module=fake_torch()) == "cpu"


def test_explicit_unavailable_accelerator_is_actionable():
    with pytest.raises(RuntimeError, match="CUDA is unavailable"):
        resolve_device("cuda", torch_module=fake_torch())
    with pytest.raises(RuntimeError, match="MPS is unavailable"):
        resolve_device("mps", torch_module=fake_torch())


def test_cuda_index_and_dtype():
    torch = fake_torch(cuda=True, count=2, bf16=True)
    assert resolve_device("cuda:1", torch_module=torch) == "cuda:1"
    assert preferred_dtype(torch, "cuda:1") == "bf16"
    assert preferred_dtype(torch, "cpu") == "fp32"

