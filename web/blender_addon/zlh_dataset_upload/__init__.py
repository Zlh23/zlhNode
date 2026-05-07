"""渲染当前激活相机视图：过滤视锥体内物体名字后提交到网页格子。"""

import datetime


def _log(msg: str):
    """带时间戳的日志，方便在 Blender 控制台追踪各步骤。"""
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[zlh][{ts}] {msg}")


bl_info = {
    "name": "zlh 数据集渲染上传",
    "author": "zlhNode",
    "version": (1, 13, 1),
    "blender": (5, 1, 0),
    "location": "快捷键（默认 Ctrl+Shift+B / Ctrl+Shift+O）",
    "description": "渲染当前相机、修改物体名称（自动过滤视锥体内物体）、分配到网页格子",
    "category": "Render",
    "tracker_url": "https://github.com/Zlh23/zlhNode/releases",
}

import json
import math
import mathutils
import os
import random
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import bpy
from bpy.props import StringProperty
from bpy.types import AddonPreferences, Operator
from mathutils import Vector

ADDON_ID = "zlh_dataset_upload"


def _prefs(context):
    return context.preferences.addons[ADDON_ID].preferences


def _normalize_base(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _http_json(method: str, url: str, body_obj: Optional[Dict[str, Any]], timeout: float = 180.0) -> dict:
    payload = None
    if body_obj is not None:
        payload = json.dumps(body_obj).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    data = json.loads(text) if text else {}
    if not isinstance(data, dict):
        raise RuntimeError("服务器返回非 JSON 对象")
    return data


def _post_render_output(api_base: str, filename: str, object_names: str) -> dict:
    """POST 只传文件路径和物体名，不再传图片数据。"""
    url = api_base + "/bridge/render/output"
    body = {
        "filename": filename,
        "object_names": object_names,
    }
    return _http_json("POST", url, body)


def _render_to_file(context, output_dir: str) -> str:
    """渲染当前相机视图到 output_dir，返回文件名（不含路径）。"""
    scene = context.scene
    fp_orig = scene.render.filepath
    fmt_orig = scene.render.image_settings.file_format
    scene.render.image_settings.file_format = "PNG"

    os.makedirs(output_dir, exist_ok=True)
    # 用 uuid 防止多线程/多实例冲突
    import uuid
    fname = uuid.uuid4().hex + ".png"
    out_path = os.path.join(output_dir, fname)
    try:
        scene.render.filepath = out_path
        bpy.ops.render.render(write_still=True)
    finally:
        scene.render.filepath = fp_orig
        scene.render.image_settings.file_format = fmt_orig

    if not os.path.isfile(out_path):
        raise RuntimeError(f"渲染失败，未生成文件: {out_path}")
    return fname


class ZLH_AddonPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    api_base: StringProperty(
        name="API 根地址",
        description="Comfy 服务地址，例如 http://127.0.0.1:8188（不要末尾 /）",
        default="http://127.0.0.1:8188",
    )

    output_dir: StringProperty(
        name="输出目录",
        description="WSL 共享目录路径（Windows 侧），渲染的 PNG 直接保存到这里。"
                    "例如 \\\\wsl.localhost\\Ubuntu\\home\\zlh-linux\\ComfyUI\\custom_nodes\\zlhNode\\temp\\blender_render",
        default=r"\\wsl.localhost\Ubuntu\home\zlh-linux\ComfyUI\custom_nodes\zlhNode\temp\blender_render",
        subtype="DIR_PATH",
    )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "api_base")
        layout.prop(self, "output_dir")
        box = layout.box()
        box.label(text="快捷键：编辑 → 偏好设置 → 键位映射 → 搜索「zlh」", icon="INFO")
        box.label(text="Ctrl+Shift+B：渲染并上传（outfit 为可见物体名）")
        box.label(text="  - 弹出对话框选择模式：单张 / 全部组合 / 随机 N 种")
        box.label(text="Ctrl+Shift+O：修改选中物体的名称，并设置 removable 标记", icon="OBJECT_DATA")
        box.label(text="若快捷键冲突，请手动在上述键位映射中改为其它按键", icon="ERROR")
        box.separator()
        row = box.row()
        row.operator("zlh.check_update", text="检查更新", icon="URL")
        row.label(text="当前版本: 1.13.1")


