#!/usr/bin/env python3
"""打包 Blender 插件 zip - 版本 1.6.0。"""
import os, zipfile, sys

ROOT = "/home/zlh-linux/ComfyUI/custom_nodes/zlhNode"
ADDON_DIR = os.path.join(ROOT, "web", "blender_addon", "zlh_dataset_upload")
ZIP_PATH = os.path.join(ROOT, "web", "blender_addon", "zlh_dataset_upload.zip")

LOG = os.path.join(ROOT, "scripts", "_pack_result.txt")
with open(LOG, "w") as f:
    f.write(f"ROOT={ROOT}\n")
    f.write(f"ADDON_DIR={ADDON_DIR}\n")
    f.write(f"ADDON_EXISTS={os.path.isdir(ADDON_DIR)}\n")

if not os.path.isdir(ADDON_DIR):
    with open(LOG, "a") as f:
        f.write("ERROR: addon dir not found\n")
    sys.exit(1)

with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
    for dp, _, names in os.walk(ADDON_DIR):
        for name in names:
            full = os.path.join(dp, name)
            arc = os.path.relpath(full, os.path.dirname(ADDON_DIR))
            zf.write(full, arc)
            with open(LOG, "a") as f:
                f.write(f"  added: {arc}\n")

sz = os.path.getsize(ZIP_PATH)
with open(LOG, "a") as f:
    f.write(f"DONE: {ZIP_PATH} ({sz} bytes)\n")
