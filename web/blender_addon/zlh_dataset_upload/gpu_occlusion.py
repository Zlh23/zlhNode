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
     b. 用 Eevee 快速渲染一张 IndexOB 图
     c. 记录该组合下实际可见了哪些物体（pass_index → 有像素）
     d. 恢复所有物体的 hide_render
  4. 对所有组合的结果去重，得到「实际有效」的组合种类

用法：Blender 中按 Ctrl+Shift+Q 触发
"""

import time as _time
from typing import Dict, List, Optional, Set, Tuple

import bpy
import numpy as np

from . import _log

# ── 渲染参数 ──────────────────────────────────────────────
RENDER_RESOLUTION_PERCENT = 100  # 渲染分辨率百分比
MAX_PASS_INDEX = 32767           # Blender pass_index 上限


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
        "use_nodes": scene.use_nodes,
    }


def _restore_render_state(scene, state: dict):
    render = scene.render
    vl = scene.view_layers[0]
    render.engine = state["engine"]
    render.resolution_percentage = state["resolution_percentage"]
    vl.use_pass_object_index = state["use_pass_object_index"]

    if not state["use_nodes"]:
        if scene.node_tree:
            scene.node_tree.nodes.clear()
        scene.use_nodes = False
    else:
        scene.use_nodes = True


def _get_scene_node_tree(scene):
    """安全获取 scene 的 Compositor 节点树。"""
    scene.use_nodes = True
    # Blender 5.1 中 use_nodes=True 后 node_tree 可能仍未创建
    tree = scene.node_tree
    if tree is not None:
        return tree
    # 手动查找或创建
    for ng in bpy.data.node_groups:
        if ng.name == f"CompositingNodeTree_{scene.name}":
            return ng
    # 强制触发一次 use_nodes 切换来创建
    scene.use_nodes = False
    scene.use_nodes = True
    tree = scene.node_tree
    if tree is not None:
        return tree
    raise RuntimeError("无法创建场景的 Compositor 节点树")


def _reset_compositor_for_indexob(scene):
    """设置 Compositor（或清理后设置）为 IndexOB → Viewer Node。"""
    tree = _get_scene_node_tree(scene)
    for n in list(tree.nodes):
        tree.nodes.remove(n)

    rl = tree.nodes.new(type="CompositorNodeRLayers")
    rl.location = (0, 0)
    viewer = tree.nodes.new(type="CompositorNodeViewer")
    viewer.location = (300, 0)
    viewer.name = "zlh_viewer"

    # 尝试按名称连 IndexOB
    try:
        tree.links.new(rl.outputs["IndexOB"], viewer.inputs[0])
    except KeyError:
        for out in rl.outputs:
            if "Index" in out.name or "index" in out.name:
                tree.links.new(out, viewer.inputs[0])
                break
    tree.update_tag()


# ════════════════════════════════════════════════════════════
# 渲染并读取 IndexOB
# ════════════════════════════════════════════════════════════

def _render_and_read_indexob(scene, max_wait: int = 15) -> Optional[np.ndarray]:
    """渲染当前帧，读 IndexOB Viewer Node → (H, W) int32 数组。"""
    try:
        bpy.ops.render.render(write_still=False)
    except Exception as e:
        _log(f"  渲染失败: {e}")
        return None

    img = None
    for att in range(max_wait):
        img = bpy.data.images.get("Viewer Node")
        if img and img.size[0] > 0 and img.size[1] > 0:
            break
        _time.sleep(0.2)

    if img is None or img.size[0] == 0 or img.size[1] == 0:
        return None

    w, h = img.size
    pix = np.array(img.pixels[:]).reshape(h, w, 4)
    return np.round(pix[:, :, 0]).astype(np.int32)


# ════════════════════════════════════════════════════════════
# 核心枚举逻辑
# ════════════════════════════════════════════════════════════

def _enumerate_visible_sets(context) -> dict:
    """枚举所有组合并去重，返回有效组合信息。"""
    scene = context.scene
    cam = scene.camera
    if cam is None:
        return {"error": "场景中没有激活相机"}

    _log("[gpu_occlusion] ===== 开始枚举各组合的实际可见物体 =====")

    # ── 1. 收集所有可见 MESH 物体 ──
    all_meshes: List[bpy.types.Object] = []
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        if obj.hide_get() or not obj.visible_get():
            continue
        all_meshes.append(obj)

    if not all_meshes:
        return {"error": "场景中无可见 MESH 物体"}

    # ── 2. 分配 pass_index ──
    index_to_name: Dict[int, str] = {}
    for idx, obj in enumerate(all_meshes):
        pid = idx + 1
        obj.pass_index = pid
        index_to_name[pid] = obj.name

    # ── 3. 筛选 removable ──
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

    # ── 4. 保存原始状态 ──
    orig_render = _save_render_state(scene)
    orig_hide: Dict[str, bool] = {}
    for obj in all_meshes:
        orig_hide[obj.name] = obj.hide_render
    orig_pass: Dict[str, int] = {}
    for obj in scene.objects:
        orig_pass[obj.name] = obj.pass_index
    orig_workspace = context.window.workspace.name if context.window else None

    # ── 5. 设置 Eevee + Compositor ──
    scene.render.engine = "BLENDER_EEVEE_NEXT" if hasattr(bpy.types, "BLENDER_EEVEE_NEXT") else "BLENDER_EEVEE"
    scene.render.resolution_percentage = RENDER_RESOLUTION_PERCENT
    scene.view_layers[0].use_pass_object_index = True

    if context.window:
        for ws in bpy.data.workspaces:
            if ws.name == "Compositing":
                context.window.workspace = ws
                break

    _reset_compositor_for_indexob(scene)

    try:
        # ════════════════════════════════════════════════════
        # 第一步：先渲染全场景（全部可见），确定「所有可能出现的物体」
        # ════════════════════════════════════════════════════
        for obj in all_meshes:
            obj.hide_render = False
        context.view_layer.update()

        full_index = _render_and_read_indexob(scene)
        if full_index is None:
            return {"error": "全场景渲染失败"}

        # 所有在全场景中有像素的物体（就是当前视角能看到的）
        all_visible_set: Set[str] = set()
        for pid, name in index_to_name.items():
            if np.any(full_index == pid):
                all_visible_set.add(name)

        _log(f"[gpu_occlusion] 全场景可见物体 = {len(all_visible_set)} 个")

        # ════════════════════════════════════════════════════
        # 第二步：枚举所有组合
        # ════════════════════════════════════════════════════
        seen_signatures: Set[Tuple[str, ...]] = set()
        effective_list: List[Tuple[int, Set[str]]] = []

        for mask in range(total_combo):
            # 当前组合中 hidden 的物体
            hidden_names: Set[str] = set()
            for i in range(n_rem):
                if not ((mask >> i) & 1):
                    hidden_names.add(removable[i].name)

            # 设置 hide_render
            for obj in all_meshes:
                obj.hide_render = obj.name in hidden_names

            context.view_layer.update()

            # 渲染一张
            idx_map = _render_and_read_indexob(scene)
            if idx_map is None:
                _log(f"  组合 mask={mask}: 渲染失败，跳过")
                continue

            # 该组合下实际可见的物体
            actually_visible: Set[str] = set()
            for pid, name in index_to_name.items():
                if np.any(idx_map == pid):
                    actually_visible.add(name)

            # 去重
            sig = tuple(sorted(actually_visible))
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                effective_list.append((mask, actually_visible))
                _log(f"  组合 mask={mask}: 新增有效组合 → {sorted(actually_visible)}")
            else:
                _log(f"  组合 mask={mask}: 重复，跳过")

        _log(f"[gpu_occlusion] 枚举完成: {len(effective_list)} / {total_combo} 种有效组合")

        # ── 构建结果 ──
        # 统计每种物体在所有有效组合中的出现频次
        from collections import Counter
        freq: Counter[str] = Counter()
        for _mask, vis in effective_list:
            for name in vis:
                freq[name] += 1

        return {
            "all_objects": [o.name for o in all_meshes],
            "removable_names": [o.name for o in removable],
            "non_removable_names": [o.name for o in non_removable],
            "all_visible": sorted(all_visible_set),
            "effective_combinations": [
                {"mask": m, "visible": sorted(v)}
                for m, v in effective_list
            ],
            "count": len(effective_list),
            "total_theoretical": total_combo,
            "frequency": dict(freq.most_common()),
        }

    except Exception as e:
        _log(f"[gpu_occlusion] 异常: {e}")
        import traceback
        _log(traceback.format_exc())
        return {"error": str(e)}

    finally:
        for obj in scene.objects:
            if obj.name in orig_hide:
                obj.hide_render = orig_hide[obj.name]
            if obj.name in orig_pass:
                obj.pass_index = orig_pass[obj.name]

        _restore_render_state(scene, orig_render)

        if orig_workspace and context.window:
            for ws in bpy.data.workspaces:
                if ws.name == orig_workspace:
                    context.window.workspace = ws
                    break

        context.view_layer.update()
        _log("[gpu_occlusion] 场景状态已恢复")


# ════════════════════════════════════════════════════════════
# Operator
# ════════════════════════════════════════════════════════════

class ZLH_OT_GPUOcclusionAnalysis(bpy.types.Operator):
    """GPU 加速遮挡分析：枚举 removable 各子集，记录实际可见物体"""
    bl_idname = "zlh.gpu_occlusion_analysis"
    bl_label = "zlh: GPU 遮挡分析"
    bl_options = {"REGISTER"}

    _render_lock = False

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.camera is not None

    def invoke(self, context, _event):
        if ZLH_OT_GPUOcclusionAnalysis._render_lock:
            self.report({"WARNING"}, "遮挡分析已在进行中，请等待完成")
            return {"CANCELLED"}
        ZLH_OT_GPUOcclusionAnalysis._render_lock = True

        _log("[operator] ===== 开始 GPU 遮挡分析 =====")
        result = _enumerate_visible_sets(context)
        ZLH_OT_GPUOcclusionAnalysis._render_lock = False

        if "error" in result:
            self.report({"ERROR"}, result["error"])
            _log(f"[operator] 失败: {result['error']}")
            return {"CANCELLED"}

        # 输出摘要
        _log(f"[operator] 可见物体总数: {len(result.get('all_visible', []))}")
        _log(f"[operator] removable: {result.get('removable_names', [])}")
        _log(f"[operator] 理论组合: {result['total_theoretical']}")
        _log(f"[operator] 有效组合: {result['count']}")
        _log(f"[operator] 频次统计: {result.get('frequency', {})}")

        context.scene["zlh_gpu_occlusion_result"] = result
        return context.window_manager.invoke_props_dialog(self, width=700)

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

        # ── 头部摘要 ──
        box = layout.box()
        box.label(text=f"removable: {n_rem} 个物体", icon="OBJECT_DATA")
        box.label(text=f"非 removable: {len(non_removable)} 个", icon="SCENE_DATA")
        box.label(text=f"理论组合: {theoretical} 种", icon="MODIFIER")
        box.label(text=f"实际有效: {actual} 种 (节省 {saved} 次渲染, "
                       f"{saved/theoretical*100:.1f}%)",
                  icon="SORT_ASC")

        # ── 当前全场景可见 ──
        box0 = layout.box()
        box0.label(text=f"当前视角下可见物体 ({len(all_visible)} 个):", icon="VIEWZOOM")
        box0.label(text=f"    {', '.join(all_visible[:30])}"
                        f"{'…' if len(all_visible) > 30 else ''}")

        # ── 每种物体的出现频次 ──
        if freq:
            box1 = layout.box()
            box1.label(text="各物体在有效组合中的出现频次:", icon="TEXTURE")
            for name, count in list(freq.items())[:25]:
                pct = count / actual * 100
                box1.label(text=f"    {name}: {count}/{actual} ({pct:.0f}%)")

        # ── 有效组合列表 ──
        if combos:
            box2 = layout.box()
            n_show = min(20, len(combos))
            box2.label(text=f"有效组合（展示前 {n_show}/{len(combos)} 种）:",
                       icon="RENDER_STILL")
            for i, combo in enumerate(combos[:n_show]):
                label = f"    {i + 1}. "
                visible = combo.get("visible", [])
                label += ", ".join(visible) if visible else "（空 — 所有物体均不可见）"
                box2.label(text=label)

    def execute(self, context):
        return {"FINISHED"}
