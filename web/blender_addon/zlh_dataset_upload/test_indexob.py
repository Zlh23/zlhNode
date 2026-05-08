"""测试脚本：模拟新版 Ctrl+Shift+B 流程

流程：
1. 视锥体检测 → 所有 MESH 物体
2. 筛选 removable
3. 随机选择 N 个组合（随机决定 removable 是否可见）
4. 渲染
5. IndexOB（Viewer Node）检测实际可见物体
6. 打印结果
"""

import bpy
import numpy as np
from mathutils import Vector
from collections import Counter
import os
import tempfile
import random


# ──────────────────────────────────────────────
# 从 gpu_occlusion.py 复制过来的视锥体工具
# ──────────────────────────────────────────────

def _build_frustum_planes(scene):
    """从场景相机构建视锥体 6 个平面（仅用于 AABB 粗筛，不精确）。"""
    cam = scene.camera
    if cam is None:
        return []
    cs = cam.data.clip_start
    ce = cam.data.clip_end
    frame = cam.data.view_frame(scene=scene)
    cam_matrix = cam.matrix_world.normalized()
    cam_pos = cam_matrix.translation
    cam_dir = cam_matrix @ Vector((0, 0, -1)) - cam_pos
    cam_dir.normalize()
    near_center = cam_pos + cam_dir * cs
    far_center = cam_pos + cam_dir * ce
    near_corners = [cam_matrix @ Vector((p.x, p.y, -cs)) for p in frame]
    far_corners = [cam_matrix @ Vector((p.x, p.y, -ce)) for p in frame]
    planes = []
    # 近平面
    n = cam_dir.copy()
    d = n.dot(near_center)
    planes.append((n, d))
    # 远平面
    n = -cam_dir
    d = n.dot(far_center)
    planes.append((n, d))
    # 四个侧面
    for i in range(4):
        p0 = near_corners[i]
        p1 = near_corners[(i + 1) % 4]
        p2 = far_corners[i]
        n = (p1 - p0).cross(p2 - p0).normalized()
        d = n.dot(p0)
        if n.dot(cam_pos) < d:
            n = -n
            d = -d
        planes.append((n, d))
    return planes


def _aabb_in_frustum(frustum_planes, min_c, max_c):
    for n, d in frustum_planes:
        p = Vector((
            max_c.x if n.x > 0 else min_c.x,
            max_c.y if n.y > 0 else min_c.y,
            max_c.z if n.z > 0 else min_c.z,
        ))
        if n.dot(p) < d - 1e-6:
            return False
    return True


# ──────────────────────────────────────────────
# IndexOB 检测（简化版）
# ──────────────────────────────────────────────

def _setup_compositor_for_indexob(scene):
    """设置 Compositor: RLayers.IndexOB -> Viewer"""
    scene.render.use_compositing = True
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
    viewer.name = "indexob_viewer"
    connected = False
    for out in rl.outputs:
        if "IndexOB" in out.name or out.name == "IndexOB" or "index" in out.name.lower():
            tree.links.new(out, viewer.inputs[0])
            connected = True
            print(f"  [Compositor] IndexOB -> Viewer (output: {out.name})")
            break
    if not connected:
        print("  [Compositor] 未找到 IndexOB 输出，尝试 Col")
        tree.links.new(rl.outputs["Col"], viewer.inputs[0])
    tree.update_tag()