def _register_object_removable():
    """在 bpy.types.Object 上注册一个 removable 布尔属性。"""
    bpy.types.Object.zlh_removable = bpy.props.BoolProperty(
        name="removable",
        description="渲染时该物体可被移除（Ctrl+Shift+B 会生成包含/不包含它的多张图片）",
        default=False,
    )


def _unregister_object_removable():
    try:
        del bpy.types.Object.zlh_removable
    except AttributeError:
        pass


class ZLH_OT_SetObjectNames(Operator):
    bl_idname = "zlh.set_object_names"
    bl_label = "修改选中物体名称"
    bl_options = {"REGISTER"}

    new_name: StringProperty(
        name="新名称",
        description="输入新名称，确认后将修改所有选中物体的名字",
        default="",
    )

    @classmethod
    def poll(cls, context):
        return context.scene is not None and len(context.selected_objects) > 0

    def invoke(self, context, _event):
        obs = context.selected_objects
        if not obs:
            self.report({"ERROR"}, "请先选中至少一个物体")
            return {"CANCELLED"}
        # 默认用第一个物体的名字
        self.new_name = obs[0].name
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width=500)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "new_name")
        box = layout.box()
        box.label(text=f"当前选中 {len(context.selected_objects)} 个物体：", icon="OBJECT_DATA")
        for o in context.selected_objects:
            row = box.row()
            row.label(text=f"  {o.name}")
            row.prop(o, "zlh_removable", text="removable")
        box2 = layout.box()
        box2.label(text="提示：标记为 removable 的物体在渲染时会生成含/不含该物体的多张图片", icon="INFO")

    def execute(self, context):
        new = self.new_name.strip()
        if not new:
            self.report({"ERROR"}, "名称不能为空")
            return {"CANCELLED"}
        obs = context.selected_objects
        if len(obs) == 1:
            obs[0].name = new
            self.report({"INFO"}, f"已重命名为: {new}")
        else:
            for i, o in enumerate(obs):
                suffix = f"_{i}" if i > 0 else ""
                o.name = f"{new}{suffix}"
            self.report({"INFO"}, f"已重命名 {len(obs)} 个物体为: {new}_*")
        return {"FINISHED"}



def _sample_mesh_surface(
    mesh: bpy.types.Mesh, obj: bpy.types.Object, num_samples: int,
) -> List[Vector]:
    """在网格三角面表面上按面积加权均匀采样，返回世界坐标列表。

    使用 mesh.calc_loop_triangles()（Blender 4.1+），不依赖 bmesh。
    """
    # 计算三角形（loop triangles）
    mesh.calc_loop_triangles()
    tri_loops = mesh.loop_triangles
    if not tri_loops:
        return []

    # 收集三角形顶点和面积
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

    # 按面积加权采样
    weights = [a / total_area for a in areas]
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
        # 重心坐标均匀采样
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
    """预计算并缓存物体表面的世界坐标采样点（使用 evaluated_get 标准 API）。"""
    eval_obj = obj.evaluated_get(depsgraph)

    mesh: bpy.types.Mesh | None = eval_obj.data
    if mesh is None:
        _log(f"[_cache_mesh_samples] {obj.name} eval_obj.data 为 None，回退到 obj.data")
        mesh = obj.data
    if mesh is None or not hasattr(mesh, "vertices"):
        _log(f"[_cache_mesh_samples] {obj.name} 无网格数据，返回 None")
        return None
    # 使用 evaluated object 的世界矩阵，确保变形/修改器生效
    samples = _sample_mesh_surface(mesh, eval_obj, num_samples)
    _log(f"[_cache_mesh_samples] {obj.name} 采样 {num_samples} 点，实际获取 {len(samples) if samples else 0} 点")
    return samples if samples else None


