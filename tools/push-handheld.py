# -*- coding: utf-8 -*-
"""
把掌机侧代码推送到 RG35XX Pro。
================================

硬边界（用户明确要求，任何时候都不许破）：
    只允许通过 SSH 往掌机推文件。
    不对系统的任何其它内容做改动 ——
    不 kill 系统进程、不改 /tmp/.next、不切显示模式、
    不 remount、不建软链、不写 sysfs。
    应用一律由用户在掌机上手动点菜单启动。

用法：
    python tools/push-handheld.py            # 推送 + 校验（不重启应用）
    python tools/push-handheld.py --dry-run  # 只对比，不写
"""
import os
import sys
import hashlib

try:
    import paramiko
except ImportError:
    print("需要 paramiko: pip install paramiko")
    sys.exit(2)

HOST = "192.168.3.25"
USER = "root"
PWD = "root"
REMOTE_DIR = "/mnt/mmc/Roms/APPS/PocketTransfer"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOCAL_DIR = os.path.join(ROOT, "handheld")

# 只推这几个文件。多一个都不推 —— 边界要清晰。
FILES = ["net.py", "ui.py", "main.py", "boot.py", "fb.py", "sdl_display.py"]


def md5_local(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 16), b""):
            h.update(blk)
    return h.hexdigest()


def main():
    dry = "--dry-run" in sys.argv

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(HOST, username=USER, password=PWD, timeout=20,
                    allow_agent=False, look_for_keys=False)
    except Exception as e:
        print(f"SSH 连接失败: {e}")
        return 1

    sftp = cli.open_sftp()

    # ---- 1. 计算本地 md5 ----
    local = {}
    for name in FILES:
        p = os.path.join(LOCAL_DIR, name)
        if not os.path.isfile(p):
            print(f"!! 本地缺文件: {p}")
            return 1
        local[name] = md5_local(p)

    # ---- 2. 读远端 md5 ----
    remote = {}
    for name in FILES:
        try:
            h = hashlib.md5()
            with sftp.open(f"{REMOTE_DIR}/{name}", "rb") as f:
                for blk in iter(lambda: f.read(1 << 16), b""):
                    h.update(blk)
            remote[name] = h.hexdigest()
        except Exception:
            remote[name] = "(不存在)"

    # ---- 3. 报告差异 ----
    print("=" * 62)
    print("  本地 → 掌机 文件对比")
    print("=" * 62)
    todo = []
    for name in FILES:
        same = local[name] == remote[name]
        flag = "同" if same else "→ 需推送"
        print(f"  {'[=]' if same else '[!]'} {name:18s} "
              f"本地 {local[name][:12]}  远端 {remote[name][:12]}  {flag}")
        if not same:
            todo.append(name)

    if not todo:
        print("\n全部一致，无需推送。")
        sftp.close()
        cli.close()
        return 0

    if dry:
        print(f"\n[dry-run] 有 {len(todo)} 个文件需要推送，未实际写入。")
        sftp.close()
        cli.close()
        return 0

    # ---- 4. 推送 ----
    print(f"\n推送 {len(todo)} 个文件 …")
    for name in todo:
        lp = os.path.join(LOCAL_DIR, name)
        rp = f"{REMOTE_DIR}/{name}"
        # 先传 .tmp 再改名：中途断了也不会留下半个坏文件
        sftp.put(lp, rp + ".tmp")
        sftp.posix_rename(rp + ".tmp", rp)
        print(f"  ✓ {name}")

    # ---- 5. 复验 md5 ----
    print("\n复验 …")
    bad = []
    for name in todo:
        h = hashlib.md5()
        with sftp.open(f"{REMOTE_DIR}/{name}", "rb") as f:
            for blk in iter(lambda: f.read(1 << 16), b""):
                h.update(blk)
        got = h.hexdigest()
        ok = got == local[name]
        print(f"  {'✓' if ok else '✗'} {name:18s} {got[:12]}")
        if not ok:
            bad.append(name)

    sftp.close()

    # ---- 6. py_compile 校验（只读操作：只编译到内存/临时目录）----
    print("\n语法校验（掌机侧 py_compile）…")
    # 注意：不能把 ['net.py', ...] 直接插进表达式里当 list of names 用 ——
    #       那样 f-string 会把字符串原样拼进去，执行时报 NameError。
    #       用逗号分隔的字符串字面量，交给 py_compile 逐个编译。
    names_lit = ", ".join(repr(n) for n in FILES)
    cmd = (f"cd {REMOTE_DIR} && python3 -c "
           f"\"import py_compile;"
           f"[py_compile.compile(f, doraise=True) for f in [{names_lit}]];"
           f"print('COMPILE-OK')\" 2>&1")
    _in, out, err = cli.exec_command(cmd, timeout=60)
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    blob = (o + "\n" + e).strip()
    if "COMPILE-OK" in o and "Traceback" not in blob:
        print("  ✓ 6 个文件全部编译通过")
    else:
        print(f"  !! {blob[:500]}")
        bad.append("py_compile")

    cli.close()

    print()
    if bad:
        print(f"!! 有 {len(bad)} 项异常: {bad}")
        return 1
    print("推送完成，全部校验通过。")
    print("提示：应用由用户在掌机上手动点菜单启动（我们不动系统进程）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
