"""遮挡检测核心逻辑：视锥体裁剪、表面采样、光线投射遮挡检测。"""

import random
from mathutils import Vector
from typing import Dict, List, Optional, Tuple

import bpy
from bpy_extras.object_utils import world_to_camera_view

from . import _log


def _sample_mesh_surface(
    mesh: bpy.types.Mesh, obj: bpy.types.Object, num_samples: int,
) -> List[Vector]:
    """在网格三角面表面上按面积加权均匀采样，返回世界坐标列表。"""
    mesh.calc_loop_triangles()
    tri_loops = mesh.loop_triangles
    if not tri_loops:
        return []

    tris_local: List[Tuple[Vector, Vector, Vector]] = []
    areas: List[float] = []
    vertices = mesh.vertices
    for tri in tri_loops:
        v0 = Vector(vertices[tri.vertices[0]].co)
        v1 = Vector(vertices[tri.vertices[1]].co)
        v2 = Vector(vertices[tri.vertices[2]].co)
        tris_local.append((v0, v1, v2))
        areas.append((v1 - v0).cross(v2 - v0).length / 2.0)

    total_area = sum(areas)
    if total_area <= 0:
        return []

    samples: List[Vector] = []
    for _ in range(num_samples):
        r = random.random() * total_area
        cum = 0.0
        idx = 0
        for i, a in enumerate(areas):
            cum += a
            if r <= cum:
                idx = i
                break
        v0, v1, v2 = tris_local[idx]
        s = random.random()
        t = random.random()
        if s + t > 1.0:
            s = 1.0 - s
            t = 1.0 - t
        local_pt = v0 + s * (v1 - v0) + t * (v2 - v0)
        world_pt = obj.matrix_world @ local_pt
        samples.append(world_pt)

    return samples


def _cache_mesh_samples(
    obj: bpy.types.Object, depsgraph, num_samples: int,
) -> Optional[List[Vector]]:
    """预计算并缓存物体表面的世界坐标采样点。"""
    eval_obj = obj.evaluated_get(depsgraph)

    mesh: bpy.types.Mesh | None = eval_obj.data
    if mesh is None:
        _log(f"[_cache_mesh_samples] {obj.name} eval_obj.data 为 None，回退到 obj.data")
        mesh = obj.data
    if mesh is None or not hasattr(mesh, "vertices"):
        return None
    samples = _sample_mesh_surface(mesh, eval_obj, num_samples)
    _log(f"[_cache_mesh_samples] {obj.name} 采样 {num_samples} 点，实际获取 {len(samples) if samples else 0} 点")
    return samples if samples else None


def _is_occluded(
    scene, cam_pos: Vector,
    obj: bpy.types.Object,
    obj_samples: List[Vector],
    depsgraph,
    occlusion_threshold: float = 0.5,
    hidden_set: Optional[set[str]] = None,
) -> bool:
    """通过光线投射判断物体是否被遮挡。

    hidden_set: 当前被隐藏（hide_render=True）的物体名集合。
        ray_cast 会命中所有物体（包括 hide_render=True 的），
        我们需要跳过这些本该被隐藏的物体，才能正确处理嵌套遮挡场景。
    """
    if hidden_set is None:
        hidden_set = set()

    occluded_count = 0
    for idx, pt in enumerate(obj_samples):
        direction = pt - cam_pos
        total_dist = direction.length
        if total_dist < 1e-6:
            _log(f"[_is_occluded] {obj.name} 采样点 {idx} 与相机重合，跳过")
            continue
        direction.normalize()

        hit_obj = None
        ray_origin = cam_pos
        remaining_dist = total_dist
        max_iter = 32
        for _ in range(max_iter):
            result, _hit_pos, _hit_normal, _hit_index, hit_obj, _ = scene.ray_cast(
                depsgraph, ray_origin, direction, distance=remaining_dist,
            )
            if not result:
                hit_obj = None
                break
            if hit_obj.name not in hidden_set:
                break
            _log(f"[_is_occluded] {obj.name} 采样点 {idx}/{len(obj_samples)} 穿透已隐藏物体 {hit_obj.name}")
            hit_dist = (_hit_pos - ray_origin).length
            remaining_dist -= hit_dist
            if remaining_dist < 1e-6:
                hit_obj = None
                break
            ray_origin = _hit_pos + direction * 1e-4

        if hit_obj is None:
            continue
        if hit_obj != obj:
            occluded_count += 1
            _log(f"[_is_occluded] {obj.name} 采样点 {idx}/{len(obj_samples)} 被 {hit_obj.name} 遮挡")

    ratio = occluded_count / len(obj_samples) if obj_samples else 0
    _log(f"[_is_occluded] {obj.name} 遮挡比例 {occluded_count}/{len(obj_samples)}={ratio:.2f} 阈值={occlusion_threshold} 判定={'遮挡' if ratio > occlusion_threshold else '可见'}")
    return ratio > occlusion_threshold