def _is_occluded(
    scene, cam_pos: Vector,
    obj: bpy.types.Object,
    obj_samples: List[Vector],
    depsgraph,
    occlusion_threshold: float = 0.5,
) -> bool:
    """通过光线投射判断物体是否被遮挡（使用预缓存的采样点）。"""
    occluded_count = 0
    for idx, pt in enumerate(obj_samples):
        direction = pt - cam_pos
        dist = direction.length
        if dist < 1e-6:
            _log(f"[_is_occluded] {obj.name} 采样点 {idx} 与相机重合，跳过")
            continue
        direction.normalize()
        result, _hit_pos, _hit_normal, _hit_index, hit_obj, _ = scene.ray_cast(
            depsgraph, cam_pos, direction, distance=dist,
        )
        if result and hit_obj != obj:
            occluded_count += 1
            _log(f"[_is_occluded] {obj.name} 采样点 {idx}/{len(obj_samples)} 被 {hit_obj.name} 遮挡")
    ratio = occluded_count / len(obj_samples) if obj_samples else 0
    _log(f"[_is_occluded] {obj.name} 遮挡比例 {occluded_count}/{len(obj_samples)}={ratio:.2f} 阈值={occlusion_threshold} 判定={'遮挡' if ratio > occlusion_threshold else '可见'}")
    return ratio > occlusion_threshold


def _get_visible_objects(
    context,
    hidden_set: Optional[set[str]] = None,
    sample_cache: Optional[Dict[str, List[Vector]]] = None,
) -> tuple[set[str], list[tuple[str, bpy.types.Object]]]:
    """三步过滤：场景物体迭代 → 视锥体 AABB 粗筛 → 顶点 NDC 精确检测 → 光线投射遮挡检测。

    用 scene.objects 替代 context.visible_objects，配合 depsgraph
    获取正确求值后的矩阵和网格数据。

    hidden_set: 当前被隐藏的物体名集合（用于枚举组合时模拟不同状态）
    sample_cache: 预缓存的采样点 {obj_name: [world_pos, ...]}
    """
    scene = context.scene
    cam = scene.camera
    if cam is None:
        _log("[_get_visible_objects] 场景中没有相机，返回空")
        return set(), []

    depsgraph = context.evaluated_depsgraph_get()
    cs = cam.data.clip_start
    ce = cam.data.clip_end
    from bpy_extras.object_utils import world_to_camera_view

    # 用标准 API 获取求值后的相机
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

    def _plane_from_three(a: Vector, b: Vector, c: Vector) -> tuple[Vector, float]:
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

    side_planes: list[tuple[Vector, float]] = []
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
        """用 evaluated object 检测网格顶点是否在视锥体内。"""
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

    # 先收集所有可能物体，判断是否有 MESH 类型
    all_candidates: list[bpy.types.Object] = []
    have_mesh = False
    scene_objects = list(scene.objects)
    _log(f"[_get_visible_objects] 场景中共 {len(scene_objects)} 个物体")

    for obj in scene_objects:
        try:
            if obj.type not in visible_types:
                _log(f"[_get_visible_objects]   跳过 {obj.name} 类型={obj.type}（不在可见类型中）")
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
    removable_list: list[tuple[str, bpy.types.Object]] = []
    passed_aabb = 0
    passed_vertex = 0
    passed_occlusion = 0

    for obj in all_candidates:
        try:
            # 从 depsgraph 获取 evaluated object（标准 API）
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
                    is_occ = _is_occluded(scene, cam_pos, obj, obj_samples, depsgraph)
                    _log(f"[_get_visible_objects]   {obj.name} 遮挡检测结果: occluded={is_occ}")
                    if is_occ:
                        continue
                else:
                    _log(f"[_get_visible_objects]   {obj.name} 采样返回空，视为未遮挡")
            else:
                _log(f"[_get_visible_objects]   {obj.name} 非 MESH 或场景无 MESH，跳过遮挡检测")
            passed_occlusion += 1

            all_names.add(obj.name)
            _log(f"[_get_visible_objects]   {obj.name} 最终判定为 可见 ✅")
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


