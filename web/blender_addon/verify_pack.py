import os, zipfile

root = r'\\wsl.localhost\Ubuntu\home\zlh-linux\ComfyUI\custom_nodes\zlhNode\web\blender_addon'
src = os.path.join(root, 'zlh_dataset_upload')
out = os.path.join(root, 'zlh_dataset_upload.zip')

print("source files:")
for f in os.listdir(src):
    fp = os.path.join(src, f)
    st = os.stat(fp)
    print(f"  {f}: {st.st_size} bytes, mtime={st.st_mtime}")

if os.path.exists(out):
    print(f"\nold zip: {os.path.getsize(out)} bytes, mtime={os.stat(out).st_mtime}")
    os.remove(out)

with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for dp, _, fs in os.walk(src):
        for f in fs:
            full = os.path.join(dp, f)
            arc = os.path.relpath(full, root)
            z.write(full, arc)

st2 = os.stat(out)
print(f"new zip: {st2.st_size} bytes, mtime={st2.st_mtime}")
print("\nzip contents:")
with zipfile.ZipFile(out) as z:
    for info in z.infolist():
        print(f"  {info.filename} ({info.file_size} bytes)")

print("\nbl_info version in __init__.py:")
with open(os.path.join(src, '__init__.py'), encoding='utf-8') as f:
    for line in f:
        if '"version"' in line or "'version'" in line:
            print(f"  {line.strip()}")
