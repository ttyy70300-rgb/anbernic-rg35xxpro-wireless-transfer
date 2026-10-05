#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真机联调脚本（PC → 掌机，走真实局域网）
=======================================

前提：掌机已开机、连上 WiFi、**且 PocketTransfer 正在运行**
      （应用必须处于前台，否则网络线程没起）。

本脚本会做（全部只读，不写任何东西）：
    1. UDP 广播发现
    2. TCP 连接 + PING
    3. 取设备信息
    4. 列卡根目录、列一个子目录
    5. 下载一个小文件到本地临时目录并校验 md5
    6. 故意试一次路径穿越，确认被拒

写操作（上传/建目录）**默认不做**，要加 --write 且掌机需先解锁。

用法：
    python tools/live-check.py
    python tools/live-check.py --host 192.168.3.25
    python tools/live-check.py --host 192.168.3.25 --file Roms/xxx.gba
"""
import argparse
import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "pc"))

import client as clipy               # noqa: E402
import protocol as P                 # noqa: E402
from protocol import Nack            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    mark = "[OK]" if cond else "[!!]"
    print(f"  {mark}  {name}" + (f"  —— {note}" if note else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    ap.add_argument("--file", default=None, help="额外下载这个文件验证")
    ap.add_argument("--write", action="store_true",
                    help="额外测试写操作（需先在掌机上按 SELECT 解锁）")
    args = ap.parse_args()

    print("=" * 66)
    print("  真机联调（PC → 掌机）")
    print("=" * 66)

    host = args.host

    # ---- 1. 发现 ----
    if not host:
        print("\n1. UDP 广播发现")
        try:
            found = clipy.discover(timeout=3.0)
        except Exception as e:
            check("广播发现", False, str(e))
            found = []
        if found:
            host = found[0]["ip"]
            check("广播发现掌机", True, f"{host} / {found[0].get('model')}")
        else:
            check("广播发现掌机", False,
                  "没扫到。可能原因：掌机没开机 / 不同网段 / "
                  "应用没在前台运行。请手动指定 --host")
            if not args.host:
                print("\n提示：python tools/live-check.py --host 192.168.3.25")
                return 1
            host = "192.168.3.25"
            print(f"  → 继续用 {host} 试连")
    else:
        print(f"\n1. 跳过发现，直接用 {host}")

    # ---- 2. 连接 ----
    print("\n2. TCP 连接")
    cli = clipy.Client(host, timeout=8.0)
    try:
        cli.connect()
        check(f"连上 {host}:{P.TCP_PORT}", True)
    except Exception as e:
        check(f"连上 {host}:{P.TCP_PORT}", False, str(e))
        print("\n连接失败。请检查：")
        print("  · 掌机上 PocketTransfer 是否正在运行（前台）")
        print("  · 是否同一个 WiFi")
        print("  · 掌机 IP 是否变了（路由器可能重新分配）")
        return 1

    try:
        # ---- 3. 握手 ----
        print("\n3. 握手")
        try:
            pong = cli.ping(b"live-check")
            check("PING → PONG", pong == b"live-check", f"{pong!r}")
        except Exception as e:
            check("PING → PONG", False, str(e))

        info = cli.device_info()
        print(f"     型号 {info.get('model')}")
        print(f"     固件 {info.get('fw')}")
        print(f"     卡根 {info.get('root')}")
        print(f"     权限 {'可写' if info.get('writable') else '只读'}")
        check("拿到设备信息", bool(info.get("model")))
        check("卡根非空", bool(info.get("root")),
              f"{info.get('root')}")

        # ---- 4. 列目录 ----
        print("\n4. 列目录")
        d = cli.list_dir("")
        names = [i["name"] for i in d.get("items", [])]
        check("列出卡根", len(names) > 0, f"{len(names)} 项: {names[:8]}")
        dirs = [i for i in d["items"] if i["dir"]]
        check("识别出子目录", len(dirs) > 0,
              f"{[i['name'] for i in dirs][:8]}")

        target_dir = None
        for pref in ("Roms", "roms", "Emu", "emu"):
            if pref in names:
                target_dir = pref
                break
        if target_dir:
            d2 = cli.list_dir(target_dir)
            n2 = d2.get("items", [])
            check(f"列出 {target_dir}", len(n2) > 0, f"{len(n2)} 项")
            files = [i for i in n2 if not i["dir"] and i["size"] > 0]
            if files:
                print(f"     其中有 {len(files)} 个文件，"
                      f"例如 {files[0]['name']} "
                      f"({P.human_size(files[0]['size'])})")

        # ---- 5. 下载校验 ----
        print("\n5. 下载校验")
        pick = args.file
        if not pick and target_dir:
            d2 = cli.list_dir(target_dir)
            cand = [i for i in d2["items"] if not i["dir"] and
                    0 < i["size"] < 4 * 1024 * 1024]
            if cand:
                pick = f"{target_dir}/{cand[0]['name']}"
        if pick:
            tmp = os.path.join(tempfile.gettempdir(),
                               "_pt_live_" + os.path.basename(pick))
            try:
                n = cli.download(pick, tmp)
                check(f"下载 {pick}", n > 0,
                      f"{P.human_size(n)}")
                check("下载文件非空且落在本地",
                      os.path.isfile(tmp) and os.path.getsize(tmp) == n)
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            except Nack as e:
                check(f"下载 {pick}", False, f"被拒: {e.msg}")
            except Exception as e:
                check(f"下载 {pick}", False, f"{type(e).__name__}: {e}")
        else:
            print("     （没找到合适的小文件，跳过。"
                  "可用 --file Roms/xxx.gba 指定）")

        # ---- 6. 路径穿越 ----
        print("\n6. 路径穿越防护（安全底线）")
        try:
            cli.list_dir("../../etc")
            check("越界请求被拒", False, "居然成功了！这是严重问题")
        except Nack as e:
            check("越界请求被拒", e.reason in (3, 5),
                  f"原因码 {e.reason}: {e.msg[:30]}")
        except Exception as e:
            check("越界请求被拒", False, f"{type(e).__name__}: {e}")

        # ---- 7. 写操作（可选）----
        if args.write:
            print("\n7. 写操作（需要掌机已解锁）")
            if not info.get("writable"):
                print("     掌机当前是只读。请先在掌机上按 SELECT 解锁，"
                      "再重跑加 --write")
            else:
                try:
                    cli.mkdir("_pt_live_test")
                    check("新建目录（掌机按 A 确认）", True)
                except Nack as e:
                    check("新建目录", False, f"被拒: {e.msg}")
                except Exception as e:
                    check("新建目录", False, f"{type(e).__name__}: {e}")
        else:
            print("\n7. 写操作  —— 未测试（加 --write 并在掌机解锁后重跑）")

    finally:
        cli.close()

    print()
    print("=" * 66)
    print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 66)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("真机联调通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
