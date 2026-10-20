#!/usr/bin/env python3
"""纯 C 排序代码生成器（PC 后端 MVP，E3226）：证明/原语组合 → 单文件 C → 编译 → 证书。

## 生成契约
防哪一类：**"只在手机上跑得快"的特化工具**——把平台参数（UMA/PCIE/阈值）写死进后端；
本件只读 `platform_profile`，同一套原语语义生成目标平台 C 代码，并用同一 golden 证书对账。
p：1（E3226）
可判性：生成代码必须 `cc` 编译 rc=0；运行输出最终 checksum == `np.sort` 的 checksum；否则报错。
后置条件：`python3 组件/c_codegen_sort.py --矩阵` rc=0 且每档打印 `CERT=OK`。
C_逃逸：中 —— 只覆盖 radix8/radix16（uint32）；counting/run_merge 与 64-bit 尚未纳入（T-349）。

用法：
  python3 组件/c_codegen_sort.py --原语 radix8 --输入 a.bin --输出 out.bin
  python3 组件/c_codegen_sort.py --矩阵            # 本机 1M/4M/8M 生成+编译+证书
"""
__派发__ = {
    "类别": "支持件/代码生成",
    "入口": ["python3", "组件/c_codegen_sort.py", "--矩阵"],
    "一句话": "PC/通用 C 后端 MVP：radix8/radix16 原语组合 → 单文件 C → cc 编译 → 与 numpy 同证书对账",
}

import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

_COMP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _COMP)
import platform_profile as pp  # noqa: E402


def _emit_radix(ops, profile):
    """按原语组合生成 C 源码；目前 radix8=4×8bit LSD，radix16=2×16bit LSD（均稳定）。"""
    ops = [o for o in ops if o in ("radix8", "radix16")]
    if not ops:
        raise ValueError("暂只支持 radix8/radix16: %r" % ops)
    body = []
    if "radix16" in ops:
        body.append(r'''
static void radix16(uint32_t *a, uint32_t *tmp, size_t n){
    uint32_t *cnt = (uint32_t*)calloc(65536, sizeof(uint32_t));
    for (int pass=0; pass<2; ++pass){
        int sh = pass*16;
        memset(cnt, 0, 65536*sizeof(uint32_t));
        for (size_t i=0;i<n;i++) cnt[(a[i]>>sh)&0xFFFFu]++;
        size_t s=0; for (uint32_t b=0;b<65536;b++){ uint32_t c=cnt[b]; cnt[b]=(uint32_t)s; s+=c; }
        for (size_t i=0;i<n;i++){ uint32_t b=(a[i]>>sh)&0xFFFFu; tmp[cnt[b]++] = a[i]; }
        memcpy(a,tmp,n*sizeof(uint32_t));
        /* 还原偏移表（下一趟重新 count，无需保留） */
    }
    free(cnt);
}''')
    if "radix8" in ops:
        body.append(r'''
static void radix8(uint32_t *a, uint32_t *tmp, size_t n){
    uint32_t cnt[256];
    for (int pass=0; pass<4; ++pass){
        int sh = pass*8;
        memset(cnt, 0, sizeof(cnt));
        for (size_t i=0;i<n;i++) cnt[(a[i]>>sh)&0xFFu]++;
        uint32_t s=0; for (uint32_t b=0;b<256;b++){ uint32_t c=cnt[b]; cnt[b]=s; s+=c; }
        for (size_t i=0;i<n;i++){ uint32_t b=(a[i]>>sh)&0xFFu; tmp[cnt[b]++] = a[i]; }
        memcpy(a,tmp,n*sizeof(uint32_t));
    }
}''')
    sort_call = "\n".join("    %s(a,tmp,n);" % o for o in ops)
    return r'''#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

static double now_ms(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec*1e3 + t.tv_nsec/1e6; }
static uint64_t fnv1a(const uint8_t *p, size_t n){ uint64_t h=1469598103934665603ULL; for(size_t i=0;i<n;i++){ h^=p[i]; h*=1099511628211ULL; } return h; }
%s
int main(int argc, char **argv){
    if(argc<2){ fprintf(stderr,"用法: %%s <in.bin> [out.bin]\n", argv[0]); return 2; }
    FILE *f=fopen(argv[1],"rb"); if(!f){ perror("open"); return 3; }
    fseek(f,0,SEEK_END); long bytes=ftell(f); fseek(f,0,SEEK_SET);
    if(bytes%%4){ fprintf(stderr,"文件长度不是4的倍数\n"); return 4; }
    size_t n=(size_t)bytes/4;
    uint32_t *a=(uint32_t*)malloc(bytes), *tmp=(uint32_t*)malloc(bytes);
    if(fread(a,4,n,f)!=(size_t)n){ fprintf(stderr,"read 短\n"); return 5; }
    fclose(f);
    double t0=now_ms();
%s
    double dt=now_ms()-t0;
    printf("N=%%zu TIME_MS=%%.2f CHECKSUM=%%016llx\n", n, dt, (unsigned long long)fnv1a((uint8_t*)a, n*4));
    if(argc>2){ FILE *g=fopen(argv[2],"wb"); fwrite(a,4,n,g); fclose(g); }
    free(a); free(tmp);
    return 0;
}
''' % ("\n".join(body), sort_call)


