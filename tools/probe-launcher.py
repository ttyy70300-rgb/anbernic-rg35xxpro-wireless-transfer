#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
搞清原厂"从菜单启动应用"的完整契约

已知：
  dmenu_ln 的 app_scheduling():
      if $CMD > /dev/null 2>&1; then      # dmenu.bin 退出
          if [ -f "$ACT" ]; then          # ACT=/tmp/.next
              if ! sh $ACT; then ... fi   # 执行 /tmp/.next 指向的脚本
      fi

要查清：
  1. app_scheduling 完整实现
  2. dmenu.bin 被用户选中某个 .sh 时，/tmp/.next 里被写的是什么
     （推测是 sh /mnt/mmc/Roms/APPS/xxx.sh）
  3. 我们能否模拟：写 /tmp/.next 然后让 dmenu 退出
  4. 我们的应用退出后，怎么把控制权还给 dmenu（重启 dmenu_ln）
  5. dmenu 退出时 fb0 模式会变回 640x480 吗
"""
import sys

import paramiko

USER, PWD = "root", "root"

SHELL = [
    ("dmenu_ln 全文", "cat -A /mnt/vendor/ctrl/dmenu_ln 2>/dev/null | sed 's/\\$$//' "
     "|| cat /mnt/vendor/ctrl/dmenu_ln"),
    ("loadapp.sh 里与 dmenu 启停相关的部分",
     "grep -n -A6 -B2 -E 'RunBin|StopBin|stopAPP|killall|dmenu' /mnt/vendor/ctrl/loadapp.sh"),
    ("当前 /tmp/.next 是否存在", "ls -la /tmp/.next 2>&1; echo '--- 内容:'; cat /tmp/.next 2>&1"),
    ("/tmp 下的启动契约文件", "ls -la /tmp/*.ini /tmp/.next /tmp/*app* 2>&1"),
    ("dmenu 的配置里应用列表从哪来",
     "ls -la /mnt/vendor/*.ini 2>&1 | head -20; echo '--- arcade-oem.csv 头部:'; "
     "head -5 /mnt/vendor/bin/arcade-oem.csv 2>&1"),
    ("dmenu.bin 的字符串里找 /tmp/.next 相关",
     "strings /mnt/vendor/bin/dmenu.bin 2>/dev/null | grep -E '^/tmp|\\.next|\\.ini$' | head -20"),
    ("找 dmenu 写 .next 的逻辑线索",
     "strings /mnt/vendor/bin/dmenu.bin 2>/dev/null | grep -iE 'APPS|\\.sh|Roms' | head -20"),
    ("stopAPP.ini 机制", "cat /tmp/stopAPP.ini 2>&1; echo '--- 谁写它:'; "
     "strings /mnt/vendor/bin/dmenu.bin 2>/dev/null | grep -i stopapp"),
]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)
    for t, c in SHELL:
        print(f">>> {t}")
        print("-" * 68)
        _, o, e = cli.exec_command(c, timeout=45)
        d = (o.read().decode("utf-8", "replace")
             + e.read().decode("utf-8", "replace")).strip()
        print(d or "(空)")
        print()
    cli.close()


if __name__ == "__main__":
    main()
