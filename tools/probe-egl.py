#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
诊断 SDL2 mali 驱动 EGL 初始化失败

现象：SDL_Init(VIDEO) 成功（当前驱动 = mali），但 SDL_CreateWindow 报
      "Could not initialize EGL"

已知背景：
  - /usr/lib/libEGL.so.1 -> libEGL.so.1.4.0，仅 4360 字节 = 转发桩
  - /dev/mali 不存在
  - SDL2 视频驱动只有 mali / dummy
  - 但原厂 clock app 用同样的 pysdl2 且能正常显示（log 无 Fatal）

本脚本要找的是：原厂能跑通的环境与我们有什么差别。
"""
import sys

import paramiko

USER, PWD = "root", "root"

CMDS = [
    ("libEGL 桩的转发目标",
     "readelf -d /usr/lib/libEGL.so.1 2>&1 | head -15; "
     "echo '--- strings:'; "
     "strings /usr/lib/libEGL.so.1 | grep -iE 'libEGL|libmali|so[.]' | head -20"),
    ("系统内所有 EGL 库",
     "find / -xdev -name 'libEGL*' 2>/dev/null | head -20"),
    ("GPU/显示相关设备节点",
     "ls /dev/ | tr ' ' '\\n' | grep -iE 'mali|dri|gpu|ion|ump|fb|sunxi|disp'"),
    ("misc 类设备（mali 常注册于此）",
     "ls -la /sys/class/misc/ 2>&1"),
    ("sys 下 mali/gpu 节点",
     "find /sys -maxdepth 5 -iname '*mali*' 2>/dev/null | head -20"),
    ("libSDL2 的 NEEDED（是否链 EGL）",
     "readelf -d /usr/lib/libSDL2-2.0.so.0 | grep -iE 'NEEDED'"),
    ("原厂 clock app 完整日志",
     "cat /mnt/mmc/Roms/APPS/clock/log.txt 2>&1 | tail -25"),
    ("原厂 launcher 主进程",
     "ps aux 2>/dev/null | grep -iE 'launcher|mainui|emulator' | grep -v grep | head -10"),
    ("原厂 launcher 启动的环境变量（从 /proc 读）",
     "for p in $(ls /proc | grep -E '^[0-9]+$'); do "
     "c=$(tr '\\0' ' ' < /proc/$p/cmdline 2>/dev/null); "
     "case \"$c\" in *launcher*|*Launcher*|*mainui*) "
     "echo \"PID $p: $c\"; "
     "tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null | grep -iE 'EGL|LD_LIBRARY|SDL|MALI|PYSDL|LIBGL';; "
     "esac; done"),
    ("原厂系统里 EGL 相关的 lib 目录",
     "ls -la /mnt/vendor/lib/ 2>/dev/null | grep -iE 'egl|gles|mali'; "
     "echo '--- /mnt/vendor/bin:'; ls /mnt/vendor/bin/ 2>/dev/null | head -30"),
    ("原厂 .sh 脚本里的环境变量",
     "grep -h -E 'export|LD_LIBRARY|PYSDL' /mnt/mmc/Roms/APPS/*.sh 2>/dev/null | sort -u | head -20"),
    ("是否存在 mali 的 EGL 扩展库",
     "find / -xdev -iname '*mali*' 2>/dev/null | head -20"),
    ("当前 SSH 会话的环境（对照）",
     "env | grep -iE 'LD_LIBRARY|PYSDL|EGL|DISPLAY|SHELL|PATH'"),
]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)
    print(f"== {host} ==\n")
    for title, cmd in CMDS:
        print(f">>> {title}")
        print("-" * 66)
        _, o, e = cli.exec_command(cmd, timeout=45)
        d = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
        print(d.strip() or "(空)")
        print()
    cli.close()


if __name__ == "__main__":
    main()
