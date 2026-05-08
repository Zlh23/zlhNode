"""球形随机相机：将相机锁定在一个球面上，可随机移动并始终对准球心。

功能：
1. 标记相机为"球形随机相机"（通过 Ctrl+Shift+O 弹窗设置）
2. 场景中显示球面辅助线（空物体圈）
3. Ctrl+Shift+Q：在球面上随机选一个位置，相机对准球心
"""

import math
import random
from mathutils import Vector, Matrix
import bpy
from bpy.props import FloatProperty, BoolProperty

from . import _log


# ════════════════════════════════════════════════════════════
# 属性注册
# ════════════════════════════════════════════════════════════

def _register_sphere_camera_props():
    """在 Object 上注册球形相机标记属性，在 Scene 上注册球参数。"""
    bpy.types.Object.zlh_sphere_camera = BoolProperty(
        name="球形随机相机",
        description="将该相机设为球形随机相机（在球面上随机移动，始终对准球心）",
        default=False,
    )
    bpy.types.Scene.zlh_sphere_radius = FloatProperty(
        name="球半径",
        description="球形随机相机的球半径",
        default=5.0,
        min=0.1,
        max=1000.0,
    )
    bpy.types.Scene.zlh_sphere_center_x = FloatProperty(
        name="球心 X",
        description="球形随机相机的球心 X 坐标",
        default=0.0,
    )
    bpy.types.Scene.zlh_sphere_center_y = FloatProperty(
        name="球心 Y",
        description="球形随机相机的球心 Y 坐标",
        default=0.0,
    )
    bpy.types.Scene.zlh_sphere_center_z = FloatProperty(
        name="球心 Z",
        description="球形随机相机的球心 Z 坐标",
        default=0.0,
    )


def _unregister_sphere_camera_props():
    for attr in ("zlh_sphere_camera",):
        try:
            delattr(bpy.types.Object, attr)
        except AttributeError:
            pass
    for attr in ("zlh_sphere_radius", "zlh_sphere_center_x",
                 "zlh_sphere_center_y", "zlh_sphere_center_z"):
        try:
            delattr(bpy.types.Scene, attr)
        except AttributeError:
            pass


# ════════════════════════════════════════════════════════════
# 辅助线绘制（球面圈）
# ════════════════════════════════════════════════════════════

def _update_sphere_visualization(scene):
    """更新或创建球面辅助线（3 个正交圆环绕空物体）。"""
    cam = scene.camera
    if cam is None or not getattr(cam, "zlh_sphere_camera", False):
        _remove_sphere_visualization(scene)
        return

    center = Vector((
        scene.zlh_sphere_center_x,
        scene.zlh_sphere_center_y,
        scene.zlh_sphere_center_z,
    ))
    radius = scene.zlh_sphere_radius

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
        parent.empty_display_type = "SPHERE"

    # 创建 3 个圆环线：XY, XZ, YZ 平面
    rings_data = []
    for name, axis, color in [
        ("zlh_sphere_ring_xy", "Z", (1, 1, 0)),    # 黄色
        ("zlh_sphere_ring_xz", "Y", (0, 1, 1)),    # 青色
        ("zlh_sphere_ring_yz", "X", (1, 0, 1)),    # 洋红
    ]:
        ring = scene.objects.get(name)
        if ring is None:
            # 创建圆环曲线
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
                else:  # X
                    x = 0
                    y = radius * math.cos(theta)
                    z = radius * math.sin(theta)
                spline.points[i].co = (x, y, z, 1)

            curve_data.materials.append(None)
            # 设置材质颜色
            mat = bpy.data.materials.new(name=name + "_mat")
            mat.use_nodes = True
            mat.diffuse_color = (*color, 1)
            pr = mat.node_tree.nodes.get("Principled BSDF")
            if pr:
                pr.inputs["Base Color"].default_value = (*color, 1)
            curve_data.materials[0] = mat

            ring_obj = bpy.data.objects.new(name, curve_data)
            ring_obj.location = center
            scene.collection.objects.link(ring_obj)
        else:
            ring_obj = ring
            ring_obj.location = center

        # 设置显示选项
        ring_obj.hide_render = True
        ring_obj.hide_select = True
        ring_obj.display_type = "WIRE"
        ring_obj.color = color
        # 子级到 parent
        if ring_obj.parent != parent:
            ring_obj.parent = parent

        rings_data.append(ring_obj)

    # 移除多余的旧环
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
# 随机位置生成
# ════════════════════════════════════════════════════════════

def _random_sphere_position(scene) -> Vector:
    """在球面上随机生成一个点，返回世界坐标。"""
    center = Vector((
        scene.zlh_sphere_center_x,
        scene.zlh_sphere_center_y,
        scene.zlh_sphere_center_z,
    ))
    radius = scene.zlh_sphere_radius

    # 球面上的均匀随机分布
    u = random.random()
    v = random.random()
    theta = 2 * math.pi * u
    phi = math.acos(2 * v - 1)

    x = radius * math.sin(phi) * math.cos(theta)
    y = radius * math.sin(phi) * math.sin(theta)
    z = radius * math.cos(phi)

    return center + Vector((x, y, z))


def _point_camera_at_center(cam_obj: bpy.types.Object, scene):
    """让相机对准球心。"""
    center = Vector((
        scene.zlh_sphere_center_x,
        scene.zlh_sphere_center_y,
        scene.zlh_sphere_center_z,
    ))
    direction = center - cam_obj.location
    if direction.length_squared < 1e-8:
        return
    # 使用 track-to 约束
    # 先移除已有的 track-to 约束
    old_constraints = [c for c in cam_obj.constraints if c.type == "TRACK_TO"]
    for c in old_constraints:
        cam_obj.constraints.remove(c)

    # 创建空目标
    target_name = "zlh_sphere_target"
    target = scene.objects.get(target_name)
    if target is None:
        target = bpy.data.objects.new(target_name, None)
        target.empty_display_size = 0.1
        scene.collection.objects.link(target)

    target.location = center

    track = cam_obj.constraints.new(type="TRACK_TO")
    track.target = target
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"
    track.owner_space = "WORLD"
    track.target_space = "WORLD"


def _randomize_sphere_camera(scene) -> bool:
    """对当前场景的球形相机执行随机位置设置。成功返回 True。"""
    cam = scene.camera
    if cam is None:
        _log("[sphere_camera] 场景中没有相机")
        return False
    if not getattr(cam, "zlh_sphere_camera", False):
        _log(f"[sphere_camera] {cam.name} 不是球形随机相机")
        return False

    center = Vector((
        scene.zlh_sphere_center_x,
        scene.zlh_sphere_center_y,
        scene.zlh_sphere_center_z,
    ))
    radius = scene.zlh_sphere_radius
    _log(f"[sphere_camera] 球心={center} 半径={radius}")

    # 随机位置
    new_pos = _random_sphere_position(scene)
    cam.location = new_pos
    _log(f"[sphere_camera] 相机新位置: {new_pos}")

    # 对准球心
    _point_camera_at_center(cam, scene)

    # 更新场景
    bpy.context.view_layer.update()

    _log(f"[sphere_camera] 完成: cam={cam.name} pos={cam.location}")
    return True