def _render_and_upload(context, base: str, output_dir: str,
                        visible_names: set[str],
                        affected: set[str], orig_hide: dict[str, bool]) -> None:
    """修改 affected 中物体的 hide_render，渲染并上传，然后恢复。"""
    try:
        for name in affected:
            obj = context.scene.objects.get(name)
            if not obj:
                continue
            obj.hide_render = name not in visible_names

        fname = _render_to_file(context, output_dir)

        names_str = ",".join(sorted(visible_names))
        data = _post_render_output(base, fname, names_str)

        if not data.get("ok"):
            raise RuntimeError(f"服务器返回错误: {data.get('error', 'unknown')}")
        print(f"[zlh] 已上传 outfit={names_str} file={fname}")
    finally:
        for name in affected:
            obj = context.scene.objects.get(name)
            if obj and name in orig_hide:
                obj.hide_render = orig_hide[name]


def _enumerate_effective_combinations(
    context,
    all_visible: set[str],
    removable_names: List[str],
    removable_objs: List[bpy.types.Object],
) -> List[Tuple[int, set[str]]]:
    """枚举所有 removable 组合，对每种组合做遮挡检测，返回去重后的有效组合列表。

    返回 [(mask, actually_visible_names), ...]
    - mask: bitmask，第 i 位表示 removable_names[i] 是否显示
    - actually_visible_names: 在「该组合下」实际通过遮挡检测的物体名
    """
    # 第一步：缓存所有 MESH 物体的采样点
    scene = context.scene
    cam = scene.camera
    depsgraph = context.evaluated_depsgraph_get()
    cam_pos = cam.matrix_world.translation

    _log(f"[_enumerate_effective_combinations] 开始枚举组合，removable 物体数={len(removable_names)}")
    _log(f"[_enumerate_effective_combinations] removable 名称列表: {removable_names}")
    _log(f"[_enumerate_effective_combinations] all_visible 总数={len(all_visible)}: {sorted(all_visible)}")

    sample_cache: Dict[str, List[Vector]] = {}
    cached_count = 0
    for name in all_visible:
        obj = scene.objects.get(name)
        if obj and obj.type == "MESH":
            samples = _cache_mesh_samples(obj, depsgraph, 50)
            if samples:
                sample_cache[name] = samples
                cached_count += 1
    _log(f"[_enumerate_effective_combinations] 采样缓存完成：共缓存 {cached_count} 个 MESH 物体")

    # 记住原始 hide_render 状态
    orig_hide: Dict[str, bool] = {}
    for name in removable_names:
        obj = scene.objects.get(name)
        if obj:
            orig_hide[name] = obj.hide_render

    n = len(removable_names)
    total_combinations = 1 << n
    _log(f"[_enumerate_effective_combinations] 理论组合数: {total_combinations}")

    seen_signatures: set[tuple[str, ...]] = set()
    effective: List[Tuple[int, set[str]]] = []

    try:
        for mask in range(total_combinations):
            # 构建当前组合的 hidden_set：不在此组合中的 removable 物体
            current_hidden: set[str] = set()
            for i in range(n):
                if not (mask >> i) & 1:
                    current_hidden.add(removable_names[i])

            # 设置 hide_render 模拟该组合
            for name in removable_names:
                obj = scene.objects.get(name)
                if obj:
                    obj.hide_render = name not in current_hidden

            # 强制更新 depsgraph（让 hide_render 生效）
            context.view_layer.update()
            depsgraph = context.evaluated_depsgraph_get()

            # 对该组合重新做遮挡检测
            _log(f"[_enumerate_effective_combinations] 组合 mask={mask}（隐藏={current_hidden if current_hidden else '无'}）开始检测")
            vis, _ = _get_visible_objects(
                context,
                hidden_set=current_hidden,
                sample_cache=sample_cache,
            )
            _log(f"[_enumerate_effective_combinations] 组合 mask={mask} 可见物体数={len(vis)}")

            # 生成签名用于去重
            sig = tuple(sorted(vis))
            if sig not in seen_signatures:
                seen_signatures.add(sig)
                effective.append((mask, vis))
                _log(f"[_enumerate_effective_combinations] 组合 mask={mask} 新增有效组合，可见={sorted(vis)}")
            else:
                _log(f"[_enumerate_effective_combinations] 组合 mask={mask} 重复，跳过")

        _log(f"[_enumerate_effective_combinations] 枚举完成：有效组合数={len(effective)}")

    finally:
        # 恢复原始 hide_render 状态
        for name in removable_names:
            obj = scene.objects.get(name)
            if obj and name in orig_hide:
                obj.hide_render = orig_hide[name]
        context.view_layer.update()

    return effective


