"""GPU 加速遮挡检测：基于深度图的组合推断。

核心思路：
  渲染 n+1 张深度图代替 2^n 次 IndexOB 渲染：
  - 1 张仅 non-removable 的基准深度图
  - n 张各 removable 单独深度图（仅该物体可见）
  然后通过增量画布列表算法，逐一将每个 removable 尝试叠加到已有画布，
  若深度图发生变化则产生新分支（该物体可见），否则不产生新分支。

用法：Blender 中按 Ctrl+Shift+Q 触发
"""

import os
import tempfile
import traceback
from typing import Dict, List, Optional, Set, Tuple

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

    # Near plane
    n_near, d_near = _plane(near_corners[0], near_corners[1], near_corners[2])
    if n_near.dot(near_center - cam_pos) < 0:
        n_near, d_near = -n_near, -d_near

    # Far plane
    n_far, d_far = _plane(far_corners[0], far_corners[2], far_corners[1])
    if n_far.dot(near_center - (near_center + cam_dir * (ce - cs))) < 0:
        n_far, d_far = -n_far, -d_far

    # Side planes
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


def _save_render_state(scene) -> dict:
    render = scene.render
    vl = scene.view_layers[0]
    return {
        "engine": render.engine,
        "resolution_percentage": render.resolution_percentage,
        "use_pass_z": vl.use_pass_z,
        "use_compositing": render.use_compositing,
    }


def _restore_render_state(scene, state: dict):
    render = scene.render
    vl = scene.view_layers[0]
    render.engine = state["engine"]
    render.resolution_percentage = state["resolution_percentage"]
    vl.use_pass_z = state["use_pass_z"]
    render.use_compositing = state["use_compositing"]


def _create_compositor_node_tree(scene):
    _log(f"[gpu_occlusion] 创建 Compositor 节点树 (scene={scene.name})")
    tree = bpy.data.node_groups.new(
        name=f"CompositorNodeTree_{scene.name}",
        type="CompositorNodeTree",
    )
    scene.compositing_node_group = tree
    tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    scene.render.use_compositing = True
    _log(f"[gpu_occlusion] Compositor 节点树创建完成")
    return tree


def _reset_compositor_for_depth(scene):
    _log(f"[gpu_occlusion] 设置 Compositor: RLayers.Z -> Viewer")
    tree = scene.compositing_node_group
    if tree is None:
        _log(f"[gpu_occlusion] compositing_node_group 为 None，需要创建")
        tree = _create_compositor_node_tree(scene)
    else:
        scene.render.use_compositing = True

    _log(f"[gpu_occlusion] 清除旧节点，当前 nodes 数量: {len(tree.nodes)}")
    for n in list(tree.nodes):
        tree.nodes.remove(n)

    rl = tree.nodes.new(type="CompositorNodeRLayers")
    rl.location = (0, 0)
    _log(f"[gpu_occlusion] 创建 RenderLayers 节点完成")

    viewer = tree.nodes.new(type="CompositorNodeViewer")
    viewer.location = (300, 0)
    viewer.name = "zlh_viewer"

    output = tree.nodes.new(type="NodeGroupOutput")
    output.location = (600, 0)

    try:
        tree.links.new(rl.outputs["Image"], output.inputs["Image"])
        _log(f"[gpu_occlusion] Image -> GroupOutput 连接成功")
    except Exception as e:
        _log(f"[gpu_occlusion] Image -> GroupOutput 连接失败: {e}")

    z_connected = False
    try:
        tree.links.new(rl.outputs["Z"], viewer.inputs[0])
        z_connected = True
        _log(f"[gpu_occlusion] Z -> Viewer 连接成功")
    except KeyError:
        _log(f"[gpu_occlusion] 未找到 Z 输出，尝试查找 Depth")
        for out in rl.outputs:
            if out.name == "Depth" or "depth" in out.name.lower():
                tree.links.new(out, viewer.inputs[0])
                z_connected = True
                _log(f"[gpu_occlusion] {out.name} -> Viewer 连接成功")
                break

    if not z_connected:
        _log(f"[gpu_occlusion] 警告: 未找到深度输出！可用 outputs: {[o.name for o in rl.outputs]}")

    tree.update_tag()
    _log(f"[gpu_occlusion] Compositor 设置完成")


