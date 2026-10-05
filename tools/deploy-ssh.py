#!/usr/bin/env python3
"""deploy-ssh.py — 通过 SSH 远程部署到掌机（开发迭代用）

阶段 0-0 打通 SSH 后，编译产物可以直接推送到掌机，
不必每次拔卡插卡。开发循环从「分钟级」变成「秒级」。

用法：
    python tools/deploy-ssh.py --host 192.168.3.25              # 部署测试程序
    python tools/deploy-ssh.py --host 192.168.3.25 --run fbtest # 部署后直接运行
    python tools/deploy-ssh.py --host 192.168.3.25 --log        # 抓日志
"""

import argparse
import os
import posixpath
import sys
import time

import paramiko

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER, PASS = "root", "root"

# 掌机上的部署位置（由 launcher.sh 自算 BASE_PATH，等价于 TF1 根目录）
APPS = "/mnt/mmc/Roms/APPS"
PAYLOAD = f"{APPS}/PocketTransfer"


def sftp_put(cli, local, remote, mode=None):
    sftp = cli.open_sftp()
    try:
        sftp.put(local, remote)
        if mode is not None:
            sftp.chmod(remote, mode)
        print(f"  [上传] {os.path.basename(local)} -> {remote}"
              + (f"  (mode {oct(mode)})" if mode else ""))
    finally:
        sftp.close()


def sh(cli, cmd, timeout=60):
    _in, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    return o, e


def deploy_fbtest(cli):
    print("== 部署 fbtest ==")
    binary = os.path.join(REPO, "handheld", "build", "fbtest")
    if not os.path.isfile(binary):
        sys.exit(f"[错误] 找不到 {binary}，先编译")

    for d in (PAYLOAD, f"{PAYLOAD}/logs"):
        sh(cli, f"mkdir -p {d}")

    sftp_put(cli, binary, f"{PAYLOAD}/fbtest", 0o755)

    # 启动脚本直接复制桌面包里那份，改用正式 launcher.sh 的四必做点版本
    launcher = os.path.join(REPO, "handheld", "test", "fbtest.sh")
    sftp_put(cli, launcher, f"{APPS}/fbtest.sh", 0o755)

    # 清掉旧日志，方便看本次结果
    sh(cli, f"rm -f {PAYLOAD}/logs/stdout.log")
    print("  [清理] 旧日志已删除")


def deploy_full(cli, binary_name="PocketTransfer"):
    """部署正式程序（阶段 0-D 之后用）。"""
    print(f"== 部署 {binary_name} ==")
    binary = os.path.join(REPO, "handheld", "build", binary_name)
    if not os.path.isfile(binary):
        sys.exit(f"[错误] 找不到 {binary}，先编译")

    for d in (PAYLOAD, f"{PAYLOAD}/logs", f"{PAYLOAD}/lib", f"{APPS}/Imgs"):
        sh(cli, f"mkdir -p {d}")

    sftp_put(cli, binary, f"{PAYLOAD}/{binary_name}", 0o755)
    sftp_put(cli, os.path.join(REPO, "handheld", "launcher.sh"),
             f"{APPS}/{binary_name}.sh", 0o755)
    icon = os.path.join(REPO, "handheld", "assets", "icon.png")
    if os.path.isfile(icon):
        sftp_put(cli, icon, f"{APPS}/Imgs/{binary_name}.png")

    # 自带 SDL2 库（若有）
    libdir = os.path.join(REPO, "handheld", "build", "lib")
    if os.path.isdir(libdir):
        for fn in os.listdir(libdir):
            if fn.endswith((".so", ".so.0")):
                sftp_put(cli, os.path.join(libdir, fn), f"{PAYLOAD}/lib/{fn}", 0o755)

    sh(cli, f"rm -f {PAYLOAD}/logs/stdout.log")
    print("  [清理] 旧日志已删除")


def run_program(cli, prog="fbtest"):
    """后台运行程序，这样 SSH 不会因 sleep 3600 而挂住。"""
    print(f"\n== 运行 {prog} ==")
    script = f"{APPS}/fbtest.sh" if prog == "fbtest" else f"{APPS}/{prog}.sh"
    # setsid + 脱离 SSH 会话，程序继续在掌机屏幕上跑
    o, e = sh(cli, f"cd {PAYLOAD} && setsid ./{prog} > logs/stdout.log 2>&1 & sleep 1; echo started")
    print(f"  {o} {e}")
    time.sleep(2.5)
    o, e = sh(cli, f"cat {PAYLOAD}/logs/stdout.log 2>/dev/null || echo '(无日志)'")
    print("--- 程序输出 ---")
    print(o)
    if e:
        print(f"[stderr] {e}")


def show_log(cli, tail=40):
    print("--- logs/stdout.log ---")
    o, e = sh(cli, f"tail -{tail} {PAYLOAD}/logs/stdout.log 2>/dev/null || echo '(无日志)'")
    print(o)
    if e:
        print(f"[stderr] {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--full", action="store_true", help="部署正式程序")
    ap.add_argument("--run", metavar="PROG", help="部署后运行指定程序")
    ap.add_argument("--log", action="store_true", help="抓日志")
    ap.add_argument("--no-deploy", action="store_true", help="只运行/看日志，不重新上传")
    a = ap.parse_args()

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(a.host, username=USER, password=PASS, timeout=12,
                    banner_timeout=12, auth_timeout=12)
    except Exception as e:
        sys.exit(f"[错误] 连接 {a.host} 失败: {e}")
    print(f"[已连接] {a.host}\n" + "=" * 50)

    if not a.no_deploy:
        if a.full:
            deploy_full(cli)
        else:
            deploy_fbtest(cli)

    if a.run:
        run_program(cli, a.run)

    if a.log or not a.run:
        show_log(cli)

    cli.close()


if __name__ == "__main__":
    main()
