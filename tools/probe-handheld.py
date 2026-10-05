#!/usr/bin/env python3
"""probe-handheld.py — 连上掌机抓真机信息（阶段 0-0 / 排障）

用途：SSH 登录 RG35XX Pro，一次性抓齐开发需要的全部信息。
只读操作，不修改掌机上任何文件。

用法：
    python tools/probe-handheld.py --host 192.168.3.25
    python tools/probe-handheld.py --host 192.168.3.25 --log-only
"""

import argparse
import sys

import paramiko

USER = "root"
PASS = "root"

# (标题, 命令) —— 全部是只读命令
PROBES = [
    ("系统信息", "uname -a; echo; cat /etc/version 2>/dev/null || true"),
    ("CPU / 架构", "cat /proc/cpuinfo | head -20"),
    ("挂载表（关键：确认卡槽挂载点）", "cat /proc/mounts"),
    ("磁盘空间", "df -h"),
    ("=== SDL2 位置 ===", "find / -name 'libSDL2*' 2>/dev/null"),
    ("SDL2 版本", """for f in $(find / -name 'libSDL2*.so*' 2>/dev/null | head -3); do
        echo "--- $f"
        ls -l "$f"
        strings "$f" 2>/dev/null | grep -Eo '2\\.[0-9]+\\.[0-9]+' | head -3
    done"""),
    ("=== framebuffer ===", "ls -l /dev/fb* 2>/dev/null || echo '无 /dev/fb*'"),
    ("fb 详细信息", "cat /proc/fb 2>/dev/null || echo '无 /proc/fb'"),
    ("dri 设备", "ls -l /dev/dri 2>/dev/null || echo '无 /dev/dri'"),
    ("glibc 版本", "ls -l /lib/libc.so* /lib/libc-*.so 2>/dev/null; "
                  "strings /lib/libc.so.6 2>/dev/null | grep -Eo 'GNU C Library.*version [0-9.]+' | head -2"),
    ("=== 输入设备 ===", "cat /proc/bus/input/devices"),
    ("=== 震动马达 ===", "ls /sys/class/leds/ 2>/dev/null | head -30; "
                     "find /sys -iname '*vibrat*' -o -iname '*motor*' 2>/dev/null | head -10"),
    ("=== 休眠/电源 ===", "ls /sys/power/ 2>/dev/null; "
                     "cat /sys/power/state 2>/dev/null; "
                     "cat /sys/power/wakeup_count 2>/dev/null"),
    ("=== APPS 目录实况 ===", "ls -la /mnt/mmc/Roms/APPS/ 2>/dev/null"),
    ("=== fbtest 部署检查 ===", "ls -la /mnt/mmc/Roms/APPS/PocketTransfer/ 2>/dev/null"),
    ("=== 日志 ===", "cat /mnt/mmc/Roms/APPS/PocketTransfer/logs/stdout.log 2>/dev/null || echo '无日志'"),
    ("=== 环境变量（BASE_PATH 是否注入）===", "env | grep -Ei 'base_path|ld_library|disp|SDL' || echo '无相关环境变量'"),
    ("=== 进程/占用 ===", "ps | head -20"),
    ("无线网状态", "ifconfig wlan0 2>/dev/null | head -5 || ip addr 2>/dev/null | head -20"),
]


def run(host: str, log_only: bool = False) -> int:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, username=USER, password=PASS, timeout=12,
                    banner_timeout=12, auth_timeout=12)
    except Exception as e:
        print(f"[错误] 无法连接 {host}: {type(e).__name__}: {e}")
        print("       请确认：1) 掌机已开机  2) IP 正确  3) WiFi 与本机同一网段  "
              "4) Temporary SSH Server 已开启")
        return 1

    print(f"[已连接] {host}  用户 {USER}\n" + "=" * 62)

    if log_only:
        probes = [("=== 日志 ===", "cat /mnt/mmc/Roms/APPS/PocketTransfer/logs/stdout.log 2>/dev/null || echo '无日志'")]
    else:
        probes = PROBES

    for title, cmd in probes:
        print(f"\n########## {title} ##########")
        try:
            _in, out, err = cli.exec_command(cmd, timeout=25)
            o = out.read().decode("utf-8", "replace").strip()
            e = err.read().decode("utf-8", "replace").strip()
            if o:
                print(o)
            if e:
                print(f"[stderr] {e}")
            if not o and not e:
                print("(无输出)")
        except Exception as ex:
            print(f"[命令失败] {type(ex).__name__}: {ex}")

    cli.close()
    print("\n" + "=" * 62 + "\n[完成]")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="掌机 IP")
    ap.add_argument("--log-only", action="store_true", help="只抓日志")
    a = ap.parse_args()
    sys.exit(run(a.host, a.log_only))