def _render_depth_to_file(scene, filepath: str, label: str = "") -> bool:
    _log(f"[gpu_occlusion] 渲染深度图 {label}: {filepath}")
    fp_orig = scene.render.filepath
    fmt_orig = scene.render.image_settings.file_format
    scene.render.image_settings.file_format = "PNG"
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


def _read_depth_from_file(filepath: str, label: str = "") -> Optional[np.ndarray]:
    _log(f"[gpu_occlusion] 读取深度图 {label}: {filepath}")
    if not os.path.isfile(filepath):
        _log(f"[gpu_occlusion]  文件不存在: {filepath}")
        return None

    try:
        _log(f"[gpu_occlusion]  调用 bpy.data.images.load")
        img = bpy.data.images.load(filepath)
    except Exception as e:
        _log(f"[gpu_occlusion]  加载深度图异常 {label}: {e}")
        _log(f"[gpu_occlusion]  traceback: {traceback.format_exc()}")
        return None

    w, h = img.size
    _log(f"[gpu_occlusion]  图片尺寸: {w}x{h}")

    try:
        pix = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
        _log(f"[gpu_occlusion]  numpy 数组 shape={pix.shape}, dtype={pix.dtype}")
    except Exception as e:
        _log(f"[gpu_occlusion]  numpy 转换异常: {e}")
        img.user_clear()
        bpy.data.images.remove(img)
        return None

    img.user_clear()
    bpy.data.images.remove(img)
    _log(f"[gpu_occlusion]  图片资源已释放")

    depth = pix[:, :, 0]
    _log(f"[gpu_occlusion]  R 通道统计: min={depth.min():.6f}, max={depth.max():.6f}, mean={depth.mean():.6f}")
    _log(f"[gpu_occlusion]  非零像素数: {(depth > 0).sum()} / {depth.size}")
    return depth


