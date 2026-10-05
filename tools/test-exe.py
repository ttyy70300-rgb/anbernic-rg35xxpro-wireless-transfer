#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC 端 exe 冒烟测试
==================

打包后**必须真跑一次**再交付 —— 打包成功不等于能运行。

典型翻车：用不含 Tcl/Tk 的解释器打包，过程零报错，
用户双击才看到 `ModuleNotFoundError: No module named 'tkinter'`。

判定标准（★ 关键）：
    --windowed 模式下**没有 stdout 是正常的**。
    不要因为"没输出"就以为失败 —— 判定依据是 **进程是否存活**。
    如果它因为缺模块立刻退出，poll() 会马上返回非 None。

用法：
    python tools/test-exe.py
    python tools/test-exe.py --secs 10
"""
import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PC = os.path.join(ROOT, "pc")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=8.0,
                    help="观察时长（默认 8 秒）")
    ap.add_argument("--exe", default=None)
    args = ap.parse_args()

    exe = args.exe or os.path.join(PC, "dist", "PocketTransfer.exe")
    print("=" * 62)
    print("  PC 端 exe 冒烟测试")
    print("=" * 62)

    if not os.path.exists(exe):
        print(f"!! 找不到 {exe}")
        print("   请先跑: python tools/build-exe.py")
        return 1

    size = os.path.getsize(exe) / 1024 / 1024
    print(f"  文件: {exe}")
    print(f"  大小: {size:.1f} MB")

    # ★ 先静态确认 exe 里是否真的含 tkinter 相关文件。
    #   --onefile 是压缩包，直接搜字节串不一定准，
    #   所以这只作参考，真正的判据是"能不能活过 N 秒"。
    try:
        with open(exe, "rb") as f:
            blob = f.read()
        has_tcl = b"_tkinter" in blob or b"tkinter" in blob
        has_tcl_dll = b"tcl86" in blob or b"tk86" in blob or b"tcl8" in blob
        print(f"  含 tkinter 引用: {'是' if has_tcl else '否'}")
        print(f"  含 Tcl/Tk 库   : {'是' if has_tcl_dll else '否（可能仍可用）'}")
    except Exception as e:
        print(f"  (静态检查跳过: {e})")

    print(f"\n  启动并观察 {args.secs:.0f} 秒…")
    t0 = time.time()
    try:
        p = subprocess.Popen(
            [exe, "--host", "127.0.0.1"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=os.path.dirname(exe))
    except Exception as e:
        print(f"!! 启动失败: {e}")
        return 1

    died = False
    while time.time() - t0 < args.secs:
        if p.poll() is not None:
            died = True
            break
        time.sleep(0.2)

    print()
    if died:
        out = b""
        try:
            out = p.stdout.read() or b""
        except Exception:
            pass
        txt = out.decode("utf-8", "replace").strip()
        print(f"  [!!] 进程提前退出，退出码 {p.returncode}")
        if txt:
            print("  ---- 输出 ----")
            print(txt[:2000])
        # 常见根因提示
        low = txt.lower()
        if "tkinter" in low:
            print("\n  ★ 根因：打包用的解释器不含 Tcl/Tk。")
            print("    解决：用带 tkinter 的官方 Python 打包，")
            print("    工具会自动探测；也可 --python 指定。")
        # 看看有没有落错误日志
        log = os.path.join(os.path.dirname(exe), "PocketTransfer-error.log")
        if os.path.exists(log):
            print(f"\n  ---- {log} ----")
            print(open(log, encoding="utf-8", errors="replace").read()[-1500:])
        return 1

    # 存活 = 通过
    print(f"  [OK] 进程存活 {args.secs:.0f} 秒，未崩溃")
    print("       （--windowed 无 stdout 属正常）")
    try:
        p.terminate()
        time.sleep(0.4)
        if p.poll() is None:
            p.kill()
    except Exception:
        pass

    # 清掉本次冒烟可能产生的日志
    log = os.path.join(os.path.dirname(exe), "PocketTransfer-error.log")
    if os.path.exists(log):
        try:
            os.unlink(log)
        except Exception:
            pass

    print("\n  exe 可正常启动。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
