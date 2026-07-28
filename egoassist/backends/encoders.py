"""冻结 CLIP/SigLIP 检索编码器（三个真实后端共用）。

作用：把帧与文本查询编码到**同一个跨模态空间**，供检索与时间定位使用。
把它与"作答 VLM"解耦，好处是：无论作答走本地 Qwen 还是云端 API，
检索空间都一致 —— 消融时只变作答模型，对比才干净（也对齐 MemoLens 用
X-Clip/CLIP4Clip 的做法，便于同编码器下比较）。

优先用 open_clip（模型多、社区活跃）；未装则回退 transformers 的 CLIP。
全部 lazy import：不装这些依赖时，import 本模块仍不报错，只有实例化才需要。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np


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
                 pretrained: str = "laion2b_s34b_b79k", device: str = "cuda"):
        self.device = device
        self._backend = None
        try:
            import open_clip  # type: ignore
            import torch
            self._torch = torch
            self._model, _, self._preprocess = open_clip.create_model_and_transforms(
                model_name, pretrained=pretrained, device=device)
            self._model.eval()
            self._tokenizer = open_clip.get_tokenizer(model_name)
            self.dim = self._model.text_projection.shape[1]
            self._backend = "open_clip"
        except Exception:
            # 回退：transformers CLIP
            import torch
            from transformers import CLIPModel, CLIPProcessor
            self._torch = torch
            hf = "openai/clip-vit-base-patch32"
            self._model = CLIPModel.from_pretrained(hf).to(device).eval()
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
        with t.no_grad():
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
        with t.no_grad():
            if self._backend == "open_clip":
                tok = self._tokenizer([text]).to(self.device)
                feats = self._model.encode_text(tok)[0]
            else:
                x = self._proc(text=[text], return_tensors="pt", padding=True).to(self.device)
                feats = self._model.get_text_features(**x)[0]
        return self._l2(feats.float().cpu().numpy())
