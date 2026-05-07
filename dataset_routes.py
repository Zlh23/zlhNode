"""数据集：图片列表，每张图片自带 outfit/scene 元数据。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import shutil
import uuid
from typing import Any

import imagehash
from aiohttp import web
from PIL import Image

from server import PromptServer

from .bridge_routes import _json, _preflight

logger = logging.getLogger(__name__)

STATE_VERSION = 5

# 图片去重：phash 汉明距离小于此阈值视为重复图
PHASH_DUPE_THRESHOLD = 8


def _package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _work_dir() -> str:
    p = os.path.join(_package_dir(), "temp", "dataset")
    os.makedirs(p, exist_ok=True)
    return p


def _state_path() -> str:
    return os.path.join(_work_dir(), "state.json")


def _legacy_flat_image_path(index1: int) -> str:
    """旧版单文件 temp/dataset/images/{n}.png。"""
    d = os.path.join(_work_dir(), "images")
    return os.path.join(d, f"{index1}.png")


def _cells_root() -> str:
    p = os.path.join(_work_dir(), "cells")
    os.makedirs(p, exist_ok=True)
    return p


def _cell_dir(index1: int) -> str:
    p = os.path.join(_cells_root(), str(index1))
    os.makedirs(p, exist_ok=True)
    return p


def _pool_dir() -> str:
    p = os.path.join(_work_dir(), "pool")
    os.makedirs(p, exist_ok=True)
    return p


def _pool_file_path(file_id: str) -> str:
    return os.path.join(_pool_dir(), f"{file_id}.png")


def _stash_dir() -> str:
    p = os.path.join(_work_dir(), "bridge_stash")
    os.makedirs(p, exist_ok=True)
    return p


def _stash_manifest_path() -> str:
    return os.path.join(_stash_dir(), "manifest.json")


def datasets_root() -> str:
    p = os.path.join(_package_dir(), "output", "datasets")
    os.makedirs(p, exist_ok=True)
    return p


def _ndjson_job_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/x-ndjson; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-store",
    }


async def _write_ndjson_line(resp: web.StreamResponse, obj: dict[str, Any]) -> None:
    await resp.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def _valid_file_id(fid: str) -> bool:
    if not isinstance(fid, str) or len(fid) != 32:
        return False
    return all(c in "0123456789abcdef" for c in fid)


def _default_image_entry() -> dict[str, Any]:
    return {"fid": "", "outfit": "", "scene": ""}


def _normalize_image_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return _default_image_entry()
    out = _default_image_entry()
    fid = entry.get("fid")
    if isinstance(fid, str) and _valid_file_id(fid):
        out["fid"] = fid
        if os.path.isfile(_pool_file_path(fid)):
            out["fid"] = fid
        else:
            out["fid"] = ""
    out["outfit"] = str(entry.get("outfit", "") or "")
    out["scene"] = str(entry.get("scene", "") or "")
    return out


def _default_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "images": []}


def _reconcile_images(st: dict[str, Any]) -> None:
    """清理 images 中 fid 无效或文件不存在的条目，同时清理孤立 hash 缓存。"""
    raw = st.get("images")
    if not isinstance(raw, list):
        st["images"] = []
        st["image_hashes"] = {}
        return
    out: list[dict[str, Any]] = []
    seen_fids: set[str] = set()
    for entry in raw:
        e = _normalize_image_entry(entry)
        if not e["fid"] or e["fid"] in seen_fids:
            continue
        seen_fids.add(e["fid"])
        out.append(e)
    st["images"] = out
    # 清理 hash_cache 中已不存在的 fid
    hc = st.get("image_hashes")
    if isinstance(hc, dict):
        st["image_hashes"] = {k: v for k, v in hc.items() if k in seen_fids}


def _migrate_v4_to_v5(st: dict[str, Any]) -> dict[str, Any]:
    """从 v4（stringAs + cells + pool）迁移到 v5（images 数组）。"""
    images: list[dict[str, Any]] = []
    seen_fids: set[str] = set()

    # 从旧 cells 中解析出 headerId 的图片
    cells = st.get("cells")
    string_as = st.get("stringAs", [])
    if isinstance(cells, list):
        for i, cell in enumerate(cells):
            if not isinstance(cell, dict):
                continue
            hid = cell.get("headerId")
            if isinstance(hid, str) and _valid_file_id(hid) and hid not in seen_fids:
                row = i // 8  # COL_COUNT = 8
                outfit = ""
                if isinstance(string_as, list) and 0 <= row < len(string_as):
                    outfit = str(string_as[row] or "")
                scene = str(cell.get("stringB", "") or "")
                images.append({"fid": hid, "outfit": outfit, "scene": scene})
                seen_fids.add(hid)

    # 从旧 pool 中取出未出现在 cell 中的图片
    pool = st.get("pool")
    if isinstance(pool, list):
        for fid in pool:
            if isinstance(fid, str) and _valid_file_id(fid) and fid not in seen_fids:
                images.append({"fid": fid, "outfit": "", "scene": ""})
                seen_fids.add(fid)

    # 清理 pool 中不在任何引用里的孤立 png 文件
    for fname in os.listdir(_pool_dir()):
        if fname.endswith(".png"):
            fid = fname[:-4]
            if _valid_file_id(fid) and fid not in seen_fids:
                try:
                    os.remove(os.path.join(_pool_dir(), fname))
                except OSError:
                    pass

    return {"version": STATE_VERSION, "images": images}


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not os.path.isfile(path):
        st = _default_state()
        save_state(st)
        return st
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, json.JSONDecodeError):
        st = _default_state()
    if not isinstance(st, dict):
        st = _default_state()

    ver = st.get("version")

    if ver == STATE_VERSION:
        # 已经是 v5
        _reconcile_images(st)
        return st

    # 从任意低版本（v1-v4 或 None）迁移到 v5
    # 如果具有旧版数据结构（cells/pool/stringAs），从中提取图片
    if isinstance(st.get("cells"), list) or isinstance(st.get("pool"), list) or ver is None or ver < STATE_VERSION:
        st = _migrate_v4_to_v5(st)
        save_state(st)
        return st

    # 回退
    st = _default_state()
    save_state(st)
    return st


def save_state(st: dict[str, Any]) -> None:
    _reconcile_images(st)
    st["version"] = STATE_VERSION
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp." + uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _b64_to_png_bytes(data_b64: str) -> bytes:
    if len(data_b64) > 50 * 1024 * 1024:
        raise ValueError("base64 输入过长")
    raw = base64.standard_b64decode(data_b64)
    im = Image.open(io.BytesIO(raw))
    if im.mode in ("P", "PA"):
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def _save_image_file(png: bytes) -> str | None:
    """保存图片到 pool 目录，返回 fid。"""
    if len(png) > 40 * 1024 * 1024:
        return None
    fid = uuid.uuid4().hex
    path = _pool_file_path(fid)
    try:
        tmp = path + ".tmp." + uuid.uuid4().hex
        with open(tmp, "wb") as f:
            f.write(png)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("[dataset] save image: %s", e)
        return None
    return fid


def _phash_from_png(png: bytes) -> imagehash.ImageHash | None:
    """从 PNG bytes 计算 phash，失败返回 None。"""
    try:
        im = Image.open(io.BytesIO(png))
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGB")
        return imagehash.phash(im)
    except Exception:
        return None


def _shorter_outfit(a: str, b: str) -> str:
    """两个 outfit 取较短的那个。等长时取 a。"""
    return a if len(a) <= len(b) else b


def _save_image_entry(b64: str, outfit: str, scene: str) -> tuple[str | None, dict[str, Any]]:
    """统一处理：校验 base64 → 转 PNG → phash 去重 → 存文件 → 写入 state → 返回 (fid, images)。

    如果新图 phash 与已有图片高度相似，不新保存文件，而是复用已有 fid，
    并且 outfit 取两者中较短的那个。
    """
    try:
        png = _b64_to_png_bytes(b64)
    except Exception as e:
        raise ValueError(f"image_decode_failed: {e}") from e

    new_hash = _phash_from_png(png)

    st = load_state()
    imgs = st.setdefault("images", [])
    if not isinstance(imgs, list):
        imgs = []
        st["images"] = imgs

    # 已有 hash 缓存（fid -> hex）
    hash_cache: dict[str, str] = st.get("image_hashes") or {}
    if not isinstance(hash_cache, dict):
        hash_cache = {}

    # 对比已有图片
    dupe_fid: str | None = None
    if new_hash is not None:
        for entry in imgs:
            efid = entry.get("fid", "")
            if not efid or not _valid_file_id(efid):
                continue
            hex_hash = hash_cache.get(efid)
            if hex_hash:
                try:
                    existing_hash = imagehash.hex_to_hash(hex_hash)
                    if new_hash - existing_hash <= PHASH_DUPE_THRESHOLD:
                        dupe_fid = efid
                        break
                except Exception:
                    continue

    if dupe_fid is not None:
        # 重复：合并，不保存新文件
        existing_outfit = ""
        for entry in imgs:
            if entry.get("fid") == dupe_fid:
                existing_outfit = str(entry.get("outfit", "") or "")
                entry["outfit"] = _shorter_outfit(existing_outfit, outfit)
                break
        logger.info(
            "[dataset] dupe detected: new=%s... existing=%s outfit=%s -> %s",
            new_hash.__str__()[:12] if new_hash else "?",
            dupe_fid[:8],
            existing_outfit,
            outfit,
        )
        save_state(st)
        return dupe_fid, st["images"]

    # 非重复：正常保存
    fid = _save_image_file(png)
    if not fid:
        raise RuntimeError("write_failed")

    imgs.append({"fid": fid, "outfit": outfit, "scene": scene})

    # 缓存新图的 hash
    if new_hash is not None:
        hash_cache[fid] = str(new_hash)
        st["image_hashes"] = hash_cache

    save_state(st)
    return fid, st["images"]


def _bridge_stash_count() -> int:
    mp = _stash_manifest_path()
    if not os.path.isfile(mp):
        return 0
    try:
        with open(mp, encoding="utf-8") as f:
            m = json.load(f)
        n = m.get("count")
        return int(n) if isinstance(n, int) and n >= 0 else 0
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _clear_bridge_stash() -> None:
    d = _stash_dir()
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


def register() -> None:
    server = PromptServer.instance

    # --- state ---

    @server.routes.options("/bridge/dataset/state")
    async def _opt_state(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.get("/bridge/dataset/state")
    async def dataset_get_state(_request: web.Request) -> web.Response:
        st = load_state()
        images = st.get("images", [])
        h = hashlib.sha256(json.dumps(images, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return _json(
            {
                "version": st["version"],
                "images": images,
                "hash": h,
            }
        )

    @server.routes.put("/bridge/dataset/state")
    async def dataset_put_state(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)
        images_in = body.get("images")
        if not isinstance(images_in, list):
            return _json({"error": "images must be array"}, status=400)
        st = load_state()
        # 收集所有可能的 fid 并更新/新增
        new_images: list[dict[str, Any]] = []
        seen_fids: set[str] = set()
        for entry in images_in:
            e = _normalize_image_entry(entry)
            if not e["fid"] or e["fid"] in seen_fids:
                continue
            seen_fids.add(e["fid"])
            new_images.append(e)
        # 清理不再引用的文件
        for fname in os.listdir(_pool_dir()):
            if fname.endswith(".png"):
                fid = fname[:-4]
                if _valid_file_id(fid) and fid not in seen_fids:
                    try:
                        os.remove(os.path.join(_pool_dir(), fname))
                    except OSError:
                        pass
        st["images"] = new_images
        save_state(st)
        h = hashlib.sha256(json.dumps(st["images"], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return _json({"ok": True, "images": st["images"], "hash": h})

    # --- 图片上传/读取/删除 ---

    @server.routes.options("/bridge/dataset/image")
    async def _opt_image(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/image")
    async def dataset_post_image(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)
        b64 = body.get("image_base64")
        if not isinstance(b64, str) or not b64.strip():
            return _json({"error": "image_base64 required"}, status=400)
        outfit = body.get("outfit", "")
        if not isinstance(outfit, str):
            outfit = ""
        scene = body.get("scene", "")
        if not isinstance(scene, str):
            scene = ""
        try:
            fid, images = _save_image_entry(b64, outfit, scene)
        except ValueError as e:
            return _json({"error": str(e)}, status=400)
        except RuntimeError as e:
            return _json({"error": str(e)}, status=500)
        except Exception as e:
            return _json({"error": f"unexpected: {e}"}, status=500)
        return _json({"ok": True, "id": fid, "images": images})

    @server.routes.options("/bridge/dataset/image/{file_id}")
    async def _opt_image_item(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.get("/bridge/dataset/image/{file_id}")
    async def dataset_get_image(request: web.Request) -> web.Response:
        fid = request.match_info.get("file_id", "")
        if not _valid_file_id(fid):
            return _json({"error": "bad file_id"}, status=400)
        path = _pool_file_path(fid)
        if not os.path.isfile(path):
            return web.Response(status=404)
        try:
            data = open(path, "rb").read()
        except OSError as e:
            return _json({"error": str(e)}, status=500)
        r = web.Response(body=data, content_type="image/png")
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Cache-Control"] = "no-store"
        return r

    @server.routes.delete("/bridge/dataset/image/{file_id}")
    async def dataset_delete_image(request: web.Request) -> web.Response:
        fid = request.match_info.get("file_id", "")
        if not _valid_file_id(fid):
            return _json({"error": "bad file_id"}, status=400)
        path = _pool_file_path(fid)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            return _json({"error": str(e)}, status=500)
        st = load_state()
        imgs = st.get("images")
        if isinstance(imgs, list):
            st["images"] = [x for x in imgs if isinstance(x, dict) and x.get("fid") != fid]
        save_state(st)
        return _json({"ok": True, "images": st.get("images", [])})

    # --- Blender 渲染输出接口 ---

    @server.routes.options("/bridge/render/output")
    async def _opt_render_output(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/render/output")
    async def bridge_render_output(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)

        b64 = body.get("image_base64")
        if not isinstance(b64, str) or not b64.strip():
            return _json({"error": "image_base64 required"}, status=400)
        outfit = body.get("object_names", "")
        if not isinstance(outfit, str):
            outfit = ""

        try:
            fid, images = _save_image_entry(b64, outfit, "")
        except ValueError as e:
            return _json({"error": str(e)}, status=400)
        except RuntimeError as e:
            return _json({"error": str(e)}, status=500)
        except Exception as e:
            return _json({"error": f"unexpected: {e}"}, status=500)

        return _json({"ok": True, "id": fid, "images": images})

    # --- 清空 ---

    @server.routes.options("/bridge/dataset/clear-images")
    async def _opt_clear(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/clear-images")
    async def dataset_clear_images(_request: web.Request) -> web.Response:
        pd = _pool_dir()
        if os.path.isdir(pd):
            for name in os.listdir(pd):
                if not name.endswith(".png"):
                    continue
                p = os.path.join(pd, name)
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
        st = load_state()
        st["images"] = []
        save_state(st)
        return _json({"ok": True})

    # --- 导出 ---

    @server.routes.options("/bridge/dataset/save")
    async def _opt_dataset_save(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/save")
    async def dataset_save(_request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers=_ndjson_job_headers())
        await resp.prepare(_request)
        st = load_state()
        images = st.get("images", [])
        if not isinstance(images, list):
            images = []

        # 按 outfit 分组导出
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in images:
            e = _normalize_image_entry(entry)
            if not e["fid"]:
                continue
            src = _pool_file_path(e["fid"])
            if not os.path.isfile(src):
                continue
            outfit = e["outfit"] or "unknown"
            groups.setdefault(outfit, []).append(e)

        total = sum(len(items) for items in groups.values())
        if total == 0:
            rel0 = os.path.relpath(datasets_root(), _package_dir()).replace("\\", "/")
            await _write_ndjson_line(resp, {"type": "done", "ok": True, "saved": [], "package_relative": rel0})
            await resp.write_eof()
            return resp

        await _write_ndjson_line(resp, {"type": "progress", "phase": "export", "current": 0, "total": total})
        saved: list[dict[str, Any]] = []
        exported = 0
        for outfit, items in groups.items():
            # 每个 outfit 一个子文件夹
            folder_name = outfit.replace("/", "_").replace("\\", "_")
            sub = os.path.join(datasets_root(), folder_name)
            os.makedirs(sub, exist_ok=True)
            for idx, entry in enumerate(items, 1):
                src = _pool_file_path(entry["fid"])
                png_path = os.path.join(sub, f"{idx}.png")
                txt_path = os.path.join(sub, f"{idx}.txt")
                try:
                    shutil.copy2(src, png_path)
                    scene = entry.get("scene", "")
                    txt_body = f"{outfit}, {scene}" if scene else outfit
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(txt_body)
                except OSError as e:
                    await _write_ndjson_line(
                        resp,
                        {"type": "done", "ok": False, "error": "write_failed", "message": str(e), "path": sub},
                    )
                    await resp.write_eof()
                    return resp
                saved.append({"folder": folder_name, "index": idx})
                exported += 1
                await _write_ndjson_line(
                    resp, {"type": "progress", "phase": "export", "current": exported, "total": total}
                )

        rel = os.path.relpath(datasets_root(), _package_dir())
        await _write_ndjson_line(
            resp,
            {"type": "done", "ok": True, "saved": saved, "package_relative": rel.replace("\\", "/")},
        )
        await resp.write_eof()
        return resp

    # --- Bridge 暂存（保留，用于 Workflow 集成） ---

    @server.routes.options("/bridge/dataset/bridge-stash")
    async def _opt_stash(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/bridge-stash")
    async def dataset_bridge_stash(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)
        images = body.get("images")
        if not isinstance(images, list):
            return _json({"error": "images must be array"}, status=400)
        if len(images) > 64:
            return _json({"error": "too many images"}, status=400)
        _clear_bridge_stash()
        n = 0
        for im in images:
            if not isinstance(im, dict):
                continue
            b64 = im.get("data_base64")
            if not isinstance(b64, str) or not b64.strip():
                continue
            try:
                png = _b64_to_png_bytes(b64)
            except Exception:
                continue
            if len(png) > 40 * 1024 * 1024:
                continue
            out_path = os.path.join(_stash_dir(), f"{n}.png")
            try:
                with open(out_path, "wb") as f:
                    f.write(png)
                n += 1
            except OSError:
                continue
        try:
            with open(_stash_manifest_path(), "w", encoding="utf-8") as f:
                json.dump({"count": n}, f)
        except OSError as e:
            return _json({"error": "manifest_write", "message": str(e)}, status=500)
        return _json({"ok": True, "count": n})

    @server.routes.options("/bridge/dataset/bridge-stash/import")
    async def _opt_stash_imp(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/bridge-stash/import")
    async def dataset_bridge_stash_import(_request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers=_ndjson_job_headers())
        await resp.prepare(_request)
        cnt = _bridge_stash_count()
        if cnt == 0:
            await _write_ndjson_line(resp, {"type": "done", "ok": True, "imported": 0})
            await resp.write_eof()
            return resp
        await _write_ndjson_line(resp, {"type": "progress", "phase": "import", "current": 0, "total": cnt})
        st = load_state()
        imported = 0
        for si in range(cnt):
            src = os.path.join(_stash_dir(), f"{si}.png")
            if not os.path.isfile(src):
                break
            try:
                with open(src, "rb") as f:
                    png = f.read()
            except OSError:
                continue
            fid = _save_image_file(png)
            if not fid:
                continue
            imgs = st.setdefault("images", [])
            if not isinstance(imgs, list):
                imgs = []
                st["images"] = imgs
            imgs.append({"fid": fid, "outfit": "", "scene": ""})
            imported += 1
            await _write_ndjson_line(
                resp, {"type": "progress", "phase": "import", "current": imported, "total": cnt}
            )
        _clear_bridge_stash()
        try:
            if os.path.isfile(_stash_manifest_path()):
                os.remove(_stash_manifest_path())
        except OSError:
            pass
        save_state(st)
        await _write_ndjson_line(resp, {"type": "done", "ok": True, "imported": imported})
        await resp.write_eof()
        return resp

    # 保留旧接口（GET pool image）用于兼容旧引用，返回 404
    @server.routes.get("/bridge/dataset/pool/gallery/{file_id}/image")
    async def _old_pool_image(request: web.Request) -> web.Response:
        # 重定向到新接口
        fid = request.match_info.get("file_id", "")
        if not _valid_file_id(fid):
            return _json({"error": "bad file_id"}, status=400)
        path = _pool_file_path(fid)
        if not os.path.isfile(path):
            return web.Response(status=404)
        try:
            data = open(path, "rb").read()
        except OSError as e:
            return _json({"error": str(e)}, status=500)
        r = web.Response(body=data, content_type="image/png")
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Cache-Control"] = "no-store"
        return r
