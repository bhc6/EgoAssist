"""真实作答后端：本地 Qwen-VL 与可选云端视觉模型。

三者共用 encoders.ClipEncoder 做检索嵌入（检索空间一致，只有作答模型变），
各自实现 caption() 与 answer()。作答置信度默认用 verbalized confidence
（要求模型输出一行 CONFIDENCE: <0-1>）；本地/OpenAI 可进一步用 logprob 更准。

全部 lazy import：不装 torch/transformers/openai/google 时，import 本模块不报错，
仅实例化对应后端才需要相应依赖 —— 因此不影响 MockBackend 的离线链路与测试。
"""

from __future__ import annotations

from egoassist.prompts import prompt_text

import base64
import io
import re
from typing import List, Sequence

import numpy as np

from .base import Answer, VLMBackend
from .devices import preferred_dtype, resolve_device
from .encoders import ClipEncoder, _to_pil

_SYS = (
    prompt_text('real/_SYS.1')
)
_CAP = prompt_text('real/_CAP.1')


def _build_prompt(query: str, evidence_texts: list[str],
                  event_time: str | None) -> str:
    """把时间上下文、证据 caption、问题拼成最终 prompt。"""
    ev = "\n".join(f"- {c}" for c in evidence_texts[:5])
    time_line = (f"[Memory evidence from {event_time}]\n" if event_time else "")
    return prompt_text('real/_build_prompt.1', v0=time_line, v1=ev, v2=query)


def _parse_conf(text: str, default: float = 0.5) -> float:
    m = re.search(r"CONFIDENCE:\s*([01](?:\.\d+)?)", text)
    if not m:
        return default
    return float(np.clip(float(m.group(1)), 0.0, 1.0))


def _strip_conf(text: str) -> str:
    return re.sub(r"\n?CONFIDENCE:\s*[01](?:\.\d+)?\s*$", "", text).strip()


def _valid_generation(text: str) -> bool:
    cleaned = _strip_conf(text)
    if not cleaned:
        return False
    alnum = sum(char.isalnum() for char in cleaned)
    unique_ratio = len(set(cleaned)) / len(cleaned)
    return alnum >= 2 and unique_ratio >= 0.05


def _truncated_generation(text: str) -> bool:
    return bool(re.search(r"\bCONF(?:IDENCE)?\s*:?\s*$", text.strip(), re.I))


def _data_url(frame: np.ndarray) -> str:
    img = _to_pil(frame)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


