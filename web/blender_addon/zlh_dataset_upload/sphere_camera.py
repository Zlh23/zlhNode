"""球形随机相机：选中锚点物体作为球心，相机绕其旋转。

用法：
1. Ctrl+Shift+O 选中相机 → 弹窗中选一个物体作为锚点（球心）
2. 自动用相机到锚点的距离作为半径，显示球面辅助线
3. Ctrl+Shift+Q 在球面上随机移动，始终对准锚点
"""

import math
import random
from mathutils import Vector
import bpy
from bpy.props import BoolProperty, PointerProperty

from . import _log


# ════════════════════════════════════════════════════════════
# 属性注册
# ════════════════════════════════════════════════════════════

def _register_sphere_camera_props():
    """在 Object 上注册球形相机标记，在 Scene 上记录锚点物体。"""
    bpy.types.Object.zlh_sphere_camera = BoolProperty(
        name="球形随机相机",
        description="设为球形随机相机（绕锚点物体随机移动）",
        default=False,
    )
    bpy.types.Scene.zlh_sphere_anchor = PointerProperty(
        name="球心锚点",
        type=bpy.types.Object,
        description="球形随机相机的锚点物体，相机绕其旋转",
    )


def _unregister_sphere_camera_props():
    for attr in ("zlh_sphere_camera",):
        try:
            delattr(bpy.types.Object, attr)
        except AttributeError:
            pass
    for attr in ("zlh_sphere_anchor",):
        try:
            delattr(bpy.types.Scene, attr)
        except AttributeError:
            pass


# ════════════════════════════════════════════════════════════
# 辅助线
# ════════════════════════════════════════════════════════════

def _get_sphere_params(scene):
    """获取球心位置和半径。如果锚点不存在则返回 None。"""
    cam = scene.camera
    anchor = scene.zlh_sphere_anchor
    if cam is None or anchor is None:
        return None
    center = anchor.location.copy()
    radius = (cam.location - center).length
    if radius < 0.001:
        return None
    return center, radius


def _update_sphere_visualization(scene):
    """更新或创建球面辅助线。"""
    cam = scene.camera
    if cam is None or not getattr(cam, "zlh_sphere_camera", False):
        _remove_sphere_visualization(scene)
        return

    params = _get_sphere_params(scene)
    if params is None:
        _remove_sphere_visualization(scene)
        return

    center, radius = params

    # 查找或创建空物体作为辅助线容器
    parent_name = "zlh_sphere_gizmo"
    parent = scene.objects.get(parent_name)
    if parent is None:
        parent = bpy.data.objects.new(parent_name, None)
        parent.empty_display_type = "SPHERE"
        parent.empty_display_size = radius
        parent.location = center
        scene.collection.objects.link(parent)
    else:
        parent.location = center
        parent.empty_display_size = radius

    # 3 个圆环线
    rings_data = []
    for name, axis, color in [
        ("zlh_sphere_ring_xy", "Z", (1, 1, 0, 1)),
        ("zlh_sphere_ring_xz", "Y", (0, 1, 1, 1)),
        ("zlh_sphere_ring_yz", "X", (1, 0, 1, 1)),
    ]:
        ring = scene.objects.get(name)
        if ring is None:
            curve_data = bpy.data.curves.new(name=name + "_curve", type="CURVE")
            curve_data.dimensions = "3D"
            spline = curve_data.splines.new(type="NURBS")
            n_points = 64
            spline.points.add(n_points - 1)
            for i in range(n_points):
                theta = 2 * math.pi * i / n_points
                if axis == "Z":
                    x = radius * math.cos(theta)
                    y = radius * math.sin(theta)
                    z = 0
                elif axis == "Y":
                    x = radius * math.cos(theta)
                    y = 0
                    z = radius * math.sin(theta)
                else:
                    x = 0
                    y = radius * math.cos(theta)
                    z = radius * math.sin(theta)
                spline.points[i].co = (x, y, z, 1)

            ring_obj = bpy.data.objects.new(name, curve_data)
            ring_obj.location = center
            scene.collection.objects.link(ring_obj)
        else:
            ring_obj = ring
            ring_obj.location = center

        ring_obj.hide_render = True
        ring_obj.hide_select = True
        ring_obj.display_type = "WIRE"
        ring_obj.color = color
        if ring_obj.parent != parent:
            ring_obj.parent = parent

        rings_data.append(ring_obj)

    # 移除多余旧环
    for obj in list(scene.objects):
        if obj.name.startswith("zlh_sphere_ring_") and obj not in rings_data:
            try:
                scene.collection.objects.unlink(obj)
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass


def _remove_sphere_visualization(scene):
    """移除球面辅助线。"""
    for name in list(bpy.data.objects.keys()):
        if name.startswith("zlh_sphere_"):
            obj = bpy.data.objects.get(name)
            if obj:
                try:
                    scene.collection.objects.unlink(obj)
                    bpy.data.objects.remove(obj, do_unlink=True)
                except Exception:
                    pass


# ════════════════════════════════════════════════════════════
# 随机移动
# ════════════════════════════════════════════════════════════

def _randomize_sphere_camera(scene) -> bool:
    """在球面上随机移动相机，对准锚点。"""
    cam = scene.camera
    if cam is None:
        _log("[sphere_camera] 场景中没有相机")
        return False
    if not getattr(cam, "zlh_sphere_camera", False):
        _log(f"[sphere_camera] {cam.name} 不是球形随机相机")
        return False

    params = _get_sphere_params(scene)
    if params is None:
        _log("[sphere_camera] 锚点无效")
        return False

    center, radius = params
    _log(f"[sphere_camera] 锚点={scene.zlh_sphere_anchor.name} 球心={center} 半径={radius}")

    # 球面均匀随机
    u = random.random()
    v = random.random()
    theta = 2 * math.pi * u
    phi = math.acos(2 * v - 1)

    x = radius * math.sin(phi) * math.cos(theta)
    y = radius * math.sin(phi) * math.sin(theta)
    z = radius * math.cos(phi)

    new_pos = center + Vector((x, y, z))
    cam.location = new_pos
    _log(f"[sphere_camera] 相机新位置: {new_pos}")

    # 对准锚点
    _point_camera_at(cam, center, scene)

    bpy.context.view_layer.update()
    _log(f"[sphere_camera] 完成: cam={cam.name}")
    return True


def _point_camera_at(cam_obj, target_pos, scene):
    """用 Track To 约束让相机对准目标位置。"""
    # 移除旧 track-to
    for c in list(cam_obj.constraints):
        if c.type == "TRACK_TO":
            cam_obj.constraints.remove(c)

    # 创建/复用空目标
    target_name = "zlh_sphere_target"
    target = scene.objects.get(target_name)
    if target is None:
        target = bpy.data.objects.new(target_name, None)
        target.empty_display_size = 0.1
        scene.collection.objects.link(target)
    target.location = target_pos

    track = cam_obj.constraints.new(type="TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    track.owner_space = "WORLD"
    track.target_space = "WORLD"
