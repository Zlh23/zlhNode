"""通过 Windows Python 打包 Blender 插件 zip。"""
import os, zipfile, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_DIR = os.path.join(ROOT, "web", "blender_addon", "zlh_dataset_upload")
ZIP_PATH = os.path.join(ROOT, "web", "blender_addon", "zlh_dataset_upload.zip")

print(f"ROOT={ROOT}")
print(f"ADDON_DIR={ADDON_DIR}")
print(f"ADDON_EXISTS={os.path.isdir(ADDON_DIR)}")

if not os.path.isdir(ADDON_DIR):
    print("ERROR: addon dir not found")
    sys.exit(1)

with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
    for dp, _, names in os.walk(ADDON_DIR):
        for name in names:
            full = os.path.join(dp, name)
            arc = os.path.relpath(full, os.path.dirname(ADDON_DIR))
            zf.write(full, arc)
            print(f"  added: {arc}")

sz = os.path.getsize(ZIP_PATH)
print(f"DONE: {ZIP_PATH} ({sz} bytes)")
