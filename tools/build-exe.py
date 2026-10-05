#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer PC 端打包脚本 —— 生成单文件 exe
=============================================

产物：dist/PocketTransfer.exe（约 12~15 MB，单文件，免安装）

【为什么要打包】
    双击即用、不依赖用户的 Python 环境、不弹控制台黑框。
    用脚本启动会有两个问题：① 用户机器上未必有 Python
    ② .bat 在中文 Windows 上按 GBK 解析，UTF-8 写的中文会变乱码。

【关键参数说明（都是踩过的坑）】

  --windowed / --noconsole
      不弹控制台窗口。tkinter GUI 程序必须加，否则后面挂个黑框很难看。

  --onefile
      打成单个 exe。启动时先解压到临时目录再跑，首次启动慢 1~2 秒。
      好处是分发只要一个文件。

  --name PocketTransfer
      exe 文件名。中文名在某些环境下会有编码问题，用英文名最稳。

  --hidden-import
      PyInstaller 的静态分析抓不到动态 import，必须显式声明。
      本项目 protocol.py 是被 client.py 直接 import 的，
      正常能分析到，但保险起见还是写上。

  --exclude-module
      排掉用不到的重量级库，能显著减小体积。
      tkinter 是必须的，不要排。

  --add-data
      把 protocol.py 一起带上。虽然 --onefile 会把 .py 编译进
      archive，但显式声明更保险（防止有人改成 --onedir 时漏文件）。

用法：
    python tools/build-exe.py
    python tools/build-exe.py --clean      # 先清理再打
    python tools/build-exe.py --console    # 保留控制台（调试用）
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PC = os.path.join(ROOT, "pc")


def has_tkinter(py):
    """这个解释器能不能 import tkinter？

    ⚠️⚠️ 打包 tkinter 程序最常见的翻车点：
        某些"精简版" Python（比如各种 portable / embeddable 发行版，
        以及某些工具链自带的解释器）**不包含 Tcl/Tk**。
        用这种解释器打包出来的 exe，运行时第一行就炸：
            ModuleNotFoundError: No module named 'tkinter'
        而且**打包过程本身不报任何错** —— 直到用户双击才发现。

    → 所以打包前必须实际探一次，不能只看版本号。
    """
    try:
        r = subprocess.run(
            [py, "-c", "import tkinter;print(tkinter.TkVersion)"],
            capture_output=True, text=True, timeout=20)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return False, str(e)


def _ver_key(path):
    """
    从路径里抠出版本号，用于**按数值**排序候选解释器。

    ⚠️ 不能直接对路径做字符串排序：
        "Python39" > "Python314" >> "Python311"（字符串比较）
        结果会把最老的 3.9 排到最前面。
    必须解析成 (3, 9) / (3, 14) 这样再比。
    """
    import re
    m = re.search(r"Python(\d)(\d*)", path)
    if not m:
        return (0, 0)
    major = int(m.group(1))
    minor = m.group(2)
    return (major, int(minor) if minor else 0)


