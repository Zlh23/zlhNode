"""数据集：图册（Album）管理，每个图册有一组 images + tags（标注）。"""

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

from aiohttp import web
from PIL import Image

from server import PromptServer

from .bridge_routes import _json, _preflight

logger = logging.getLogger(__name__)

ALBUM_STATE_VERSION = 6


def _package_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _work_dir() -> str:
    p = os.path.join(_package_dir(), "temp", "dataset")
    os.makedirs(p, exist_ok=True)
    return p


def _state_path() -> str:
    return os.path.join(_work_dir(), "state.json")


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


def _blender_render_dir() -> str:
    p = os.path.join(_package_dir(), "temp", "blender_render")
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


def _valid_album_id(aid: str) -> bool:
    if not isinstance(aid, str) or len(aid) != 32:
        return False
    return all(c in "0123456789abcdef" for c in aid)


# ────────────────────────────────── Album 数据模型 ──────────────────────────────────


def _default_album() -> dict[str, Any]:
    return {"aid": "", "tags": "", "images": []}


def _default_state() -> dict[str, Any]:
    return {"version": ALBUM_STATE_VERSION, "albums": []}


def _normalize_album(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    aid = entry.get("aid")
    if not isinstance(aid, str) or not _valid_album_id(aid):
        return None
    tags = str(entry.get("tags") or "")
    raw_images = entry.get("images")
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw_images, list):
        for img in raw_images:
            if not isinstance(img, dict):
                continue
            fid = img.get("fid")
            if isinstance(fid, str) and _valid_file_id(fid) and fid not in seen:
                if os.path.isfile(_pool_file_path(fid)):
                    images.append({"fid": fid})
                    seen.add(fid)
    if not images:
        return None
    return {"aid": aid, "tags": tags, "images": images}


def _reconcile_albums(st: dict[str, Any]) -> None:
    raw = st.get("albums")
    if not isinstance(raw, list):
        st["albums"] = []
        return
    out: list[dict[str, Any]] = []
    seen_aids: set[str] = set()
    for entry in raw:
        a = _normalize_album(entry)
        if a is None or a["aid"] in seen_aids:
            continue
        seen_aids.add(a["aid"])
        out.append(a)
    st["albums"] = out


