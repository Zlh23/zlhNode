"""GPU 加速遮挡检测：基于深度图的组合推断。

核心思路：
  渲染 n+1 张深度图代替 2^n 次 IndexOB 渲染：
  - 1 张仅 non-removable 的基准深度图
  - n 张各 removable 单独深度图（仅该物体可见）
  然后通过 numpy 比较深度值推断各组合的实际可见物体：
  - 从基准深度开始，逐个叠加 removable
  - 如果该物体的深度图在某像素上比当前画布浅，则它可见

用法：Blender 中按 Ctrl+Shift+Q 触发
"""

import os
import tempfile
from typing import Dict, List, Optional, Set, Tuple

import bpy
import numpy as np

from . import _log

# ── 渲染参数 ──────────────────────────────────────────────
RENDER_RESOLUTION_PERCENT = 100


# ════════════════════════════════════════════════════════════
# 状态保存/恢复
# ════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════
# Blender 5.x Compositor 节点树管理
# ════════════════════════════════════════════════════════════

def _create_compositor_node_tree(scene) -> bpy.types.NodeTree:
    """为场景创建并分配一个 Compositor 节点树。"""
    _log(f"[gpu_occlusion] 创建 Compositor 节点树 (scene={scene.name})")
    tree = bpy.data.node_groups.new(
        name=f"CompositorNodeTree_{scene.name}",
        type="CompositorNodeTree",
    )
    scene.compositing_node_group = tree
    tree.interface.new_socket(
        name="Image", in_out="OUTPUT", socket_type="NodeSocketColor",
    )
    scene.render.use_compositing = True
    _log(f"[gpu_occlusion] Compositor 节点树创建完成")
    return tree


def _reset_compositor_for_depth(scene):
    """设置 Compositor：RLayers.Z → Viewer（用于读取深度图）。"""
    tree = scene.compositing_node_group
    if tree is None:
        tree = _create_compositor_node_tree(scene)
    else:
        scene.render.use_compositing = True

    for n in list(tree.nodes):
        tree.nodes.remove(n)

    rl = tree.nodes.new(type="CompositorNodeRLayers")
    rl.location = (0, 0)

    viewer = tree.nodes.new(type="CompositorNodeViewer")
    viewer.location = (300, 0)
    viewer.name = "zlh_viewer"

    output = tree.nodes.new(type="NodeGroupOutput")
    output.location = (600, 0)

    # 连 Image 到 GroupOutput（Compositor 必须有输出）
    try:
        tree.links.new(rl.outputs["Image"], output.inputs["Image"])
    except Exception:
        pass

    # 连 Z 到 Viewer（深度图数据通过 Viewer Node 读取）
    try:
        tree.links.new(rl.outputs["Z"], viewer.inputs[0])
    except KeyError:
        for out in rl.outputs:
            if out.name == "Depth" or "depth" in out.name.lower():
                tree.links.new(out, viewer.inputs[0])
                break

    tree.update_tag()


# ════════════════════════════════════════════════════════════
# 渲染并读取深度图
# ════════════════════════════════════════════════════════════

def _render_depth_to_file(scene, filepath: str) -> bool:
    """渲染深度图到文件。"""
    fp_orig = scene.render.filepath
    fmt_orig = scene.render.image_settings.file_format
    scene.render.image_settings.file_format = "PNG"
    try:
        scene.render.filepath = filepath
        bpy.ops.render.render(write_still=True)
        return os.path.isfile(filepath)
    except Exception as e:
        _log(f"  渲染深度图异常: {e}")
        return False
    finally:
        scene.render.filepath = fp_orig
        scene.render.image_settings.file_format = fmt_orig


def _read_depth_from_file(filepath: str) -> Optional[np.ndarray]:
    """从 PNG 文件读取 Z 通道数据，返回 (H, W) float32 数组。

    Eevee 渲染深度图时 Viewer Node 将 Z 值映射到 [0,1] 范围，
    读取后需要还原为实际深度（近裁面=0, 远裁面=1 mapped）。
    """
    if not os.path.isfile(filepath):
        return None
    try:
        img = bpy.data.images.load(filepath)
    except Exception as e:
        _log(f"  加载深度图异常: {e}")
        return None

    w, h = img.size
    pix = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)

    # 释放 Blender image datablock
    img.user_clear()
    bpy.data.images.remove(img)

    # Eevee 深度图在 Viewer Node 的 R 通道
    return pix[:, :, 0]


