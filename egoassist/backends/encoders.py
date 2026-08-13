"""冻结 CLIP/SigLIP 检索编码器（三个真实后端共用）。

作用：把帧与文本查询编码到**同一个跨模态空间**，供检索与时间定位使用。
把它与"作答 VLM"解耦，好处是：无论作答走本地 Qwen 还是云端 API，
检索空间都一致 —— 消融时只变作答模型，对比才干净（也对齐 MemoLens 用
X-Clip/CLIP4Clip 的做法，便于同编码器下比较）。

优先用 open_clip（模型多、社区活跃）；未装则回退 transformers 的 CLIP。
全部 lazy import：不装这些依赖时，import 本模块仍不报错，只有实例化才需要。
"""

from __future__ import annotations

import os
from typing import Sequence

import numpy as np

from .devices import resolve_device


def _to_pil(frame: np.ndarray):
    from PIL import Image
    arr = frame
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    return Image.fromarray(arr[:, :, :3])


class ClipEncoder:
    """冻结图文双塔编码器。默认 open_clip ViT-B-32（轻，8G 显存无压力）。"""

    def __init__(self, model_name: str = "ViT-B-32",
                 pretrained: str = "laion2b_s34b_b79k", device: str = "auto",
                 batch_size: int | None = None):
        import torch

        self.device = resolve_device(device, torch_module=torch)
        configured_batch = (batch_size if batch_size is not None
                            else int(os.environ.get("EGOASSIST_EMBED_BATCH_SIZE", "64")))
        if configured_batch < 1:
            raise ValueError("CLIP embedding batch size must be at least 1")
        self.batch_size = configured_batch
        self._backend = None
        # Warn only when CPU was explicitly requested; auto may legitimately resolve to CPU.
        if str(device).strip().lower() == "cpu":
            try:
                import torch as _t
                if _t.cuda.is_available():
                    import warnings
                    warnings.warn(
                        "ClipEncoder: CUDA is available but device='cpu' — "
                        "retrieval will be ~10× slower. Pass device='cuda'.",
                        RuntimeWarning, stacklevel=2)
            except ImportError:
                pass
        try:
            import open_clip  # type: ignore
            self._torch = torch
            self._model, _, self._preprocess = open_clip.create_model_and_transforms(
                model_name, pretrained=pretrained, device=self.device)
            self._model.eval()
            self._tokenizer = open_clip.get_tokenizer(model_name)
            self.dim = self._model.text_projection.shape[1]
            self._backend = "open_clip"
        except Exception:
            # 回退：transformers CLIP
            from transformers import CLIPModel, CLIPProcessor
            self._torch = torch
            hf = "openai/clip-vit-base-patch32"
            self._model = CLIPModel.from_pretrained(hf).to(self.device).eval()
            self._proc = CLIPProcessor.from_pretrained(hf)
            self.dim = self._model.config.projection_dim
            self._backend = "hf"

    def _l2(self, v):
        return (v / (np.linalg.norm(v) + 1e-8)).astype(np.float32)

    @property
    def torch(self):
        return self._torch

    def embed_image(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        if len(frames) == 0:
            return np.zeros(self.dim, dtype=np.float32)
        t = self._torch
        pil = [_to_pil(f) for f in frames]
        with t.inference_mode():
            if self._backend == "open_clip":
                x = t.stack([self._preprocess(p) for p in pil]).to(self.device)
                feats = self._model.encode_image(x)
            else:
                x = self._proc(images=pil, return_tensors="pt").to(self.device)
                feats = self._model.get_image_features(**x)
            feats = feats.mean(dim=0)                 # 事件级：多帧取平均
        return self._l2(feats.float().cpu().numpy())

    def embed_text(self, text: str) -> np.ndarray:
        t = self._torch
        with t.inference_mode():
            if self._backend == "open_clip":
                tok = self._tokenizer([text]).to(self.device)
                feats = self._model.encode_text(tok)[0]
            else:
                x = self._proc(text=[text], return_tensors="pt", padding=True).to(self.device)
                feats = self._model.get_text_features(**x)[0]
        return self._l2(feats.float().cpu().numpy())

    def _l2_batch(self, v: np.ndarray) -> np.ndarray:
        """逐行 L2 归一化，返回 (N, D) float32。"""
        norms = np.linalg.norm(v, axis=1, keepdims=True) + 1e-8
        return (v / norms).astype(np.float32)

    def embed_image_batch(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        """一次 GPU forward 返回逐帧 L2 归一化向量，shape (N, D)。

        与 embed_image 不同：不做多帧平均，每帧单独返回，
        供 localize() 滑窗打分用，避免 O(n_windows) 次重复推理。
        """
        if len(frames) == 0:
            return np.empty((0, self.dim), dtype=np.float32)
        t = self._torch
        chunks = []
        with t.inference_mode():
            for start in range(0, len(frames), self.batch_size):
                pil = [_to_pil(f) for f in frames[start:start + self.batch_size]]
                if self._backend == "open_clip":
                    x = t.stack([self._preprocess(p) for p in pil]).to(self.device)
                    feats = self._model.encode_image(x)
                else:
                    x = self._proc(images=pil, return_tensors="pt").to(self.device)
                    feats = self._model.get_image_features(**x)
                chunks.append(feats.float().cpu().numpy())
        return self._l2_batch(np.concatenate(chunks, axis=0))
