import os, zipfile

# 这个脚本直接双击运行，会在桌面上生成 zip
addon_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zlh_dataset_upload")
desktop = os.path.join(os.path.expanduser("~"), "Desktop")
zip_path = os.path.join(desktop, "zlh_dataset_upload.zip")

with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for dp, _, names in os.walk(addon_dir):
        for name in names:
            full = os.path.join(dp, name)
            arc = os.path.relpath(full, os.path.dirname(addon_dir))
            zf.write(full, arc)

print(f"OK: {zip_path} ({os.path.getsize(zip_path)} bytes)")
input("\n按 Enter 退出...")
