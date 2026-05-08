"""GPU 加速遮挡检测：枚举 removable 所有子集，渲染并记录实际可见物体。

核心问题：
  给定一组标了 removable 的物体，它们可以通过隐藏/显示产生 2^n 种组合。
  但实际上，由于物体之间的相互遮挡，很多组合会产生相同的「可见物体集合」。
  例如：物体 A 完全在物体 B 后面，无论 A 是否隐藏，可见物体集合都一样。
  这个工具就是枚举所有组合，记录每种组合下实际可见了哪些物体。

核心思路：
  1. 给所有可见 MESH 物体分配唯一的 pass_index
  2. 枚举 removable 物体的所有子集组合（2^n 种）
  3. 对每种组合：
     a. 设置该组合中隐藏的物体不可渲染（hide_render=True）
     b. 用 Eevee 快速渲染一张 IndexOB 图（写到临时文件，避免弹出渲染窗口）
     c. 从文件读取渲染结果，记录该组合下实际可见了哪些物体（pass_index → 有像素）
     d. 恢复所有物体的 hide_render
  4. 对所有组合的结果去重，得到「实际有效」的组合种类

用法：Blender 中按 Ctrl+Shift+Q 触发
"""

import os
import tempfile
import time as _time
from typing import Dict, List, Optional, Set, Tuple

import bpy
import numpy as np

from . import _log, VERSION_STR

# ── 渲染参数 ──────────────────────────────────────────────
RENDER_RESOLUTION_PERCENT = 100
MAX_PASS_INDEX = 32767


# ════════════════════════════════════════════════════════════
# 状态保存/恢复
# ════════════════════════════════════════════════════════════

def _save_render_state(scene) -> dict:
    render = scene.render
    vl = scene.view_layers[0]
    return {
        "engine": render.engine,
        "resolution_percentage": render.resolution_percentage,
        "use_pass_object_index": vl.use_pass_object_index,
        "use_compositing": render.use_compositing,
    }


def _restore_render_state(scene, state: dict):
    render = scene.render
    vl = scene.view_layers[0]
    render.engine = state["engine"]
    render.resolution_percentage = state["resolution_percentage"]
    vl.use_pass_object_index = state["use_pass_object_index"]
    render.use_compositing = state["use_compositing"]


def _get_scene_node_tree(scene):
    """安全获取 scene 的 Compositor 节点树。

    Blender 5.x API:
      - Scene.compositing_node_group (替代已废弃的 Scene.node_tree)
      - Scene.render.use_compositing (替代已废弃的 Scene.use_nodes)
    """
    render = scene.render
    render.use_compositing = True
    tree = scene.compositing_node_group
    if tree is not None:
        return tree
    render.use_compositing = False
    render.use_compositing = True
    tree = scene.compositing_node_group
    if tree is not None:
        return tree
    raise RuntimeError("无法创建场景的 Compositor 节点树")


def _reset_compositor_for_indexob(scene):
    """设置 Compositor 为 IndexOB → Viewer Node。"""
    tree = _get_scene_node_tree(scene)
    for n in list(tree.nodes):
        tree.nodes.remove(n)

    rl = tree.nodes.new(type="CompositorNodeRLayers")
    rl.location = (0, 0)
    viewer = tree.nodes.new(type="CompositorNodeViewer")
    viewer.location = (300, 0)
    viewer.name = "zlh_viewer"

    try:
        tree.links.new(rl.outputs["IndexOB"], viewer.inputs[0])
    except KeyError:
        for out in rl.outputs:
            if "Index" in out.name or "index" in out.name:
                tree.links.new(out, viewer.inputs[0])
                break
    tree.update_tag()


# ════════════════════════════════════════════════════════════
# 渲染并读取 IndexOB（写文件方式，避免弹出渲染窗口）
# ════════════════════════════════════════════════════════════

def _render_indexob_to_file(scene, filepath: str) -> bool:
    """渲染 IndexOB 到临时 PNG 文件，避免弹出渲染窗口阻塞 UI。"""
    fp_orig = scene.render.filepath
    fmt_orig = scene.render.image_settings.file_format
    scene.render.image_settings.file_format = "PNG"
    try:
        scene.render.filepath = filepath
        bpy.ops.render.render(write_still=True)
        return os.path.isfile(filepath)
    except Exception as e:
        _log(f"  渲染异常: {e}")
        return False
    finally:
        scene.render.filepath = fp_orig
        scene.render.image_settings.file_format = fmt_orig


