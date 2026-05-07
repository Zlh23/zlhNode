"""在 web/blender_addon/ 下生成 zlh_dataset_upload.zip（需 Python3）。"""

from __future__ import annotations

import os
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDON_DIR = os.path.join(_ROOT, "web", "blender_addon", "zlh_dataset_upload")
_ZIP_PATH = os.path.join(_ROOT, "web", "blender_addon", "zlh_dataset_upload.zip")


def main() -> None:
    if not os.path.isdir(_ADDON_DIR):
        print(f"ERROR: missing {_ADDON_DIR}", flush=True)
        sys.exit(1)
    with zipfile.ZipFile(_ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for dp, _, names in os.walk(_ADDON_DIR):
            for name in names:
                full = os.path.join(dp, name)
                arc = os.path.relpath(full, os.path.dirname(_ADDON_DIR))
                zf.write(full, arc)
    print(f"OK: {_ZIP_PATH} ({os.path.getsize(_ZIP_PATH)} bytes)", flush=True)


if __name__ == "__main__":
    main()
