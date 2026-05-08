"""GPU 加速遮挡检测：基于 IndexOB 的可见物体检测。

核心思路：
  通过 Compositor Viewer Node 读取 Cycles 的 Object Index pass，
  精确知道哪些物体实际出现在当前相机渲染结果中。

  关键经验：
  - Cycles 的 Object Index 输出名是 "Object Index"
  - 必须通过 Viewer Node 读取，不能用 EXR 文件加载
  - 渲染前清空场景中其他 mesh 的 pass_index 防止干扰
  - Cycles 无边缘混合问题，0.01% 阈值即可过滤噪声

用法：Blender 中按 Ctrl+Shift+Q 触发
"""

import os
import tempfile
import traceback
from typing import Dict, List, Optional

import bpy
import numpy as np
from mathutils import Vector

from . import _log

RENDER_RESOLUTION_PERCENT = 100


# ════════════════════════════════════════════════════════════
# 视锥体裁剪
# ════════════════════════════════════════════════════════════

def _build_frustum_planes(scene):
    """构建相机视锥体 6 个平面 (normal, d)，法线指向内部。"""
    cam = scene.camera
    depsgraph = bpy.context.evaluated_depsgraph_get()
    cs = cam.data.clip_start
    ce = cam.data.clip_end

    eval_cam = cam.evaluated_get(depsgraph)
    eval_cam_matrix = eval_cam.matrix_world
    cam_pos = eval_cam_matrix.translation
    cam_dir = eval_cam_matrix.to_quaternion() @ Vector((0, 0, -1))

    frame = cam.data.view_frame(scene=scene)
    near_corners = [eval_cam_matrix @ Vector((p.x, p.y, p.z)) for p in frame]
    far_corners = [p + cam_dir * (ce - cs) for p in near_corners]
    near_center = sum(near_corners, Vector()) / 4

    def _plane(a, b, c):
        n = (b - a).cross(c - a).normalized()
        return n, -n.dot(a)

    n_near, d_near = _plane(near_corners[0], near_corners[1], near_corners[2])
    if n_near.dot(near_center - cam_pos) < 0:
        n_near, d_near = -n_near, -d_near

    n_far, d_far = _plane(far_corners[0], far_corners[2], far_corners[1])
    if n_far.dot(near_center - (near_center + cam_dir * (ce - cs))) < 0:
        n_far, d_far = -n_far, -d_far

    sides = []
    for i in range(4):
        j = (i + 1) % 4
        n, d = _plane(cam_pos, near_corners[i], near_corners[j])
        if n.dot(near_center) + d < 0:
            n, d = -n, -d
        sides.append((n, d))

    return [*sides, (n_near, d_near), (n_far, d_far)]


def _aabb_in_frustum(frustum_planes, min_c, max_c):
    """AABB 是否与视锥体相交。"""
    for n, d in frustum_planes:
        px = max_c.x if n.x >= 0 else min_c.x
        py = max_c.y if n.y >= 0 else min_c.y
        pz = max_c.z if n.z >= 0 else min_c.z
        if n.dot(Vector((px, py, pz))) + d < 0:
            return False
    return True


# ════════════════════════════════════════════════════════════
# 渲染 & Compositor 管理
# ════════════════════════════════════════════════════════════

def _save_render_state(scene) -> dict:
    render = scene.render
    vl = scene.view_layers[0]
    return {
        "engine": render.engine,
        "resolution_percentage": render.resolution_percentage,
        "use_pass_object_index": vl.use_pass_object_index,
        "use_compositing": render.use_compositing,
        "file_format": render.image_settings.file_format,
        "cycles_samples": scene.cycles.samples if hasattr(scene, "cycles") else 0,
    }


def _restore_render_state(scene, state: dict):
    render = scene.render
    vl = scene.view_layers[0]
    render.engine = state["engine"]
    render.resolution_percentage = state["resolution_percentage"]
    vl.use_pass_object_index = state["use_pass_object_index"]
    render.use_compositing = state["use_compositing"]
    render.image_settings.file_format = state["file_format"]
    if hasattr(scene, "cycles") and state.get("cycles_samples"):
        scene.cycles.samples = state["cycles_samples"]