class ZLH_OT_RenderUpload(Operator):
    """渲染并上传，带模式选择：单张 / 全部组合 / 随机 N 种"""
    bl_idname = "zlh.render_upload"
    bl_label = "zlh: 渲染上传"
    bl_options = {"REGISTER"}

    _render_lock = threading.Lock()

    mode: bpy.props.EnumProperty(
        name="模式",
        items=[
            ("SINGLE", "单张", "仅渲染一张当前视角的图片", 0),
            ("ALL", "全部组合", "渲染所有实际有效的组合", 1),
            ("RANDOM", "随机", "随机选 N 种有效组合", 2),
        ],
        default="ALL",
    )
    random_count: bpy.props.IntProperty(
        name="随机数量 N",
        description="随机模式的渲染张数",
        default=4,
        min=1,
        max=128,
    )

    # 实例属性：在 invoke 中预计算，execute 中使用
    _precomputed: List[Tuple[int, set[str]]] = []
    _removable_names: List[str] = []
    _all_effective: List[Tuple[int, set[str]]] = []

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.camera is not None

    def invoke(self, context, _event):
        scene = context.scene
        if scene.camera is None:
            _log("[invoke] 场景中没有激活相机")
            self.report({"ERROR"}, "场景中没有激活相机")
            return {"CANCELLED"}
        base = _normalize_base(_prefs(context).api_base)
        if not base.startswith(("http://", "https://")):
            _log(f"[invoke] API 根地址无效: {base}")
            self.report({"ERROR"}, "API 根地址需以 http:// 或 https:// 开头")
            return {"CANCELLED"}

        _log("[invoke] ===== 开始 _get_visible_objects 第一次检测 =====")
        # 第 1 步：获取所有在视锥体内的物体
        all_visible, removable_list = _get_visible_objects(context)
        _log(f"[invoke] _get_visible_objects 返回: visible={len(all_visible)} 个, removable={len(removable_list)} 个")
        if not all_visible:
            _log("[invoke] ★★★ 相机视锥体内没有可见物体 ★★★")
            self.report({"WARNING"}, "相机视锥体内没有可见物体")
            return {"CANCELLED"}

        self._removable_names = [name for name, _ in removable_list]
        _log(f"[invoke] visible 物体: {sorted(all_visible)}")
        _log(f"[invoke] removable 物体: {self._removable_names}")

        if not self._removable_names:
            _log("[invoke] 无 removable 物体，直接单张渲染")
            self.mode = "SINGLE"
            self._precomputed = [(0, all_visible)]
            return self.execute(context)

        # 第 2 步：枚举所有组合，遮挡检测 + 去重
        _log("[invoke] ===== 开始 _enumerate_effective_combinations 组合枚举 =====")
        self.report({"INFO"}, "正在分析遮挡关系，计算有效组合…")
        removable_objs = [obj for _, obj in removable_list]
        try:
            effective = _enumerate_effective_combinations(
                context, all_visible, self._removable_names, removable_objs,
            )
            _log(f"[invoke] _enumerate_effective_combinations 返回 {len(effective)} 种有效组合")
        except Exception as e:
            _log(f"[invoke] 遮挡分析异常: {e}")
            import traceback
            _log(f"[invoke] traceback: {traceback.format_exc()}")
            self.report({"ERROR"}, f"遮挡分析失败: {e}")
            return {"CANCELLED"}

        self._all_effective = effective
        self._precomputed = list(effective)

        if len(effective) == 0:
            _log("[invoke] ★★★ 所有组合的遮挡分析结果为空 ★★★")
            self.report({"WARNING"}, "所有组合的遮挡分析结果为空，请检查场景")
            return {"CANCELLED"}

        if len(effective) == 1:
            _log("[invoke] 只有 1 种有效组合，直接渲染")
            return self.execute(context)

        _log(f"[invoke] 弹出选择对话框: {len(effective)} 种有效组合")
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        effective = getattr(self, "_all_effective", [])
        removable_names = getattr(self, "_removable_names", [])
        n = len(removable_names)
        total_effective = len(effective)

        box_info = layout.box()
        box_info.label(text=f"removable 物体: {n} 个  |  理论组合: {1 << n} 种", icon="OBJECT_DATA")
        box_info.label(text=f"遮挡分析后实际有效: {total_effective} 种", icon="RENDER_STILL")

        layout.prop(self, "mode", expand=True)
        if self.mode == "RANDOM":
            row = layout.row()
            row.prop(self, "random_count")
            count = min(self.random_count, total_effective)
        elif self.mode == "ALL":
            count = total_effective
        else:
            count = 1

        box = layout.box()
        box.label(text=f"即将渲染 {count} 张，有效组合预览:", icon="INFO")
        for idx, (mask, vis_names) in enumerate(effective):
            parts = []
            for i, rname in enumerate(removable_names):
                shown = (mask >> i) & 1
                parts.append(f"✅{rname}" if shown else f"❌{rname}")
            tag = ",".join(sorted(vis_names))
            box.label(text=f"  #{idx+1}: {' | '.join(parts)}")
            box.label(text=f"         tag: {tag}")

        box.label(text="确认后将开始渲染，是否继续？", icon="QUESTION")

    def execute(self, context):
        _log("[execute] 开始执行渲染流程")
        if not ZLH_OT_RenderUpload._render_lock.acquire(blocking=False):
            self.report({"WARNING"}, "渲染已在执行中，请等待完成")
            return {"CANCELLED"}

        scene = context.scene
        if scene.camera is None:
            self.report({"ERROR"}, "场景中没有激活相机")
            ZLH_OT_RenderUpload._render_lock.release()
            return {"CANCELLED"}

        base = _normalize_base(_prefs(context).api_base)
        if not base.startswith(("http://", "https://")):
            self.report({"ERROR"}, "API 根地址需以 http:// 或 https:// 开头")
            ZLH_OT_RenderUpload._render_lock.release()
            return {"CANCELLED"}

        output_dir = _prefs(context).output_dir.strip()
        if not output_dir:
            self.report({"ERROR"}, "请先在偏好设置中配置输出目录（指向 WSL 共享目录）")
            ZLH_OT_RenderUpload._render_lock.release()
            return {"CANCELLED"}

        try:
            effective = self._precomputed
            removable_names = self._removable_names

            if not effective:
                self.report({"ERROR"}, "未找到有效组合（请重新按 Ctrl+Shift+B）")
                return {"CANCELLED"}

            if self.mode == "SINGLE":
                masks_to_render = [effective[0]]
            elif self.mode == "ALL":
                masks_to_render = list(effective)
            elif self.mode == "RANDOM":
                count = min(self.random_count, len(effective))
                masks_to_render = random.sample(effective, count)
            else:
                masks_to_render = []

            _log(f"[execute] mode={self.mode} 需渲染 {len(masks_to_render)} 张")

            # 收集所有可能被隐藏/恢复的 removable 物体
            affected: set[str] = set()
            for _, vis_names in masks_to_render:
                affected.update(vis_names)
            affected &= set(removable_names)

            orig_hide = {}
            for name in affected:
                obj = scene.objects.get(name)
                if obj:
                    orig_hide[name] = obj.hide_render

            wm = context.window_manager
            total = len(masks_to_render)
            wm.progress_begin(0, total)

            uploaded = 0
            errors = []
            for idx, (_mask, vis_names) in enumerate(masks_to_render):
                wm.progress_update(idx)
                self.report({"INFO"}, f"渲染中… {idx + 1}/{total}")
                _log(f"[execute] 渲染 {idx + 1}/{total} vis_names={sorted(vis_names)}")

                try:
                    _render_and_upload(context, base, output_dir, vis_names, affected, orig_hide)
                    uploaded += 1
                    _log(f"[execute] 渲染上传成功 {idx + 1}/{total}")
                except urllib.error.HTTPError as e:
                    err_text = ""
                    try:
                        err_text = e.read().decode("utf-8", errors="replace")[:200]
                    except Exception:
                        pass
                    msg = f"第 {idx + 1}/{total} 上传失败 HTTP {e.code} {err_text}"
                    _log(f"[execute] 错误: {msg}")
                    errors.append(msg)
                    self.report({"WARNING"}, msg)
                except urllib.error.URLError as e:
                    msg = f"第 {idx + 1}/{total} 网络错误: {e.reason}"
                    _log(f"[execute] 错误: {msg}")
                    errors.append(msg)
                    self.report({"WARNING"}, msg)
                except Exception as e:
                    msg = f"第 {idx + 1}/{total} 错误: {e}"
                    _log(f"[execute] 错误: {msg}")
                    import traceback
                    _log(f"[execute] traceback: {traceback.format_exc()}")
                    errors.append(msg)
                    self.report({"WARNING"}, msg)

            wm.progress_end()

            if errors:
                _log(f"[execute] 完成：成功 {uploaded}/{total}，{len(errors)} 个错误")
                self.report({"WARNING"}, f"上传完成：成功 {uploaded}/{total}，{len(errors)} 个错误")
            else:
                _log(f"[execute] 全部完成：共 {uploaded} 张图片")
                self.report({"INFO"}, f"全部上传完成：共 {uploaded} 张图片")
            return {"FINISHED"}
        finally:
            ZLH_OT_RenderUpload._render_lock.release()


