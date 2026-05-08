"""Blender Operator 定义：渲染上传、重命名、检查更新。"""

import json
import os
import random
import shutil
import tempfile
import threading
import urllib.error
import urllib.request

import bpy
from mathutils import Vector
from bpy.props import StringProperty

from . import _log, ADDON_ID, VERSION_STR, bl_info
from .http_util import _normalize_base
from .gpu_occlusion import _run_indexob_detection, _build_frustum_planes, _aabb_in_frustum
from .preferences import _prefs
from .render_ops import _render_to_file


class ZLH_OT_SetObjectNames(bpy.types.Operator):
    """修改选中物体名称"""
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
        box2.label(text="提示：标记为 removable 的物体在渲染时会随机选择可见/隐藏", icon="INFO")

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


class ZLH_OT_RenderUpload(bpy.types.Operator):
    """渲染并上传：随机选择 removable 组合，IndexOB 检测实际可见物体"""
    bl_idname = "zlh.render_upload"
    bl_label = "zlh: 渲染上传"
    bl_options = {"REGISTER"}

    _render_lock = threading.Lock()

    random_count: bpy.props.IntProperty(
        name="随机张数",
        description="随机渲染的张数",
        default=4,
        min=1,
        max=128,
    )

    _removable_names: list[str] = []
    _all_meshes_in_frustum: list = []

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

        # 1. 视锥体检测所有 MESH
        _log("[invoke] 构建视锥体...")
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
            except Exception:
                pass

        _log(f"[invoke] 视锥体内 MESH 数量: {len(all_meshes)}")
        if not all_meshes:
            self.report({"ERROR"}, "场景中无可见 MESH 物体")
            return {"CANCELLED"}

        # 2. 筛选 removable
        removable_names = [
            o.name for o in all_meshes
            if getattr(o, "zlh_removable", False)
        ]

        if not removable_names:
            self.report({"ERROR"}, "没有标记为 removable 的物体（请先用 Ctrl+Shift+O 标记）")
            return {"CANCELLED"}

        _log(f"[invoke] removable 物体: {removable_names}")
        self._removable_names = removable_names
        self._all_meshes_in_frustum = all_meshes

        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        removable_names = getattr(self, "_removable_names", [])
        layout.label(text=f"removable 物体: {len(removable_names)} 个", icon="OBJECT_DATA")
        for name in removable_names:
            layout.label(text=f"  {name}")
        layout.separator()
        layout.prop(self, "random_count")
        box = layout.box()
        box.label(text="流程：随机选择可见子集 → 渲染 →", icon="RENDER_STILL")
        box.label(text="      IndexOB 检测实际出现的物体 → 上传", icon="FILE_TICK")
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
            self.report({"ERROR"}, "请先在偏好设置中配置输出目录")
            ZLH_OT_RenderUpload._render_lock.release()
            return {"CANCELLED"}

        removable_names = self._removable_names
        all_meshes = self._all_meshes_in_frustum
        n_rem = len(removable_names)

        # 生成随机组合（mask: 第 i 位=1 表示 removable_names[i] 显示）
        count = min(self.random_count, 1 << n_rem)
        masks = list(range(1 << n_rem))
        if len(masks) > count:
            masks = random.sample(masks, count)

        _log(f"[execute] 随机 {count} 种组合，masks={masks}")

        wm = context.window_manager
        wm.progress_begin(0, count)

        uploaded = 0
        errors = []
        try:
            for idx, mask in enumerate(masks):
                wm.progress_update(idx)
                self.report({"INFO"}, f"渲染中… {idx + 1}/{count}")
                _log(f"[execute] === 组合 {idx + 1}/{count} mask={mask} ===")

                # 哪些 removable 可见
                visible_rem = set()
                for i in range(n_rem):
                    if (mask >> i) & 1:
                        visible_rem.add(removable_names[i])

                _log(f"[execute]  预定可见: {sorted(visible_rem)}")

                # 隐藏不需要的 removable
                orig_hide = {}
                for name in removable_names:
                    obj = scene.objects.get(name)
                    if obj:
                        orig_hide[name] = obj.hide_render
                        obj.hide_render = name not in visible_rem

                context.view_layer.update()

                # 渲染图片
                fname = None
                try:
                    fname = _render_to_file(context, output_dir)
                    _log(f"[execute]  渲染完成: {fname}")
                except Exception as e:
                    _log(f"[execute]  渲染失败: {e}")
                    errors.append(f"第 {idx + 1}/{count} 渲染失败: {e}")
                    for name in removable_names:
                        obj = scene.objects.get(name)
                        if obj and name in orig_hide:
                            obj.hide_render = orig_hide[name]
                    continue
                finally:
                    for name in removable_names:
                        obj = scene.objects.get(name)
                        if obj and name in orig_hide:
                            obj.hide_render = orig_hide[name]

                # IndexOB 检测实际可见物体
                # 构建需要传给检测函数的物体列表（包括不可移除的 + 预定可见的 removable）
                all_mesh_objs_for_idx = []
                for obj in all_meshes:
                    if obj.name not in removable_names or obj.name in visible_rem:
                        all_mesh_objs_for_idx.append(obj)

                try:
                    idx_result = _run_indexob_detection(context, all_mesh_objs_for_idx)
                    actual_visible = idx_result["visible_objects"]
                    _log(f"[execute]  IndexOB 检测实际可见: {actual_visible}")
                except Exception as e:
                    _log(f"[execute]  IndexOB 检测失败: {e}")
                    import traceback
                    _log(traceback.format_exc())
                    # 降级：用预定的可见物体
                    all_visible = [o.name for o in all_meshes
                                   if o.name not in removable_names or o.name in visible_rem]
                    actual_visible = list(all_visible)

                # 上传
                names_str = ",".join(sorted(actual_visible))
                try:
                    from .http_util import _post_render_output
                    data = _post_render_output(base, fname, names_str)
                    if not data.get("ok"):
                        raise RuntimeError(f"服务器返回错误: {data.get('error', 'unknown')}")
                    uploaded += 1
                    _log(f"[execute]  上传成功: outfit={names_str} file={fname}")
                except urllib.error.HTTPError as e:
                    err_text = ""
                    try:
                        err_text = e.read().decode("utf-8", errors="replace")[:200]
                    except Exception:
                        pass
                    msg = f"第 {idx + 1}/{count} 上传失败 HTTP {e.code} {err_text}"
                    _log(f"[execute]  错误: {msg}")
                    errors.append(msg)
                    self.report({"WARNING"}, msg)
                except urllib.error.URLError as e:
                    msg = f"第 {idx + 1}/{count} 网络错误: {e.reason}"
                    _log(f"[execute]  错误: {msg}")
                    errors.append(msg)
                    self.report({"WARNING"}, msg)
                except Exception as e:
                    msg = f"第 {idx + 1}/{count} 错误: {e}"
                    _log(f"[execute]  错误: {msg}")
                    import traceback
                    _log(traceback.format_exc())
                    errors.append(msg)
                    self.report({"WARNING"}, msg)

            wm.progress_end()

            if errors:
                self.report({"WARNING"}, f"上传完成：成功 {uploaded}/{count}，{len(errors)} 个错误")
            else:
                self.report({"INFO"}, f"全部上传完成：共 {uploaded} 张图片")
            return {"FINISHED"}
        finally:
            ZLH_OT_RenderUpload._render_lock.release()


class ZLH_OT_CheckUpdate(bpy.types.Operator):
    """检查 zlh 插件是否有新版本"""
    bl_idname = "zlh.check_update"
    bl_label = "检查更新"
    bl_options = {"REGISTER"}

    do_update: bpy.props.BoolProperty(default=False)

    def invoke(self, context, _event):
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
                self.report({"INFO"}, f"当前已是最新版本 {VERSION_STR}")
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
        layout.label(text=f"发现新版本 {self.new_tag}（当前 {VERSION_STR}）")
        layout.label(text="是否下载并自动安装更新？")
        layout.separator()
        row = layout.row()
        row.operator("wm.url_open", text="手动下载", icon="URL").url = self.html_url

    def execute(self, context):
        current = VERSION_STR
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