def _infer_combinations_from_depth(
    depth_nonrem: np.ndarray,
    depth_rm: Dict[str, np.ndarray],
    removable_names: List[str],
    non_removable_names: List[str],
    index_to_name: Dict[int, str],
    all_meshes: List[bpy.types.Object],
) -> dict:
    _log(f"[gpu_occlusion] ===== 开始 CPU 深度推断有效组合 =====")
    _log(f"[gpu_occlusion] depth_nonrem shape={depth_nonrem.shape}, dtype={depth_nonrem.dtype}")

    base_visible = set(non_removable_names)
    n_rem = len(removable_names)

    # 检查 depth_rm 是否有缺失
    depth_rm_list: List[Optional[np.ndarray]] = []
    for name in removable_names:
        d = depth_rm.get(name)
        if d is None:
            _log(f"[gpu_occlusion]  警告: {name} depth_rm 为 None")
            depth_rm_list.append(None)
        else:
            _log(f"[gpu_occlusion]  {name} depth_rm shape={d.shape}, 非零={(d>0).sum()}, "
                 f"min={d.min():.6f}, max={d.max():.6f}")
            depth_rm_list.append(d)

    idx_to_rm_name = {i: name for i, name in enumerate(removable_names)}

    _log(f"[gpu_occlusion] 开始增量构建画布列表")
    # 画布列表中每个元素: (canvas, visible_set, mask)
    # canvas: 该组合对应的深度图（叠加后的结果）
    # visible: 该组合可见的物体名集合
    # mask: 位掩码表示哪些 removable 存在于该组合中
    canvases: List[Tuple[np.ndarray, Set[str], int]] = [
        (depth_nonrem.copy(), base_visible.copy(), 0),
    ]

    for i in range(n_rem):
        name = idx_to_rm_name[i]
        d = depth_rm_list[i]
        _log(f"[gpu_occlusion] 处理 removable[{i}]={name}: "
             f"当前画布数量={len(canvases)}")

        if d is None:
            _log(f"[gpu_occlusion]  跳过（无深度数据）")
            continue

        new_entries: List[Tuple[np.ndarray, Set[str], int]] = []

        for canvas, visible, mask in canvases:
            # 尝试叠加：合并 d 到 canvas，每个像素取最近值（最小值）
            merged = np.minimum(canvas, d)

            # 判断 merged 是否与 canvas 有显著差异
            # 背景像素 (d ≈ 1.0, canvas ≈ 1.0) 不做比较
            diff_mask = (d > 1e-6) & (np.abs(merged - canvas) > 1e-6)

            if diff_mask.any():
                # 不一致 → 产生新分支: Rᵢ 在某些像素上比当前画布更近
                new_visible = visible | {name}
                new_mask = mask | (1 << i)
                new_entries.append((merged, new_visible, new_mask))
                _log(f"[gpu_occlusion]  组合 mask={new_mask}: 叠加 {name} 产生新分支, "
                     f"visible={sorted(new_visible)}")
            else:
                _log(f"[gpu_occlusion]  组合 mask={mask}: 叠加 {name} 无变化，不分叉")

        # 新增的分支加入列表（原有分支保持不变）
        canvases.extend(new_entries)

    _log(f"[gpu_occlusion] 增量构建完成: {len(canvases)} 种有效组合")

    from collections import Counter
    freq: Counter[str] = Counter()
    for _c, vis, _m in canvases:
        for n in vis:
            freq[n] += 1

    all_visible_set: Set[str] = set(base_visible)
    for name in removable_names:
        d = depth_rm.get(name)
        if d is not None and (d > 0).any():
            all_visible_set.add(name)

    return {
        "all_objects": [o.name for o in all_meshes],
        "removable_names": removable_names,
        "non_removable_names": non_removable_names,
        "all_visible": sorted(all_visible_set),
        "effective_combinations": [
            {"mask": m, "visible": sorted(v)}
            for _c, v, m in canvases
        ],
        "count": len(canvases),
        "total_theoretical": 1 << n_rem,
        "frequency": dict(freq.most_common()),
    }