class _ClipEmbedBackend(VLMBackend):
    """共享检索嵌入 + 关键帧下采样；子类实现 caption()/answer()。"""

    def __init__(self, encoder: ClipEncoder | None = None, device: str = "auto",
                 max_frames: int = 4):
        # Timeline captioning/QA does not use visual embeddings. Load CLIP only
        # when embedding is actually requested, not for every cloud API client.
        self._encoder = encoder
        self._encoder_device = device
        self.max_frames = max_frames

    @property
    def encoder(self) -> ClipEncoder:
        if self._encoder is None:
            self._encoder = ClipEncoder(device=self._encoder_device)
        return self._encoder

    @property
    def dim(self) -> int:
        return self.encoder.dim

    def embed_frames(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        return self.encoder.embed_image(self._few(frames))

    def embed_text(self, text: str) -> np.ndarray:
        return self.encoder.embed_text(text)

    def _few(self, frames: Sequence[np.ndarray]) -> List[np.ndarray]:
        frames = list(frames)
        if len(frames) <= self.max_frames:
            return frames
        idx = np.linspace(0, len(frames) - 1, self.max_frames).round().astype(int)
        return [frames[i] for i in idx]

    def embed_frames_each(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        """逐帧 L2 嵌入（不平均），shape (N, D)。

        覆盖 VLMBackend 默认实现，利用 ClipEncoder 的批量 forward pass，
        把 localize() 内原本 O(n_windows) 次 GPU 调用降至一次 GPU batch。
        注意：此处不做 _few 下采样，关键帧本身已经过精简。
        """
        if not frames:
            return np.empty((0, self.dim), dtype=np.float32)
        return self.encoder.embed_image_batch(list(frames))


# ============================ 本地 Qwen2.5-VL ============================
class LocalQwenBackend(_ClipEmbedBackend):
    """本地 Qwen2.5-VL（默认 3B，bfloat16，不量化）。12G 显存友好；
    作答走本地，检索走 CLIP。Blackwell(sm_120) 上 INT4 量化会产生乱码输出，
    故默认改为 bf16 全精度；显存确实不足时可手动传 load_in_4bit=True。"""

    def __init__(self, model_id: str = "Qwen/Qwen2.5-VL-3B-Instruct",
                 load_in_4bit: bool = False, device: str = "auto",
                 encoder: ClipEncoder | None = None, encoder_device: str | None = None,
                 max_frames: int = 4, caption_tokens: int = 48,
                 answer_tokens: int = 128):
        if encoder is None and encoder_device:
            encoder = ClipEncoder(device=encoder_device)
        super().__init__(encoder=encoder, device=device, max_frames=max_frames)
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor
        try:
            model_class = AutoModelForImageTextToText
        except ImportError:
            try:
                from transformers import Qwen2_5_VLForConditionalGeneration as model_class
            except ImportError:
                from transformers import Qwen2VLForConditionalGeneration as model_class
        self.device = resolve_device(device, torch_module=torch)
        dtype = preferred_dtype(torch, self.device)
        kw = {"dtype": dtype}
        if load_in_4bit:
            if not self.device.startswith("cuda"):
                raise ValueError("4-bit bitsandbytes loading requires a CUDA device")
            from transformers import BitsAndBytesConfig
            kw["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_compute_dtype=dtype,
                bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True)
            kw.pop("dtype", None)
            kw["device_map"] = {"": self.device}
        self.torch = torch
        self.caption_tokens = caption_tokens
        self.answer_tokens = answer_tokens
        self.model = model_class.from_pretrained(model_id, **kw).eval()
        if not load_in_4bit:
            self.model = self.model.to(self.device)
        self.processor = AutoProcessor.from_pretrained(model_id)

    def _model_device(self):
        device = getattr(self.model, "device", None)
        if device is not None:
            return device
        try:
            return next(self.model.parameters()).device
        except (StopIteration, AttributeError):
            return self.device

    def _run(self, images: List[np.ndarray], prompt: str, system: str | None = None,
             max_new_tokens: int | None = None) -> str:
        content = [{"type": "image", "image": _to_pil(im)} for im in images]
        content.append({"type": "text", "text": prompt})
        messages = ([{"role": "system", "content": system}] if system else [])
        messages += [{"role": "user", "content": content}]
        text = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        pil_images = [_to_pil(im) for im in images]
        inputs = self.processor(text=[text], images=pil_images or None,
                                return_tensors="pt").to(self._model_device())
        with self.torch.inference_mode():
            out = self.model.generate(
                **inputs, max_new_tokens=max_new_tokens or self.answer_tokens,
                do_sample=False,
            )
        gen = out[:, inputs.input_ids.shape[1]:]
        return self.processor.batch_decode(gen, skip_special_tokens=True)[0].strip()

    def caption(self, frames: Sequence[np.ndarray]) -> str:
        raw = self._run(self._few(frames), _CAP, max_new_tokens=self.caption_tokens)
        return raw if _valid_generation(raw) else "[invalid visual caption]"

    def answer(self, query, evidence_texts, evidence_frames, *,
               event_time: str | None = None) -> Answer:
        prompt = _build_prompt(query, evidence_texts, event_time)
        raw = self._run(
            self._few(evidence_frames), prompt, system=_SYS,
            max_new_tokens=self.answer_tokens,
        )
        if not _valid_generation(raw) or _truncated_generation(raw):
            return Answer(text="Evidence is insufficient; the model response was invalid.", confidence=0.0)
        return Answer(text=_strip_conf(raw), confidence=_parse_conf(raw))


# ============================ 云端 Gemini ============================
class GeminiBackend(_ClipEmbedBackend):
    """作答走 Gemini（贴近 VisionClaw 的 Gemini 路径），检索走本地 CLIP。"""

    def __init__(self, model: str = "gemini-2.5-flash", api_key: str | None = None,
                 device: str = "auto", encoder: ClipEncoder | None = None, max_frames: int = 4):
        super().__init__(encoder=encoder, device=device, max_frames=max_frames)
        import os

        import google.generativeai as genai

        from .._env import load_env
        load_env()
        genai.configure(api_key=api_key or os.environ.get("GEMINI_API_KEY"))
        self.model = genai.GenerativeModel(model)

    def _run(self, images: List[np.ndarray], prompt: str) -> str:
        parts = [_to_pil(im) for im in images] + [prompt]
        r = self.model.generate_content(parts)
        return (r.text or "").strip()

    def caption(self, frames: Sequence[np.ndarray]) -> str:
        raw = self._run(self._few(frames), _CAP)
        return raw if _valid_generation(raw) else "[invalid visual caption]"

    def answer(self, query, evidence_texts, evidence_frames, *,
               event_time: str | None = None) -> Answer:
        prompt = f"{_SYS}\n\n{_build_prompt(query, evidence_texts, event_time)}"
        raw = self._run(self._few(evidence_frames), prompt)
        if not _valid_generation(raw) or _truncated_generation(raw):
            return Answer(text="Evidence is insufficient; the model response was invalid.", confidence=0.0)
        return Answer(text=_strip_conf(raw), confidence=_parse_conf(raw))


# ============================ 云端 OpenAI ============================
class OpenAIBackend(_ClipEmbedBackend):
    """作答走 OpenAI 多模态（如 gpt-4o），检索走本地 CLIP。可选 logprob 置信度。"""

    def __init__(self, model: str = "gpt-4o", api_key: str | None = None,
                 use_logprobs: bool = True, device: str = "auto",
                 encoder: ClipEncoder | None = None, max_frames: int = 4):
        super().__init__(encoder=encoder, device=device, max_frames=max_frames)
        import os

        from openai import OpenAI

        from .._env import load_env
        load_env()
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        self.model = model
        self.use_logprobs = use_logprobs

    def _run(self, images: List[np.ndarray], prompt: str, system: str | None = None):
        content = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": _data_url(im)}} for im in images]
        messages = ([{"role": "system", "content": system}] if system else [])
        messages += [{"role": "user", "content": content}]
        r = self.client.chat.completions.create(
            model=self.model, messages=messages, max_tokens=192,
            logprobs=self.use_logprobs, temperature=0)
        choice = r.choices[0]
        text = choice.message.content or ""
        # logprob 置信度：答案 token 平均概率
        lp = None
        if self.use_logprobs and getattr(choice, "logprobs", None) and choice.logprobs.content:
            ps = [np.exp(t.logprob) for t in choice.logprobs.content]
            lp = float(np.mean(ps)) if ps else None
        return text, lp

    def caption(self, frames: Sequence[np.ndarray]) -> str:
        text, _ = self._run(self._few(frames), _CAP)
        return text.strip() if _valid_generation(text) else "[invalid visual caption]"

    def answer(self, query, evidence_texts, evidence_frames, *,
               event_time: str | None = None) -> Answer:
        prompt = _build_prompt(query, evidence_texts, event_time)
        text, lp = self._run(self._few(evidence_frames), prompt, system=_SYS)
        if not _valid_generation(text) or _truncated_generation(text):
            return Answer(text="Evidence is insufficient; the model response was invalid.", confidence=0.0)
        verbal = _parse_conf(text, default=lp if lp is not None else 0.5)
        # 融合 verbalized 与 logprob（若有）
        conf = verbal if lp is None else float(np.clip(0.5 * verbal + 0.5 * lp, 0, 1))
        return Answer(text=_strip_conf(text), confidence=conf)


# ============================ 云端 DashScope Qwen ============================
class DashScopeQwenBackend(_ClipEmbedBackend):
    """DashScope Qwen-VL/Omni视觉后端；检索仍使用同一OpenCLIP编码器。"""

    def __init__(self, model: str = "qwen3-vl-plus", api_key: str | None = None,
                 device: str = "auto", encoder: ClipEncoder | None = None,
                 max_frames: int = 4):
        super().__init__(encoder=encoder, device=device, max_frames=max_frames)
        import os

        import dashscope
        from dashscope import MultiModalConversation

        from .._env import load_env
        load_env()
        self.api_key = api_key or os.environ.get("QWEN_API_KEY") or os.environ.get(
            "DASHSCOPE_API_KEY"
        )
        if not self.api_key:
            raise ValueError("QWEN_API_KEY or DASHSCOPE_API_KEY is required")
        dashscope.api_key = self.api_key
        self.client = MultiModalConversation
        self.model = model
        self.usage = {"input_tokens": 0, "output_tokens": 0, "image_tokens": 0}

    def _run(self, images: List[np.ndarray], prompt: str, system: str | None = None) -> str:
        content = [{"image": _data_url(image)} for image in images]
        content.append({"text": prompt})
        messages = ([{"role": "system", "content": [{"text": system}]}]
                    if system else [])
        messages.append({"role": "user", "content": content})
        response = self.client.call(
            model=self.model,
            api_key=self.api_key,
            messages=messages,
            max_tokens=192,
            temperature=0,
            result_format="message",
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"DashScope request failed: {response.status_code} "
                f"{getattr(response, 'code', '')} {getattr(response, 'message', '')}"
            )
        usage = response.usage or {}
        details = usage.get("input_tokens_details", {})
        self.usage["input_tokens"] += int(usage.get("input_tokens", 0))
        self.usage["output_tokens"] += int(usage.get("output_tokens", 0))
        self.usage["image_tokens"] += int(details.get("image_tokens", 0))
        choices = response.output.get("choices", [])
        if not choices:
            return ""
        parts = choices[0]["message"].get("content", [])
        return "".join(part.get("text", "") for part in parts).strip()

    def caption(self, frames: Sequence[np.ndarray]) -> str:
        raw = self._run(self._few(frames), _CAP)
        return raw if _valid_generation(raw) else "[invalid visual caption]"

    def answer(self, query, evidence_texts, evidence_frames, *,
               event_time: str | None = None) -> Answer:
        prompt = _build_prompt(query, evidence_texts, event_time)
        raw = self._run(self._few(evidence_frames), prompt, system=_SYS)
        if not _valid_generation(raw) or _truncated_generation(raw):
            return Answer(
                text="Evidence is insufficient; the model response was invalid.",
                confidence=0.0,
            )
        return Answer(text=_strip_conf(raw), confidence=_parse_conf(raw))
