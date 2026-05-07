"""检查并重建 Blender 插件 zip。"""
import os, zipfile

target = r'\\wsl.localhost\Ubuntu\home\zlh-linux\ComfyUI\custom_nodes\zlhNode\web\blender_addon\zlh_dataset_upload.zip'
src = r'\\wsl.localhost\Ubuntu\home\zlh-linux\ComfyUI\custom_nodes\zlhNode\web\blender_addon\zlh_dataset_upload'

# 检查源文件
for f in os.listdir(src):
    fp = os.path.join(src, f)
    print(f, os.path.getsize(fp))

print("---")

# 重建 zip：Blender 要求 zip 内第一层是文件夹，直接是 addon 名
os.makedirs(os.path.dirname(target), exist_ok=True)
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
    for dp, _, fs in os.walk(src):
        for f in fs:
            full = os.path.join(dp, f)
            # 保持 zlh_dataset_upload/__init__.py 结构
            arc = os.path.relpath(full, os.path.dirname(src))
            z.write(full, arc)

print("zip created:", os.path.getsize(target), "bytes")
print("contents:")
with zipfile.ZipFile(target) as z:
    for info in z.infolist():
        print(f"  {info.filename} ({info.file_size} bytes)")