# ════════════════════════════════════════════════════════════
# 深度图推断有效组合
# ════════════════════════════════════════════════════════════

def _infer_combinations_from_depth(
    depth_nonrem: np.ndarray,
    depth_rm: Dict[str, np.ndarray],
    removable_names: List[str],
    non_removable_names: List[str],
    index_to_name: Dict[int, str],
    all_meshes: List[bpy.types.Object],
) -> dict:
    """从深度图推断有效组合。

    从基准深度开始，逐个叠加 removable 物体的深度图。
    如果某物体在某像素上比当前画布浅，则它在当前画布上是可见的。

    返回与之前相同格式的 result dict。
    """
    _log("[gpu_occlusion] ===== 开始 CPU 深度推断有效组合 =====")

    current_depth = depth_nonrem.copy()
    base_visible_names = set(non_removable_names)

    # 对 removable 按平均深度排序（近到远）
    rm_mean_depths: List[Tuple[str, float]] = []
    for name in removable_names:
        d = depth_rm.get(name)
        if d is None:
            continue
        mask = d > 0
        if mask.any():
            rm_mean_depths.append((name, float(d[mask].mean())))
        else:
            rm_mean_depths.append((name, float("inf")))
    rm_mean_depths.sort(key=lambda x: x[1])

    # 收集全场景可见物体
    depth_full = depth_nonrem.copy()
    for name, d in depth_rm.items():
        if d is None:
            continue
        depth_full = np.minimum(depth_full, d)
    all_visible_names: Set[str] = set()
    for pid, name in index_to_name.items():
        if name in non_removable_names:
            all_visible_names.add(name)
        elif name in removable_names:
            dd = np.minimum(depth_nonrem, depth_rm.get(name, depth_nonrem))
            if (dd < current_depth).any() or (depth_rm.get(name, np.zeros_like(current_depth)) > 0).any():
                all_visible_names.add(name)

    # 逐层叠加
    seen_signatures: Set[Tuple[str, ...]] = set()
    effective_list: List[Tuple[int, Set[str]]] = []

    # 初始：仅 non-removable
    sig0 = tuple(sorted(base_visible_names))
    seen_signatures.add(sig0)
    effective_list.append((0, base_visible_names.copy()))
    _log(f"[gpu_occlusion] 基准组合: visible={sorted(base_visible_names)}")

    # 对每个 priority mask（1 << i），对应 removable_names[i] 是否可见
    n_rem = len(removable_names)
    # 建立 name -> index 映射
    name_to_idx = {name: i for i, name in enumerate(removable_names)}

    # 逐个叠加 removable
    for name, _mean_d in rm_mean_depths:
        d_rm = depth_rm.get(name)
        if d_rm is None:
            continue

        # 检查：该物体是否有像素比当前画布浅
        deeper_mask = d_rm < current_depth
        if deeper_mask.any():
            # 这个物体可见，更新画布
            current_depth = np.minimum(current_depth, d_rm)
            # 构建当前可见集：base + 所有比当前画布浅的 removable
            current_visible = base_visible_names.copy()
            for other_name, other_d in depth_rm.items():
                if other_d is None:
                    continue
                if (other_d < current_depth).any():
                    current_visible.add(other_name)
                else:
                    # 检查该物体自身是否有任何非零深度像素（即它本体出现了）
                    if (other_d > 0).any():
                        current_visible.add(other_name)
            sig = tuple(sorted(current_visible))
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                # mask：每个 removable 的位置
                mask = 0
                for i, rm_name in enumerate(removable_names):
                    if rm_name in current_visible:
                        mask |= (1 << i)
                effective_list.append((mask, current_visible))
                _log(f"[gpu_occlusion] 组合 mask={mask}: 新增有效组合 → {sorted(current_visible)}")

    _log(f"[gpu_occlusion] 深度推断完成: {len(effective_list)} 种有效组合")

    # 统计频次
    from collections import Counter
    freq: Counter[str] = Counter()
    for _mask, vis in effective_list:
        for name in vis:
            freq[name] += 1

    return {
        "all_objects": [o.name for o in all_meshes],
        "removable_names": removable_names,
        "non_removable_names": non_removable_names,
        "all_visible": sorted(all_visible_names),
        "effective_combinations": [
            {"mask": m, "visible": sorted(v)}
            for m, v in effective_list
        ],
        "count": len(effective_list),
        "total_theoretical": 1 << n_rem,
        "frequency": dict(freq.most_common()),
    }


