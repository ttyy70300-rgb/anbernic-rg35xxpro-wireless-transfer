#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe-firmware-origin.py —— 只读探测：判断固件是「官方原厂」还是「社区魔改」

动机：用户反馈这台机器刷的是「从官方客服要来的最新包」。
      需要核实用户空间特征，修正文档里"被社区刷写"的推断。

用法：
    python tools/probe-firmware-origin.py [IP]

只读，不做任何修改。
"""
import sys
import paramiko

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
USER = "root"
PASS = "root"

CMDS = [
    # ---- 1. 发行版身份 ----
    ("01-发行版身份", "cat /etc/os-release"),
    ("02-lsb-release", "cat /etc/lsb-release 2>/dev/null"),
    ("03-issue", "cat /etc/issue 2>/dev/null"),

    # ---- 2. 文件时间戳（判断是否是厂商构建时写入 vs 后期手工改） ----
    ("04-系统文件时间", "ls -la --time-style=long-iso /etc/os-release /etc/issue /etc/lsb-release /etc/hostname 2>/dev/null"),

    # ---- 3. 厂商标识 ----
    ("05-厂商oem目录", "ls -la --time-style=long-iso /mnt/vendor/oem/ 2>/dev/null"),
    ("06-厂商board.ini", "cat /mnt/vendor/oem/board.ini 2>/dev/null"),
    ("07-厂商全部ini", "for f in /mnt/vendor/oem/*.ini; do echo \"--- $f\"; cat \"$f\"; done 2>/dev/null"),
    ("08-厂商vendor根", "ls -la --time-style=long-iso /mnt/vendor/ 2>/dev/null"),

    # ---- 4. 固件版本线索 ----
    ("09-固件版本文件", "find / -maxdepth 3 -iname '*version*' -not -path '/proc/*' -not -path '/sys/*' 2>/dev/null | head -30"),
    ("10-etc下版本", "ls -la --time-style=long-iso /etc/*version* /etc/*release* 2>/dev/null"),
    ("11-anbernic标识", "grep -ril 'anbernic\\|rg35\\|h700' /etc/ /mnt/vendor/ 2>/dev/null | head -20"),

    # ---- 5. apt 源（官方固件一般也会配源，但看具体指向） ----
    ("12-apt源", "cat /etc/apt/sources.list 2>/dev/null; echo '--- sources.list.d:'; ls /etc/apt/sources.list.d/ 2>/dev/null; for f in /etc/apt/sources.list.d/*; do echo \"--- $f\"; cat \"$f\"; done 2>/dev/null"),

    # ---- 6. 包数量与来源分布（判断是完整 Ubuntu 还是精简裁剪） ----
    ("13-包总数", "dpkg -l | grep -c '^ii'"),

    # ---- 7. 根fs构建时间（镜像烧录证据） ----
    ("14-根目录时间", "ls -la --time-style=long-iso / 2>/dev/null"),
    ("15-关键目录时间", "ls -lad --time-style=long-iso /usr /etc /opt /root /home 2>/dev/null"),
    ("16-bin时间抽样", "ls -la --time-style=long-iso /usr/bin/ | head -12"),

    # ---- 8. 内核与启动 ----
    ("17-内核版本", "uname -a"),
    ("18-proc版本", "cat /proc/version"),
    ("19-内核构建时间", "ls -la --time-style=long-iso /lib/modules/ 2>/dev/null; uname -v"),

    # ---- 9. 启动与系统管理（区分 systemd / sysvinit / busybox） ----
    ("20-init类型", "ls -la /sbin/init 2>/dev/null; ls -la /etc/init.d/ 2>/dev/null | head -25"),
    ("21-systemd", "systemctl --version 2>/dev/null | head -2; ls /lib/systemd/ 2>/dev/null | head -5"),

    # ---- 10. 磁盘布局 ----
    ("22-挂载", "mount | head -25"),
    ("23-分区", "cat /proc/partitions 2>/dev/null"),
    ("24-fstab", "cat /etc/fstab 2>/dev/null"),

    # ---- 11. 时区/语言（出厂默认） ----
    ("25-时区", "cat /etc/timezone 2>/dev/null; ls -la /etc/localtime 2>/dev/null"),
    ("26-语言", "cat /etc/default/locale 2>/dev/null"),

    # ---- 12. 出厂日志/首次启动痕迹 ----
    ("27-root家目录", "ls -la --time-style=long-iso /root/ 2>/dev/null | head -20"),
    ("28-厂商logo", "ls -la --time-style=long-iso /mnt/vendor/ /mnt/vendor/bin/ 2>/dev/null | head -40"),
]


def main():
    print("=" * 62)
    print("  固件来源探测（只读）  target=%s" % HOST)
    print("=" * 62)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(HOST, username=USER, password=PASS, timeout=12,
                    banner_timeout=15, auth_timeout=15)
    except Exception as e:
        print("[FAIL] SSH 连接失败: %s" % e)
        print("       → 掌机可能已休眠断开 WiFi（文档中标记的『最大风险』）")
        return 2

    for name, cmd in CMDS:
        print("\n" + "-" * 62)
        print("[%s]" % name)
        print("  $ %s" % cmd)
        try:
            _, so, se = cli.exec_command(cmd, timeout=20)
            out = so.read().decode("utf-8", "replace").rstrip()
            err = se.read().decode("utf-8", "replace").rstrip()
            if out:
                for line in out.split("\n"):
                    print("  " + line)
            else:
                print("  (无输出)")
            if err:
                for line in err.split("\n")[:5]:
                    print("  ! " + line)
        except Exception as e:
            print("  [ERR] %s" % e)

    cli.close()
    print("\n" + "=" * 62)
    print("  探测完成")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