def _read_indexob_from_viewer():
    """从 Viewer Node 读取 IndexOB 数据"""
    viewer_img = bpy.data.images.get("Viewer Node")
    if viewer_img is None:
        print("  [IndexOB] 错误: 找不到 Viewer Node")
        return None
    w, h = viewer_img.size
    if w == 0 or h == 0:
        print("  [IndexOB] Viewer Node 尺寸为 0")
        return None
    pix = np.array(viewer_img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    return pix[:, :, 0]


def _detect_visible_objects(indexob, index_to_name, min_pixel_pct=0.01):
    """解码 IndexOB 数据，返回可见物体列表"""
    decoded = np.round(indexob).astype(np.int32)
    total_px = decoded.size
    present = set(decoded[decoded > 0])
    print(f"\n  [IndexOB] 像素中的 pass_index: {sorted(present)}")
    visible = []
    for pid in sorted(present):
        cnt = int((decoded == pid).sum())
        pct = cnt / total_px * 100
        name = index_to_name.get(int(pid), "???")
        if pct >= min_pixel_pct:
            visible.append(name)
            print(f"    ✅ {name} (pid={pid}): {cnt}px ({pct:.2f}%)")
        else:
            print(f"    ❌ 排除(噪声) {name} (pid={pid}): {cnt}px ({pct:.4f}%)")
    return visible


# ──────────────────────────────────────────────
# 主测试流程
# ──────────────────────────────────────────────

def test_new_ctrl_shift_b():
    print("=" * 70)
    print("测试新版 Ctrl+Shift+B 流程")
    print("=" * 70)

    scene = bpy.context.scene
    cam = scene.camera
    if cam is None:
        print("错误: 场景中没有激活相机")
        return

    print(f"当前相机: {cam.name}")

    # ── 1. 视锥体检测 ──
    print("\n[1] 视锥体检测...")
    frustum_planes = _build_frustum_planes(scene)
    all_meshes = []
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
            min_c = Vector((min(p.x for p in corners_world),
                            min(p.y for p in corners_world),
                            min(p.z for p in corners_world)))
            max_c = Vector((max(p.x for p in corners_world),
                            max(p.y for p in corners_world),
                            max(p.z for p in corners_world)))
            if _aabb_in_frustum(frustum_planes, min_c, max_c):
                all_meshes.append(obj)
        except Exception as e:
            print(f"    跳过 {obj.name}: {e}")

    if not all_meshes:
        print("视锥体内没有 MESH 物体！")
        return

    print(f"  视锥体内 MESH 物体 ({len(all_meshes)} 个):")
    for o in all_meshes:
        rem = " (removable)" if getattr(o, "zlh_removable", False) else ""
        print(f"    - {o.name}{rem}")

    # ── 2. 筛选 removable ──
    removable = [o for o in all_meshes if getattr(o, "zlh_removable", False)]
    if not removable:
        print("\n[2] 没有 removable 物体，不能测试随机组合！")
        return

    non_removable = [o for o in all_meshes if not getattr(o, "zlh_removable", False)]
    rem_names = [o.name for o in removable]
    print(f"\n[2] removable ({len(rem_names)} 个): {rem_names}")

    # ── 3. 用户输入随机张数 ──
    n_rem = len(removable)
    max_combinations = 1 << n_rem
    default_n = min(4, max_combinations)
    # 在 Blender 控制台显示提示
    print(f"\n[3] 理论组合数: {max_combinations}")
    print(f"    默认随机 {default_n} 张")

    # 生成随机组合
    all_masks = list(range(max_combinations))
    chosen = random.sample(all_masks, min(default_n, len(all_masks)))
    print(f"    选中的 masks: {chosen}")

    # ── 4-6. 循环渲染+IndexOB ──
    print("\n[4-6] 开始逐张渲染+IndexOB 检测...")

    for idx, mask in enumerate(chosen):
        print(f"\n{'─' * 60}")
        print(f"组合 {idx + 1}/{len(chosen)}  mask={mask}")

        # 计算哪些 removable 可见
        visible_rem = set()
        for i in range(n_rem):
            if (mask >> i) & 1:
                visible_rem.add(rem_names[i])
        print(f"  预定可见: {sorted(visible_rem)}")

        # 设置 hide_render
        orig_hide = {}
        for o in removable:
            orig_hide[o.name] = o.hide_render
            o.hide_render = o.name not in visible_rem

        bpy.context.view_layer.update()

        # ── 渲染 ──
        tmp_dir = tempfile.mkdtemp(prefix="zlh_test_")
        try:
            fp_orig = scene.render.filepath
            fm_orig = scene.render.image_settings.file_format
            scene.render.image_settings.file_format = "PNG"
            out_path = os.path.join(tmp_dir, "render.png")
            scene.render.filepath = out_path

            print(f"  渲染 {out_path}...")
            bpy.ops.render.render(write_still=True)
            print("  渲染完成")
        except Exception as e:
            print(f"  渲染失败: {e}")
            import traceback; traceback.print_exc()
            continue
        finally:
            scene.render.filepath = fp_orig
            scene.render.image_settings.file_format = fm_orig
            for o in removable:
                if o.name in orig_hide:
                    o.hide_render = orig_hide[o.name]

        # ── IndexOB 检测 ──
        print("  IndexOB 检测...")

        # 分配 pass_index（仅对当前应该可见的物体分配）
        all_idx_objects = [o for o in non_removable]
        for o in removable:
            if o.name in visible_rem:
                all_idx_objects.append(o)

        index_to_name = {}
        for pi, obj in enumerate(all_idx_objects):
            pid = pi + 1
            obj.pass_index = pid
            index_to_name[pid] = obj.name

        # 清空其他 mesh
        for obj in bpy.data.objects:
            if obj.type == "MESH" and obj not in all_idx_objects:
                obj.pass_index = 0

        # 保存原始渲染状态
        orig_engine = scene.render.engine
        orig_res_pct = scene.render.resolution_percentage
        orig_pass = scene.view_layers[0].use_pass_object_index
        orig_comp = scene.render.use_compositing
        orig_cycles_samples = scene.cycles.samples if hasattr(scene, "cycles") else 0

        try:
            scene.render.engine = "CYCLES"
            scene.render.resolution_percentage = 100
            scene.cycles.samples = 1
            scene.view_layers[0].use_pass_object_index = True
            _setup_compositor_for_indexob(scene)

            # 再次渲染以触发 Compositor
            scene.render.filepath = os.path.join(tmp_dir, "idx.png")
            bpy.ops.render.render(write_still=True)

            indexob = _read_indexob_from_viewer()
            if indexob is None:
                print("  IndexOB 读取失败")
                visible = [o.name for o in all_idx_objects]
                print(f"  降级使用: {visible}")
            else:
                visible = _detect_visible_objects(indexob, index_to_name)
                print(f"\n  ▸ 实际可见 ({len(visible)} 个): {visible}")
        except Exception as e:
            print(f"  IndexOB 检测异常: {e}")
            import traceback; traceback.print_exc()
            visible = [o.name for o in all_idx_objects]
            print(f"  降级使用: {visible}")
        finally:
            scene.render.engine = orig_engine
            scene.render.resolution_percentage = orig_res_pct
            scene.view_layers[0].use_pass_object_index = orig_pass
            scene.render.use_compositing = orig_comp
            if hasattr(scene, "cycles"):
                scene.cycles.samples = orig_cycles_samples
            # 清理 compositor
            tree = scene.compositing_node_group
            if tree:
                for n in list(tree.nodes):
                    tree.nodes.remove(n)
            for obj in bpy.data.objects:
                if obj.type == "MESH":
                    obj.pass_index = 0

        # ── 结果 ──
        names_str = ",".join(sorted(visible))
        print(f"\n  ✦ 逗号分隔: {names_str}")
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("\n" + "=" * 70)
    print("测试完成")
    print("=" * 70)


if __name__ == "__main__":
    test_new_ctrl_shift_b()