def _read_indexob_from_file(filepath: str) -> Optional[np.ndarray]:
    """从渲染输出的 PNG 读取 IndexOB 数据，返回 (H, W) int32 数组。"""
    if not os.path.isfile(filepath):
        return None
    try:
        img = bpy.data.images.load(filepath)
    except Exception as e:
        _log(f"  加载图片异常: {e}")
        return None

    w, h = img.size
    pix = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    # 只留下第一个通道
    return np.round(pix[:, :, 0]).astype(np.int32)


# ════════════════════════════════════════════════════════════
# 核心枚举逻辑
# ════════════════════════════════════════════════════════════

def _prepare_enumeration(context) -> Optional[dict]:
    """枚举前的准备工作：收集物体、分配 pass_index、设置场景。

    返回包含所有上下文信息的 dict，如果准备失败返回 None（带 error 信息）。
    """
    scene = context.scene
    cam = scene.camera
    if cam is None:
        return {"error": "场景中没有激活相机"}

    _log("[gpu_occlusion] ===== 开始枚举各组合的实际可见物体 =====")

    # 1. 收集所有可见 MESH
    all_meshes: List[bpy.types.Object] = []
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        if obj.hide_get() or not obj.visible_get():
            continue
        all_meshes.append(obj)

    if not all_meshes:
        return {"error": "场景中无可见 MESH 物体"}

    # 2. 分配 pass_index
    index_to_name: Dict[int, str] = {}
    for idx, obj in enumerate(all_meshes):
        pid = idx + 1
        obj.pass_index = pid
        index_to_name[pid] = obj.name

    # 3. 筛选 removable
    removable: List[bpy.types.Object] = []
    non_removable: List[bpy.types.Object] = []
    for obj in all_meshes:
        if getattr(obj, "zlh_removable", False):
            removable.append(obj)
        else:
            non_removable.append(obj)

    n_rem = len(removable)
    total_combo = 1 << n_rem
    _log(f"[gpu_occlusion] 总 MESH = {len(all_meshes)}, "
         f"removable = {n_rem}, 理论组合 = {total_combo}")

    # 4. 保存原始状态
    orig_render = _save_render_state(scene)
    orig_hide: Dict[str, bool] = {}
    for obj in all_meshes:
        orig_hide[obj.name] = obj.hide_render
    orig_pass: Dict[str, int] = {}
    for obj in scene.objects:
        orig_pass[obj.name] = obj.pass_index
    orig_workspace = context.window.workspace.name if context.window else None

    # 5. 设置 Eevee + Compositor
    scene.render.engine = (
        "BLENDER_EEVEE_NEXT"
        if hasattr(bpy.types, "BLENDER_EEVEE_NEXT")
        else "BLENDER_EEVEE"
    )
    scene.render.resolution_percentage = RENDER_RESOLUTION_PERCENT
    scene.view_layers[0].use_pass_object_index = True

    if context.window:
        for ws in bpy.data.workspaces:
            if ws.name == "Compositing":
                context.window.workspace = ws
                break

    _reset_compositor_for_indexob(scene)

    return {
        "all_meshes": all_meshes,
        "removable": removable,
        "non_removable": non_removable,
        "index_to_name": index_to_name,
        "n_rem": n_rem,
        "total_combo": total_combo,
        "orig_render": orig_render,
        "orig_hide": orig_hide,
        "orig_pass": orig_pass,
        "orig_workspace": orig_workspace,
    }


