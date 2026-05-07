"""ComfyUI nodes: pull plain text from HTTP store and push result images back to the client API."""

from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
import torch
from PIL import Image

from .bridge_store import get_input, set_output_images, set_output_text


def _tensor_batch_to_png_entries(images: torch.Tensor) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i in range(images.shape[0]):
        img = images[i]
        arr = (img.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
        pil = Image.fromarray(arr)
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        out.append({"mime": "image/png", "data_base64": b64})
    return out


def _png_entries_to_tensor(entries: list[Any]) -> torch.Tensor | None:
    """首张 `{mime?, data_base64}` → IMAGE batch [B,H,W,C] float32。"""
    if not entries:
        return None
    first = entries[0]
    if not isinstance(first, dict):
        return None
    b64 = first.get("data_base64")
    if not isinstance(b64, str) or not b64.strip():
        return None
    try:
        raw = base64.standard_b64decode(b64)
    except (ValueError, TypeError):
        return None
    try:
        pil = Image.open(io.BytesIO(raw)).convert("RGB")
    except OSError:
        return None
    arr = np.asarray(pil, dtype=np.float32) / 255.0
    t = torch.from_numpy(arr).unsqueeze(0)
    return t


class WebBridgeInput:
    """仅从 Web Bridge（/bridge/run、POST /bridge/input）读入数据；图上无输入桩，只有输出。"""

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {},
            "hidden": {
                "session_key": (
                    "STRING",
                    {"default": ""},
                ),
            },
        }

    RETURN_TYPES = ("STRING", "IMAGE")
    RETURN_NAMES = ("text", "image")
    FUNCTION = "execute"
    CATEGORY = "web_bridge"
    DESCRIPTION = (
        "仅从网络 Bridge 读取本轮请求的文本与可选图像；不在图中连接上游节点。"
        "请在网页或 API 中随请求传入 text / images。"
    )

    def execute(self, session_key: str) -> tuple[str, torch.Tensor]:
        raw = get_input(session_key)
        if isinstance(raw, str):
            text = raw
            entries: list[Any] = []
        elif isinstance(raw, dict):
            t = raw.get("text")
            text = "" if t is None else str(t)
            ent = raw.get("images")
            entries = ent if isinstance(ent, list) else []
        else:
            text = ""
            entries = []

        bridge_img = _png_entries_to_tensor(entries)
        if bridge_img is not None:
            out_img = bridge_img
        else:
            out_img = torch.zeros((1, 64, 64, 3), dtype=torch.float32)

        return (text, out_img)


class WebBridgeOutput:
    """接收图中 IMAGE / STRING，写入 Web Bridge；仅为终点节点，不向图中引出输出线（与 Save Image 同类）。"""

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {},
            "optional": {
                "images": ("IMAGE",),
                "text": ("STRING", {"default": "", "multiline": True}),
            },
            "hidden": {
                "session_key": (
                    "STRING",
                    {"default": ""},
                ),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = "web_bridge"
    DESCRIPTION = (
        "将上游图像与可选文本提交到 Web Bridge，供 GET /bridge/output 轮询。"
        "仅输入桩（图中连线流入）；无输出桩。"
    )

    def execute(self, session_key: str, images: torch.Tensor | None = None, text: str = "") -> dict[str, Any]:
        if images is not None and isinstance(images, torch.Tensor) and images.ndim == 4 and images.shape[0] > 0:
            entries = _tensor_batch_to_png_entries(images)
        else:
            entries = []
        set_output_images(session_key, entries)
        s = text if isinstance(text, str) else str(text)
        set_output_text(session_key, s)
        return {}

NODE_CLASS_MAPPINGS = {
    "WebBridgeInput": WebBridgeInput,
    "WebBridgeOutput": WebBridgeOutput,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "WebBridgeInput": "Web Bridge Input",
    "WebBridgeOutput": "Web Bridge Output",
}
