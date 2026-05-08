"""GPU 加速遮挡检测：基于 IndexOB 的可见物体检测。

核心思路：
  渲染一张图，通过 Object Index pass 精确知道哪些物体实际出现在渲染结果中。
  - 使用 EXR 格式保存（避免 PNG 8-bit 精度丢失）
  - 直接从 Render Result 读取 IndexOB 通道

用法：Blender 中按 Ctrl+Shift+Q 触发
"""

import os
import tempfile
import traceback
from typing import Dict, List, Optional, Set

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
# 渲染状态管理
# ════════════════════════════════════════════════════════════

def _save_render_state(scene) -> dict:
    render = scene.render
    vl = scene.view_layers[0]
    return {
        "engine": render.engine,
        "resolution_percentage": render.resolution_percentage,
        "use_pass_object_index": vl.use_pass_object_index,
        "file_format": render.image_settings.file_format,
        "color_depth": render.image_settings.color_depth,
        "use_antialiasing": render.use_antialiasing,
    }


def _restore_render_state(scene, state: dict):
    render = scene.render
    vl = scene.view_layers[0]
    render.engine = state["engine"]
    render.resolution_percentage = state["resolution_percentage"]
    vl.use_pass_object_index = state["use_pass_object_index"]
    render.image_settings.file_format = state["file_format"]
    render.image_settings.color_depth = state["color_depth"]
    render.use_antialiasing = state["use_antialiasing"]


def _render_to_exr(scene, filepath: str, label: str = "") -> bool:
    """渲染场景到 EXR 文件（支持 32-bit 浮点，保留所有通道）。"""
    _log(f"[gpu_occlusion] 渲染 EXR {label}: {filepath}")
    fp_orig = scene.render.filepath
    fmt_orig = scene.render.image_settings.file_format
    depth_orig = scene.render.image_settings.color_depth
    aa_orig = scene.render.use_antialiasing

    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.image_settings.color_depth = "32"
    scene.render.use_antialiasing = False
    try:
        scene.render.filepath = filepath
        _log(f"[gpu_occlusion]  调用 bpy.ops.render.render(write_still=True)")
        bpy.ops.render.render(write_still=True)
        result = os.path.isfile(filepath)
        _log(f"[gpu_occlusion]  渲染完成，文件存在={result}")
        return result
    except Exception as e:
        _log(f"[gpu_occlusion]  渲染异常 {label}: {e}")
        _log(f"[gpu_occlusion]  traceback: {traceback.format_exc()}")
        return False
    finally:
        scene.render.filepath = fp_orig
        scene.render.image_settings.file_format = fmt_orig
        scene.render.image_settings.color_depth = depth_orig
        scene.render.use_antialiasing = aa_orig


def _read_indexob_from_exr(filepath: str, label: str = "") -> Optional[np.ndarray]:
    """从 EXR 文件读取 IndexOB 数据。

    EXR 格式的 pass_index 以 float32 直接存储，每个像素的值 = 物体 pass_index（float）。
    背景像素 = 0。
    """
    _log(f"[gpu_occlusion] 读取 EXR IndexOB {label}: {filepath}")
    if not os.path.isfile(filepath):
        _log(f"[gpu_occlusion]  文件不存在: {filepath}")
        return None

    try:
        img = bpy.data.images.load(filepath)
    except Exception as e:
        _log(f"[gpu_occlusion]  加载 EXR 异常 {label}: {e}")
        return None

    w, h = img.size
    _log(f"[gpu_occlusion]  图片尺寸: {w}x{h}")

    try:
        pix = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    except Exception as e:
        _log(f"[gpu_occlusion]  numpy 转换异常: {e}")
        img.user_clear()
        bpy.data.images.remove(img)
        return None

    img.user_clear()
    bpy.data.images.remove(img)
    _log(f"[gpu_occlusion]  图片资源已释放")

    # EXR IndexOB 在 R 通道，值直接是 pass_index 的 float 值
    indexob = pix[:, :, 0]
    _log(f"[gpu_occlusion]  IndexOB R 通道: min={indexob.min():.6f}, max={indexob.max():.6f}, "
         f"非零={(indexob > 0.5).sum()} 像素")
    return indexob


def _detect_visible_objects_from_indexob(
    indexob: np.ndarray,
    index_to_name: Dict[int, str],
    all_meshes: List[bpy.types.Object],
) -> dict:
    """从 IndexOB 数据中提取实际出现在渲染结果中的物体名。

    EXR 中 pass_index 是 float32，四舍五入到最近的整数。
    背景像素 = 0，物体像素 > 0.5。
    """
    _log(f"[gpu_occlusion] ===== 从 IndexOB 检测可见物体 =====")

    # 过滤出物体像素（pass_index > 0.5），四舍五入取整
    values = indexob[indexob > 0.5]
    if len(values) == 0:
        _log(f"[gpu_occlusion] 画面中没有物体像素（全部背景）")
        return {
            "all_objects": [o.name for o in all_meshes],
            "visible_objects": [],
            "visible_count": 0,
            "invisible_objects": [o.name for o in all_meshes],
        }

    present_indices = set(np.round(values).astype(np.int32))
    _log(f"[gpu_occlusion] 渲染结果中出现的 pass_index: {sorted(present_indices)}")

    visible_names: List[str] = []
    for pid in sorted(present_indices):
        name = index_to_name.get(int(pid))
        if name:
            visible_names.append(name)
        else:
            _log(f"[gpu_occlusion]  pass_index={pid} 无法映射到物体")

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
# Operator
# ════════════════════════════════════════════════════════════

