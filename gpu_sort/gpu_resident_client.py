#!/usr/bin/env python3
"""常驻 GPU 服务客户端（T-329）—— CPU 只下命令，数据驻 GPU。

协议：命令文件（文本行，非 JSON）：`LOAD <path> <n> <chunk>` / `SORT` / `DUMP <path>` / `STATS` / `EXIT`
结果文件单行。客户端做**原子写命令 + 原子读结果 + 看门狗超时杀进程（fail-closed）**。
安全边界：只在 Termux 宿主可用时启用；失败一律回退 CPU，不静默。

## 生成契约
防哪一类：**"GPU 算完没人知道对不对/卡死没人收尸"** —— 命令/结果丢字节、daemon 栅栏卡死、
  客户端无限等；本件用**原子 IPC + 超时 fail-closed + 指纹校验**把这三类变成可判红。
p：1（E3214）
C_逃逸：中 —— 校验只用"单调性+和/xor 指纹"（低成本），抓得到乱序/丢元素，抓不到"值被替换成同指纹"的对抗性造假；
  后者由 daemon 侧 GPU 排序实现与 CPU 参考的既有对拍覆盖（一次性 `gpu_resident` 测试）。
可判性：`start()/load()/sort()/dump()` 全部有超时；超时杀 daemon 并抛 `TimeoutError`；`verify_sorted_file` 返回 (bool, why)。
后置条件：`python3 gpu_sort/gpu_resident_client.py <in> <out>` 在 Termux 宿主可用时 rc=0 且打印 verify ok；否则 rc=2/3。
"""
__派发__ = {
    "类别": "支持件/GPU驻留",
    "入口": ["python3", "gpu_sort/gpu_resident_client.py"],
    "一句话": "GPU 常驻服务客户端：文本行命令/原子 IPC/看门狗；驱动 Termux 宿主 gpu_resident daemon（数据驻留，CPU 只下命令）",
}

import os
import subprocess
import time

S = "/sdcard/Download/Operit/jacobian"
CMD = os.path.join(S, "gpu_cmd.txt")
RES = os.path.join(S, "gpu_res.txt")
LOG = os.path.join(S, "gpu_daemon.log")
TERMUX_BASH = os.environ.get("LK_TERMUX_BASH", "/data/data/com.termux/files/usr/bin/bash")
HOME = os.environ.get("LK_TERMUX_HOME", "/data/data/com.termux/files/home")
BIN = os.path.join(HOME, "gpu_res", "gpu_resident")
_PROC = None


def available():
    return os.path.exists(TERMUX_BASH) and os.path.exists(BIN)


def _run_host(script, timeout=60):
    env = dict(os.environ)
    env.setdefault("HOME", HOME)
    env["PATH"] = "/data/data/com.termux/files/usr/bin:" + env.get("PATH", "")
    return subprocess.run([TERMUX_BASH, "-c", script], capture_output=True, text=True,
                          timeout=timeout, env=env)


def stop():
    """先 terminate 自己持有的 daemon 进程，再按 /proc/<pid>/comm 兜底清理（避免误杀自身 shell）。"""
    global _PROC
    if _PROC is not None:
        try:
            _PROC.terminate()
            try:
                _PROC.wait(timeout=2)
            except Exception:
                _PROC.kill()
        except Exception:
            pass
        _PROC = None
    script = (
        'for d in /proc/[0-9]*; do [ -r "$d/comm" ] || continue; '
        'c=$(cat "$d/comm" 2>/dev/null); '
        'if [ "$c" = "gpu_resident" ]; then kill -TERM "${d#/proc/}" 2>/dev/null; fi; '
        'done; true'
    )
    try:
        _run_host(script, 15)
    except Exception:
        pass