def build_and_run(ops, inp, out=None, profile=None, workdir=None, compiler=None):
    profile = profile or pp.load()
    src = _emit_radix(ops, profile)
    workdir = workdir or tempfile.mkdtemp(prefix="ccg_")
    cpath = os.path.join(workdir, "sort_gen.c")
    exe = os.path.join(workdir, "sort_gen")
    open(cpath, "w", encoding="utf-8").write(src)
    cc = compiler or os.environ.get("CC", "cc")
    tb = time.time()
    r = subprocess.run([cc, "-O3", "-std=c11", cpath, "-o", exe], capture_output=True, text=True)
    compile_ms = (time.time() - tb) * 1e3
    if r.returncode != 0:
        raise RuntimeError("cc 编译失败: %s" % (r.stderr[-1000:]))
    tr = time.time()
    rr = subprocess.run([exe, inp] + ([out] if out else []), capture_output=True, text=True)
    run_ms = (time.time() - tr) * 1e3
    if rr.returncode != 0:
        raise RuntimeError("生成程序 rc=%d: %s" % (rr.returncode, rr.stderr[-500:]))
    fields = dict(kv.split("=", 1) for kv in rr.stdout.strip().split() if "=" in kv)
    return {
        "ops": ops, "c_path": cpath, "exe": exe, "compile_ms": compile_ms, "run_wall_ms": run_ms,
        "n": int(fields.get("N", 0)), "time_ms": float(fields.get("TIME_MS", 0)),
        "checksum": fields.get("CHECKSUM"), "profile": profile["target_id"],
    }


def expected_checksum(arr):
    """同 C 端 FNV-1a（对已排序 uint32 小端字节流）。"""
    h = 1469598103934665603
    for b in np.sort(arr).astype("<u4").tobytes():
        h ^= b
        h = (h * 1099511628211) & ((1 << 64) - 1)
    return "%016x" % h


def certificate(ops, inp, out=None, profile=None):
    arr = np.fromfile(inp, dtype=np.uint32)
    r = build_and_run(ops, inp, out, profile)
    if out and os.path.exists(out):
        got = np.fromfile(out, dtype=np.uint32)
        r["cert"] = bool(r["n"] == len(arr) and np.array_equal(got, np.sort(arr)))
    else:
        exp = expected_checksum(arr)
        r["expected_checksum"] = exp
        r["cert"] = (r["checksum"] == exp) and r["n"] == len(arr)
    return r


def main(argv):
    profile = pp.load()
    if "--原语" in argv:
        ops = [argv[argv.index("--原语") + 1]]
        inp = argv[argv.index("--输入") + 1] if "--输入" in argv else None
        out = argv[argv.index("--输出") + 1] if "--输出" in argv else None
        if not inp:
            print("需要 --输入"); return 2
        r = certificate(ops, inp, out, profile)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r["cert"] else 1
    if "--矩阵" in argv:
        import tempfile
        S = tempfile.mkdtemp(prefix="ccg_in_")
        sizes = [int(x) for x in os.environ.get("CCG_SIZES", "1000000,4000000,8000000").split(",") if x]
        rows = []
        for n in sizes:
            p = os.path.join(S, "fresh_%d.bin" % n)
            rng = np.random.default_rng(99 + n)
            rng.integers(0, 2**31, size=n, dtype=np.uint32).tofile(p)
            for ops in (["radix8"], ["radix16"]):
                out = os.path.join(S, "out_%d_%s.bin" % (n, ops[0]))
                r = certificate(ops, p, out, profile)
                rows.append(r)
                print("%9d %-12s C生成 %8.2fms 编译 %.0fms CERT=%s" % (
                    n, "+".join(ops), r["time_ms"], r["compile_ms"], "OK" if r["cert"] else "FAIL"), flush=True)
        ok = all(r["cert"] for r in rows)
        print("PROFILE=%s · CERT=%s" % (profile["target_id"], "OK" if ok else "FAIL"))
        return 0 if ok else 1
    if "--生成" in argv:
        i = argv.index("--生成")
        ops = argv[i + 1].split(",") if i + 1 < len(argv) else ["radix16"]
        out = argv[argv.index("--输出") + 1] if "--输出" in argv else "sort_gen.c"
        open(out, "w", encoding="utf-8").write(_emit_radix(ops, profile))
        print(out)
        return 0
    if "--交叉编译" in argv:
        import tempfile
        cc = os.environ.get("CC_X86", "x86_64-linux-gnu-gcc")
        ops = ["radix16"]
        src = os.path.join(tempfile.mkdtemp(prefix="ccg_x_"), "sort_gen.c")
        open(src, "w", encoding="utf-8").write(_emit_radix(ops, profile))
        obj = src[:-2] + ".o"
        try:
            r = subprocess.run([cc, "-O3", "-std=c11", "-c", src, "-o", obj], capture_output=True, text=True)
        except FileNotFoundError:
            print("CC=%s 不存在：本机无 x86 交叉工具链，诚实 rc=2（不伪造编译通过）" % cc)
            return 2
        print("CC=%s rc=%d obj=%s" % (cc, r.returncode, obj if r.returncode == 0 else "-"))
        if r.returncode != 0:
            print((r.stderr or "")[-800:])
        return 0 if r.returncode == 0 else 2
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
