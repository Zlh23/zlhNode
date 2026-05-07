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
from bpy.props import StringProperty

from . import _log, ADDON_ID, VERSION_STR, bl_info
from .http_util import _normalize_base
from .occlusion import _get_visible_objects
from .preferences import _prefs
from .render_ops import _enumerate_effective_combinations, _render_and_upload


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


class ZLH_OT_RenderUpload(bpy.types.Operator):
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

    _precomputed: list[tuple[int, set[str]]] = []
    _removable_names: list[str] = []
    _all_effective: list[tuple[int, set[str]]] = []

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

        _log("[invoke] ===== 开始 _get_visible_objects 第一次检测 =====")
        all_visible, removable_list = _get_visible_objects(context)
        _log(f"[invoke] _get_visible_objects 返回: visible={len(all_visible)} 个, removable={len(removable_list)} 个")
        if not all_visible:
            self.report({"WARNING"}, "相机视锥体内没有可见物体")
            return {"CANCELLED"}

        self._removable_names = [name for name, _ in removable_list]
        _log(f"[invoke] visible 物体: {sorted(all_visible)}")
        _log(f"[invoke] removable 物体: {self._removable_names}")

        if not self._removable_names:
            self.mode = "SINGLE"
            self._precomputed = [(0, all_visible)]
            return self.execute(context)

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

        if not effective:
            self.report({"WARNING"}, "所有组合的遮挡分析结果为空，请检查场景")
            return {"CANCELLED"}

        if len(effective) == 1:
            return self.execute(context)

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
        box.label(text=f"即将渲染 {count} 张，各 tag 出现频次:", icon="INFO")

        # 统计每种 tag 在多少种有效组合中出现
        from collections import Counter
        tag_counter: Counter[str] = Counter()
        for _mask, vis_names in effective:
            tag = ",".join(sorted(vis_names))
            tag_counter[tag] += 1
        for tag, freq in tag_counter.most_common():
            box.label(text=f"  {tag} — {freq} 种")

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
                self.report({"WARNING"}, f"上传完成：成功 {uploaded}/{total}，{len(errors)} 个错误")
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

    _do_update: bpy.props.BoolProperty(default=False)

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