def _setup_compositor_for_indexob(scene):
    """设置 Compositor: RLayers."Object Index" -> Viewer，用于读取 IndexOB。

    注意：Cycles 下 IndexOB 的输出名是 "Object Index"（不是 "IndexOB"）。
    """
    _log(f"[gpu_occlusion] 设置 Compositor: RLayers.Object Index -> Viewer")
    tree = scene.compositing_node_group
    if tree is None:
        tree = bpy.data.node_groups.new(
            name=f"CompositorNodeTree_{scene.name}",
            type="CompositorNodeTree",
        )
        scene.compositing_node_group = tree

    for n in list(tree.nodes):
        tree.nodes.remove(n)

    rl = tree.nodes.new(type="CompositorNodeRLayers")
    rl.location = (0, 0)

    viewer = tree.nodes.new(type="CompositorNodeViewer")
    viewer.location = (300, 0)
    viewer.name = "zlh_indexob_viewer"

    # Cycles 下的 IndexOB 输出名
    connected = False
    for out in rl.outputs:
        if "Object Index" in out.name or out.name == "Object Index":
            try:
                tree.links.new(out, viewer.inputs[0])
                connected = True
                _log(f"[gpu_occlusion]  {out.name} -> Viewer 连接成功")
                break
            except Exception as e:
                _log(f"[gpu_occlusion]  连接 {out.name} 失败: {e}")

    if not connected:
        _log(f"[gpu_occlusion]  警告: 未找到 Object Index 输出, "
             f"可用: {[o.name for o in rl.outputs]}")

    tree.update_tag()
    scene.render.use_compositing = True
    _log(f"[gpu_occlusion] Compositor 设置完成")


def _render_to_trigger_compositor(scene, filepath: str) -> bool:
    """渲染场景到文件，主要目的是触发 Compositor 更新 Viewer Node。"""
    _log(f"[gpu_occlusion] 渲染触发 Compositor: {filepath}")
    fp_orig = scene.render.filepath
    fmt_orig = scene.render.image_settings.file_format
    try:
        scene.render.filepath = filepath
        scene.render.image_settings.file_format = "PNG"
        bpy.ops.render.render(write_still=True)
        return True
    except Exception as e:
        _log(f"[gpu_occlusion]  渲染异常: {e}")
        _log(f"[gpu_occlusion]  traceback: {traceback.format_exc()}")
        return False
    finally:
        scene.render.filepath = fp_orig
        scene.render.image_settings.file_format = fmt_orig