# ════════════════════════════════════════════════════════════
# Operator
# ════════════════════════════════════════════════════════════

class ZLH_OT_GPUOcclusionAnalysis(bpy.types.Operator):
    """GPU 加速遮挡分析：基于深度图的组合推断"""
    bl_idname = "zlh.gpu_occlusion_analysis"
    bl_label = "zlh: GPU 遮挡分析"
    bl_options = {"REGISTER", "BLOCKING"}

    # ── 模态状态 ──
    _state: dict = {}
    _timer = None
    _temp_dir: str = ""
    _phase: str = ""            # "render_base", "render_rm", "infer", "done"
    _render_idx: int = 0
    _removable_objs: list = []
    _non_removable_objs: list = []
    _all_meshes: list = []
    _depth_nonrem: Optional[np.ndarray] = None
    _depth_rm: Dict[str, Optional[np.ndarray]] = {}

    # ── 分析结果 ──
    _result: dict = {}

    def _cleanup(self, context):
        if self._temp_dir and os.path.isdir(self._temp_dir):
            import shutil
            try:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            except Exception:
                pass
            self._temp_dir = ""

        state = self._state
        if not state:
            return

        scene = context.scene
        orig_hide = state.get("orig_hide", {})
        orig_render = state.get("orig_render")
        orig_workspace = state.get("orig_workspace")

        for obj in scene.objects:
            if obj.name in orig_hide:
                obj.hide_render = orig_hide[obj.name]

        if orig_render:
            _restore_render_state(scene, orig_render)

        if orig_workspace and context.window:
            for ws in bpy.data.workspaces:
                if ws.name == orig_workspace:
                    context.window.workspace = ws
                    break

        context.view_layer.update()
        _log("[gpu_occlusion] 场景状态已恢复")

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.camera is not None

    def invoke(self, context, _event):
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

        # ── 1. 收集所有可见 MESH 物体并分配 pass_index（用于识别） ──
        all_meshes: List[bpy.types.Object] = []
        for obj in scene.objects:
            if obj.type != "MESH":
                continue
            if obj.hide_get() or not obj.visible_get():
                continue
            all_meshes.append(obj)

        if not all_meshes:
            self.report({"ERROR"}, "场景中无可见 MESH 物体")
            return {"CANCELLED"}

        # 分配 pass_index
        index_to_name: Dict[int, str] = {}
        for idx, obj in enumerate(all_meshes):
            pid = idx + 1
            obj.pass_index = pid
            index_to_name[pid] = obj.name

        # 筛选 removable / non-removable
        removable_objs: List[bpy.types.Object] = []
        non_removable_objs: List[bpy.types.Object] = []
        for obj in all_meshes:
            if getattr(obj, "zlh_removable", False):
                removable_objs.append(obj)
            else:
                non_removable_objs.append(obj)

        if not removable_objs:
            self.report({"ERROR"}, "没有标记为 removable 的物体，无需分析")
            return {"CANCELLED"}

        removable_names = [o.name for o in removable_objs]
        non_removable_names = [o.name for o in non_removable_objs]
        n_rem = len(removable_objs)
        _log(f"[gpu_occlusion] 总 MESH = {len(all_meshes)}, "
             f"removable = {n_rem}, non-removable = {len(non_removable_objs)}")

        # ── 2. 保存原始状态 ──
        orig_render = _save_render_state(scene)
        orig_hide: Dict[str, bool] = {}
        for obj in all_meshes:
            orig_hide[obj.name] = obj.hide_render
        orig_pass: Dict[str, int] = {}
        for obj in scene.objects:
            orig_pass[obj.name] = obj.pass_index
        orig_workspace = context.window.workspace.name if context.window else None

        # ── 3. 设置 Eevee + Compositor ──
        scene.render.engine = (
            "BLENDER_EEVEE_NEXT"
            if hasattr(bpy.types, "BLENDER_EEVEE_NEXT")
            else "BLENDER_EEVEE"
        )
        scene.render.resolution_percentage = RENDER_RESOLUTION_PERCENT

        # 启用 Z pass
        scene.view_layers[0].use_pass_z = True

        # 切换到 Compositing workspace
        if context.window:
            for ws in bpy.data.workspaces:
                if ws.name == "Compositing":
                    context.window.workspace = ws
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

        # ── 4. 创建临时目录, 启动渲染流程 ──
        self._temp_dir = tempfile.mkdtemp(prefix="zlh_gpu_occlusion_")

        total_renders = 1 + n_rem  # base + 每个 removable
        wm = context.window_manager
        wm.progress_begin(0, total_renders)

        # 先渲染基准深度（仅 non-removable）
        self._phase = "render_base"
        self._render_idx = 0  # 渲染步骤索引：0 = base, 1..n = rm_i
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        wm = context.window_manager
        scene = context.scene

        if self._phase == "render_base":
            # 隐藏所有 removable，只保留 non-removable
            for obj in self._all_meshes:
                obj.hide_render = obj in self._removable_objs
            context.view_layer.update()

            filepath = os.path.join(self._temp_dir, "depth_base.png")
            ok = _render_depth_to_file(scene, filepath)
            if not ok:
                wm.progress_end()
                self._cleanup(context)
                self.report({"ERROR"}, "基准深度渲染失败")
                return {"CANCELLED"}

            self._depth_nonrem = _read_depth_from_file(filepath)
            if self._depth_nonrem is None:
                wm.progress_end()
                self._cleanup(context)
                self.report({"ERROR"}, "基准深度读取失败")
                return {"CANCELLED"}

            _log(f"[gpu_occlusion] 基准深度渲染完成 {self._depth_nonrem.shape}")
            wm.progress_update(0)

            # 切换到渲染每个 removable
            self._phase = "render_rm"
            self._render_idx = 0
            return {"PASS_THROUGH"}

        elif self._phase == "render_rm":
            if self._render_idx >= len(self._removable_objs):
                # 所有深度图渲染完毕，进入推断阶段
                self._phase = "infer"
                wm.progress_update(1 + len(self._removable_objs))
                self.report({"INFO"}, "深度图渲染完成，开始推断有效组合…")
                return {"PASS_THROUGH"}

            rm_obj = self._removable_objs[self._render_idx]
            rm_name = rm_obj.name

            # 只显示当前 removable 物体
            for obj in self._all_meshes:
                obj.hide_render = (obj != rm_obj)
            context.view_layer.update()

            filepath = os.path.join(self._temp_dir, f"depth_rm_{self._render_idx}.png")
            ok = _render_depth_to_file(scene, filepath)
            if not ok:
                _log(f"[gpu_occlusion]  {rm_name} 深度渲染失败，跳过")
                self._depth_rm[rm_name] = np.zeros_like(self._depth_nonrem)
            else:
                depth = _read_depth_from_file(filepath)
                if depth is None:
                    depth = np.zeros_like(self._depth_nonrem)
                self._depth_rm[rm_name] = depth
                _log(f"[gpu_occlusion]  {rm_name} 深度图渲染完成 ({self._render_idx + 1}/{len(self._removable_objs)})")

            wm.progress_update(1 + self._render_idx + 1)
            self._render_idx += 1
            return {"PASS_THROUGH"}

        elif self._phase == "infer":
            # 关闭 timer
            if self._timer:
                wm.event_timer_remove(self._timer)
                self._timer = None
            wm.progress_end()

            state = self._state
            result = _infer_combinations_from_depth(
                depth_nonrem=self._depth_nonrem,
                depth_rm=self._depth_rm,
                removable_names=state["removable_names"],
                non_removable_names=state["non_removable_names"],
                index_to_name=state["index_to_name"],
                all_meshes=state["all_meshes"],
            )
            self._result = result

            self._cleanup(context)

            self._phase = "done"
            _log(f"[gpu_occlusion] 分析完成: {len(result.get('effective_combinations', []))} 种有效组合")
            return context.window_manager.invoke_props_dialog(self, width=700)

        elif self._phase == "done":
            return {"PASS_THROUGH"}

        return {"PASS_THROUGH"}

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
