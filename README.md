# gpu-resident-sort

平台画像驱动的 **GPU 常驻排序**（Vulkan / Adreno UMA）+ **C 代码生成后端** +
跨后端 golden 证书；GitHub Actions 在真实 x86_64 上跑生成/编译/证书。

## Status

- 手机 UMA（Adreno 829 / Vulkan）常驻排序，fresh uint32 输入：
  - 1M: ~7–13 ms
  - 4M: ~25–40 ms
  - 8M: ~52–74 ms（紧凑散射）
- **跨后端证书**：GPU 输出 == 生成 C 输出（1M/4M/8M，逐元素）
- **C 代码生成 MVP**：`radix8`/`radix16` → 单文件 C → `cc -O3` → 与 numpy 证书对账
- **x86_64 CI**：`.github/workflows/pc-codegen.yml`（ubuntu-latest，artifact 留证据）

## x86_64 CI（实测）

GitHub Actions `ubuntu-24.04` 真实 x86_64，生成 C 后编译执行（证书全部 OK）：

| n | radix16 生成 | radix8 生成 |
|---|---|---|
| 1M | **9.63 ms** | 14.96 ms |
| 4M | **31.21 ms** | 57.68 ms |
| 8M | **64.11 ms** | 118.70 ms |

> 注意：PC profile 当前是 `generic_x86_64_pcie · memory_model=UNKNOWN`（未识别独显/PCIe），这些是 **CPU C 生成后端**在真实 x86 上的物理结果，不是 GPU 结果。

## Quick start

```bash
# 1) 平台画像（自动检测 UMA/PCIE；检测不到退回 CPU 保守 profile）
python3 组件/platform_profile.py --检测
python3 组件/platform_profile.py --校验

# 2) C 生成器：生成 → 编译 → 证书（矩阵用临时目录，不依赖手机 /sdcard）
CCG_SIZES=1000000,4000000,8000000 python3 组件/c_codegen_sort.py --矩阵

# 3) 只生成 C 源码
python3 组件/c_codegen_sort.py --生成 radix16,radix8 --输出 sort_gen.c

# 4) 交叉编译自检（本机无 x86 工具链时诚实 rc=2，不伪造通过）
python3 组件/c_codegen_sort.py --交叉编译
```

## Architecture

```
platform_profile (UMA/PCIE, 带宽, 阈值)
        │
        ├── Vulkan resident backend（手机 UMA：compact scatter + 桶内变长 bitonic）
        └── C codegen backend（radix8/radix16 → 单文件 C）
                    │
              same golden certificate (ordered + multiset vs np.sort)
```

## Honest boundaries

- 以上毫秒数是**手机 UMA 物理实测**；PC 的绝对跑分等待 x86 CI 补齐（当前只证明编译+证书）。
- **16M fresh 输入仍输给 C radix8q**（~454–687 ms vs 167.5 ms，64 MB 随机写墙）。
- 小数据（<1M）与低基数（dup/锯齿）仍留给 CPU。
- 当前 C 后端只覆盖 `radix8`/`radix16`；`counting`/`run_merge` 与 64-bit 未纳入。
- License: MIT.

## Evidence

- `30_记录与台账/平台画像_20261020.json` — 实测 profile
- `30_记录与台账/排序_GPU官方矩阵_20261020.md` — GPU 矩阵与 C 对照
- `00_真源/GPU定位_20261020.md` — GPU 定位/失败边界记录