class ZLH_OT_CheckUpdate(Operator):
    """检查 zlh 插件是否有新版本"""
    bl_idname = "zlh.check_update"
    bl_label = "检查更新"
    bl_options = {"REGISTER"}

    _do_update: bpy.props.BoolProperty(default=False)

    def invoke(self, context, _event):
        import urllib.request
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/Zlh23/zlhNode/releases/latest",
                headers={"Accept": "application/json", "User-Agent": "zlh-blender-addon"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            tag = data.get("tag_name", "")
            if not tag:
                self.report({"ERROR"}, "无法获取最新版本号")
                return {"CANCELLED"}

            ver_str = tag.lstrip("v")
            parts = ver_str.split(".")
            latest_ver = tuple(int(p) for p in parts if p.isdigit())

            current_ver = bl_info["version"]

            if latest_ver > current_ver:
                assets = data.get("assets", [])
                download_url = ""
                for asset in assets:
                    if asset.get("name") == "zlh_dataset_upload.zip":
                        download_url = asset.get("browser_download_url", "")
                        break
                self.new_tag = tag
                self.download_url = download_url if download_url else data.get("zipball_url", "")
                self.html_url = data.get("html_url", "")
                return context.window_manager.invoke_props_dialog(self, width=450)
            else:
                self.report({"INFO"}, f"当前已是最新版本 {'.'.join(str(v) for v in current_ver)}")
                return {"FINISHED"}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.report({"WARNING"}, "GitHub 仓库未找到 Release，请手动检查")
            else:
                self.report({"ERROR"}, f"检查更新失败 HTTP {e.code}")
            return {"CANCELLED"}
        except urllib.error.URLError:
            self.report({"ERROR"}, "网络连接失败，请检查网络")
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"检查更新失败: {e}")
            return {"CANCELLED"}

    def draw(self, _context):
        layout = self.layout
        current = ".".join(str(v) for v in bl_info["version"])
        layout.label(text=f"发现新版本 {self.new_tag}（当前 {current}）")
        layout.label(text="是否下载并自动安装更新？")
        layout.separator()
        row = layout.row()
        row.operator("wm.url_open", text="手动下载", icon="URL").url = self.html_url

    def execute(self, context):
        import urllib.request
        import tempfile
        import shutil

        current = ".".join(str(v) for v in bl_info["version"])
        self.report({"INFO"}, f"正在下载 {self.new_tag} …")
        try:
            req = urllib.request.Request(
                self.download_url,
                headers={"User-Agent": "zlh-blender-addon"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                zip_data = resp.read()
        except Exception as e:
            self.report({"ERROR"}, f"下载失败: {e}")
            return {"CANCELLED"}

        self.report({"INFO"}, "正在安装 …")
        try:
            tmp_dir = tempfile.mkdtemp(prefix="zlh_update_")
            zip_path = os.path.join(tmp_dir, "zlh_dataset_upload.zip")
            with open(zip_path, "wb") as f:
                f.write(zip_data)

            bpy.ops.preferences.addon_install(overwrite=True, filepath=zip_path)

            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            self.report({"ERROR"}, f"安装失败: {e}")
            return {"CANCELLED"}

        bpy.ops.preferences.addon_refresh()
        self.report({"INFO"}, f"已安装 {self.new_tag}，请前往 编辑→偏好设置→插件 搜索「zlh」并勾选启用")
        return {"FINISHED"}


classes = (ZLH_AddonPreferences, ZLH_OT_SetObjectNames, ZLH_OT_RenderUpload, ZLH_OT_CheckUpdate)

addon_keymaps: list[tuple] = []


def register():
    for c in classes:
        bpy.utils.register_class(c)

    _register_object_removable()

    # 注册场景属性（保留但不再用于自动填充，仅用于兼容旧版本）
    bpy.types.Scene.zlh_render_object_names = StringProperty(
        name="zlh 渲染物体名字（已弃用）",
        description="保留兼容，不再使用；渲染时自动从视锥体计算",
        default="",
    )

    try:
        wm = bpy.context.window_manager
    except AttributeError:
        wm = None
    if wm:
        kc = wm.keyconfigs.addon
        if kc:
            km = kc.keymaps.new(name="Window", space_type="EMPTY", region_type="WINDOW")
            kmi = km.keymap_items.new(ZLH_OT_RenderUpload.bl_idname, "B", "PRESS", ctrl=True, shift=True)
            addon_keymaps.append((km, kmi))

            km2 = kc.keymaps.new(name="Window", space_type="EMPTY", region_type="WINDOW")
            kmi2 = km2.keymap_items.new(ZLH_OT_SetObjectNames.bl_idname, "O", "PRESS", ctrl=True, shift=True)
            addon_keymaps.append((km2, kmi2))


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    for c in reversed(classes):
        bpy.utils.unregister_class(c)

    _unregister_object_removable()
    try:
        del bpy.types.Scene.zlh_render_object_names
    except AttributeError:
        pass