class ZLH_OT_GPUOcclusionAnalysis(bpy.types.Operator):
    """GPU 加速遮挡分析：枚举 removable 各子集，记录实际可见物体"""
    bl_idname = "zlh.gpu_occlusion_analysis"
    bl_label = "zlh: GPU 遮挡分析"
    bl_options = {"REGISTER", "BLOCKING"}

    # ── 模态状态 ──
    _state: dict = {}
    _timer = None
    _temp_dir: str = ""

    # ── 分析结果 ──
    _result: dict = {}

    def _cleanup(self, context):
        """清理临时文件和场景状态。"""
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
        orig_pass = state.get("orig_pass", {})
        orig_render = state.get("orig_render")
        orig_workspace = state.get("orig_workspace")

        for obj in scene.objects:
            if obj.name in orig_hide:
                obj.hide_render = orig_hide[obj.name]
            if obj.name in orig_pass:
                obj.pass_index = orig_pass[obj.name]

        if orig_render:
            _restore_render_state(scene, orig_render)

        if orig_workspace and context.window:
            for ws in bpy.data.workspaces:
                if ws.name == orig_workspace:
                    context.window.workspace = ws
                    break

        context.view_layer.update()
        _log("[gpu_occlusion] 场景状态已恢复")

    def _render_one_combo(self, context) -> Optional[str]:
        """渲染当前组合的一张 IndexOB，返回唯一文件名（不含路径），失败返回 None。"""
        scene = context.scene
        fname = f"zlh_idx_{self._mask:04d}.png"
        filepath = os.path.join(self._temp_dir, fname)
        ok = _render_indexob_to_file(scene, filepath)
        if not ok:
            return None
        return fname

    def _analyze_frame(self, context) -> bool:
        """解析当前帧的渲染结果，更新有效组合列表。返回 True 表示成功。"""
        scene = context.scene
        state = self._state

        fname = self._render_one_combo(context)
        if fname is None:
            _log(f"  组合 mask={self._mask}: 渲染失败，跳过")
            self._mask += 1
            return True  # 跳过，继续下一步

        filepath = os.path.join(self._temp_dir, fname)
        idx_map = _read_indexob_from_file(filepath)
        if idx_map is None:
            _log(f"  组合 mask={self._mask}: 读取渲染结果失败，跳过")
            self._mask += 1
            return True

        index_to_name = state["index_to_name"]

        actually_visible: Set[str] = set()
        for pid, name in index_to_name.items():
            if np.any(idx_map == pid):
                actually_visible.add(name)

        sig = tuple(sorted(actually_visible))
        if sig not in self._seen_signatures:
            self._seen_signatures.add(sig)
            self._effective_list.append((self._mask, actually_visible))
            _log(f"  组合 mask={self._mask}: 新增有效组合 → {sorted(actually_visible)}")

        self._mask += 1
        return True

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.camera is not None

    def invoke(self, context, _event):
        self._state = {}
        self._result = {}
        self._temp_dir = ""
        self._timer = None

        # 准备工作
        state = _prepare_enumeration(context)
        if state is None or "error" in (state or {}):
            err = (state or {}).get("error", "未知错误")
            self.report({"ERROR"}, err)
            _log(f"[operator] 准备失败: {err}")
            return {"CANCELLED"}

        self._state = state

        total_combo = state["total_combo"]
        _log(f"[operator] 开始模态 GPU 遮挡分析，共 {total_combo} 种组合")

        # 先渲染全场景（全部可见），确定「所有可能出现的物体」
        scene = context.scene
        all_meshes = state["all_meshes"]
        index_to_name = state["index_to_name"]

        for obj in all_meshes:
            obj.hide_render = False
        context.view_layer.update()

        # 创建临时目录
        self._temp_dir = tempfile.mkdtemp(prefix="zlh_gpu_occlusion_")

        # 渲染全场景
        fname_full = "zlh_idx_full.png"
        full_path = os.path.join(self._temp_dir, fname_full)
        ok = _render_indexob_to_file(scene, full_path)
        if not ok:
            self._cleanup(context)
            self.report({"ERROR"}, "全场景渲染失败")
            return {"CANCELLED"}

        full_index = _read_indexob_from_file(full_path)
        if full_index is None:
            self._cleanup(context)
            self.report({"ERROR"}, "全场景渲染结果读取失败")
            return {"CANCELLED"}

        all_visible_set: Set[str] = set()
        for pid, name in index_to_name.items():
            if np.any(full_index == pid):
                all_visible_set.add(name)

        _log(f"[gpu_occlusion] 全场景可见物体 = {len(all_visible_set)} 个")
        self._all_visible_set = all_visible_set
        self._removable = state["removable"]
        self._n_rem = state["n_rem"]
        self._mask = 0
        self._seen_signatures: Set[Tuple[str, ...]] = set()
        self._effective_list: List[Tuple[int, Set[str]]] = []

        # 启动模态 timer
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "TIMER":
            wm = context.window_manager
            state = self._state
            scene = context.scene
            total_combo = state["total_combo"]
            all_meshes = state["all_meshes"]

            if self._mask >= total_combo:
                # 全部枚举完成
                wm.progress_end()
                if self._timer:
                    wm.event_timer_remove(self._timer)
                    self._timer = None

                _log(f"[gpu_occlusion] 枚举完成: {len(self._effective_list)} / {total_combo} 种有效组合")

                # 构建结果
                from collections import Counter
                removable = self._removable
                non_removable = state["non_removable"]
                freq: Counter[str] = Counter()
                for _mask, vis in self._effective_list:
                    for name in vis:
                        freq[name] += 1

                self._result = {
                    "all_objects": [o.name for o in all_meshes],
                    "removable_names": [o.name for o in removable],
                    "non_removable_names": [o.name for o in non_removable],
                    "all_visible": sorted(self._all_visible_set),
                    "effective_combinations": [
                        {"mask": m, "visible": sorted(v)}
                        for m, v in self._effective_list
                    ],
                    "count": len(self._effective_list),
                    "total_theoretical": total_combo,
                    "frequency": dict(freq.most_common()),
                }

                self._cleanup(context)

                # 显示结果对话框
                context.scene["zlh_gpu_occlusion_result"] = self._result
                return context.window_manager.invoke_props_dialog(self, width=700)

            # 设置当前组合的 hide_render
            removable = self._removable
            for i in range(self._n_rem):
                visible = (self._mask >> i) & 1
                removable[i].hide_render = not visible

            context.view_layer.update()

            # 更新进度
            wm.progress_update(self._mask)
            self.report({"INFO"}, f"GPU 遮挡分析: {self._mask}/{total_combo}")

            # 渲染并分析
            ok = self._analyze_frame(context)
            if not ok:
                # 严重错误，终止
                wm.progress_end()
                if self._timer:
                    wm.event_timer_remove(self._timer)
                    self._timer = None
                self._cleanup(context)
                self.report({"ERROR"}, f"组合 mask={self._mask}: 分析失败，终止")
                return {"CANCELLED"}

            return {"PASS_THROUGH"}

        return {"PASS_THROUGH"}

    def draw(self, context):
        layout = self.layout
        r = context.scene.get("zlh_gpu_occlusion_result", {})
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
        box.label(text=f"非 removable: {len(non_removable)} 个", icon="SCENE_DATA")
        box.label(text=f"理论组合: {theoretical} 种", icon="MODIFIER")
        box.label(
            text=f"实际有效: {actual} 种 (节省 {saved} 次渲染, "
                 f"{saved/theoretical*100:.1f}%)",
            icon="SORT_ASC",
        )

        box0 = layout.box()
        box0.label(text=f"当前视角下可见物体 ({len(all_visible)} 个):", icon="VIEWZOOM")
        box0.label(
            text=f"    {', '.join(all_visible[:30])}"
                 f"{'…' if len(all_visible) > 30 else ''}"
        )

        if freq:
            box1 = layout.box()
            box1.label(text="各物体在有效组合中的出现频次:", icon="TEXTURE")
            for name, count in list(freq.items())[:25]:
                pct = count / actual * 100
                box1.label(text=f"    {name}: {count}/{actual} ({pct:.0f}%)")

        if combos:
            box2 = layout.box()
            n_show = min(20, len(combos))
            box2.label(
                text=f"有效组合（展示前 {n_show}/{len(combos)} 种）:", icon="RENDER_STILL"
            )
            for i, combo in enumerate(combos[:n_show]):
                label = f"    {i + 1}. "
                visible = combo.get("visible", [])
                label += ", ".join(visible) if visible else "（空）"
                box2.label(text=label)

    def execute(self, context):
        """对话框确认后执行——什么都不做，结果已保存在 scene 中。"""
        return {"FINISHED"}
