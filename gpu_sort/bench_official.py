#!/usr/bin/env python3


"""GPU 排序 · 官方分区网格 v2 + 规模曲线（E3220）。

分布/规模按 `faithfulness_checks/排序分区与支配_20260918.py::分区网格v2`（随机/有序/逆序/几乎有序/多重复/锯齿 × 1e5/1e6/4e6），
另加 random_full 的规模曲线 1e4..1e7。纪律照 `排序测量装置_20260919.py`：证书门过了才计时、N 遍中位+MAD、
相对 numpy 倍数；GPU 回退 CPU 标出；**不对单次精确数下结论**。
"""

# ## 生成契约
# 防哪一类：**只在随机均匀上优化，遇到真实分区（低基数/窄值域/不同规模）就翻车**
# p：1（E3222）
# 可判性：每格 `cert` 必须 True；GPU 回退 CPU 必须标出；输出 JSON 可复算
# 后置条件：`python3 gpu_sort/bench_official.py` rc=0 且打印「证书通过 N/N」
# C_逃逸：中 —— 只覆盖 uint32；int64/结构化/argsort 未测（T-338）

__派发__ = {
    "类别": "支持件/GPU基准",
    "入口": ["python3", "gpu_sort/bench_official.py"],
    "一句话": "GPU 排序官方分布/规模矩阵：证书门+稳态中位+MAD+回退标记（E3222）",
}

import importlib.util as iu
import json
import os
import random
import statistics
import sys
import time

import numpy as np

S = "/sdcard/Download/Operit/jacobian/gpu_sort/bench_off"
os.makedirs(S, exist_ok=True)
REPS = 3


def _cli():
    sp = iu.spec_from_file_location("_grc", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_resident_client.py"))
    m = iu.module_from_spec(sp); sp.loader.exec_module(m); return m


def grid_v2(n):
    """与官方 `分区网格v2` 同源定义（6 分布；数据全为非负 int，适配 uint32 GPU）。"""
    r = random.Random(11)
    return {
        "随机": np.array([r.randint(0, 10**6) for _ in range(n)], dtype=np.uint32),
        "有序": np.arange(n, dtype=np.uint32),
        "逆序": np.arange(n, 0, -1, dtype=np.uint32),
        "几乎有序": (lambda a: a)(np.arange(n, dtype=np.uint32)),
        "多重复": np.array([r.choice([1, 2, 3, 7]) for _ in range(n)], dtype=np.uint32),
        "锯齿": np.array([(i % 32) * 1000 + (n - i) % 7 for i in range(n)], dtype=np.uint32),
    }


def real_almost(n):
    a = np.arange(n, dtype=np.uint32)
    r = random.Random(12)
    for _ in range(max(1, n // 100)):
        i = r.randrange(n - 1); a[i], a[i + 1] = a[i + 1], a[i]
    return a


def make_case(n, name):
    if name == "随机":
        r = random.Random(11); return np.array([r.randint(0, 10**6) for _ in range(n)], dtype=np.uint32)
    if name == "有序": return np.arange(n, dtype=np.uint32)
    if name == "逆序": return np.arange(n, 0, -1, dtype=np.uint32)
    if name == "几乎有序": return real_almost(n)
    if name == "多重复":
        r = random.Random(13); return np.array([r.choice([1, 2, 3, 7]) for _ in range(n)], dtype=np.uint32)
    if name == "锯齿": return np.array([(i % 32) * 1000 + (n - i) % 7 for i in range(n)], dtype=np.uint32)
    if name == "random_full":
        rng = np.random.default_rng(7 + n); return rng.integers(0, 2**31, n, dtype=np.uint32)
    raise KeyError(name)


def mm(xs):
    m = statistics.median(xs); return m, statistics.median([abs(x - m) for x in xs]) if len(xs) > 1 else 0.0


def ensure(c, n):
    if c.stats().startswith("OK"):
        return True
    try: c.stop()
    except Exception: pass
    return c.start(10_000_000, timeout=20)


def main():
    c = _cli()
    if not c.available(): print("GPU daemon 不可用"); return 2
    c.stop(); print("start", c.start(10_000_000, timeout=30), flush=True)
    cases = [(n, d) for n in (10**5, 10**6, 4 * 10**6) for d in ("随机", "有序", "逆序", "几乎有序", "多重复", "锯齿")]
    cases += [(n, "random_full") for n in (10**4, 10**5, 10**6, 2 * 10**6, 10**7)]
    rows = []
    for n, name in cases:
        arr = make_case(n, name)
        fp = os.path.join(S, f"in_{n}_{name}.bin"); arr.tofile(fp)
        rec = {"n": n, "dist": name}
        try:
            if not ensure(c, n):
                raise RuntimeError("daemon 不可用")
            c.load(fp, n, chunk=8 << 20)
            r0 = c.sort(timeout=20); rec["cold"] = r0
            meds = []
            for _ in range(REPS):
                c.load(fp, n, chunk=8 << 20)   # ★[E3224] 每轮重载：否则第 2/3 轮排的是已排序数据（假快）
                t = time.time()
                r = c.sort(timeout=20)
                try: meds.append(float(r.split("fallback=")[1].split(" ")[1].replace("ms", "")))
                except Exception: meds.append(float(r.split(" ")[3].replace("ms", "")))
            rec["gpu_ms"], rec["gpu_mad"] = mm(meds)
            rec["fallback"] = "fallback=1" in r0
            if n <= 10**6:
                out = os.path.join(S, f"out_{n}_{name}.bin"); c.dump(out)
                got = np.fromfile(out, dtype=np.uint32); rec["cert"] = bool(np.array_equal(got, np.sort(arr)))
            else:
                v = c.verify(timeout=60); rec["verify"] = v
                f = c.fp(timeout=60)
                rec["cert"] = ("sorted=1" in v) and (f"sum={int(arr.sum(dtype=np.uint64))}" in f)
            cms = []
            for _ in range(3):
                t = time.time(); np.sort(arr); cms.append((time.time() - t) * 1e3)
            rec["cpu_ms"], _ = mm(cms)
            rec["ratio"] = rec["cpu_ms"] / rec["gpu_ms"] if rec["gpu_ms"] else None
        except Exception as e:
            rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:100])
            try:
                c.stop(); c.start(10_000_000, timeout=30)
            except Exception as e2:
                rec["restart_error"] = str(e2)[:80]
        rows.append(rec)
        json.dump(rows, open(os.path.join(S, "official_matrix_20261020.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("%8d %-10s cert=%-5s fb=%-5s gpu=%7.2f±%-6.2f cpu=%8.2f ratio=%5.2f %s" % (
            n, name, rec.get("cert"), rec.get("fallback"), rec.get("gpu_ms", 0), rec.get("gpu_mad", 0),
            rec.get("cpu_ms", 0), rec.get("ratio") or 0, rec.get("error", "")[:50]), flush=True)
    c.stop()
    good = [r for r in rows if r.get("cert")]
    print("证书通过 %d/%d" % (len(good), len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
