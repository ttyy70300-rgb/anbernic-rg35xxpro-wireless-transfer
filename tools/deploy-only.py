#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PocketTransfer 掌机端部署器 —— 只推文件，绝不改系统

边界（用户明确要求，务必遵守）：
    只允许往 /mnt/mmc/Roms/APPS/ 下写本应用自己的文件。
    不做任何其他改动：不 kill 进程、不碰 /tmp/.next、不切显示模式、
    不 remount、不建软链、不写 sysfs。

重要教训：
    dmenu.bin 是原厂菜单，也是 /dev/fb0 的持有者。
    反复 kill 它会让它来不及完成初始化，表现为黑屏（已发生）。
    启动应用一律由用户在掌机上手动点击菜单完成。
    → 所以本工具【不启动】程序，只拷贝。

本工具做四件事：
    1. 上传 handheld/*.py 等文件到 /mnt/mmc/Roms/APPS/PocketTransfer/
    2. 安装 /mnt/mmc/Roms/APPS/PocketTransfer.sh（菜单里会显示为应用）
    3. 读回掌机上的日志（--log）
    4. 查状态：进程、显示参数、文件清单（--status）

用法:
    python tools/deploy-only.py                  # 只部署
    python tools/deploy-only.py --dry            # 预览要传什么，不真传
    python tools/deploy-only.py --log            # 部署后读日志
    python tools/deploy-only.py --status         # 只查状态，不改动
    python tools/deploy-only.py --clean          # 删除本应用的文件
"""
import argparse
import os
import posixpath
import sys

import paramiko

USER, PWD = "root", "root"
APPS = "/mnt/mmc/Roms/APPS"
APPNAME = "PocketTransfer"
REMOTE = f"{APPS}/{APPNAME}"
SH_PATH = f"{APPS}/{APPNAME}.sh"

# 不传这些目录（本地构建产物 / 缓存）
IGNORE_DIRS = {"__pycache__", "logs", "build", ".git", ".idea", "preview",
               "dist-test", "test", "pysdl2test"}
# 只传这些扩展名
UPLOAD_EXT = (".py", ".txt", ".json", ".cfg", ".ttf", ".png", ".md")
# 明确排除的文件（本地工具，不需要上掌机）
IGNORE_FILES = {"launcher.sh"}  # launcher.sh 单独处理成 .sh 放 APPS 根


def sftp_mkdirs(sftp, path):
    cur = ""
    for p in path.strip("/").split("/"):
        cur += "/" + p
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def fix_lf(path):
    """Windows 换行会让 shell 脚本在 Linux 上炸掉，统一成 LF。"""
    with open(path, "rb") as f:
        data = f.read()
    if b"\r\n" in data:
        new = data.replace(b"\r\n", b"\n")
        with open(path, "wb") as f:
            f.write(new)
        return True
    return False


def walk_src(src):
    out = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for fn in filenames:
            if fn in IGNORE_FILES:
                continue
            if fn.endswith((".DISABLED", ".pyc", ".c", ".o", ".log")):
                continue
            if fn.endswith(UPLOAD_EXT):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.3.25")
    ap.add_argument("--src", default="handheld")
    ap.add_argument("--log", action="store_true", help="部署后显示掌机日志")
    ap.add_argument("--clean", action="store_true", help="删除本应用文件")
    ap.add_argument("--status", action="store_true", help="只查状态，不改动")
    ap.add_argument("--dry", action="store_true", help="只列出要传的文件")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    files = walk_src(src)

    # ---- 本地先列清单（--dry 或网络不通时也有用）----
    if args.dry:
        print(f"将上传 {len(files)} 个文件到 {REMOTE}:")
        for fp in files:
            rel = os.path.relpath(fp, src).replace(os.sep, "/")
            size = os.path.getsize(fp)
            print(f"  {size:>8}  {rel}")
        lf = os.path.join(src, "launcher.sh")
        if os.path.exists(lf):
            print(f"  {os.path.getsize(lf):>8}  -> {SH_PATH}  (启动脚本)")
        return

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(args.host, username=USER, password=PWD, timeout=15,
                    look_for_keys=False, allow_agent=False)
    except Exception as e:
        print(f"!! 连不上 {args.host}: {e}")
        print("   请确认掌机已开机、WiFi 已连接、IP 是否正确。")
        print("   （可以用 --dry 先看要传哪些文件）")
        sys.exit(2)
    print(f"== {args.host} ==\n")

    def sh(cmd, t=30):
        try:
            _, o, e = cli.exec_command(cmd, timeout=t)
            out = o.read().decode("utf-8", "replace")
            err = e.read().decode("utf-8", "replace")
            return (out + err).strip()
        except Exception as ex:
            return f"(命令执行失败: {ex})"

    # ---- 只查状态 ----
    if args.status:
        print("--- 进程 ---")
        print(sh("ps aux | grep -v grep | grep -E "
                 "'dmenu.bin|dmenu_ln|loadapp|main.py'") or "(无)")
        print("\n--- 显示参数 (sysfs) ---")
        print(sh("for f in virtual_size bits_per_pixel; do "
                 "printf '%s: ' $f; cat /sys/class/graphics/fb0/$f; done"))
        print("\n--- 卡片挂载 ---")
        print(sh("mount | grep -E 'mmcblk0p1|sdcard'"))
        print("\n--- 应用文件 ---")
        print(sh(f"ls -la {REMOTE}/ 2>&1 | head -30"))
        print("\n--- 启动脚本 ---")
        print(sh(f"ls -la {SH_PATH} 2>&1"))
        cli.close()
        return

    # ---- 清理（只删本应用自己的东西）----
    if args.clean:
        print("将删除:")
        print(f"  {REMOTE}/")
        print(f"  {SH_PATH}")
        r = sh(f"rm -rf {REMOTE} {SH_PATH} 2>&1; echo rc=$?")
        print(r)
        print("\n已清理。菜单里可能仍有残留条目，重启掌机即刷新。")
        print("（本工具只删自己的文件，不动系统任何内容）")
        cli.close()
        return

    # ---- 部署 ----
    print(f"准备上传 {len(files)} 个文件 -> {REMOTE}")
    sftp = cli.open_sftp()
    try:
        sftp_mkdirs(sftp, REMOTE)
        n = 0
        for fp in files:
            rel = os.path.relpath(fp, src).replace(os.sep, "/")
            rf = posixpath.join(REMOTE, rel)
            d = os.path.dirname(rf)
            if d != REMOTE:
                sftp_mkdirs(sftp, d)
            fix_lf(fp)
            sftp.put(fp, rf)
            if fp.endswith(".py"):
                sftp.chmod(rf, 0o755)
            n += 1
            print(f"  [{n:>2}/{len(files)}] {rel}")
    finally:
        sftp.close()

    # 启动脚本：本地 launcher.sh -> APPS 根/PocketTransfer.sh
    lf = os.path.join(src, "launcher.sh")
    if os.path.exists(lf):
        with open(lf, "r", encoding="utf-8") as f:
            text = f.read().replace("\r\n", "\n")
        tmp = os.path.join(src, ".gen_launcher.sh")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        sftp = cli.open_sftp()
        try:
            sftp.put(tmp, SH_PATH)
            sftp.chmod(SH_PATH, 0o755)
        finally:
            sftp.close()
        os.remove(tmp)
        print(f"\n已安装启动脚本 {SH_PATH}")
    else:
        print(f"\n!! 缺少 {lf}，菜单里不会出现这个应用")

    print("\n" + "=" * 60)
    print("部署完成（未启动任何东西）。")
    print()
    print("请在掌机上：菜单 → APPS → PocketTransfer 点击进入。")
    print("如果菜单里看不到，重启一次掌机刷新应用列表。")
    print("=" * 60)

    # ---- 日志 ----
    if args.log:
        for name in ("launcher.log", "boot.log"):
            print(f"\n--- {name} ---")
            print(sh(f"tail -60 {REMOTE}/{name} 2>&1") or "(不存在或为空)")

    cli.close()


if __name__ == "__main__":
    main()
