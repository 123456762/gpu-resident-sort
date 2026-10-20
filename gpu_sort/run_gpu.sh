#!/data/data/com.termux/files/usr/bin/bash
# run_gpu.sh —— GPU 一键脚本 v3(重建, 2026-09-01)
RESULT=/sdcard/Download/Operit/jacobian/gpu_result.json
# 多候选文件轮换(FUSE 权限不稳定: 写多个, 读任一成功即执行)
for CMDREQ in /sdcard/Download/Operit/jacobian/cmd_request \
              /sdcard/Download/Operit/jacobian/cmd_request2 \
              /sdcard/Download/Operit/jacobian/cmd_request3; do
    if [ -f "$CMDREQ" ]; then
        CMD=$(cat "$CMDREQ")
        rm -f "$CMDREQ"
        OUT=$(eval "$CMD" 2>&1)
        echo "{\"ts\":\"$(date +%Y-%m-%d_%H:%M)\",\"cmd\":\"$CMD\",\"output\":\"$OUT\"}" > "$RESULT"
        exit 0
    fi
done
cd /sdcard/Download/Operit/jacobian/gpu_sort
if [ ! -f $HOME/vk_radix ] || [ vulkan_radix_sort.c -nt $HOME/vk_radix ]; then
    gcc -O2 vulkan_radix_sort.c -L/system/lib64 -lvulkan -o $HOME/vk_radix 2>/dev/null
fi
# shader 编译（缺 spv 时；glslangValidator 在 Termux 宿主）
for _s in count16 scatter16 bitonic; do
    if [ ! -f "${_s}.spv" ] || [ "${_s}.comp" -nt "${_s}.spv" ]; then
        glslangValidator -V "${_s}.comp" -o "${_s}.spv" || { echo "shader 编译失败: ${_s}.comp"; exit 1; }
    fi
done
export LD_LIBRARY_PATH=/system/lib64
if [ "${1:-}" = "--自检" ]; then
    OUT=$("$HOME/vk_radix" 1000 2>&1)
    echo "$OUT"
    echo "$OUT" | grep -q "正确=YES" || { echo "❌ GPU 自检失败"; exit 1; }
    DEV=$(echo "$OUT" | grep '^GPU:' | head -1)
    case "$DEV" in
        *llvmpipe*|*lavapipe*) echo "✅ GPU 自检通过（★软件 Vulkan：$DEV ⟹ 只证正确性，不作 GPU 性能基准）" ;;
        *) echo "✅ GPU 自检通过（设备：$DEV）" ;;
    esac
    exit 0
fi
if [ -n "${2:-}" ]; then
    "$HOME/vk_radix" "${1:-10000000}" "$2" "${3:-}"
else
    "$HOME/vk_radix" "${1:-10000000}"
fi
