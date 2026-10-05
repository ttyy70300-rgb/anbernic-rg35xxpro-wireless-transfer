#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify-deploy.py —— 部署后校验（只读）

三件事，全部只读：
  1. 逐文件 MD5 比对（本地 vs 掌机）—— 用 shell 侧 md5sum
     （教训：SFTP 子系统的 chroot 根不是 /mnt/mmc，sftp.open 读不到）
  2. 掌机上逐文件语法检查（py_compile）—— 确保上传过程没损坏
  3. 干跑一次网络层：在掌机上绑定端口再立刻释放，
     证明端口可用且 net.py 能 import（不启动 UI）
"""
import hashlib
import os
import sys

import paramiko

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.3.25"
USER, PWD = "root", "root"
REMOTE = "/mnt/mmc/Roms/APPS/PocketTransfer"
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(os.path.dirname(HERE), "handheld")

FILES = ["boot.py", "fb.py", "main.py", "net.py", "sdl_display.py", "ui.py"]


def md5_local(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PWD, timeout=15,
                look_for_keys=False, allow_agent=False)

    def sh(cmd, t=30):
        _, o, e = cli.exec_command(cmd, timeout=t)
        return (o.read().decode("utf-8", "replace") +
                e.read().decode("utf-8", "replace")).strip()

    ok = True

    # ---- 1. MD5 ----
    print("=" * 62)
    print("  1. MD5 校验（shell 侧 md5sum，规避 SFTP chroot 问题）")
    print("=" * 62)
    remote_md5 = {}
    for fn in FILES:
        out = sh(f"md5sum {REMOTE}/{fn} 2>&1")
        remote_md5[fn] = out.split()[0] if out and " " in out else "?"

    for fn in FILES:
        lp = os.path.join(LOCAL, fn)
        if not os.path.exists(lp):
            print(f"  !! 本地缺 {fn}")
            ok = False
            continue
        lm = md5_local(lp)
        rm = remote_md5.get(fn, "?")
        good = (lm == rm)
        print(f"  {'OK ' if good else '!! '} {fn:18s} {lm[:12]}  {'==' if good else '!='}  {rm[:12]}")
        if not good:
            ok = False

    # ---- 2. 语法检查 ----
    print()
    print("=" * 62)
    print("  2. 掌机侧语法检查（py_compile）")
    print("=" * 62)
    out = sh(f"cd {REMOTE} && for f in *.py; do "
             f"python3 -m py_compile \"$f\" 2>&1 && echo \"OK  $f\" "
             f"|| echo \"FAIL $f\"; done")
    print(out)
    if "FAIL" in out:
        ok = False

    # ---- 3. 网络层干跑（只绑定再释放，不启动 UI） ----
    print()
    print("=" * 62)
    print("  3. 掌机上干跑网络层（绑定端口后立即释放）")
    print("=" * 62)
    probe = r'''
import sys, socket, time
sys.path.insert(0, "@REMOTE@")
try:
    import net
except Exception as e:
    print("IMPORT_FAIL", type(e).__name__, e); sys.exit(1)
print("net.py ok  MAGIC=0x{:04X} TCP={} UDP={}".format(net.MAGIC, net.TCP_PORT, net.UDP_BEACON_PORT))
try:
    t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    t.bind(("0.0.0.0", net.TCP_PORT)); t.listen(1)
    print("TCP {} bind OK".format(net.TCP_PORT)); t.close()
except Exception as e:
    print("TCP_BIND_FAIL", e); sys.exit(2)
try:
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    u.bind(("0.0.0.0", net.UDP_BEACON_PORT))
    print("UDP {} bind OK".format(net.UDP_BEACON_PORT)); u.close()
except Exception as e:
    print("UDP_BIND_FAIL", e); sys.exit(3)
bad = net.safe_join("/mnt/mmc", "../../../etc/passwd")
good = net.safe_join("/mnt/mmc", "Roms/APPS")
print("safe_join escape ->", bad)
print("safe_join normal ->", good)
# 顺带读出真机的 SDL 运行时版本（证明用运行时自报而非硬编码）
try:
    import sdl2
    v = sdl2.SDL_version(); sdl2.SDL_GetVersion(v)
    print("SDL runtime -> {}.{}.{}".format(v.major, v.minor, v.patch))
except Exception as e:
    print("SDL probe skip:", e)
print("PROBE_ALL_OK")
'''.replace("@REMOTE@", REMOTE)
    # 用 base64 传脚本，避免引号转义地狱
    import base64
    b64 = base64.b64encode(probe.encode("utf-8")).decode("ascii")
    out = sh(f"echo {b64} | base64 -d > /tmp/_pt_probe.py && "
             f"python3 /tmp/_pt_probe.py; rc=$?; rm -f /tmp/_pt_probe.py; "
             f"exit $rc", t=60)
    print(out)
    if "PROBE_ALL_OK" not in out:
        ok = False

    # ---- 4. 目录清单 ----
    print()
    print("=" * 62)
    print("  4. 掌机上的文件清单")
    print("=" * 62)
    print(sh(f"ls -la {REMOTE}/"))
    print()
    print(sh(f"ls -la /mnt/mmc/Roms/APPS/PocketTransfer.sh"))

    cli.close()
    print()
    print("=" * 62)
    print("  校验结果：" + ("全部通过 ✓" if ok else "存在问题 ✗"))
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
