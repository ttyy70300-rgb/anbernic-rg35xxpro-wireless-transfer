#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
阶段 0-0 第三轮：路线最终确认
  1. 震动马达 moto 节点实测可写性
  2. pysdl2 是否完整可用 + 原厂怎么初始化视频
  3. SDL2 mali-fbdev 驱动名
  4. Python 路线 vs C 路线可行性对比数据
"""
import argparse
import sys

import paramiko

USER, PWD = "root", "root"

PROBES = [
    ("V1 震动节点是否存在/权限", "ls -la /sys/class/power_supply/ 2>&1; echo '--- axp2202-battery:'; ls -la /sys/class/power_supply/axp2202-battery/ 2>&1 | head -30"),
    ("V2 moto 节点详情", "ls -la /sys/class/power_supply/axp2202-battery/moto 2>&1; echo '--- cat:'; cat /sys/class/power_supply/axp2202-battery/moto 2>&1; echo '--- power_supply 属性:'; cat /sys/class/power_supply/axp2202-battery/uevent 2>&1"),
    ("V3 实测写 moto（短震 200ms）",
     "echo 1 > /sys/class/power_supply/axp2202-battery/moto 2>&1 && echo '写1 OK' || echo '写1 失败'; sleep 0.2; echo 0 > /sys/class/power_supply/axp2202-battery/moto 2>&1 && echo '写0 OK' || echo '写0 失败'; sleep 0.3; echo '[震动测试完成 - 请感受马达]'",),

    ("P1 pysdl2 包完整性", "ls -la /usr/lib/python3/dist-packages/sdl2/ 2>&1 | head -20; echo '--- dist-packages 全部:'; ls /usr/lib/python3/dist-packages/ 2>&1 | head -20"),
    ("P2 原厂 main.py 全文（看 hw_info 与初始化）", "cat /mnt/mmc/Roms/APPS/clock/main.py 2>&1"),
    ("P3 原厂 graphic.py 前 80 行（看 SDL 初始化）", "head -80 /mnt/mmc/Roms/APPS/clock/graphic.py 2>&1"),
    ("P4 原厂 input.py 全文（看按键读取）", "cat /mnt/mmc/Roms/APPS/clock/input.py 2>&1"),
    ("P5 原厂 sdl2.zip 是什么（可能是 pysdl2 源码）", "unzip -l /mnt/mmc/Roms/APPS/clock/sdl2.zip 2>&1 | head -20"),
    ("P6 Python 能否 import sdl2", "python3 -c \"import sdl2; print('sdl2 ver:', sdl2.__version__); print('SDL ver:', sdl2.version.SDL_COMPILEDVERSION)\" 2>&1 | head -10"),
    ("P7 Python 枚举 SDL 视频驱动（关键）", "python3 -c \"import sdl2; sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO); n=sdl2.SDL_GetNumVideoDrivers(); print('num drivers:', n); [print('  [%d] %s' % (i, sdl2.SDL_GetVideoDriver(i))) for i in range(n)]; sdl2.SDL_Quit()\" 2>&1 | head -15"),
    ("P8 Python 实际用 fbdev 初始化窗口（关键测试）",
     "cd /tmp && SDL_VIDEODRIVER=mali-fbdev python3 -c \"import sdl2,sdl2.ext; print('ext ok')\" 2>&1 | tail -3; echo '--- 直接建窗:'; cd /tmp && python3 -c \"\nimport sdl2, sdl2.ext\nsdl2.SDL_Init(sdl2.SDL_INIT_VIDEO)\nfor drv in ['', 'mali-fbdev', 'fbdev', 'KMSDRM', 'dummy']:\n    if drv: sdl2.SDL_SetHint(sdl2.SDL_HINT_VIDEODRIVER, drv)\n    w = sdl2.SDL_CreateWindow(b'test', 0,0, 640,480, sdl2.SDL_WINDOW_HIDDEN)\n    if w: print('OK  ->', drv or '(default)', sdl2.SDL_GetCurrentVideoDriver()); sdl2.SDL_DestroyWindow(w); break\n    else: print('FAIL->', drv or '(default)', sdl2.SDL_GetError())\nsdl2.SDL_Quit()\n\" 2>&1 | head -15"),
    ("P9 三个原厂 app 的 .sh 启动脚本内容", "for f in /mnt/mmc/Roms/APPS/*.sh; do case \"$f\" in *fbtest*|*PocketTransfer*) continue;; esac; echo \"===== $f =====\"; cat \"$f\" 2>&1 | head -20; echo; done"),
    ("C2 原厂 RixelHK 下载器用的什么语言", "head -30 '/mnt/mmc/Roms/APPS/RixelHK游戏下载器.sh' 2>&1; echo '--- 目录:'; ls -la '/mnt/mmc/Roms/APPS/RixelHK游戏下载器/' 2>&1 | head -15"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.3.25")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(args.host, username=USER, password=PWD, timeout=15,
                    look_for_keys=False, allow_agent=False)
    except Exception as e:
        print(f"!! SSH 失败: {e}")
        sys.exit(1)
    print(f"== 已连接 {args.host} ==\n")

    for title, cmd in PROBES:
        if only and title.split()[0] not in only:
            continue
        print(f"\n>>> {title}")
        print("-" * 66)
        _, out, err = cli.exec_command(cmd, timeout=60)
        data = out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
        print(data.strip() or "(空)")

    cli.close()


if __name__ == "__main__":
    main()
