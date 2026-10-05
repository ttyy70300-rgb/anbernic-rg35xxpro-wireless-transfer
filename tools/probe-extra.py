#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
阶段 0-0 补充探测：补齐 probe-handheld.py 未覆盖的项目
  1. 震动马达接口  2. SDL2 版本/视频驱动  3. 固件 libc
  4. TF 卡2 状态   5. 输入设备细节        6. 音频
  7. framebuffer 能力  8. 电源/背光/温度   9. 网络

用法:
  python tools/probe-extra.py --host 192.168.3.25
  python tools/probe-extra.py --host 192.168.3.25 --only sdl2 fb
"""
import argparse
import re

import paramiko

USER = "root"
PWD = "root"

PROBES = {
    "vibr": [
        ("/sys/class/leds 全部", "ls -la /sys/class/leds/ 2>&1 | head -30"),
        ("sys 下搜 vibrat/motor", "find /sys -maxdepth 4 -iname '*vibrat*' -o -maxdepth 4 -iname '*motor*' 2>/dev/null | head -20"),
        ("input devices 里的震动", "grep -iE 'vibrat|motor|haptic' /proc/bus/input/devices 2>&1 | head -10"),
        ("/dev/input 节点", "ls -la /dev/input/ 2>&1 | head -20"),
        ("内核模块里的 haptics", "cat /proc/modules 2>/dev/null | grep -iE 'hapt|vibrat|max776' | head"),
        ("原厂 app 是否用到震动", "grep -rliE 'vibrat' /mnt/mmc/Roms/APPS/ 2>/dev/null | head -5"),
    ],
    "sdl2": [
        ("SDL2 文件", "ls -l /usr/lib/libSDL2* 2>&1"),
        ("SDL2 精确版本", "readlink -f /usr/lib/libSDL2-2.0.so.0 2>&1"),
        ("SDL2 支持的视频驱动", "strings /usr/lib/libSDL2-2.0.so.0 2>/dev/null | grep -xE 'dummy|fbdev|directfb|kmsdrm|wayland|x11|offscreen|android' | sort -u"),
        ("SDL2 静态库 + ttf", "ls -l /usr/lib/libSDL2*.a 2>&1; echo '--- ttf:'; ls -l /usr/lib/libSDL2_ttf* 2>&1"),
        ("SDL2 头文件", "ls /usr/include/SDL2/ 2>&1 | head -8"),
        ("SDL2_ttf 精确版本", "readlink -f /usr/lib/libSDL2_ttf-2.0.so.0 2>&1"),
        ("SDL2 依赖的库", "readelf -d /usr/lib/libSDL2-2.0.so.0 2>&1 | grep NEEDED"),
    ],
    "libc": [
        ("libc 文件", "ls -l /lib/libc.so* /lib/libc-*.so /lib/ld-*.so* 2>&1"),
        ("/lib 里的 libc/ld", "ls /lib/ 2>&1 | grep -iE 'libc|ld-linux|ld-musl'"),
        ("libc 版本串", "strings /lib/libc-*.so 2>/dev/null | grep -E 'GNU C Library|GLIBC 2\\.[0-9]+' | sort -u | head -5"),
        ("buildroot/工具链痕迹", "ls /usr/bin/ 2>&1 | grep -iE '^(gcc|cc|make|cmake|pkg-config|bash|busybox|strings|readelf|nm|objdump)$'"),
        ("uname + 发行标识", "uname -a; echo '---'; cat /etc/os-release 2>&1 | head -5; cat /etc/version 2>&1"),
    ],
    "tf": [
        ("所有块设备", "ls -l /dev/mmcblk* /dev/sd* 2>&1"),
        ("当前挂载", "mount 2>&1 | grep -E 'mmc|vfat|sdcard'"),
        ("/mnt 目录", "ls -la /mnt/ 2>&1"),
        ("sdcard 目录", "ls /mnt/sdcard 2>&1 | head -5"),
        ("磁盘占用", "df -h 2>&1"),
    ],
    "input": [
        ("input devices 全文", "cat /proc/bus/input/devices 2>&1 | head -100"),
        ("每个 event 的名字", "for e in /dev/input/event*; do echo \"$e -> $(cat /sys/class/input/$(basename $e)/device/name 2>&1)\"; done"),
        ("js0 ABS 能力", "cat /sys/class/input/js0/device/capabilities/abs 2>&1"),
        ("js0 支持的按键数", "wc -l < /sys/class/input/js0/device/capabilities/key 2>&1; cat /sys/class/input/js0/device/capabilities/key 2>&1 | head -3"),
    ],
    "audio": [
        ("ALSA 卡", "cat /proc/asound/cards 2>&1; echo '--- /dev/snd:'; ls /dev/snd 2>&1"),
        ("asound.conf", "cat /etc/asound.conf 2>&1 | head -20"),
    ],
    "fb": [
        ("fb0 可变参数", "for f in bits_per_pixel blank mode stride virtual_size activate modes flags; do echo \"$f = $(cat /sys/class/graphics/fb0/$f 2>&1)\"; done"),
        ("所有 fb 设备", "ls -la /dev/fb* 2>&1"),
        ("fb 所在平台设备", "ls /sys/devices/platform/ 2>&1 | head -25"),
        ("sunxi disp 节点", "ls -la /sys/class/disp* /sys/class/sunxi* 2>&1 | head -20"),
    ],
    "power": [
        ("/sys/power", "ls /sys/power/ 2>&1"),
        ("背光", "ls /sys/class/backlight/ 2>&1; for d in /sys/class/backlight/*/; do echo \"$d -> $(cat $d/brightness 2>&1) / max $(cat $d/max_brightness 2>&1)\"; done 2>&1"),
        ("CPU 调频", "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>&1; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>&1"),
        ("温度", "for z in /sys/class/thermal/thermal_zone*; do echo \"$(cat $z/type 2>&1): $(cat $z/temp 2>&1)\"; done 2>&1 | head -6"),
        ("内存", "free -m 2>&1; echo '---'; cat /proc/meminfo 2>&1 | head -5"),
    ],
    "net": [
        ("网卡与 IP", "ifconfig 2>&1 | grep -E '^[a-z0-9]|inet '"),
        ("无线驱动", "cat /sys/class/net/wlan0/device/driver/module/version 2>&1; ls /sys/class/net/ 2>&1"),
        ("网络工具", "which hostapd udhcpd dnsmasq iw wpa_supplicant 2>&1"),
        ("端口占用", "cat /proc/net/tcp 2>&1 | head -15"),
    ],
}

ORDER = ["vibr", "sdl2", "libc", "tf", "input", "audio", "fb", "power", "net"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.3.25")
    ap.add_argument("--only", default="", help="逗号分隔的组名")
    ap.add_argument("--raw", action="store_true", help="保存原始输出")
    args = ap.parse_args()

    groups = [g.strip() for g in args.only.split(",") if g.strip()] or ORDER
    for g in groups:
        if g not in PROBES:
            print(f"!! 未知组: {g}，可选: {','.join(ORDER)}")
            continue

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(args.host, username=USER, password=PWD, timeout=15,
                    look_for_keys=False, allow_agent=False)
    except Exception as e:
        print(f"!! SSH 连接失败 {args.host}: {e}")
        sys.exit(1)
    print(f"== 已连接 {args.host} ==\n")

    buf = []
    for g in groups:
        print(f"########## {g} ##########")
        buf.append(f"########## {g} ##########")
        for title, cmd in PROBES[g]:
            print(f"\n--- {title} ---")
            _, out, err = cli.exec_command(cmd, timeout=30)
            data = out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
            data = data.strip() or "(空)"
            print(data)
            buf.append(f"--- {title} ---\n{data}")
        print()

    cli.close()
    if args.raw:
        with open("probe-extra-raw.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(buf))
        print("\n>> 已保存 probe-extra-raw.txt")


if __name__ == "__main__":
    import sys
    main()
