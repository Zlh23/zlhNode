"""渲染当前激活相机视图：过滤视锥体内物体名字后提交到网页格子。"""

bl_info = {
    "name": "zlh 数据集渲染上传",
    "author": "zlhNode",
    "version": (1, 7, 0),
    "blender": (3, 0, 0),
    "location": "快捷键（默认 Ctrl+Shift+B / Ctrl+Shift+O）",
    "description": "渲染当前相机、修改物体名称（自动过滤视锥体内物体）、分配到网页格子",
    "category": "Render",
    "tracker_url": "https://github.com/Zlh23/zlhNode/releases",
}

import json
import mathutils
import os
import random
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

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
        row.label(text="当前版本: 1.7.0")


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



def _get_visible_objects(context) -> tuple[set[str], list[tuple[str, bpy.types.Object]]]:
    """用 camera.view_frame() 构建视锥体 6 个平面做 AABB 粗筛 + 网格顶点精确检测。"""
    scene = context.scene
    cam = scene.camera
    if cam is None:
        return set(), []

    depsgraph = context.evaluated_depsgraph_get()
    cs = cam.data.clip_start
    ce = cam.data.clip_end
    from bpy_extras.object_utils import world_to_camera_view

    # ---- 构建视锥体 6 个平面 ----
    frame = cam.data.view_frame(scene=scene)
    near_corners = [cam.matrix_world @ Vector((p.x, p.y, p.z)) for p in frame]
    cam_pos = cam.matrix_world.translation
    cam_dir = cam.matrix_world.to_quaternion() @ Vector((0, 0, -1))

    def _plane_from_three(a: Vector, b: Vector, c: Vector) -> tuple[Vector, float]:
        n = (b - a).cross(c - a).normalized()
        d = -n.dot(a)
        return n, d

    # 近平面
    n_near, d_near = _plane_from_three(near_corners[0], near_corners[1], near_corners[2])
    if n_near.dot(sum(near_corners, Vector()) / 4 - cam_pos) < 0:
        n_near, d_near = -n_near, -d_near

    # 远平面
    far_corners = [p + cam_dir * (ce - cs) for p in near_corners]
    n_far, d_far = _plane_from_three(far_corners[0], far_corners[2], far_corners[1])
    near_center = sum(near_corners, Vector()) / 4
    if n_far.dot(near_center - (near_center + cam_dir * (ce - cs))) < 0:
        n_far, d_far = -n_far, -d_far

    # 4 个侧平面
    side_planes: list[tuple[Vector, float]] = []
    for i in range(4):
        j = (i + 1) % 4
        n, d = _plane_from_three(cam_pos, near_corners[i], near_corners[j])
        if n.dot(near_center) + d < 0:
            n, d = -n, -d
        side_planes.append((n, d))

    frustum_planes = [*side_planes, (n_near, d_near), (n_far, d_far)]

    # ---- 工具函数 ----
    def _aabb_in_frustum(min_c: Vector, max_c: Vector) -> bool:
        for n, d in frustum_planes:
            px = max_c.x if n.x >= 0 else min_c.x
            py = max_c.y if n.y >= 0 else min_c.y
            pz = max_c.z if n.z >= 0 else min_c.z
            if n.dot(Vector((px, py, pz))) + d < 0:
                return False
        return True

    MAX_VERTICES = 256

    def _has_vertex_in_frustum(obj: bpy.types.Object) -> bool:
        """检测物体的网格顶点是否至少有一个在视锥体内。"""
        mesh: bpy.types.Mesh | None = obj.data
        if mesh is None:
            return False
        # 尝试获取 evaluated mesh（带修改器效果）
        try:
            eval_obj = depsgraph.objects.get(obj.name, None)
            if eval_obj and eval_obj.data:
                mesh = eval_obj.data
        except Exception:
            pass
        verts = mesh.vertices
        total = len(verts)
        if total == 0:
            return False

        # 确定采样步长
        if total <= MAX_VERTICES:
            step = 1
        else:
            step = max(1, total // MAX_VERTICES)

        for i in range(0, total, step):
            v = verts[i]
            world_pos = obj.matrix_world @ v.co
            ndc = world_to_camera_view(scene, cam, world_pos)
            if 0.0 <= ndc.x <= 1.0 and 0.0 <= ndc.y <= 1.0 and cs <= ndc.z <= ce:
                return True
        return False

    # ---- 主循环 ----
    visible_types = {"MESH", "CURVE", "SURFACE", "META", "FONT", "GPENCIL", "ARMATURE", "LATTICE", "EMPTY"}
    all_names: set[str] = set()
    removable_list: list[tuple[str, bpy.types.Object]] = []

    for obj in context.visible_objects:
        try:
            if obj.type not in visible_types:
                continue
            if obj.hide_get() or not obj.visible_get():
                continue

            bbox_local = obj.bound_box
            if not bbox_local:
                continue

            corners_world = [obj.matrix_world @ Vector(p) for p in bbox_local]
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

            # 粗筛：AABB 必须在视锥体内
            if not _aabb_in_frustum(min_c, max_c):
                continue

            # 精确筛：必须有至少一个网格顶点在 NDC 内
            if not _has_vertex_in_frustum(obj):
                continue

            all_names.add(obj.name)
            if getattr(obj, "zlh_removable", False):
                removable_list.append((obj.name, obj))
        except Exception:
            continue

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
            ("ALL", "全部组合", "渲染所有 2^n 种 removable 组合", 1),
            ("RANDOM", "随机", "随机显示 removable 物体，渲染 N 种", 2),
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

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.scene.camera is not None

    def invoke(self, context, _event):
        scene = context.scene
        if scene.camera is None:
            self.report({"ERROR"}, "场景中没有激活相机")
            return {"CANCELLED"}
        base = _normalize_base(_prefs(context).api_base)
        if not base.startswith(("http://", "https://")):
            self.report({"ERROR"}, "API 根地址需以 http:// 或 https:// 开头")
            return {"CANCELLED"}
        # 检测是否有 removable 物体
        all_visible, removable_list = _get_visible_objects(context)
        if not all_visible:
            self.report({"WARNING"}, "相机视锥体内没有可见物体")
            return {"CANCELLED"}
        if not removable_list:
            # 没有 removable 就直接渲染单张，不弹对话框
            self.mode = "SINGLE"
            return self.execute(context)

        # 有 removable，弹对话框（draw 中会显示确认信息）
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        all_visible, removable_list = _get_visible_objects(context)
        removable_names = [name for name, _ in removable_list]
        n = len(removable_names)

        # 模式选择
        layout.prop(self, "mode", expand=True)
        if self.mode == "RANDOM":
            row = layout.row()
            row.prop(self, "random_count")

        # 计算数量
        total_combinations = 1 << n
        if self.mode == "SINGLE":
            total = 1
        elif self.mode == "ALL":
            total = total_combinations
        elif self.mode == "RANDOM":
            total = min(self.random_count, total_combinations)
        else:
            total = 0

        box = layout.box()
        box.label(text=f"即将渲染 {total} 张图片  |  removable 物体: {n}", icon="RENDER_STILL")
        box.label(text="确认后将开始渲染，是否继续？", icon="QUESTION")

    def execute(self, context):
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
            # 1. 获取视锥体内可见物体
            all_visible, removable_list = _get_visible_objects(context)
            removable_names = [name for name, _ in removable_list]

            if not all_visible:
                # 空场景：直接渲染一张空图
                self.report({"WARNING"}, "相机视锥体内没有可见物体，仍将渲染空场景")
                try:
                    fname = _render_to_file(context, output_dir)
                    data = _post_render_output(base, fname, "")
                except Exception as e:
                    self.report({"ERROR"}, str(e))
                    return {"CANCELLED"}
                self.report({"INFO"}, "已上传空场景图片")
                return {"FINISHED"}

            if not removable_names:
                # 没有 removable：渲染一张全图
                self.report({"INFO"}, "无 removable 物体，仅渲染一张全图")
                try:
                    fname = _render_to_file(context, output_dir)
                    names = ",".join(sorted(all_visible))
                    data = _post_render_output(base, fname, names)
                except urllib.error.HTTPError as e:
                    err = ""
                    try:
                        err = e.read().decode("utf-8", errors="replace")[:400]
                    except Exception:
                        pass
                    self.report({"ERROR"}, f"上传失败 HTTP {e.code} {err}")
                    return {"CANCELLED"}
                except urllib.error.URLError as e:
                    self.report({"ERROR"}, f"网络错误: {e.reason}")
                    return {"CANCELLED"}
                except Exception as e:
                    self.report({"ERROR"}, str(e))
                    return {"CANCELLED"}
                if not data.get("ok"):
                    self.report({"ERROR"}, "上传失败：服务器返回异常")
                    return {"CANCELLED"}
                self.report({"INFO"}, "已上传")
                return {"FINISHED"}

            # 有 removable 物体
            n = len(removable_names)
            total_combinations = 1 << n

            if self.mode == "SINGLE":
                masks = [total_combinations - 1]
            elif self.mode == "ALL":
                masks = list(range(total_combinations))
            elif self.mode == "RANDOM":
                count = min(self.random_count, total_combinations)
                masks = random.sample(range(total_combinations), count)
            else:
                masks = []

            removable_set = set(removable_names)
            affected = {name for name in all_visible if name in removable_set}
            orig_hide = {}
            for name in affected:
                obj = scene.objects.get(name)
                if obj:
                    orig_hide[name] = obj.hide_render

            wm = context.window_manager
            total = len(masks)
            wm.progress_begin(0, total)

            uploaded = 0
            errors = []
            for idx, mask in enumerate(masks):
                subset = set(all_visible)
                for i in range(n):
                    if not (mask >> i) & 1:
                        subset.discard(removable_names[i])

                wm.progress_update(idx)
                self.report({"INFO"}, f"渲染中… {idx + 1}/{total}")

                try:
                    _render_and_upload(context, base, output_dir, subset, affected, orig_hide)
                    uploaded += 1
                except urllib.error.HTTPError as e:
                    err_text = ""
                    try:
                        err_text = e.read().decode("utf-8", errors="replace")[:200]
                    except Exception:
                        pass
                    msg = f"第 {idx + 1}/{total} 上传失败 HTTP {e.code} {err_text}"
                    errors.append(msg)
                    self.report({"WARNING"}, msg)
                except urllib.error.URLError as e:
                    msg = f"第 {idx + 1}/{total} 网络错误: {e.reason}"
                    errors.append(msg)
                    self.report({"WARNING"}, msg)
                except Exception as e:
                    msg = f"第 {idx + 1}/{total} 错误: {e}"
                    errors.append(msg)
                    self.report({"WARNING"}, msg)

            wm.progress_end()

            if errors:
                self.report({"WARNING"}, f"上传完成：成功 {uploaded}/{total}，{len(errors)} 个错误")
            else:
                self.report({"INFO"}, f"全部上传完成：共 {uploaded} 张图片")
            return {"FINISHED"}
        finally:
            ZLH_OT_RenderUpload._render_lock.release()


class ZLH_OT_CheckUpdate(Operator):
    """检查 zlh 插件是否有新版本"""
    bl_idname = "zlh.check_update"
    bl_label = "检查更新"
    bl_options = {"REGISTER"}

    def execute(self, context):
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

            # 解析 tag，期望格式 v1.2.3
            ver_str = tag.lstrip("v")
            parts = ver_str.split(".")
            latest_ver = tuple(int(p) for p in parts if p.isdigit())

            current_ver = bl_info["version"]

            if latest_ver > current_ver:
                html_url = data.get("html_url", "")
                self.report(
                    {"INFO"},
                    f"发现新版本 {tag}（当前 {'.'.join(str(v) for v in current_ver)}），"
                    f"请前往 {html_url} 下载更新",
                )
            else:
                self.report({"INFO"}, f"当前已是最新版本 {'.'.join(str(v) for v in current_ver)}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.report({"WARNING"}, "GitHub 仓库未找到 Release，请手动检查")
            else:
                self.report({"ERROR"}, f"检查更新失败 HTTP {e.code}")
        except urllib.error.URLError:
            self.report({"ERROR"}, "网络连接失败，请检查网络")
        except Exception as e:
            self.report({"ERROR"}, f"检查更新失败: {e}")
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
