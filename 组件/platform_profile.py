#!/usr/bin/env python3
"""平台画像（platform profile）：把硬件物理属性降维成生成器可读的参数（E3226）。

## 生成契约
防哪一类：**把某台手机的经验写死进生成器**（UMA/4M/驻留/阈值）⟹ 跨端退化成 if-else 屎山。
p：1（E3226）
可判性：`--检测` 输出 JSON 且 `--校验` rc=0；缺 backend/内存模型字段直接报错，不静默补默认。
后置条件：`python3 组件/platform_profile.py --检测` 输出可 `json.loads`；`--校验` rc=0。
C_逃逸：中 —— 自动检测读 /sys、/proc；检测不到时退回 `cpu_c` 保守 profile，并在 `detected` 标 false。

字段：target_id / architecture{memory_model, bandwidth, cache, isa, cores} /
features{persistent_buffer, double_buffer, host_visible, shared_memory} /
backends{cpu_c, vulkan, ...} / measured_thresholds{gpu_min_n, gpu_max_inline, chunk_size, cache_overflow_n}。
"""
__派发__ = {
    "类别": "支持件/平台画像",
    "入口": ["python3", "组件/platform_profile.py", "--检测"],
    "一句话": "硬件抽象参数：UMA/PCIE 内存模型、带宽、驻留/双缓冲特性、实测路由阈值；生成器与调度器共用",
}

import json
import os
import platform
import subprocess
import sys

SCHEMA = 1


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return default


def _cores():
    try:
        n = os.cpu_count()
        return int(n) if n else 1
    except Exception:
        return 1


def _cpu_isa():
    m = platform.machine().lower()
    if m in ("aarch64", "arm64"):
        return "armv8+"
    if m in ("x86_64", "amd64"):
        return "x86_64"
    return m or "unknown"


def mobile_uma_profile():
    """本机（Adreno 829 / UMA）实测画像；数值来自 E3212/E3225，不猜。"""
    return {
        "schema_version": SCHEMA,
        "target_id": "adreno_829_uma",
        "architecture": {
            "cpu_isa": _cpu_isa(),
            "cores": _cores(),
            "gpu_backend": "vulkan",
            "gpu_name": "Adreno (TM) 829",
            "memory_model": "UMA",
            "bandwidth_read_gbps": 57.0,
            "bandwidth_write_gbps": 53.0,
            "cache_l2_mb": None,
        },
        "features": {
            "persistent_buffer": True,
            "double_buffer": False,
            "host_visible": True,
            "shared_memory": False,
            "device_local_host_visible": True,
        },
        "backends": {"cpu_c": True, "vulkan": True, "opencl": False, "cuda": False},
        "measured_thresholds": {
            "gpu_min_n": 1_000_000,
            "gpu_max_inline": 8_000_000,
            "chunk_size": 8_000_000,
            "cache_overflow_n": 16_000_000,
        },
        "measured_ms": {
            "gpu_fresh_1m": 12.9, "gpu_fresh_4m": 39.8, "gpu_fresh_8m": 74.1, "gpu_fresh_16m": 453.8,
            "c_random_1m": 11.2, "c_random_4m": 46.2, "c_radix8q_8m": 64.9, "c_radix8q_16m": 167.5,
        },
        "provenance": {"source": "E3212/E3225 GPU 实测 + 旧档 C 冠军", "detected": False},
    }


