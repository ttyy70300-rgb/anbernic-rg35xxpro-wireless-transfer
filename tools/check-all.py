#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键测试 —— 推文件之前跑这个

依次执行三套测试 + 生成 UI 预览图。任何一项失败就停下来，
不会往下继续（避免把坏代码推到掌机）。

用法:
    python tools/check-all.py
    python tools/check-all.py --preview-only   # 只看 UI 效果
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

SUITES = [
    ("单元自检（显示/协议/约束）", "selftest.py", 120),
    ("崩溃兜底屏", "boottest.py", 60),
    ("端到端干跑（main 全流程）", "dryrun.py", 180),
]


def run(name, script, timeout):
    print("\n" + "=" * 64)
    print(f"  {name}")
    print("=" * 64)
    p = os.path.join(HERE, script)
    if not os.path.exists(p):
        print(f"  !! 找不到 {p}")
        return False
    try:
        r = subprocess.run([PY, p], cwd=ROOT, timeout=timeout,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        print(f"  !! 超时（>{timeout}s），可能存在死循环")
        return False
    out = (r.stdout or "") + (r.stderr or "")
    # 只打印尾部，避免刷屏
    lines = out.rstrip().splitlines()
    tail = lines[-16:] if len(lines) > 16 else lines
    for ln in tail:
        print("  " + ln)
    if r.returncode != 0:
        print(f"\n  ==> 失败（退出码 {r.returncode}）")
        if len(lines) > 16:
            print("  完整输出：")
            for ln in lines[:-16]:
                print("  | " + ln)
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview-only", action="store_true")
    args = ap.parse_args()

    print("=" * 64)
    print("  PocketTransfer 本地验证")
    print(f"  python: {PY}")
    print(f"  项目: {ROOT}")
    print("=" * 64)

    if args.preview_only:
        run("UI 渲染预览", "preview.py", 60)
        return 0

    results = []
    for name, script, to in SUITES:
        ok = run(name, script, to)
        results.append((name, ok))
        if not ok:
            print("\n" + "!" * 64)
            print("  测试未通过，已停止。请先修复再部署。")
            print("!" * 64)
            return 1

    # 全部通过后生成预览
    run("UI 渲染预览", "preview.py", 60)

    print("\n" + "=" * 64)
    print("  全部通过")
    print("=" * 64)
    for name, ok in results:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
    print()
    print("  下一步：")
    print("    python tools/deploy-only.py --dry     # 看要传什么")
    print("    python tools/deploy-only.py           # 部署（不启动）")
    print()
    print("  然后在掌机上手动进入：菜单 -> APPS -> PocketTransfer")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
