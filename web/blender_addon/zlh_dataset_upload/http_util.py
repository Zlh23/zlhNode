"""HTTP 请求工具：与 ComfyUI bridge 服务通信。"""

import json
import urllib.request
from typing import Any, Dict, Optional


def _normalize_base(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _http_json(method: str, url: str, body_obj: Optional[Dict[str, Any]], timeout: float = 180.0) -> dict:
    payload = None
    if body_obj is not None:
        payload = json.dumps(body_obj).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    data = json.loads(text) if text else {}
    if not isinstance(data, dict):
        raise RuntimeError("服务器返回非 JSON 对象")
    return data


def _post_render_output(api_base: str, filename: str, object_names: str) -> dict:
    """POST 只传文件路径和物体名，不再传图片数据。"""
    url = api_base + "/bridge/render/output"
    body = {
        "filename": filename,
        "object_names": object_names,
    }
    return _http_json("POST", url, body)
