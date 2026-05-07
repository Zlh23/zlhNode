"""HTTP routes for the web client (workflows list, input buffer, queue by name, output poll)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

from aiohttp import web

import execution
import folder_paths
from server import PromptServer

from .bridge_store import clear_output, get_output, set_input
from .canvas_workflow_converter import WorkflowConverter

logger = logging.getLogger(__name__)


def _json(data: Any, status: int = 200) -> web.Response:
    r = web.json_response(data, status=status)
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r


def _preflight() -> web.Response:
    r = web.Response(status=204)
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r


def _workflows_dir() -> str:
    """与 Comfy 界面中保存的「工作流」同一目录：``user/default/workflows/``。"""
    p = os.path.join(folder_paths.get_user_directory(), "default", "workflows")
    os.makedirs(p, exist_ok=True)
    return p


def _apply_node_replacements(prompt: dict[str, Any]) -> None:
    server = PromptServer.instance
    nrm = getattr(server, "node_replace_manager", None)
    if nrm is not None and hasattr(nrm, "apply_replacements"):
        nrm.apply_replacements(prompt)


def _sensitive_keys() -> tuple[str, ...]:
    return tuple(getattr(execution, "SENSITIVE_EXTRA_DATA_KEYS", ()))


_BRIDGE_NODE_CLASS_TYPES = frozenset({"WebBridgeInput", "WebBridgeOutput"})


def _json_field_is_bridge_node_type(value: Any) -> bool:
    """部分工作流里 ``type`` 可能是列表等，不能直接 ``x in frozenset``（不可哈希会报错）。"""
    if isinstance(value, str):
        return value in _BRIDGE_NODE_CLASS_TYPES
    if isinstance(value, (list, tuple)):
        return any(_json_field_is_bridge_node_type(x) for x in value)
    return False


def _workflow_json_has_bridge_nodes(data: Any) -> bool:
    """画布节点用 ``type``，API 节点用 ``class_type``；递归扫整份 JSON（含子图等）。"""
    if isinstance(data, dict):
        if _json_field_is_bridge_node_type(data.get("class_type")):
            return True
        if _json_field_is_bridge_node_type(data.get("type")):
            return True
        for v in data.values():
            if _workflow_json_has_bridge_nodes(v):
                return True
    elif isinstance(data, list):
        for item in data:
            if _workflow_json_has_bridge_nodes(item):
                return True
    return False


def _inject_session_key_into_prompt(prompt: dict[str, Any], session_key: str) -> None:
    """每一轮运行使用独立 session_key：写回图中 Web Bridge 节点的 widgets，便于 Input/Output 在同一键上。"""
    for _nid, node in prompt.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") not in _BRIDGE_NODE_CLASS_TYPES:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
            node["inputs"] = inputs
        inputs["session_key"] = session_key


def register() -> None:
    server = PromptServer.instance

    @server.routes.options("/bridge/workflows")
    async def _opt_workflows(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.get("/bridge/workflows")
    async def bridge_list_workflows(_request: web.Request) -> web.Response:
        base = _workflows_dir()
        os.makedirs(base, exist_ok=True)
        names: list[str] = []
        for fname in sorted(f for f in os.listdir(base) if f.endswith(".json")):
            path = os.path.join(base, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and _workflow_json_has_bridge_nodes(data):
                names.append(fname[:-5])
        return _json({"workflows": names})

    @server.routes.options("/bridge/input")
    async def _opt_input(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/input")
    async def bridge_post_input(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)
        key = str(body.get("session_key", "default"))
        payload = body.get("payload")
        if payload is None or isinstance(payload, str):
            set_input(key, "" if payload is None else str(payload))
        elif isinstance(payload, dict):
            t = payload.get("text")
            if t is not None and not isinstance(t, str):
                return _json({"error": "payload.text must be a string"}, status=400)
            imgs = payload.get("images")
            if imgs is not None and not isinstance(imgs, list):
                return _json({"error": "payload.images must be an array"}, status=400)
            set_input(
                key,
                {
                    "text": str(t) if t is not None else "",
                    "images": imgs if isinstance(imgs, list) else [],
                },
            )
        else:
            return _json({"error": "payload must be a string or {text, images} object"}, status=400)
        return _json({"ok": True})

    @server.routes.options("/bridge/output/{session_key}")
    async def _opt_output(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.get("/bridge/output/{session_key}")
    async def bridge_get_output(request: web.Request) -> web.Response:
        key = request.match_info.get("session_key", "")
        data = get_output(key)
        if data is None:
            return _json({"ready": False, "images": [], "text": ""})
        images = data.get("images") if isinstance(data, dict) else []
        if not isinstance(images, list):
            images = []
        text = data.get("text", "") if isinstance(data, dict) else ""
        if not isinstance(text, str):
            text = str(text)
        return _json({"ready": True, "images": images, "text": text})

    @server.routes.options("/bridge/run")
    async def _opt_run(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/run")
    async def bridge_run(request: web.Request) -> web.Response:
        """Load workflows/<name>.json (API-format prompt), inject input store, queue prompt."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)

        try:
            name = body.get("workflow_name") or body.get("name")
            payload = body.get("input")
            if payload is None and "payload" in body:
                payload = body["payload"]

            if not name or not isinstance(name, str):
                return _json({"error": "workflow_name is required"}, status=400)

            # 每次请求新建会话键；图中 Bridge 节点由 _inject 写入同一键。
            session_key = str(uuid.uuid4())
            if payload is None or isinstance(payload, str):
                set_input(session_key, "" if payload is None else str(payload))
            elif isinstance(payload, dict):
                t = payload.get("input")
                if t is None:
                    t = payload.get("text")
                if t is not None and not isinstance(t, str):
                    return _json({"error": "input.text / input must be a string"}, status=400)
                imgs = payload.get("images")
                if imgs is not None and not isinstance(imgs, list):
                    return _json({"error": "input.images must be an array"}, status=400)
                set_input(
                    session_key,
                    {
                        "text": str(t) if t is not None else "",
                        "images": imgs if isinstance(imgs, list) else [],
                    },
                )
            else:
                return _json(
                    {"error": "input must be a string or object { text|input, images }"},
                    status=400,
                )
            clear_output(session_key)

            path = os.path.join(_workflows_dir(), f"{name}.json")
            if not os.path.isfile(path):
                return _json({"error": f"workflow file not found: {name}.json", "path": path}, status=404)

            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except OSError as e:
                return _json({"error": f"read failed: {e}"}, status=500)

            if not isinstance(data, dict):
                return _json({"error": "workflow JSON must be an object"}, status=400)

            try:
                if WorkflowConverter.is_api_format(data):
                    prompt = data
                else:
                    prompt = WorkflowConverter.convert_to_api(data)
            except Exception as e:
                logger.exception("[bridge/run] workflow JSON → API prompt 转换失败")
                return _json({"error": "canvas_to_api_failed", "message": str(e), "path": path}, status=400)

            prompt_id = str(body.get("prompt_id") or uuid.uuid4())
            _apply_node_replacements(prompt)
            _inject_session_key_into_prompt(prompt, session_key)

            partial = body.get("partial_execution_targets")
            valid = await execution.validate_prompt(prompt_id, prompt, partial)
            if not valid[0]:
                err = valid[1]
                logger.warning("[bridge/run] invalid prompt: %s", err)
                node_errors = valid[3] if len(valid) > 3 else {}
                return _json({"error": err, "node_errors": node_errors}, status=400)

            extra_data: dict[str, Any] = dict(body.get("extra_data") or {})
            if "client_id" in body:
                extra_data["client_id"] = body["client_id"]
            extra_data["create_time"] = int(time.time() * 1000)

            sensitive: dict[str, Any] = {}
            for k in _sensitive_keys():
                if k in extra_data:
                    sensitive[k] = extra_data.pop(k)

            outputs_to_execute = valid[2]
            number = float(server.number)
            server.number += 1.0

            server.prompt_queue.put((number, prompt_id, prompt, extra_data, outputs_to_execute, sensitive))
            return _json({"prompt_id": prompt_id, "number": number, "session_key": session_key})
        except Exception as ex:
            logger.exception("[bridge/run] failed")
            return _json({"error": "bridge_run_exception", "message": str(ex)}, status=500)
