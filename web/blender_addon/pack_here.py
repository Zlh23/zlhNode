import os, zipfile

root = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(root, 'zlh_dataset_upload.zip')
src = os.path.join(root, 'zlh_dataset_upload')

if not os.path.isdir(src):
    raise SystemExit(f"missing {src}")

with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for dp, _, fs in os.walk(src):
        for f in fs:
            full = os.path.join(dp, f)
            arc = os.path.relpath(full, root)
            z.write(full, arc)

size = os.path.getsize(out)
print(f"written {out} ({size} bytes)")