class ZLH_OT_GPUOcclusionAnalysis(bpy.types.Operator):
    """GPU 遮挡分析：通过 IndexOB 检测当前画面中实际渲染的物体"""
    bl_idname = "zlh.gpu_occlusion_analysis"
    bl_label = "zlh: GPU 遮挡分析"
    bl_options = {"REGISTER", "BLOCKING"}

    _state: dict = {}
    _timer = None
    _temp_dir: str = ""
    _all_meshes: list = []
    _result: dict = {}

    def _cleanup(self, context):
        _log(f"[gpu_occlusion] _cleanup 开始")
        if self._temp_dir and os.path.isdir(self._temp_dir):
            import shutil
            try:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
                _log(f"[gpu_occlusion] 临时目录已删除: {self._temp_dir}")
            except Exception as e:
                _log(f"[gpu_occlusion] 删除临时目录失败: {e}")
            self._temp_dir = ""

        state = self._state
        if not state:
            _log(f"[gpu_occlusion] _cleanup: state 为空，跳过")
            return

        scene = context.scene
        if scene is None:
            _log(f"[gpu_occlusion] _cleanup: scene 为 None")
            return

        orig_hide = state.get("orig_hide", {})
        orig_render = state.get("orig_render")
        orig_pass = state.get("orig_pass", {})

        _log(f"[gpu_occlusion] 恢复 {len(orig_hide)} 个物体的 hide_render")
        try:
            for obj in scene.objects:
                if obj.name in orig_hide:
                    obj.hide_render = orig_hide[obj.name]
        except Exception as e:
            _log(f"[gpu_occlusion] 恢复 hide_render 异常: {e}")

        _log(f"[gpu_occlusion] 恢复 pass_index")
        try:
            for obj in scene.objects:
                if obj.name in orig_pass:
                    obj.pass_index = orig_pass[obj.name]
        except Exception as e:
            _log(f"[gpu_occlusion] 恢复 pass_index 异常: {e}")

        if orig_render:
            try:
                _restore_render_state(scene, orig_render)
                _log(f"[gpu_occlusion] 渲染状态已恢复")
            except Exception as e:
                _log(f"[gpu_occlusion] 恢复渲染状态异常: {e}")

        try:
            context.view_layer.update()
        except Exception as e:
            _log(f"[gpu_occlusion] view_layer.update 异常: {e}")

        _log("[gpu_occlusion] 场景状态已恢复")

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.camera is not None

    def invoke(self, context, _event):
        _log(f"[gpu_occlusion] ===== invoke 开始 =====")
        self._state = {}
        self._result = {}
        self._temp_dir = ""
        self._timer = None
        self._all_meshes = []

        scene = context.scene
        cam = scene.camera
        if cam is None:
            self.report({"ERROR"}, "场景中没有激活相机")
            return {"CANCELLED"}

        _log("[gpu_occlusion] ===== 开始 IndexOB 可见物体检测 =====")

        # 1. 收集场景中所有可见 MESH，做视锥体粗筛
        _log("[gpu_occlusion] 构建视锥体...")
        frustum_planes = _build_frustum_planes(scene)

        all_meshes: List[bpy.types.Object] = []
        for obj in scene.objects:
            try:
                if obj.type != "MESH":
                    continue
                if obj.hide_get() or not obj.visible_get():
                    continue
                bbox = obj.bound_box
                if not bbox:
                    continue
                world_mat = obj.matrix_world
                corners_world = [world_mat @ Vector(p) for p in bbox]
                min_c = Vector((
                    min(p.x for p in corners_world),
                    min(p.y for p in corners_world),
                    min(p.z for p in corners_world),
                ))
                max_c = Vector((
                    max(p.x for p in corners_world),
                    max(p.y for p in corners_world),
                    max(p.z for p in corners_world),
                ))
                if _aabb_in_frustum(frustum_planes, min_c, max_c):
                    all_meshes.append(obj)
                else:
                    _log(f"[gpu_occlusion]   跳过 {obj.name}（不在视锥体内）")
            except Exception as e:
                _log(f"[gpu_occlusion] 收集物体异常 ({obj.name}): {e}")

        _log(f"[gpu_occlusion] 视锥体内 MESH 数量: {len(all_meshes)}")
        if not all_meshes:
            self.report({"ERROR"}, "场景中无可见 MESH 物体")
            return {"CANCELLED"}

        # 2. 分配 pass_index（从 1 开始，因为 0 = 背景）
        index_to_name: Dict[int, str] = {}
        for idx, obj in enumerate(all_meshes):
            pid = idx + 1
            obj.pass_index = pid
            index_to_name[pid] = obj.name
        _log(f"[gpu_occlusion] pass_index 分配完成")

        # 3. 保存原始状态
        orig_render = _save_render_state(scene)
        orig_hide: Dict[str, bool] = {}
        for obj in all_meshes:
            orig_hide[obj.name] = obj.hide_render
        orig_pass: Dict[str, int] = {}
        for obj in scene.objects:
            orig_pass[obj.name] = obj.pass_index

        self._state = {
            "all_meshes": all_meshes,
            "index_to_name": index_to_name,
            "orig_render": orig_render,
            "orig_hide": orig_hide,
            "orig_pass": orig_pass,
        }
        self._all_meshes = all_meshes

        # 4. 设置 Eevee + 启用 IndexOB pass
        #    使用 EXR 32-bit 保存（保留精确的 pass_index 浮点值）
        scene.render.engine = (
            "BLENDER_EEVEE_NEXT"
            if hasattr(bpy.types, "BLENDER_EEVEE_NEXT")
            else "BLENDER_EEVEE"
        )
        scene.render.resolution_percentage = RENDER_RESOLUTION_PERCENT
        scene.render.use_antialiasing = False

        scene.view_layers[0].use_pass_object_index = True
        _log(f"[gpu_occlusion] 渲染引擎: {scene.render.engine}, "
             f"use_pass_object_index=True")

        # 不需要 Compositor，EXR 直接保存所有渲染通道
        # 禁用 Compositor 避免干扰
        scene.render.use_compositing = False

        # 5. 创建临时目录
        self._temp_dir = tempfile.mkdtemp(prefix="zlh_indexob_")
        _log(f"[gpu_occlusion] 临时目录: {self._temp_dir}")

        wm = context.window_manager
        wm.progress_begin(0, 1)

        self._timer = wm.event_timer_add(0.01, window=context.window)
        _log(f"[gpu_occlusion] 开始渲染 IndexOB EXR")
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        wm = context.window_manager
        scene = context.scene

        try:
            filepath = os.path.join(self._temp_dir, "indexob.exr")
            ok = _render_to_exr(scene, filepath, "IndexOB")
            if not ok:
                wm.progress_end()
                self._cleanup(context)
                self.report({"ERROR"}, "IndexOB 渲染失败")
                return {"CANCELLED"}

            indexob = _read_indexob_from_exr(filepath, "IndexOB")
            if indexob is None:
                wm.progress_end()
                self._cleanup(context)
                self.report({"ERROR"}, "IndexOB 读取失败")
                return {"CANCELLED"}

            state = self._state
            result = _detect_visible_objects_from_indexob(
                indexob=indexob,
                index_to_name=state["index_to_name"],
                all_meshes=state["all_meshes"],
            )
            self._result = result
            _log(f"[gpu_occlusion] 检测成功: {result['visible_count']} 个物体可见")

            self.report({"INFO"},
                        f"可见物体: {result['visible_count']}/{len(state['all_meshes'])} 个（详见控制台）")

            wm.progress_end()
            self._cleanup(context)
            return {"FINISHED"}

        except Exception as e:
            _log(f"[gpu_occlusion] modal 异常: {e}")
            _log(f"[gpu_occlusion] traceback: {traceback.format_exc()}")
            try:
                wm.progress_end()
                self._cleanup(context)
            except Exception:
                pass
            self.report({"ERROR"}, f"分析异常: {e}")
            return {"CANCELLED"}

    def draw(self, context):
        layout = self.layout
        r = self._result
        if not r:
            layout.label(text="无分析结果", icon="ERROR")
            return

        visible = r.get("visible_objects", [])
        invisible = r.get("invisible_objects", [])
        total = len(r.get("all_objects", []))

        box = layout.box()
        box.label(text=f"场景 MESH 总数: {total}", icon="OBJECT_DATA")
        box.label(text=f"画面中实际可见: {len(visible)} 个", icon="HIDE_OFF")
        box.label(text=f"被遮挡/不可见: {len(invisible)} 个", icon="HIDE_ON")

        if visible:
            box0 = layout.box()
            box0.label(text="可见物体:", icon="VISIBLE_IPO_ON")
            for i, name in enumerate(visible):
                box0.label(text=f"    {i + 1}. {name}")

        if invisible:
            box1 = layout.box()
            box1.label(text="被遮挡/不可见物体:", icon="GHOST_ENABLED")
            for i, name in enumerate(invisible):
                box1.label(text=f"    {i + 1}. {name}")

    def execute(self, context):
        return {"FINISHED"}
