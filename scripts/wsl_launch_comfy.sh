#!/usr/bin/env bash
# 在 Linux 侧解析 GPU 并启动 ComfyUI，避免 Windows CMD/WSL 嵌套引号导致
# CUDA_VISIBLE_DEVICES 未传入 Python/CUDA。
#
# 用法（由 start_comfy_wsl.bat 调用）：
#   bash wsl_launch_comfy.sh          → CUDA_VISIBLE_DEVICES=1,0
#   bash wsl_launch_comfy.sh 0       → 仅物理 GPU 0
#   bash wsl_launch_comfy.sh 1       → 仅物理 GPU 1
set -eu

# Windows 经常通过 WSL 传入 CUDA_VISIBLE_DEVICES；先清掉再按下面 choice 设置，否则会一直是 1,0
unset CUDA_VISIBLE_DEVICES 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts -> zlhNode -> custom_nodes -> ComfyUI 根目录
COMFY_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
START_SH="$COMFY_ROOT/start.sh"

if [[ ! -f "$START_SH" ]]; then
  echo "找不到 ComfyUI 启动脚本: $START_SH" >&2
  exit 1
fi

# 无参数 = 默认 1,0；有参数且为 0 时必须是「真的传了 0」，不能用 ${1:-} 误判
if [[ $# -eq 0 ]]; then
  choice=""
else
  choice="$1"
fi

case "$choice" in
  "")
    export CUDA_VISIBLE_DEVICES="1,0"
    ;;
  "0")
    export CUDA_VISIBLE_DEVICES="0"
    ;;
  "1")
    export CUDA_VISIBLE_DEVICES="1"
    ;;
  *)
    echo "无效参数: $choice （应为空、0 或 1）" >&2
    exit 1
    ;;
esac

# 避免部分环境里其它变量干扰可见 GPU 集合（按需保留）
unset NVIDIA_VISIBLE_DEVICES 2>/dev/null || true

export CUDA_DEVICE_ORDER=PCI_BUS_ID

echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
echo "ComfyUI: $COMFY_ROOT"
cd "$COMFY_ROOT"

# 用 env 再绑一次，确保子进程（含 python）继承；不要用 login shell
exec env CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES" \
  CUDA_DEVICE_ORDER="$CUDA_DEVICE_ORDER" \
  bash "$START_SH"