def _read_indexob_from_viewer() -> Optional[np.ndarray]:
    """从 Compositor Viewer Node 读取 IndexOB 数据。

    Viewer Node 在渲染后会被 Compositor 更新，存储 Object Index pass 的原始值。
    """
    viewer_img = bpy.data.images.get("Viewer Node")
    if viewer_img is None:
        _log(f"[gpu_occlusion]  错误: 找不到 Viewer Node 图像")
        return None

    w, h = viewer_img.size
    if w == 0 or h == 0:
        _log(f"[gpu_occlusion]  Viewer Node 尺寸为 0")
        return None

    _log(f"[gpu_occlusion]  Viewer Node 尺寸: {w}x{h}")

    try:
        pix = np.array(viewer_img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    except Exception as e:
        _log(f"[gpu_occlusion]  numpy 转换异常: {e}")
        return None

    # R 通道 = Object Index
    indexob = pix[:, :, 0]
    _log(f"[gpu_occlusion]  R 通道: min={indexob.min():.6f}, max={indexob.max():.6f}, "
         f"非零={(indexob > 0.001).sum()} 像素")
    return indexob


# ════════════════════════════════════════════════════════════
# 检测逻辑
# ════════════════════════════════════════════════════════════

def _detect_visible_objects_from_indexob(
    indexob: np.ndarray,
    index_to_name: Dict[int, str],
    all_meshes: List[bpy.types.Object],
    min_pixel_percent: float = 0.01,
) -> dict:
    """从 IndexOB 数据中提取实际出现在渲染结果中的物体名。

    Viewer Node 输出的 R 通道直接存储 pass_index 的原始 float 值。
    解码: pass_index = round(val)。

    用 min_pixel_percent 阈值过滤 Cycles 噪声像素（默认 0.01%）。
    """
    _log(f"[gpu_occlusion] ===== 从 IndexOB 检测可见物体 =====")

    total_pixels = indexob.size
    _log(f"[gpu_occlusion] 总像素={total_pixels}")

    decoded = np.round(indexob).astype(np.int32)
    present_indices = set(decoded[decoded > 0])
    _log(f"[gpu_occlusion] 渲染结果中出现的 pass_index: {sorted(present_indices)}")

    visible_names: List[str] = []
    for pid in sorted(present_indices):
        pixel_count = int((decoded == pid).sum())
        pct = pixel_count / total_pixels * 100
        name = index_to_name.get(int(pid))
        if name is None:
            _log(f"[gpu_occlusion]  pass_index={pid}: 未分配 ({pixel_count}px, {pct:.4f}%)")
            continue
        if pct >= min_pixel_percent:
            visible_names.append(name)
            _log(f"[gpu_occlusion]  可见: {name} ({pixel_count}px, {pct:.2f}%)")
        else:
            _log(f"[gpu_occlusion]  排除(噪声): {name} ({pixel_count}px, {pct:.4f}%)")

    invisible_names = [
        o.name for o in all_meshes
        if o.name not in visible_names
    ]

    _log(f"[gpu_occlusion] 画面中实际可见 ({len(visible_names)} 个): {visible_names}")
    _log(f"[gpu_occlusion] 被遮挡/不可见 ({len(invisible_names)} 个): {invisible_names}")

    return {
        "all_objects": [o.name for o in all_meshes],
        "visible_objects": visible_names,
        "visible_count": len(visible_names),
        "invisible_objects": invisible_names,
    }


# ════════════════════════════════════════════════════════════
# Operator（已废弃，功能迁移至 sphere_camera / operators）
# ════════════════════════════════════════════════════════════

class ZLH_OT_GPUOcclusionAnalysis(bpy.types.Operator):
    """（已废弃）GPU 遮挡分析功能已迁移"""
    bl_idname = "zlh.gpu_occlusion_analysis"
    bl_label = "zlh: GPU 遮挡分析（已废弃）"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return False  # 禁用该操作符


# ════════════════════════════════════════════════════════════
# 工具函数（供其他模块调用）
# ════════════════════════════════════════════════════════════

def _run_indexob_detection(
    context,
    all_meshes: List[bpy.types.Object],
) -> dict:
    """直接运行 IndexOB 检测，返回可见物体列表。

    这是一个同步函数，供 _render_and_upload_with_indexob 等调用。

    返回: {
        "visible_objects": [name, ...],
        "visible_count": N,
        "invisible_objects": [name, ...],
    }
    """
    scene = context.scene

    # 1. 分配 pass_index + 清空其他
    index_to_name: Dict[int, str] = {}
    for idx, obj in enumerate(all_meshes):
        pid = idx + 1
        obj.pass_index = pid
        index_to_name[pid] = obj.name

    for obj in bpy.data.objects:
        if obj.type == "MESH" and obj not in all_meshes:
            obj.pass_index = 0

    # 2. 保存 & 设置 Cycles + Compositor
    orig_render = _save_render_state(scene)
    try:
        scene.render.engine = "CYCLES"
        scene.render.resolution_percentage = RENDER_RESOLUTION_PERCENT
        scene.cycles.samples = 1
        scene.view_layers[0].use_pass_object_index = True
        _setup_compositor_for_indexob(scene)

        # 3. 渲染触发 Compositor
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="zlh_idx_")
        try:
            filepath = os.path.join(tmp_dir, "_r.png")
            ok = _render_to_trigger_compositor(scene, filepath)
            if not ok:
                raise RuntimeError("渲染失败")

            indexob = _read_indexob_from_viewer()
            if indexob is None:
                raise RuntimeError("Viewer Node 未更新")

            result = _detect_visible_objects_from_indexob(
                indexob=indexob,
                index_to_name=index_to_name,
                all_meshes=all_meshes,
            )
            return result
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        _restore_render_state(scene, orig_render)
        # 清理 compositor
        tree = scene.compositing_node_group
        if tree:
            for n in list(tree.nodes):
                tree.nodes.remove(n)
