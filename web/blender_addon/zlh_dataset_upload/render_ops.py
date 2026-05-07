"""渲染执行、组合枚举相关逻辑。"""

import os
import uuid
from typing import Dict, List, Optional, Tuple, Set

import bpy
from mathutils import Vector

from .http_util import _post_render_output
from .occlusion import _cache_mesh_samples, _get_visible_objects
from . import _log


def _render_to_file(context, output_dir: str) -> str:
    """渲染当前相机视图到 output_dir，返回文件名（不含路径）。"""
    scene = context.scene
    fp_orig = scene.render.filepath
    fmt_orig = scene.render.image_settings.file_format
    scene.render.image_settings.file_format = "PNG"

    os.makedirs(output_dir, exist_ok=True)
    fname = uuid.uuid4().hex + ".png"
    out_path = os.path.join(output_dir, fname)
    try:
        scene.render.filepath = out_path
        bpy.ops.render.render(write_still=True)
    finally:
        scene.render.filepath = fp_orig
        scene.render.image_settings.file_format = fmt_orig

    if not os.path.isfile(out_path):
        raise RuntimeError(f"渲染失败，未生成文件: {out_path}")
    return fname


def _render_and_upload(context, base: str, output_dir: str,
                        visible_names: set[str],
                        affected: set[str], orig_hide: dict[str, bool]) -> None:
    """修改 affected 中物体的 hide_render，渲染并上传，然后恢复。"""
    try:
        for name in affected:
            obj = context.scene.objects.get(name)
            if not obj:
                continue
            obj.hide_render = name not in visible_names

        fname = _render_to_file(context, output_dir)

        names_str = ",".join(sorted(visible_names))
        data = _post_render_output(base, fname, names_str)

        if not data.get("ok"):
            raise RuntimeError(f"服务器返回错误: {data.get('error', 'unknown')}")
        print(f"[zlh] 已上传 outfit={names_str} file={fname}")
    finally:
        for name in affected:
            obj = context.scene.objects.get(name)
            if obj and name in orig_hide:
                obj.hide_render = orig_hide[name]


def _enumerate_effective_combinations(
    context,
    all_visible: set[str],
    removable_names: List[str],
    removable_objs: List[bpy.types.Object],
) -> List[Tuple[int, set[str]]]:
    """枚举所有 removable 组合，对每种组合做遮挡检测，返回去重后的有效组合列表。

    返回 [(mask, actually_visible_names), ...]
    - mask: bitmask，第 i 位表示 removable_names[i] 是否显示
    - actually_visible_names: 在该组合下实际通过遮挡检测的物体名
    """
    scene = context.scene
    cam = scene.camera
    depsgraph = context.evaluated_depsgraph_get()

    _log(f"[_enumerate_effective_combinations] 开始枚举组合，removable 物体数={len(removable_names)}")
    _log(f"[_enumerate_effective_combinations] removable 名称列表: {removable_names}")
    _log(f"[_enumerate_effective_combinations] all_visible 总数={len(all_visible)}: {sorted(all_visible)}")

    # 第一步：缓存所有 MESH 物体的采样点
    sample_cache: Dict[str, List[Vector]] = {}
    cached_count = 0
    for name in all_visible:
        obj = scene.objects.get(name)
        if obj and obj.type == "MESH":
            samples = _cache_mesh_samples(obj, depsgraph, 50)
            if samples:
                sample_cache[name] = samples
                cached_count += 1
    _log(f"[_enumerate_effective_combinations] 采样缓存完成：共缓存 {cached_count} 个 MESH 物体")

    # 记住原始 hide_render 状态
    orig_hide: Dict[str, bool] = {}
    for name in removable_names:
        obj = scene.objects.get(name)
        if obj:
            orig_hide[name] = obj.hide_render

    n = len(removable_names)
    total_combinations = 1 << n
    _log(f"[_enumerate_effective_combinations] 理论组合数: {total_combinations}")

    seen_signatures: Set[Tuple[str, ...]] = set()
    effective: List[Tuple[int, set[str]]] = []

    try:
        for mask in range(total_combinations):
            current_hidden: set[str] = set()
            for i in range(n):
                if not (mask >> i) & 1:
                    current_hidden.add(removable_names[i])

            # 设置 hide_render 模拟该组合
            for name in removable_names:
                obj = scene.objects.get(name)
                if obj:
                    obj.hide_render = name not in current_hidden

            context.view_layer.update()
            depsgraph = context.evaluated_depsgraph_get()

            _log(f"[_enumerate_effective_combinations] 组合 mask={mask}（隐藏={current_hidden or '无'}）开始检测")
            vis, _ = _get_visible_objects(
                context,
                hidden_set=current_hidden,
                sample_cache=sample_cache,
            )
            _log(f"[_enumerate_effective_combinations] 组合 mask={mask} 可见物体数={len(vis)}")

            sig = tuple(sorted(vis))
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                effective.append((mask, vis))
                _log(f"[_enumerate_effective_combinations] 组合 mask={mask} 新增有效组合，可见={sorted(vis)}")
            else:
                _log(f"[_enumerate_effective_combinations] 组合 mask={mask} 重复，跳过")

        _log(f"[_enumerate_effective_combinations] 枚举完成：有效组合数={len(effective)}")

    finally:
        for name in removable_names:
            obj = scene.objects.get(name)
            if obj and name in orig_hide:
                obj.hide_render = orig_hide[name]
        context.view_layer.update()

    return effective