class ZLH_OT_GPUOcclusionAnalysis(bpy.types.Operator):
    """GPU 加速遮挡分析：基于深度图的组合推断"""
    bl_idname = "zlh.gpu_occlusion_analysis"
    bl_label = "zlh: GPU 遮挡分析"
    bl_options = {"REGISTER", "BLOCKING"}

    _state: dict = {}
    _timer = None
    _temp_dir: str = ""
    _phase: str = ""
    _render_idx: int = 0
    _removable_objs: list = []
    _non_removable_objs: list = []
    _all_meshes: list = []
    _depth_nonrem: Optional[np.ndarray] = None
    _depth_rm: Dict[str, Optional[np.ndarray]] = {}
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
        orig_workspace = state.get("orig_workspace")

        _log(f"[gpu_occlusion] 恢复 {len(orig_hide)} 个物体的 hide_render")
        try:
            for obj in scene.objects:
                if obj.name in orig_hide:
                    obj.hide_render = orig_hide[obj.name]
        except Exception as e:
            _log(f"[gpu_occlusion] 恢复 hide_render 异常: {e}")
            _log(f"[gpu_occlusion] traceback: {traceback.format_exc()}")

        if orig_render:
            try:
                _restore_render_state(scene, orig_render)
                _log(f"[gpu_occlusion] 渲染状态已恢复")
            except Exception as e:
                _log(f"[gpu_occlusion] 恢复渲染状态异常: {e}")

        if orig_workspace and context.window:
            try:
                for ws in bpy.data.workspaces:
                    if ws.name == orig_workspace:
                        context.window.workspace = ws
                        break
            except Exception as e:
                _log(f"[gpu_occlusion] 恢复 workspace 异常: {e}")

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
        self._phase = ""
        self._render_idx = 0
        self._removable_objs = []
        self._non_removable_objs = []
        self._all_meshes = []
        self._depth_nonrem = None
        self._depth_rm = {}

        scene = context.scene
        cam = scene.camera
        if cam is None:
            self.report({"ERROR"}, "场景中没有激活相机")
            return {"CANCELLED"}

        _log("[gpu_occlusion] ===== 开始基于深度图的遮挡分析 =====")

        # 1. 收集场景中所有可见 MESH，并做视锥体 AABB 粗筛
        _log("[gpu_occlusion] 构建视锥体...")
        frustum_planes = _build_frustum_planes(scene)
        _log(f"[gpu_occlusion] 视锥体 6 平面构建完成")

        all_meshes: List[bpy.types.Object] = []
        for obj in scene.objects:
            try:
                if obj.type != "MESH":
                    continue
                if obj.hide_get() or not obj.visible_get():
                    continue
                # 视锥体 AABB 粗筛
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

        # 分配 pass_index (供后续可能的 IndexOB 备用)
        index_to_name: Dict[int, str] = {}
        for idx, obj in enumerate(all_meshes):
            pid = idx + 1
            obj.pass_index = pid
            index_to_name[pid] = obj.name
        _log(f"[gpu_occlusion] pass_index 分配完成: {index_to_name}")

        # 筛选 removable
        removable_objs: List[bpy.types.Object] = []
        non_removable_objs: List[bpy.types.Object] = []
        for obj in all_meshes:
            is_rem = getattr(obj, "zlh_removable", False)
            _log(f"[gpu_occlusion]   {obj.name}: zlh_removable={is_rem}")
            if is_rem:
                removable_objs.append(obj)
            else:
                non_removable_objs.append(obj)

        _log(f"[gpu_occlusion] removable: {[o.name for o in removable_objs]}")
        _log(f"[gpu_occlusion] non-removable: {[o.name for o in non_removable_objs]}")

        if not removable_objs:
            self.report({"ERROR"}, "没有标记为 removable 的物体")
            return {"CANCELLED"}

        removable_names = [o.name for o in removable_objs]
        non_removable_names = [o.name for o in non_removable_objs]
        n_rem = len(removable_objs)

        # 保存原始状态
        orig_render = _save_render_state(scene)
        orig_hide: Dict[str, bool] = {}
        for obj in all_meshes:
            orig_hide[obj.name] = obj.hide_render
        orig_pass: Dict[str, int] = {}
        for obj in scene.objects:
            orig_pass[obj.name] = obj.pass_index
        orig_workspace = context.window.workspace.name if context.window else None
        _log(f"[gpu_occlusion] 原始状态已保存")

        # 设置 Eevee + 启用 Z pass
        # 注意：不修改相机 clip_start/clip_end，完全使用当前相机配置
        scene.render.engine = (
            "BLENDER_EEVEE_NEXT"
            if hasattr(bpy.types, "BLENDER_EEVEE_NEXT")
            else "BLENDER_EEVEE"
        )
        scene.render.resolution_percentage = RENDER_RESOLUTION_PERCENT
        _log(f"[gpu_occlusion] 渲染引擎: {scene.render.engine}")

        scene.view_layers[0].use_pass_z = True
        _log(f"[gpu_occlusion] use_pass_z 已启用, "
             f"相机 clip_start={cam.data.clip_start:.4f}, clip_end={cam.data.clip_end:.4f}")

        if context.window:
            for ws in bpy.data.workspaces:
                if ws.name == "Compositing":
                    context.window.workspace = ws
                    _log(f"[gpu_occlusion] 已切换到 Compositing workspace")
                    break

        _reset_compositor_for_depth(scene)

        self._state = {
            "all_meshes": all_meshes,
            "removable_objs": removable_objs,
            "non_removable_objs": non_removable_objs,
            "removable_names": removable_names,
            "non_removable_names": non_removable_names,
            "index_to_name": index_to_name,
            "n_rem": n_rem,
            "orig_render": orig_render,
            "orig_hide": orig_hide,
            "orig_pass": orig_pass,
            "orig_workspace": orig_workspace,
        }
        self._removable_objs = removable_objs
        self._non_removable_objs = non_removable_objs
        self._all_meshes = all_meshes

        # 创建临时目录，启动渲染
        self._temp_dir = tempfile.mkdtemp(prefix="zlh_gpu_occlusion_")
        _log(f"[gpu_occlusion] 临时目录: {self._temp_dir}")

        total_renders = 1 + n_rem
        wm = context.window_manager
        wm.progress_begin(0, total_renders)

        self._phase = "render_base"
        self._render_idx = 0
        self._timer = wm.event_timer_add(0.01, window=context.window)
        _log(f"[gpu_occlusion] 渲染阶段开始，共 {total_renders} 张")
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        wm = context.window_manager
        scene = context.scene

        try:
            if self._phase == "render_base":
                return self._modal_render_base(context, wm, scene)
            elif self._phase == "render_rm":
                return self._modal_render_rm(context, wm, scene)
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

        return {"PASS_THROUGH"}

    def _modal_render_base(self, context, wm, scene):
        _log(f"[gpu_occlusion] phase=render_base: 隐藏所有 removable")
        try:
            for obj in self._all_meshes:
                obj.hide_render = obj in self._removable_objs
            context.view_layer.update()
        except Exception as e:
            _log(f"[gpu_occlusion] 设置 hide_render 异常: {e}")

        filepath = os.path.join(self._temp_dir, "depth_base.png")
        ok = _render_depth_to_file(scene, filepath, "基准深度")
        if not ok:
            wm.progress_end()
            self._cleanup(context)
            self.report({"ERROR"}, "基准深度渲染失败")
            return {"CANCELLED"}

        self._depth_nonrem = _read_depth_from_file(filepath, "基准深度")
        if self._depth_nonrem is None:
            wm.progress_end()
            self._cleanup(context)
            self.report({"ERROR"}, "基准深度读取失败")
            return {"CANCELLED"}

        _log(f"[gpu_occlusion] 基准深度图: shape={self._depth_nonrem.shape}")
        wm.progress_update(0)

        self._phase = "render_rm"
        self._render_idx = 0
        return {"PASS_THROUGH"}

    def _modal_render_rm(self, context, wm, scene):
        if self._render_idx >= len(self._removable_objs):
            self._phase = "done_infer"
            self._do_infer_and_finish(context)
            return {"PASS_THROUGH"}

        rm_obj = self._removable_objs[self._render_idx]
        rm_name = rm_obj.name
        _log(f"[gpu_occlusion] phase=render_rm: 渲染 {rm_name} ({self._render_idx + 1}/{len(self._removable_objs)})")

        try:
            for obj in self._all_meshes:
                obj.hide_render = (obj != rm_obj)
            context.view_layer.update()
        except Exception as e:
            _log(f"[gpu_occlusion] 设置 hide_render 异常: {e}")

        filepath = os.path.join(self._temp_dir, f"depth_rm_{self._render_idx}.png")
        ok = _render_depth_to_file(scene, filepath, rm_name)
        if not ok:
            _log(f"[gpu_occlusion]  {rm_name} 渲染失败，填零替代")
            self._depth_rm[rm_name] = np.zeros_like(self._depth_nonrem)
        else:
            depth = _read_depth_from_file(filepath, rm_name)
            if depth is None:
                _log(f"[gpu_occlusion]  {rm_name} 读取失败，填零替代")
                depth = np.zeros_like(self._depth_nonrem)
            self._depth_rm[rm_name] = depth

        wm.progress_update(1 + self._render_idx + 1)
        self._render_idx += 1
        return {"PASS_THROUGH"}

    def _do_infer_and_finish(self, context):
        _log(f"[gpu_occlusion] ===== _do_infer_and_finish 开始 =====")
        wm = context.window_manager
        if self._timer:
            try:
                wm.event_timer_remove(self._timer)
                _log(f"[gpu_occlusion] timer 已移除")
            except Exception as e:
                _log(f"[gpu_occlusion] 移除 timer 异常: {e}")
            self._timer = None
        wm.progress_end()

        state = self._state
        _log(f"[gpu_occlusion] depth_rm 中物体: {list(self._depth_rm.keys())}")
        _log(f"[gpu_occlusion] removable_names: {state.get('removable_names')}")

        try:
            result = _infer_combinations_from_depth(
                depth_nonrem=self._depth_nonrem,
                depth_rm=self._depth_rm,
                removable_names=state["removable_names"],
                non_removable_names=state["non_removable_names"],
                index_to_name=state["index_to_name"],
                all_meshes=state["all_meshes"],
            )
            self._result = result
            _log(f"[gpu_occlusion] 推断成功，结果: count={result.get('count')}, "
                 f"combinations={len(result.get('effective_combinations', []))}")
        except Exception as e:
            _log(f"[gpu_occlusion] 推断异常: {e}")
            _log(f"[gpu_occlusion] traceback: {traceback.format_exc()}")
            self.report({"ERROR"}, f"深度推断失败: {e}")
            self._cleanup(context)
            return

        self._cleanup(context)

        _log(f"[gpu_occlusion] 分析完成: {result.get('count')} 种有效组合")
        _log(f"[gpu_occlusion] ===== 有效组合列表 =====")
        for i, combo in enumerate(result.get("effective_combinations", [])):
            vis = combo.get("visible", [])
            _log(f"  {i + 1}. mask={combo.get('mask', 0)}: {', '.join(vis) if vis else '（空）'}")

        self.report({"INFO"}, f"分析完成: {result.get('count')} 种有效组合（详见控制台）")
        _log(f"[gpu_occlusion] ===== _do_infer_and_finish 结束 =====")

    def draw(self, context):
        layout = self.layout
        r = self._result
        if not r:
            layout.label(text="无分析结果", icon="ERROR")
            return

        removable_names = r.get("removable_names", [])
        non_removable_names = r.get("non_removable_names", [])
        all_visible = r.get("all_visible", [])
        combos = r.get("effective_combinations", [])
        freq = r.get("frequency", {})

        n_rem = len(removable_names)
        theoretical = 1 << n_rem
        actual = r.get("count", 0)
        saved = theoretical - actual

        box = layout.box()
        box.label(text=f"removable: {n_rem} 个物体", icon="OBJECT_DATA")
        box.label(text=f"非 removable: {len(non_removable_names)} 个", icon="SCENE_DATA")
        box.label(text=f"理论组合: {theoretical} 种", icon="MODIFIER")
        box.label(text=f"实际有效: {actual} 种 (节省 {saved} 次渲染, "
                       f"{saved/theoretical*100:.1f}%)",
                  icon="SORT_ASC")

        box0 = layout.box()
        box0.label(text=f"当前视角下可见物体 ({len(all_visible)} 个):", icon="VIEWZOOM")
        box0.label(text=f"    {', '.join(all_visible[:30])}"
                        f"{'…' if len(all_visible) > 30 else ''}")

        if freq:
            box1 = layout.box()
            box1.label(text="各物体在有效组合中的出现频次:", icon="TEXTURE")
            for name, count in list(freq.items())[:25]:
                pct = count / actual * 100
                box1.label(text=f"    {name}: {count}/{actual} ({pct:.0f}%)")

        if combos:
            box2 = layout.box()
            n_show = min(20, len(combos))
            box2.label(text=f"有效组合（展示前 {n_show}/{len(combos)} 种）:",
                       icon="RENDER_STILL")
            for i, combo in enumerate(combos[:n_show]):
                label = f"    {i + 1}. "
                visible = combo.get("visible", [])
                label += ", ".join(visible) if visible else "（空）"
                box2.label(text=label)

    def execute(self, context):
        return {"FINISHED"}
