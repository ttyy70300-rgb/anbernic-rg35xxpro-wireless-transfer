#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
通用 SSH 命令执行器 —— 所有远程排查都走这个，避开 Bash 嵌套引号地狱

背景：本机 Bash 工具在传入含双引号的 python -c 内联代码时会截断命令。
      教训（已发生 4 次）：远程探测逻辑必须写成 .py 文件再执行，
      或者用本工具这样参数与代码分离的调用方式。

用法:
  python tools/rr.py "ls -la /tmp"
  python tools/rr.py --file /tmp/xxx.sh
  python tools/rr.py --stdin < local.sh
  python tools/rr.py --put local.txt /tmp/remote.txt
  python tools/rr.py --get /tmp/remote.txt local.txt
  python tools/rr.py --host 192.168.3.25 "cat /mnt/mmc/Roms/APPS/PocketTransfer/log.txt"
"""
import argparse
import os
import sys
import time

import paramiko

USER, PWD = "root", "root"
DEFAULT_HOST = "192.168.3.25"


def connect(host):
    """
    连接掌机。

    失败时打清楚原因和排查方向 —— 早前版本在这里静默吞掉异常，
    表现为"命令跑完什么都没输出"，反而更难查。
    """
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, username=USER, password=PWD, timeout=15,
                    look_for_keys=False, allow_agent=False)
    except Exception as e:
        print(f"!! 连不上 {host}: {type(e).__name__}: {e}")
        print("   排查方向：")
        print("     1. 掌机是否开机且 WiFi 已连（重连后 IP 可能变了）")
        print(f"     2. 掌机屏幕上『设置 → 网络』里显示的当前 IP 是多少")
        print(f"     3. 用 --host <新IP> 重试")
        print("     4. 掌机临时 SSH 服务是否已开启")
        try:
            cli.close()
        except Exception:
            pass
        sys.exit(2)
    return cli


def run(cli, cmd, timeout=60, show=True):
    """
    执行远端命令并把 stdout / stderr 全部读回来。

    坑（已踩）：
        paramiko 的 channel.recv() 在"当前没数据"时会**立刻抛 socket.timeout**，
        而不是等到 timeout 才抛。直接 `while True: ch.recv()` 会在第一条命令
        还没输出时就 break，表现为"命令跑了但什么都没打印"，极难排查。

    正确做法：
        用 recv_ready() / exit_status_ready() 轮询判断有没有数据，
        再 recv；等到通道关闭或整体超时为止。

    另外 stdout 必须读到 EOF 再读 stderr ——
    若先阻塞在 stderr 上，stdout 缓冲区写满会把远端进程卡死。
    """
    chan = cli.get_transport().open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)

    out_buf = bytearray()
    err_buf = bytearray()
    deadline = time.time() + timeout

    while True:
        if time.time() > deadline:
            break
        got = False
        # stdout / stderr 两个方向都要照看，
        # 避免任一方向的缓冲区写满把远端进程卡住
        if chan.recv_ready():
            out_buf.extend(chan.recv(65536))
            got = True
        if chan.recv_stderr_ready():
            err_buf.extend(chan.recv_stderr(65536))
            got = True
        if chan.exit_status_ready() and not chan.recv_ready() \
                and not chan.recv_stderr_ready():
            break
        if not got:
            time.sleep(0.02)

    # 收尾：把残留数据取干净
    while chan.recv_ready():
        out_buf.extend(chan.recv(65536))
    while chan.recv_stderr_ready():
        err_buf.extend(chan.recv_stderr(65536))

    try:
        rc = chan.recv_exit_status()
    except Exception:
        rc = -1
    chan.close()

    out = out_buf.decode("utf-8", "replace").rstrip()
    err = err_buf.decode("utf-8", "replace").rstrip()

    if show:
        if out:
            print(out)
        if err:
            print("--- stderr ---")
            print(err)

    return out, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", help="要在掌机上执行的命令")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--file", help="把本地脚本内容作为命令执行")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读命令")
    ap.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"))
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    cli = connect(args.host)
    try:
        if args.put:
            local, remote = args.put
            sftp = cli.open_sftp()
            sftp.put(local, remote)
            sftp.close()
            print(f"已上传 {local} -> {remote}")
            return

        if args.get:
            remote, local = args.get
            sftp = cli.open_sftp()
            sftp.get(remote, local)
            sftp.close()
            print(f"已下载 {remote} -> {local}")
            return

        if args.stdin:
            cmd = sys.stdin.read()
        elif args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                cmd = f.read()
        elif args.cmd:
            cmd = args.cmd
        else:
            ap.error("需要 cmd / --file / --stdin / --put / --get")

        run(cli, cmd, timeout=args.timeout)
    finally:
        cli.close()


if __name__ == "__main__":
    main()
