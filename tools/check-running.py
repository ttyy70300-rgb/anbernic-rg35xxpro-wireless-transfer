# -*- coding: utf-8 -*-
"""
检查掌机上 PocketTransfer 是否在运行（端口是否监听）。
只读操作，不做任何改动。
"""
import sys

try:
    import paramiko
except ImportError:
    print("需要 paramiko")
    sys.exit(2)

HOST = "192.168.3.25"
USER = "root"
PWD = "root"

CMDS = [
    ("监听端口", "netstat -ltnp 2>/dev/null | grep -E '48200|48211' || echo '(无监听)'"),
    ("相关进程", "ps -ef | grep -E 'main\\.py|PocketTransfer' | grep -v grep || echo '(无进程)'"),
    ("前台应用", "cat /tmp/cur_app 2>/dev/null || echo '(无)'"),
]

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    cli.connect(HOST, username=USER, password=PWD, timeout=15,
                allow_agent=False, look_for_keys=False)
except Exception as e:
    print(f"SSH 连接失败: {e}")
    sys.exit(1)

for title, cmd in CMDS:
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=15)
    out = stdout.read().decode("utf-8", "replace").strip()
    print(f"=== {title} ===")
    print(out or "(空)")
    print()

cli.close()