def _get_visible_objects(
    context,
    hidden_set: Optional[set[str]] = None,
    sample_cache: Optional[Dict[str, List[Vector]]] = None,
) -> Tuple[set[str], list[Tuple[str, bpy.types.Object]]]:
    """三步过滤：场景物体迭代 → 视锥体 AABB 粗筛 → 顶点 NDC 精确检测 → 光线投射遮挡检测。

    返回 (visible_names, removable_list)
    """
    scene = context.scene
    cam = scene.camera
    if cam is None:
        _log("[_get_visible_objects] 场景中没有相机，返回空")
        return set(), []

    depsgraph = context.evaluated_depsgraph_get()
    cs = cam.data.clip_start
    ce = cam.data.clip_end

    eval_cam = cam.evaluated_get(depsgraph)
    eval_cam_matrix = eval_cam.matrix_world

    _log(f"[_get_visible_objects] 相机={cam.name} clip_start={cs} clip_end={ce}")
    _log(f"[_get_visible_objects] 相机矩阵平移={eval_cam_matrix.translation}")

    # ---- 构建视锥体 6 个平面 ----
    frame = cam.data.view_frame(scene=scene)
    _log(f"[_get_visible_objects] view_frame 近平面角点（局部）={[Vector((p.x, p.y, p.z)) for p in frame]}")
    near_corners = [eval_cam_matrix @ Vector((p.x, p.y, p.z)) for p in frame]
    cam_pos = eval_cam_matrix.translation
    cam_dir = eval_cam_matrix.to_quaternion() @ Vector((0, 0, -1))
    _log(f"[_get_visible_objects] 近平面角点（世界）={near_corners}")
    _log(f"[_get_visible_objects] 相机位置={cam_pos} 朝向={cam_dir}")

    def _plane_from_three(a: Vector, b: Vector, c: Vector) -> Tuple[Vector, float]:
        n = (b - a).cross(c - a).normalized()
        d = -n.dot(a)
        return n, d

    n_near, d_near = _plane_from_three(near_corners[0], near_corners[1], near_corners[2])
    if n_near.dot(sum(near_corners, Vector()) / 4 - cam_pos) < 0:
        n_near, d_near = -n_near, -d_near
    _log(f"[_get_visible_objects] 近平面 n={n_near} d={d_near}")

    far_corners = [p + cam_dir * (ce - cs) for p in near_corners]
    n_far, d_far = _plane_from_three(far_corners[0], far_corners[2], far_corners[1])
    near_center = sum(near_corners, Vector()) / 4
    if n_far.dot(near_center - (near_center + cam_dir * (ce - cs))) < 0:
        n_far, d_far = -n_far, -d_far
    _log(f"[_get_visible_objects] 远平面 n={n_far} d={d_far}")

    side_planes: list[Tuple[Vector, float]] = []
    for i in range(4):
        j = (i + 1) % 4
        n, d = _plane_from_three(cam_pos, near_corners[i], near_corners[j])
        if n.dot(near_center) + d < 0:
            n, d = -n, -d
        side_planes.append((n, d))

    frustum_planes = [*side_planes, (n_near, d_near), (n_far, d_far)]
    _log(f"[_get_visible_objects] 视锥体 6 个平面已构建")

    def _aabb_in_frustum(min_c: Vector, max_c: Vector) -> bool:
        for n, d in frustum_planes:
            px = max_c.x if n.x >= 0 else min_c.x
            py = max_c.y if n.y >= 0 else min_c.y
            pz = max_c.z if n.z >= 0 else min_c.z
            if n.dot(Vector((px, py, pz))) + d < 0:
                return False
        return True

    MAX_VERTICES = 256

    def _has_vertex_in_frustum(obj_eval: bpy.types.Object) -> bool:
        mesh: bpy.types.Mesh | None = obj_eval.data
        if mesh is None or not hasattr(mesh, "vertices"):
            return False
        verts = mesh.vertices
        total = len(verts)
        if total == 0:
            return False
        step = 1 if total <= MAX_VERTICES else max(1, total // MAX_VERTICES)
        world_mat = obj_eval.matrix_world
        for i in range(0, total, step):
            world_pos = world_mat @ verts[i].co
            ndc = world_to_camera_view(scene, eval_cam, world_pos)
            if 0.0 <= ndc.x <= 1.0 and 0.0 <= ndc.y <= 1.0 and cs <= ndc.z <= ce:
                return True
        return False

    # ---- 主循环 ----
    visible_types = {"MESH", "CURVE", "SURFACE", "META", "FONT", "GPENCIL", "ARMATURE", "LATTICE", "EMPTY"}

    all_candidates: list[bpy.types.Object] = []
    have_mesh = False
    scene_objects = list(scene.objects)
    _log(f"[_get_visible_objects] 场景中共 {len(scene_objects)} 个物体")

    for obj in scene_objects:
        try:
            if obj.type not in visible_types:
                _log(f"[_get_visible_objects]   跳过 {obj.name} 类型={obj.type}")
                continue
            if obj.hide_get():
                _log(f"[_get_visible_objects]   跳过 {obj.name} hide_get=True")
                continue
            if not obj.visible_get():
                _log(f"[_get_visible_objects]   跳过 {obj.name} visible_get=False")
                continue
            if hidden_set and obj.name in hidden_set:
                _log(f"[_get_visible_objects]   跳过 {obj.name}（在当前 hidden_set 中）")
                continue
            if obj.type == "MESH":
                have_mesh = True
            all_candidates.append(obj)
            _log(f"[_get_visible_objects]   候选 +{obj.name} type={obj.type}")
        except Exception as e:
            _log(f"[_get_visible_objects]   候选收集异常（{obj.name}）: {e}")
            continue

    _log(f"[_get_visible_objects] 候选物体共 {len(all_candidates)} 个, have_mesh={have_mesh}")

    all_names: set[str] = set()
    removable_list: list[Tuple[str, bpy.types.Object]] = []
    passed_aabb = 0
    passed_vertex = 0
    passed_occlusion = 0

    for obj in all_candidates:
        try:
            obj_eval = obj.evaluated_get(depsgraph)
            _log(f"[_get_visible_objects] 处理 {obj.name}：evaluated_get 成功")

            bbox_local = obj.bound_box
            if not bbox_local:
                _log(f"[_get_visible_objects]   {obj.name} bound_box 为空，跳过")
                continue

            world_mat = obj_eval.matrix_world
            corners_world = [world_mat @ Vector(p) for p in bbox_local]
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
            _log(f"[_get_visible_objects]   {obj.name} AABB min={min_c} max={max_c}")

            if not _aabb_in_frustum(min_c, max_c):
                _log(f"[_get_visible_objects]   {obj.name} AABB 视锥体粗筛 失败")
                continue
            passed_aabb += 1
            _log(f"[_get_visible_objects]   {obj.name} AABB 视锥体粗筛 通过")

            if not _has_vertex_in_frustum(obj_eval):
                _log(f"[_get_visible_objects]   {obj.name} 顶点 NDC 检测 失败")
                continue
            passed_vertex += 1
            _log(f"[_get_visible_objects]   {obj.name} 顶点 NDC 检测 通过")

            # 遮挡检测
            if have_mesh and obj.type == "MESH":
                obj_samples = None
                if sample_cache is not None:
                    obj_samples = sample_cache.get(obj.name)
                if obj_samples is None:
                    _log(f"[_get_visible_objects]   {obj.name} 开始采样遮挡检测（采样 50 点）")
                    obj_samples = _cache_mesh_samples(obj, depsgraph, 50)
                if obj_samples:
                    _log(f"[_get_visible_objects]   {obj.name} 采样点 {len(obj_samples)} 个，进行射线检测")
                    is_occ = _is_occluded(scene, cam_pos, obj, obj_samples, depsgraph,
                                          hidden_set=hidden_set)
                    _log(f"[_get_visible_objects]   {obj.name} 遮挡检测结果: occluded={is_occ}")
                    if is_occ:
                        continue
                else:
                    _log(f"[_get_visible_objects]   {obj.name} 采样返回空，视为未遮挡")
            else:
                _log(f"[_get_visible_objects]   {obj.name} 非 MESH 或场景无 MESH，跳过遮挡检测")
            passed_occlusion += 1

            all_names.add(obj.name)
            _log(f"[_get_visible_objects]   {obj.name} 最终判定为 可见")
            if getattr(obj, "zlh_removable", False):
                removable_list.append((obj.name, obj))
                _log(f"[_get_visible_objects]   {obj.name} 标记为 removable")
        except Exception as e:
            _log(f"[_get_visible_objects]   {obj.name} 处理异常: {e}")
            import traceback
            _log(f"[_get_visible_objects]   traceback: {traceback.format_exc()}")
            continue

    _log(f"[_get_visible_objects] 汇总: AABB通过={passed_aabb} 顶点NDC通过={passed_vertex} 遮挡检测通过={passed_occlusion} 最终可见={len(all_names)}")
    _log(f"[_get_visible_objects] 可见物体列表: {sorted(all_names)}")
    return all_names, removable_list
