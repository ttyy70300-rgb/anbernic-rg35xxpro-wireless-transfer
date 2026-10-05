#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只读：查看卡上目录结构，定位用户提到的 emu/exe/music 在哪一层。

用法：
    python tools/probe-tree.py
"""
import os
import sys

import paramiko

HOST = "192.168.3.25"
USER, PWD = "root", "root"


def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)

    def sh(cmd, t=30):
        _, o, e = cli.exec_command(cmd, timeout=t)
        return (o.read().decode("utf-8", "replace") +
                e.read().decode("utf-8", "replace"))

    cmds = [
        ("卡根 /mnt/mmc", "ls -la /mnt/mmc/"),
        ("卡根目录名（只看目录）", "ls -d /mnt/mmc/*/ 2>/dev/null"),
        ("Roms 下", "ls /mnt/mmc/Roms/ 2>/dev/null | head -40"),
        ("找 emu/exe/music（3 层内）",
         "find /mnt/mmc -maxdepth 3 \\( -iname 'emu' -o -iname 'exe' "
         "-o -iname 'music' \\) -type d 2>/dev/null"),
        ("APPS 下", "ls /mnt/mmc/Roms/APPS/ 2>/dev/null | head -20"),
        ("挂载点", "mount | grep -E 'mmc|vendor|data'"),
    ]
    for title, cmd in cmds:
        print("=" * 66)
        print(f"  {title}")
        print("=" * 66)
        print(sh(cmd).rstrip())
        print()

    cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
