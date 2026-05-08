"""zlh 数据集渲染上传插件入口。

渲染当前激活相机视图，过滤视锥体内物体名字后提交到网页格子。
支持 removable 标签的遮挡分析组合枚举。
"""

import datetime

import bpy
from bpy.props import StringProperty


def _log(msg: str):
    """带时间戳的日志，方便在 Blender 控制台追踪各步骤。"""
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[zlh][{ts}] {msg}")


bl_info = {
    "name": "zlh 数据集渲染上传",
    "author": "zlhNode",
    "version": (0, 0, 110),
    "blender": (5, 1, 0),
    "location": "快捷键（默认 Ctrl+Shift+B / Ctrl+Shift+O / Ctrl+Shift+Q）",
    "description": "渲染当前相机、修改物体名称（自动过滤视锥体内物体）、GPU 加速遮挡分析",
    "category": "Render",
    "tracker_url": "https://github.com/Zlh23/zlhNode/releases",
}

ADDON_ID = "zlh_dataset_upload"
VERSION_STR = ".".join(str(v) for v in bl_info["version"])


# 延迟导入，确保 addon 注册后才加载各模块
def _load_modules():
    from . import http_util            # noqa: F401
    from . import occlusion            # noqa: F401
    from . import render_ops           # noqa: F401
    from . import preferences          # noqa: F401
    from . import operators            # noqa: F401
    from . import gpu_occlusion        # noqa: F401


REGISTER_CLASSES = []


def _collect_classes():
    """收集所有需要注册的 Blender 类。放在函数内延迟执行，避免模块加载时冲突。"""
    from .preferences import ZLH_AddonPreferences
    from .operators import ZLH_OT_SetObjectNames, ZLH_OT_RenderUpload, ZLH_OT_CheckUpdate
    from .gpu_occlusion import ZLH_OT_GPUOcclusionAnalysis
    return (
        ZLH_AddonPreferences,
        ZLH_OT_SetObjectNames,
        ZLH_OT_RenderUpload,
        ZLH_OT_CheckUpdate,
        ZLH_OT_GPUOcclusionAnalysis,
    )


addon_keymaps: list[tuple] = []


def register():
    _load_modules()

    classes = _collect_classes()
    for c in classes:
        bpy.utils.register_class(c)

    from .preferences import _register_object_removable
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
            kmi = km.keymap_items.new("zlh.render_upload", "B", "PRESS", ctrl=True, shift=True)
            addon_keymaps.append((km, kmi))

            km2 = kc.keymaps.new(name="Window", space_type="EMPTY", region_type="WINDOW")
            kmi2 = km2.keymap_items.new("zlh.set_object_names", "O", "PRESS", ctrl=True, shift=True)
            addon_keymaps.append((km2, kmi2))

            km3 = kc.keymaps.new(name="Window", space_type="EMPTY", region_type="WINDOW")
            kmi3 = km3.keymap_items.new("zlh.gpu_occlusion_analysis", "Q", "PRESS", ctrl=True, shift=True)
            addon_keymaps.append((km3, kmi3))


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    classes = _collect_classes()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

    from .preferences import _unregister_object_removable
    _unregister_object_removable()
    try:
        del bpy.types.Scene.zlh_render_object_names
    except AttributeError:
        pass