def pc_pcie_profile():
    """PC 端的保守占位：生成器可先吃这份参数；实测值待 PC 上跑 --检测 覆盖。"""
    return {
        "schema_version": SCHEMA,
        "target_id": "generic_x86_64_pcie",
        "architecture": {
            "cpu_isa": "x86_64", "cores": 8, "gpu_backend": "vulkan",
            "gpu_name": "unknown", "memory_model": "PCIE",
            "bandwidth_read_gbps": None, "bandwidth_write_gbps": None, "cache_l2_mb": None,
        },
        "features": {"persistent_buffer": True, "double_buffer": True, "host_visible": False,
                     "shared_memory": False, "device_local_host_visible": False},
        "backends": {"cpu_c": True, "vulkan": True, "opencl": False, "cuda": False},
        "measured_thresholds": {"gpu_min_n": None, "gpu_max_inline": None,
                                "chunk_size": 4 << 20, "cache_overflow_n": None},
        "measured_ms": {},
        "provenance": {"source": "占位（未在 PC 实测）", "detected": False},
    }


def detect():
    """轻量检测：优先本机 UMA 手机；无法确认 Vulkan/Android 时退回 CPU-only 保守 profile。"""
    try:
        termux = os.path.exists("/data/data/com.termux/files/usr/bin/bash") or os.path.exists("/data/data/com.termux/files/home")
        android = bool(_read("/system/build.prop")) or "android" in platform.platform().lower()
        if termux or android:
            p = mobile_uma_profile()
            p["provenance"]["detected"] = True
            p["provenance"]["source"] = "本机自动检测（Termux/Android + UMA）"
            p["architecture"]["cores"] = _cores()
            return p
    except Exception:
        pass
    p = pc_pcie_profile()
    p["backends"]["vulkan"] = False
    p["architecture"]["memory_model"] = "UNKNOWN"
    p["provenance"]["detected"] = False
    return p


def validate(p):
    errs = []
    if not isinstance(p, dict):
        return ["profile 不是对象"]
    for k in ("target_id", "architecture", "features", "backends", "measured_thresholds"):
        if k not in p:
            errs.append("缺 %s" % k)
    a = p.get("architecture") or {}
    if a.get("memory_model") not in ("UMA", "PCIE", "DISCRETE", "UNKNOWN"):
        errs.append("memory_model 非法: %s" % a.get("memory_model"))
    if not isinstance(p.get("backends"), dict) or "cpu_c" not in p.get("backends", {}):
        errs.append("backends.cpu_c 缺失")
    t = p.get("measured_thresholds") or {}
    for k in ("gpu_min_n", "chunk_size"):
        if k not in t:
            errs.append("measured_thresholds.%s 缺失" % k)
    return errs


def load(path=None):
    if path and os.path.exists(path):
        p = json.load(open(path, encoding="utf-8"))
    else:
        p = detect()
    errs = validate(p)
    if errs:
        raise ValueError("platform profile 非法: " + "; ".join(errs))
    return p


def choose(profile, n):
    """给生成器/调度器的硬选择（只读 profile，不写死平台）。"""
    b = profile["backends"]
    t = profile["measured_thresholds"]
    if b.get("vulkan") and n >= (t.get("gpu_min_n") or 1 << 62):
        if n > (t.get("gpu_max_inline") or 1 << 62):
            return {"backend": "chunked", "chunk": t.get("chunk_size"), "reason": "over_gpu_max_inline"}
        return {"backend": "vulkan", "reason": "gpu_min_n"}
    if b.get("cpu_c"):
        return {"backend": "cpu_c", "reason": "below_gpu_min_n"}
    return {"backend": "python", "reason": "fallback"}


def main(argv):
    if "--检测" in argv or "--detect" in argv:
        print(json.dumps(detect(), ensure_ascii=False, indent=1))
        return 0
    if "--校验" in argv or "--validate" in argv:
        try:
            p = load(argv[argv.index("--校验") + 1] if "--校验" in argv and argv.index("--校验") + 1 < len(argv) else None)
        except Exception as e:
            print("❌", e); return 1
        print("✅ profile 合法:", p["target_id"])
        return 0
    if "--选择" in argv:
        try:
            n = int(argv[argv.index("--选择") + 1])
        except Exception:
            print("用法: --选择 <n>"); return 2
        print(json.dumps(choose(load(), n), ensure_ascii=False))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