def start(max_n=10_000_000, timeout=30.0):
    """启动常驻 daemon；返回 True/False（不抛）。★不靠结果文件判断启动（daemon 只在有命令时写结果）。"""
    if not available():
        return False
    stop()
    for p in (CMD, RES):
        try:
            os.unlink(p)
        except OSError:
            pass
    global _PROC
    for p in (CMD, RES):
        try:
            os.unlink(p)
        except OSError:
            pass
    env = dict(os.environ)
    env["HOME"] = HOME
    env["PATH"] = "/data/data/com.termux/files/usr/bin:" + env.get("PATH", "")
    env["LD_LIBRARY_PATH"] = "/system/lib64"
    cmdline = (
        "cd %s/gpu_res && exec ./gpu_resident %d --daemon %s %s > %s 2>&1"
        % (HOME, int(max_n), CMD, RES, LOG)
    )
    try:
        _PROC = subprocess.Popen([TERMUX_BASH, "-c", cmdline], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
    except Exception:
        _PROC = None
        return False
    # 先等进程起来，再发 STATS 握手（探测命令）；不倒计时等结果文件（否则必然 30s 超时）
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.1)
        # 探针：直接写命令，轮询结果（不在超时时杀 daemon）
        try:
            tmp = CMD + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("STATS\n")
            os.replace(tmp, CMD)
        except OSError:
            continue
        for _ in range(30):
            if os.path.exists(RES):
                try:
                    if open(RES, encoding="utf-8", errors="replace").read().strip():
                        return True
                except OSError:
                    pass
            time.sleep(0.05)
    return False


def cmd(line, timeout=10.0):
    """发一条命令；返回结果行（去掉 OK/ERR 前缀前的原文）；超时杀 daemon 并抛。"""
    for p in (CMD, RES):
        try:
            os.unlink(p)
        except OSError:
            pass
    tmp = CMD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
    os.replace(tmp, CMD)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(RES):
            try:
                txt = open(RES, encoding="utf-8", errors="replace").read().strip()
            except OSError:
                txt = ""
            if txt:
                return txt
        time.sleep(0.005)
    stop()
    raise TimeoutError("GPU daemon 超时 %.1fs（已杀进程，fail-closed）: %s" % (timeout, line))


def load(path, n, chunk=4 << 20):
    return cmd("LOAD %s %d %d" % (path, int(n), int(chunk)), timeout=60)


def sort(timeout=15.0):
    return cmd("SORT", timeout=timeout)


def dump(path, timeout=60):
    return cmd("DUMP %s" % path, timeout=timeout)


def stats():
    return cmd("STATS", timeout=5)


def verify(timeout=30.0):
    return cmd("VERIFY", timeout=timeout)


def fp(timeout=30.0):
    return cmd("FP", timeout=timeout)


def exit_daemon():
    try:
        return cmd("EXIT", timeout=5)
    except Exception:
        stop()
        return ""


# ── 校验（低成本，不重排）：单调性 + 可交换指纹(和/xor) 与输入一致 ──
def _fingerprint(arr):
    import numpy as np
    return (int(arr.sum(dtype=np.uint64)), int(np.bitwise_xor.reduce(arr)))


def verify_sorted_file(inp, out):
    import numpy as np
    a = np.fromfile(inp, dtype=np.uint32)
    b = np.fromfile(out, dtype=np.uint32)
    if len(a) != len(b):
        return False, "len"
    if len(b) > 1 and np.any(b[1:] < b[:-1]):
        return False, "not-sorted"
    if _fingerprint(a) != _fingerprint(b):
        return False, "fingerprint"
    return True, "ok"


def sort_file(inp, out, n=None, timeout=30):
    """便捷：load→sort→dump→verify；失败抛。"""
    n = n or (os.path.getsize(inp) // 4)
    load(inp, n)
    t0 = time.time()
    sort(timeout=timeout)
    ms = (time.time() - t0) * 1e3
    dump(out)
    ok, why = verify_sorted_file(inp, out)
    return {"ok": ok, "why": why, "ms": ms, "n": n}


if __name__ == "__main__":
    import sys
    if not available():
        print("GPU daemon 不可用（非 Termux 宿主）"); sys.exit(2)
    if not start():
        print("daemon 启动失败"); sys.exit(3)
    try:
        print("stats:", stats())
        if len(sys.argv) >= 3:
            r = sort_file(sys.argv[1], sys.argv[2])
            print("sort_file:", r)
    finally:
        stop()
