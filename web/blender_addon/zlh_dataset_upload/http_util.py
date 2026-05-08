"""本地文件工具：渲染结果写入 output_dir 供 ComfyUI 读取。"""

import json
import os
import uuid
from typing import Any, Dict, Optional


def _normalize_base(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _http_json(method: str, url: str, body_obj: Optional[Dict[str, Any]], timeout: float = 180.0) -> dict:
    """保留原 HTTP 请求工具，但不再被渲染流程调用。"""
    import urllib.request
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


_SOURCES_MANIFEST = "sources.json"


def _post_render_output(output_dir: str, filename: str, object_names: str) -> dict:
    """将渲染结果写入本地文件，不再通过 HTTP POST。

    每次调用在 output_dir 中：
    1. 图片已经由 _render_to_file 保存
    2. 在 sources.json 中追加一条 source 记录

    Args:
        output_dir: 输出目录（WSL 共享路径，Windows 侧可写）
        filename: 图片文件名（已在 output_dir 中）
        object_names: 逗号分隔的物体名（即 tags）

    Returns:
        模拟 HTTP 返回的 dict，包含 ok 和必要的字段。
        不再返回真正的 id/album_id，改为返回 {ok: True, local: True}。
    """
    if not filename or not object_names:
        return {"ok": False, "error": "缺少必要参数"}

    # 校验图片文件已存在
    img_path = os.path.join(output_dir, filename)
    if not os.path.isfile(img_path):
        return {"ok": False, "error": f"图片文件不存在: {filename}"}

    # 读取或创建 sources.json
    manifest_path = os.path.join(output_dir, _SOURCES_MANIFEST)
    sources: list[dict[str, Any]] = []
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                sources = json.load(f)
        except (OSError, json.JSONDecodeError):
            sources = []
    if not isinstance(sources, list):
        sources = []

    # 追加新 source 记录
    entry = {
        "id": uuid.uuid4().hex,
        "filename": filename,
        "object_names": object_names,
        # source_type 固定为 "blender_render"，Web 端可据此区分不同类型的 source
        "source_type": "blender_render",
    }
    sources.append(entry)

    # 写入 sources.json（原子写入：先写 tmp 再 rename）
    try:
        tmp = manifest_path + ".tmp." + uuid.uuid4().hex
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sources, f, ensure_ascii=False, indent=2)
        os.replace(tmp, manifest_path)
    except OSError as e:
        return {"ok": False, "error": f"写入 sources.json 失败: {e}"}

    return {"ok": True, "local": True, "source_id": entry["id"]}