def _compute_album_hash(albums: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(albums, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ────────────────────────────────── 状态读写 ──────────────────────────────────


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

    if ver == ALBUM_STATE_VERSION:
        _reconcile_albums(st)
        return st

    # 从旧版迁移：v5 的 images 数组 → albums
    old_images = st.get("images")
    if isinstance(old_images, list):
        albums: list[dict[str, Any]] = []
        for entry in old_images:
            if not isinstance(entry, dict):
                continue
            fid = entry.get("fid")
            if not isinstance(fid, str) or not _valid_file_id(fid):
                continue
            if not os.path.isfile(_pool_file_path(fid)):
                continue
            outfit = str(entry.get("outfit") or "")
            scene = str(entry.get("scene") or "")
            tags = f"{outfit}, {scene}" if scene else outfit
            aid = uuid.uuid4().hex
            albums.append({"aid": aid, "tags": tags, "images": [{"fid": fid}]})
        st = {"version": ALBUM_STATE_VERSION, "albums": albums}
        save_state(st)
        return st

    # 回退
    st = _default_state()
    save_state(st)
    return st


def save_state(st: dict[str, Any]) -> None:
    _reconcile_albums(st)
    st["version"] = ALBUM_STATE_VERSION
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp." + uuid.uuid4().hex
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ────────────────────────────────── 图片文件操作 ──────────────────────────────────


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


def _cleanup_orphan_pool_files(st: dict[str, Any]) -> None:
    """删除 pool 中不被任何 album 引用的图片文件。"""
    referenced: set[str] = set()
    for album in st.get("albums", []):
        if isinstance(album, dict):
            for img in album.get("images", []):
                if isinstance(img, dict):
                    fid = img.get("fid")
                    if isinstance(fid, str):
                        referenced.add(fid)
    for fname in os.listdir(_pool_dir()):
        if fname.endswith(".png"):
            fid = fname[:-4]
            if _valid_file_id(fid) and fid not in referenced:
                try:
                    os.remove(os.path.join(_pool_dir(), fname))
                except OSError:
                    pass


# ────────────────────────────────── Blender 渲染接口 ──────────────────────────────────


# ─────── _load_blender_sources() -> list[dict[str, Any]]:
    """从 blender_render 目录读取 sources.json，返回 source 列表。"""
    src_dir = _blender_render_dir()
    manifest_path = os.path.join(src_dir, "sources.json")
    if not os.path.isfile(manifest_path):
        return []
    try:
        with open(manifest_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(sources, list):
        return []
    # 过滤：只保留图片仍在的 source
    valid: list[dict[str, Any]] = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        fname = s.get("filename", "")
        if not fname:
            continue
        if os.path.isfile(os.path.join(src_dir, fname)):
            valid.append(s)
    return valid


def _import_blender_source_to_album(source: dict[str, Any]) -> dict[str, Any] | None:
    """将单个 blender source 导入为一个 album，返回 album dict 或 None。

    导入成功后，从 sources.json 中移除该记录，并删除对应的图片文件（已复制到 pool）。
    """
    fname = source.get("filename", "")
    source_id = source.get("id", "")
    tags = source.get("object_names", "")
    if not fname or not source_id:
        return None
    src_dir = _blender_render_dir()
    img_path = os.path.join(src_dir, fname)
    if not os.path.isfile(img_path):
        return None
    try:
        with open(img_path, "rb") as f:
            png = f.read()
    except OSError:
        return None
    if not png:
        return None
    fid = _save_image_file(png)
    if not fid:
        return None
    aid = uuid.uuid4().hex
    album = {"aid": aid, "tags": tags, "images": [{"fid": fid}]}
    # 加载状态并追加
    st = load_state()
    albums = st.setdefault("albums", [])
    if not isinstance(albums, list):
        albums = []
        st["albums"] = albums
    albums.append(album)
    save_state(st)

    # 导入成功后：从 sources.json 移除已导入的记录，删除图片文件
    _remove_blender_source(source_id, delete_file=True)
    return album


def _remove_blender_source(source_id: str, delete_file: bool = False) -> None:
    """从 sources.json 中移除指定 id 的 source。"""
    src_dir = _blender_render_dir()
    manifest_path = os.path.join(src_dir, "sources.json")
    if not os.path.isfile(manifest_path):
        return
    try:
        with open(manifest_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(sources, list):
        return
    new_sources: list[dict[str, Any]] = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        if s.get("id") == source_id:
            if delete_file:
                fname = s.get("filename", "")
                if fname:
                    fpath = os.path.join(src_dir, fname)
                    try:
                        if os.path.isfile(fpath):
                            os.remove(fpath)
                    except OSError:
                        pass
            continue
        new_sources.append(s)

    try:
        tmp = manifest_path + ".tmp." + uuid.uuid4().hex
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new_sources, f, ensure_ascii=False, indent=2)
        os.replace(tmp, manifest_path)
    except OSError:
        pass


# ────────────────────────────────── 注册路由 ──────────────────────────────────


def register() -> None:
    server = PromptServer.instance

    # ── state ──

    @server.routes.options("/bridge/dataset/state")
    async def _opt_state(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.get("/bridge/dataset/state")
    async def dataset_get_state(_request: web.Request) -> web.Response:
        st = load_state()
        albums = st.get("albums", [])
        h = _compute_album_hash(albums)
        return _json({"version": st["version"], "albums": albums, "hash": h})

    @server.routes.put("/bridge/dataset/state")
    async def dataset_put_state(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)
        albums_in = body.get("albums")
        if not isinstance(albums_in, list):
            return _json({"error": "albums must be array"}, status=400)
        st = load_state()
        new_albums: list[dict[str, Any]] = []
        seen_aids: set[str] = set()
        for entry in albums_in:
            a = _normalize_album(entry)
            if a is None or a["aid"] in seen_aids:
                continue
            seen_aids.add(a["aid"])
            new_albums.append(a)
        st["albums"] = new_albums
        _cleanup_orphan_pool_files(st)
        save_state(st)
        h = _compute_album_hash(st["albums"])
        return _json({"ok": True, "albums": st["albums"], "hash": h})

    # ── 图片上传（创建新 album）──

    @server.routes.options("/bridge/dataset/image")
    async def _opt_image(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/image")
    async def dataset_post_image(request: web.Request) -> web.Response:
        """上传一张图片，创建新 album。"""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)
        b64 = body.get("image_base64")
        if not isinstance(b64, str) or not b64.strip():
            return _json({"error": "image_base64 required"}, status=400)
        tags = body.get("tags", "")
        if not isinstance(tags, str):
            tags = ""
        try:
            png = _b64_to_png_bytes(b64)
        except Exception as e:
            return _json({"error": f"image_decode_failed: {e}"}, status=400)
        fid = _save_image_file(png)
        if not fid:
            return _json({"error": "write_failed"}, status=500)
        aid = uuid.uuid4().hex
        st = load_state()
        albums = st.setdefault("albums", [])
        if not isinstance(albums, list):
            albums = []
            st["albums"] = albums
        albums.append({"aid": aid, "tags": tags, "images": [{"fid": fid}]})
        save_state(st)
        return _json({"ok": True, "id": fid, "album_id": aid, "albums": st["albums"]})

    # ── 图片读取 ──

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
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return _json({"error": str(e)}, status=500)
        r = web.Response(body=data, content_type="image/png")
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Cache-Control"] = "no-store"
        return r

    # ── 删除单张图片（从 album 中移除，如果 album 空了则删 album）──

    @server.routes.delete("/bridge/dataset/image/{file_id}")
    async def dataset_delete_image(request: web.Request) -> web.Response:
        fid = request.match_info.get("file_id", "")
        if not _valid_file_id(fid):
            return _json({"error": "bad file_id"}, status=400)
        st = load_state()
        albums = st.get("albums", [])
        if isinstance(albums, list):
            for album in albums:
                if not isinstance(album, dict):
                    continue
                imgs = album.get("images", [])
                if isinstance(imgs, list):
                    album["images"] = [x for x in imgs if isinstance(x, dict) and x.get("fid") != fid]
        # 删除空 album
        st["albums"] = [a for a in albums if isinstance(a, dict) and a.get("images")]
        # 删除文件
        path = _pool_file_path(fid)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        save_state(st)
        return _json({"ok": True, "albums": st.get("albums", [])})

    # ── 往指定 album 添加子图 ──

    @server.routes.options("/bridge/dataset/album/{album_id}/image")
    async def _opt_album_image(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/album/{album_id}/image")
    async def dataset_post_album_image(request: web.Request) -> web.Response:
        aid = request.match_info.get("album_id", "")
        if not _valid_album_id(aid):
            return _json({"error": "bad album_id"}, status=400)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)
        b64 = body.get("image_base64")
        if not isinstance(b64, str) or not b64.strip():
            return _json({"error": "image_base64 required"}, status=400)
        try:
            png = _b64_to_png_bytes(b64)
        except Exception as e:
            return _json({"error": f"image_decode_failed: {e}"}, status=400)
        fid = _save_image_file(png)
        if not fid:
            return _json({"error": "write_failed"}, status=500)

        st = load_state()
        albums = st.setdefault("albums", [])
        target = None
        for a in albums:
            if isinstance(a, dict) and a.get("aid") == aid:
                target = a
                break
        if target is None:
            return _json({"error": "album not found"}, status=404)
        imgs = target.setdefault("images", [])
        if not isinstance(imgs, list):
            imgs = []
            target["images"] = imgs
        imgs.append({"fid": fid})
        save_state(st)
        return _json({"ok": True, "id": fid, "albums": st["albums"]})

    # ── 删除 album ──

    @server.routes.options("/bridge/dataset/album/{album_id}")
    async def _opt_album(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.delete("/bridge/dataset/album/{album_id}")
    async def dataset_delete_album(request: web.Request) -> web.Response:
        aid = request.match_info.get("album_id", "")
        if not _valid_album_id(aid):
            return _json({"error": "bad album_id"}, status=400)
        st = load_state()
        albums = st.get("albums", [])
        if not isinstance(albums, list):
            return _json({"ok": True, "albums": []})
        removed_fids: set[str] = set()
        for a in albums:
            if isinstance(a, dict) and a.get("aid") == aid:
                for img in a.get("images", []):
                    if isinstance(img, dict):
                        f = img.get("fid")
                        if isinstance(f, str):
                            removed_fids.add(f)
                break
        st["albums"] = [a for a in albums if isinstance(a, dict) and a.get("aid") != aid]
        # 删除文件
        for fid in removed_fids:
            p = _pool_file_path(fid)
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        save_state(st)
        return _json({"ok": True, "albums": st.get("albums", [])})

    # ── Blender 渲染输出接口 ──

    # ── 清空 ──

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
        st["albums"] = []
        save_state(st)
        return _json({"ok": True})

    # ── 导出 ──

    @server.routes.options("/bridge/dataset/save")
    async def _opt_dataset_save(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/save")
    async def dataset_save(_request: web.Request) -> web.StreamResponse:
        """导出：遍历所有 album，每张子图导出一个 PNG + 一个 TXT（内容为 album.tags）。"""
        resp = web.StreamResponse(headers=_ndjson_job_headers())
        await resp.prepare(_request)
        st = load_state()
        albums = st.get("albums", [])
        if not isinstance(albums, list):
            albums = []

        # 收集所有 (fid, tags) 对
        entries: list[tuple[str, str]] = []
        for album in albums:
            if not isinstance(album, dict):
                continue
            tags = str(album.get("tags") or "")
            for img in album.get("images", []):
                if not isinstance(img, dict):
                    continue
                fid = img.get("fid")
                if isinstance(fid, str) and _valid_file_id(fid):
                    src = _pool_file_path(fid)
                    if os.path.isfile(src):
                        entries.append((fid, tags))

        total = len(entries)
        if total == 0:
            rel0 = os.path.relpath(datasets_root(), _package_dir()).replace("\\", "/")
            await _write_ndjson_line(resp, {"type": "done", "ok": True, "saved": [], "package_relative": rel0})
            await resp.write_eof()
            return resp

        await _write_ndjson_line(resp, {"type": "progress", "phase": "export", "current": 0, "total": total})
        saved: list[dict[str, Any]] = []
        exported = 0
        for idx, (fid, tags) in enumerate(entries, 1):
            src = _pool_file_path(fid)
            png_path = os.path.join(datasets_root(), f"{idx}.png")
            txt_path = os.path.join(datasets_root(), f"{idx}.txt")
            try:
                shutil.copy2(src, png_path)
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(tags)
            except OSError as e:
                await _write_ndjson_line(
                    resp,
                    {"type": "done", "ok": False, "error": "write_failed", "message": str(e), "path": datasets_root()},
                )
                await resp.write_eof()
                return resp
            saved.append({"index": idx})
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

    # ── Bridge 暂存 ──

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
            albums = st.setdefault("albums", [])
            if not isinstance(albums, list):
                albums = []
                st["albums"] = albums
            # 每个暂存图创建一个新 album
            aid = uuid.uuid4().hex
            albums.append({"aid": aid, "tags": "", "images": [{"fid": fid}]})
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

    # ── Blender 本地 Sources ──

    @server.routes.options("/bridge/dataset/blender-sources")
    async def _opt_blender_sources(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.get("/bridge/dataset/blender-sources")
    async def dataset_blender_sources(_request: web.Request) -> web.Response:
        """返回 blender_render 目录中最新的 source 列表（含来源类型，不含图片数据）。"""
        sources = _load_blender_sources()
        # 为每个 source 添加图片 URL 路径信息
        result: list[dict[str, Any]] = []
        for s in sources:
            fname = s.get("filename", "")
            result.append({
                "id": s.get("id", ""),
                "filename": fname,
                "object_names": s.get("object_names", ""),
                "source_type": s.get("source_type", "blender_render"),
                # 客户端可通过此 URL 获取图片
                "image_url": f"/bridge/dataset/blender-source-image/{fname}",
            })
        return _json({"sources": result})

    @server.routes.options("/bridge/dataset/blender-source-image/{filename}")
    async def _opt_blender_source_image(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.get("/bridge/dataset/blender-source-image/{filename}")
    async def dataset_blender_source_image(request: web.Request) -> web.Response:
        """返回 blender source 的 PNG 图片。"""
        fname = request.match_info.get("filename", "")
        if not fname or "/" in fname or "\\" in fname or not fname.endswith(".png"):
            return _json({"error": "invalid filename"}, status=400)
        path = os.path.join(_blender_render_dir(), fname)
        if not os.path.isfile(path):
            return web.Response(status=404)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return _json({"error": str(e)}, status=500)
        r = web.Response(body=data, content_type="image/png")
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Cache-Control"] = "no-store"
        return r

    @server.routes.options("/bridge/dataset/blender-sources/import")
    async def _opt_blender_sources_import(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/blender-sources/import")
    async def dataset_blender_sources_import(request: web.Request) -> web.Response:
        """导入指定的 blender source 为 album。"""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "invalid JSON"}, status=400)

        source_id = body.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            return _json({"error": "source_id required"}, status=400)

        sources = _load_blender_sources()
        target = None
        for s in sources:
            if s.get("id") == source_id:
                target = s
                break
        if target is None:
            return _json({"error": "source not found"}, status=404)

        album = _import_blender_source_to_album(target)
        if album is None:
            return _json({"error": "import failed"}, status=500)

        st = load_state()
        return _json({"ok": True, "album": album, "albums": st.get("albums", [])})

    @server.routes.options("/bridge/dataset/blender-sources/import-all")
    async def _opt_blender_sources_import_all(_request: web.Request) -> web.Response:
        return _preflight()

    @server.routes.post("/bridge/dataset/blender-sources/import-all")
    async def dataset_blender_sources_import_all(_request: web.Request) -> web.Response:
        """导入所有尚未导入的 blender source 为 album。"""
        sources = _load_blender_sources()
        imported: list[dict[str, Any]] = []
        errors: list[str] = []
        for s in sources:
            album = _import_blender_source_to_album(s)
            if album:
                imported.append({"source_id": s.get("id", ""), "album_id": album["aid"]})
            else:
                errors.append(f"导入失败: {s.get('filename', '?')}")
        st = load_state()
        return _json({"ok": True, "imported": imported, "errors": errors, "albums": st.get("albums", [])})

    # 保留旧接口（GET pool image）用于兼容旧引用，返回 404
    @server.routes.get("/bridge/dataset/pool/gallery/{file_id}/image")
    async def _old_pool_image(request: web.Request) -> web.Response:
        fid = request.match_info.get("file_id", "")
        if not _valid_file_id(fid):
            return _json({"error": "bad file_id"}, status=400)
        path = _pool_file_path(fid)
        if not os.path.isfile(path):
            return web.Response(status=404)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return _json({"error": str(e)}, status=500)
        r = web.Response(body=data, content_type="image/png")
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Cache-Control"] = "no-store"
        return r
