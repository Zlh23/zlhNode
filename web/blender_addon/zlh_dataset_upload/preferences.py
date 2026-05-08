"""插件偏好设置。"""

import bpy
from bpy.props import StringProperty
from bpy.types import AddonPreferences

from . import ADDON_ID, VERSION_STR


def _prefs(context):
    return context.preferences.addons[ADDON_ID].preferences


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
        box.label(text="Ctrl+Shift+Q：GPU 加速遮挡分析（Eevee 深度图推断遮挡关系）", icon="RENDER_STILL")
        box.label(text="若快捷键冲突，请手动在上述键位映射中改为其它按键", icon="ERROR")
        box.separator()
        row = box.row()
        row.operator("zlh.check_update", text="检查更新", icon="URL")
        row.label(text=f"当前版本: {VERSION_STR}")


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