def find_python(prefer=None):
    """
    挑一个**带 tkinter** 的解释器来打包。

    顺序：
      1. --python 显式指定
      2. 当前解释器（若它有 tkinter）
      3. 系统安装的官方 Python（**按版本号从新到旧**）
      4. PATH 上的 python / py

    绝不静默用缺 tkinter 的解释器 —— 那会做出一个必然崩的 exe。
    """
    cands = []
    if prefer:
        cands.append(prefer)
    cands.append(sys.executable)

    found = []
    import glob
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        found += glob.glob(os.path.join(
            la, "Programs", "Python", "Python3*", "python.exe"))
    found += glob.glob(r"C:\Program Files\Python3*\python.exe")
    found += glob.glob(r"C:\Python3*\python.exe")
    # ★ 按版本号数值从新到旧
    found.sort(key=_ver_key, reverse=True)
    cands += found

    # PATH 兜底
    cands += ["python", "py"]

    seen = set()
    tried = []
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.sep in c and not os.path.exists(c):
            continue
        ok, info = has_tkinter(c)
        tried.append((c, ok, info))
        if ok:
            return c, info, tried
    return None, None, tried


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="先删旧产物")
    ap.add_argument("--console", action="store_true",
                    help="保留控制台窗口（排查启动问题用）")
    ap.add_argument("--python", default=None,
                    help="指定用于打包的解释器（必须是带 tkinter 的）")
    args = ap.parse_args()

    print("=" * 64)
    print("  PocketTransfer PC 端打包")
    print("=" * 64)

    # ---- ★ 找一个带 tkinter 的解释器 ----
    py, tkver, tried = find_python(args.python)
    print("  探测解释器（tkinter 必须可用）：")
    for c, ok, info in tried:
        mark = "OK " if ok else "无 "
        short = c if len(c) < 52 else "…" + c[-49:]
        print(f"    [{mark}] {short}" + (f"   Tk {info}" if ok else ""))

    if py is None:
        print("\n!! 没有找到带 tkinter 的解释器。")
        print("   请安装官方 Python（安装时勾选 tcl/tk）：")
        print("   https://www.python.org/downloads/")
        return 1

    print(f"\n  ★ 使用 : {py}")
    print(f"    Tk   : {tkver}")
    print(f"    Python: {subprocess.run([py, '-V'], capture_output=True, text=True).stdout.strip()}")

    # ---- 检查 PyInstaller ----
    r = subprocess.run([py, "-c", "import PyInstaller;print(PyInstaller.__version__)"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\n  该解释器没装 PyInstaller，正在安装…")
        r2 = subprocess.run([py, "-m", "pip", "install", "pyinstaller"],
                            capture_output=True, text=True)
        if r2.returncode != 0:
            print("!! 安装失败：")
            print((r2.stderr or r2.stdout or "")[-1200:])
            return 1
        r = subprocess.run([py, "-c", "import PyInstaller;print(PyInstaller.__version__)"],
                           capture_output=True, text=True)
    print(f"    PyInstaller: {r.stdout.strip()}")

    # ---- 检查源码齐不齐 ----
    need = ["main.py", "client.py", "protocol.py"]
    missing = [f for f in need if not os.path.exists(os.path.join(PC, f))]
    if missing:
        print(f"\n!! pc/ 下缺文件: {missing}")
        return 1
    print(f"  源文件 : {', '.join(need)}  ✓")

    # ---- 清理 ----
    if args.clean:
        for d in ("build", "dist"):
            p = os.path.join(PC, d)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                print(f"  已清理 {d}/")
        spec = os.path.join(PC, "PocketTransfer.spec")
        if os.path.exists(spec):
            os.remove(spec)

    # ---- 组装命令 ----
    cmd = [
        py, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name", "PocketTransfer",
        # 图标（有就用，没有就算了）
        "--distpath", os.path.join(PC, "dist"),
        "--workpath", os.path.join(PC, "build"),
        "--specpath", PC,
    ]

    if not args.console:
        cmd.append("--windowed")

    # 显式声明，防止静态分析漏掉
    cmd += ["--hidden-import", "protocol",
            "--hidden-import", "client"]

    # 排掉用不到的重量级库，压体积
    for mod in ("numpy", "PIL", "pandas", "matplotlib", "scipy",
                "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
                "IPython", "jupyter", "notebook", "pytest", "setuptools",
                "test", "unittest", "pydoc", "doctest", "sqlite3",
                "email", "http", "xmlrpc", "pdb", "curses"):
        cmd += ["--exclude-module", mod]

    # 图标
    ico = os.path.join(PC, "icon.ico")
    if os.path.exists(ico):
        cmd += ["--icon", ico]
        print(f"  图标   : icon.ico ✓")
    else:
        print(f"  图标   : (无，使用默认)")

    cmd.append(os.path.join(PC, "main.py"))

    # ---- 执行 ----
    print("\n  正在打包（首次约 1~3 分钟）…\n")
    r = subprocess.run(cmd, cwd=PC, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = (r.stdout or "")[-2000:]
    err = (r.stderr or "")[-2000:]

    if r.returncode != 0:
        print("!! 打包失败")
        print(err or tail)
        return r.returncode

    # ---- 校验产物 ----
    exe = os.path.join(PC, "dist", "PocketTransfer.exe")
    print("=" * 64)
    if os.path.exists(exe):
        size = os.path.getsize(exe) / 1024 / 1024
        print(f"  ✓ 打包成功")
        print(f"    文件: {exe}")
        print(f"    大小: {size:.1f} MB")
        print()
        print("  双击 PocketTransfer.exe 即可运行。")
        print("  分发时把这个文件单独拷走就行，目标机器不需要装 Python。")
    else:
        print("  !! 命令成功但没有产物，请检查 dist/ 目录")
        print(tail)
        return 1
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
