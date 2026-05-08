"""渲染执行相关逻辑。"""

import os
import uuid

import bpy

from . import _log


def _render_to_file(context, output_dir: str) -> str:
    """渲染当前相机视图到 output_dir，返回文件名（不含路径）。"""
    scene = context.scene
    fp_orig = scene.render.filepath
    fmt_orig = scene.render.image_settings.file_format
    scene.render.image_settings.file_format = "PNG"

    os.makedirs(output_dir, exist_ok=True)
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
