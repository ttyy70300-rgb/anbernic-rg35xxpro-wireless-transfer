#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端回环测试（本机自测，不需要掌机）
=====================================

在本机同时跑：
    · 掌机端 NetThread（服务端）—— 在临时目录上开服务
    · PC 端 Client（客户端）—— 真实 TCP 连接

验证完整协议交互：列目录 / 下载 / 上传 / 新建目录 / 拒绝路径穿越。

【为什么要做这个】
    真机联调一轮很贵（推文件 → 掌机点菜单 → 手测 → 拍照）。
    本机回环能在几秒内跑完所有协议路径，
    真正上掌机时只剩"环境差异"这一类问题。

【关键：自动应答确认框】
    写操作会阻塞等用户按键（60 秒）。测试里用一个后台线程
    自动回答"允许"，就能把整条路径跑通。

用法：
    python tools/test-loopback.py
"""
import hashlib
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "handheld"))
sys.path.insert(0, os.path.join(ROOT, "pc"))

PASS, FAIL = [], []


def check(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[!!]'}  {name}" + (f"  —— {note}" if note else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _try_upload(cli, base, remote_rel, size=4096):
    """传一个小文件，成功返回 True。用于"会话是否还活着"的探测。"""
    import os as _os
    src = _os.path.join(base, "_probe_src.bin")
    with open(src, "wb") as f:
        f.write(_os.urandom(size))
    try:
        n = cli.upload(src, remote_rel)
        return n == size
    except Exception:
        return False


def main():
    print("=" * 66)
    print("  端到端回环测试（掌机服务端 ←→ PC 客户端）")
    print("=" * 66)

    # 用临时目录当"内存卡"
    base = tempfile.mkdtemp(prefix="pt-loop-")
    os.makedirs(os.path.join(base, "Roms", "GBA"), exist_ok=True)
    with open(os.path.join(base, "Roms", "GBA", "hello.gba"), "wb") as f:
        f.write(b"GBA-DATA-" * 4096)          # ~36KB
    with open(os.path.join(base, "Roms", "readme.txt"), "wb") as f:
        # ⚠️ 必须用二进制写 + 不写 \n：
        #    Windows 上文本模式会把 \n 静默转成 \r\n，导致"文件大小"
        #    与代码里 len("...") 对不上 —— 这个坑在第一次回环测试里
        #    让"文件项带正确大小"假失败了一次。
        f.write(b"hello pocket")
    README_LEN = 12
    ref_md5 = hashlib.md5(
        open(os.path.join(base, "Roms", "GBA", "hello.gba"), "rb").read()
    ).hexdigest()
    print(f"  假卡根: {base}")
    print(f"  参考文件 md5: {ref_md5}\n")

    import net as netmod
    import client as clipy

    # ---- 起服务端（真实 NetThread）----
    tcp_port = free_port()
    udp_port = free_port()
    netmod.TCP_PORT = tcp_port
    netmod.UDP_BEACON_PORT = udp_port

    dev = {
        "model": "RG35XX Pro (loopback)", "fw": "test", "ip": "127.0.0.1",
        "serial": "LOOP", "root": base, "screen": "640x480",
    }
    nt = netmod.NetThread(
        device_info_fn=lambda: dict(dev),
        base_path_fn=lambda: base,
        log=lambda m: None,
    )
    nt.start()
    nt.send_command({"act": "root", "path": base})

    # ---- 自动应答确认框 ----
    auto = {"allow": True, "seen": 0}

    def auto_confirm():
        while nt.is_alive():
            try:
                ev = nt.events.get(timeout=0.2)
            except Exception:
                continue
            if ev.get("kind") == "confirm":
                auto["seen"] += 1
                box = ev.get("box")
                ev["box"]["ok"] = auto["allow"]
                ev["ev"].set()
            elif ev.get("kind") == "put_done":
                pass

    threading.Thread(target=auto_confirm, daemon=True).start()

    # 等监听就绪
    ok = False
    for _ in range(50):
        try:
            s = socket.create_connection(("127.0.0.1", tcp_port), timeout=0.3)
            s.close()
            ok = True
            break
        except Exception:
            time.sleep(0.1)
    if not ok:
        print("!! 服务端没起来")
        return 1
    print("1. 服务端启动")
    check("TCP 监听已就绪", ok, f"端口 {tcp_port}")
    print()

    cli = clipy.Client("127.0.0.1", port=tcp_port, timeout=10.0)

    # ---- 2. 连接 + 握手 ----
    print("2. 连接与握手")
    try:
        cli.connect()
        check("TCP 连接成功", True)
    except Exception as e:
        check("TCP 连接成功", False, str(e))
        return 1

    try:
        pong = cli.ping(b"hi")
        check("PING → PONG", pong == b"hi", f"回显 {pong!r}")
    except Exception as e:
        check("PING → PONG", False, str(e))

    try:
        info = cli.device_info()
        check("GET_DEVICE_INFO 有返回", bool(info.get("model")),
              f"model={info.get('model')}")
        check("设备信息含 root", info.get("root") == base,
              f"root={info.get('root')}")
        check("设备信息含 writable 字段", "writable" in info)
    except Exception as e:
        check("GET_DEVICE_INFO", False, str(e))
    print()

    # ---- 3. 只读锁定：写操作必须被拒 ----
    print("3. 只读锁定（★ 安全边界）")
    nt.set_writable(False)
    time.sleep(0.2)
    try:
        cli.upload(__file__, "Roms/should_fail.txt")
        check("只读时上传被拒绝", False, "居然成功了！")
    except clipy.Nack as e:
        check("只读时上传被拒绝", e.reason == 1,
              f"原因码 {e.reason}: {e.msg[:30]}")
    except Exception as e:
        check("只读时上传被拒绝", False, f"{type(e).__name__}: {e}")
    try:
        cli.mkdir("Roms/NoWay")
        check("只读时新建目录被拒绝", False, "居然成功了！")
    except clipy.Nack as e:
        check("只读时新建目录被拒绝", e.reason == 1,
              f"原因码 {e.reason}: {e.msg[:30]}")
    except Exception as e:
        check("只读时新建目录被拒绝", False, f"{type(e).__name__}: {e}")
    print()

    # ---- 4. 列目录 ----
    print("4. 列目录")
    try:
        d = cli.list_dir("")
        names = [i["name"] for i in d["items"]]
        check("根目录能列出", "Roms" in names, f"{names}")
        check("返回项含 dir/size/mtime",
              all(k in d["items"][0] for k in ("name", "dir", "size", "mtime")))
    except Exception as e:
        check("根目录能列出", False, str(e))

    try:
        d2 = cli.list_dir("Roms")
        n2 = [i["name"] for i in d2["items"]]
        check("子目录 Roms 能列出", "GBA" in n2 and "readme.txt" in n2,
              f"{sorted(n2)}")
        gba = [i for i in d2["items"] if i["name"] == "GBA"][0]
        check("目录项标记为 dir", gba["dir"] is True)
        txt = [i for i in d2["items"] if i["name"] == "readme.txt"][0]
        check("文件项带正确大小", txt["size"] == README_LEN,
              f"期望 {README_LEN} 实际 {txt['size']}")
    except Exception as e:
        check("子目录 Roms 能列出", False, str(e))
    print()

    # ---- 5. 路径穿越必须被挡 ----
    print("5. 路径穿越防护（★ 安全边界）")
    # 说明两种情况：
    #   a) 带 ../ 的 —— realpath 解析后跑出卡根 → NAK_DENIED(3)
    #   b) 带前导 / 的 —— safe_join 会剥掉前导 /（容忍客户端发
    #      "绝对风格"路径），于是 "/etc/passwd" 变成卡根下的
    #      "etc/passwd"。卡根里没这个目录 → NAK_BAD(5)。
    #      两种都**安全**：都没跑出卡根。断言接受 3 或 5。
    for evil in ("../../etc/passwd", "/etc/passwd", "Roms/../../etc/shadow",
                 "..\\..\\windows\\win.ini"):
        try:
            cli.list_dir(evil)
            check(f"拒绝 {evil}", False, "居然成功了！")
        except clipy.Nack as e:
            check(f"拒绝 {evil}", e.reason in (3, 5),
                  f"原因码 {e.reason}（3=越界 5=非法路径，都安全）")
        except Exception as e:
            check(f"拒绝 {evil}", False, f"{type(e).__name__}: {e}")

    # 更强的一条：确认越界后**真的没读到卡外内容**
    try:
        d = cli.list_dir("../../")
        names = [i["name"] for i in d.get("items", [])]
        leaked = any(n in ("etc", "usr", "windows", "Windows") for n in names)
        check("★ 上行目录未泄露到卡外", not leaked, f"列出 {names[:5]}")
    except clipy.Nack:
        check("★ 上行目录未泄露到卡外", True, "直接被拒")
    except Exception as e:
        check("★ 上行目录未泄露到卡外", False, f"{type(e).__name__}: {e}")
    print()

    # ---- 6. 下载 ----
    print("6. 下载文件")
    dl = os.path.join(base, "_dl_test.gba")
    got_bytes = [0]

    def prog(g, t):
        got_bytes[0] = g

    try:
        n = cli.download("Roms/GBA/hello.gba", dl, on_progress=prog)
        real = hashlib.md5(open(dl, "rb").read()).hexdigest()
        check("下载返回字节数正确", n == 36864, f"{n}")
        check("★ 下载内容 md5 与源文件一致", real == ref_md5,
              f"{real}")
        check("进度回调被调用", got_bytes[0] == n)
    except Exception as e:
        check("下载文件", False, f"{type(e).__name__}: {e}")
    print()

    # ---- 7. 上传（需确认）----
    print("7. 上传文件（★ 掌机确认机制）")
    nt.set_writable(True)
    time.sleep(0.3)
    auto["allow"] = True
    src = os.path.join(base, "_up_src.bin")
    with open(src, "wb") as f:
        f.write(os.urandom(150 * 1024))       # 跨多个 64KB 块
    up_md5 = hashlib.md5(open(src, "rb").read()).hexdigest()
    seen_before = auto["seen"]
    notices = []

    # ★ PC 端通过 on_notice 回调接收"掌机要你动手"的通知。
    #   这里装一个收集器，验证上传前确实收到了 NOTICE(confirm)。
    cli.on_notice = lambda kind, data: notices.append((kind, data))

    try:
        n = cli.upload(src, "Roms/uploaded.bin")
        dst = os.path.join(base, "Roms", "uploaded.bin")
        check("上传返回字节数正确", n == 153600, f"{n}")
        check("目标文件已落盘", os.path.isfile(dst))
        if os.path.isfile(dst):
            got = hashlib.md5(open(dst, "rb").read()).hexdigest()
            check("★ 上传内容 md5 与源文件一致", got == up_md5, f"{got}")
        check("掌机侧弹过确认框", auto["seen"] > seen_before,
              f"累计 {auto['seen']} 次")
        # ★ 用户实测反馈第 5 点：需要掌机确认时 PC 端应收到提示
        check("★ PC 端收到 NOTICE(confirm)（可据此提示用户去掌机按 A）",
              any(k == "confirm" for k, _ in notices),
              f"收到 {[k for k, _ in notices]}")
    except Exception as e:
        check("上传文件", False, f"{type(e).__name__}: {e}")
    print()

    # ---- 8. 用户拒绝上传 ----
    print("8. 用户在掌机上拒绝上传")
    auto["allow"] = False
    try:
        cli.upload(src, "Roms/rejected.bin")
        check("拒绝后上传应失败", False, "居然成功了！")
    except clipy.Nack as e:
        check("★ 拒绝后返回 NAK", e.reason == 2,
              f"原因码 {e.reason}: {e.msg[:24]}")
        check("拒绝后目标文件未创建",
              not os.path.exists(os.path.join(base, "Roms", "rejected.bin")))
    except Exception as e:
        check("拒绝后上传应失败", False, f"{type(e).__name__}: {e}")
    auto["allow"] = True
    print()

    # ---- 9. 新建目录 ----
    print("9. 新建目录")
    try:
        cli.mkdir("Roms/NewFolder")
        check("新建目录成功",
              os.path.isdir(os.path.join(base, "Roms", "NewFolder")))
    except Exception as e:
        check("新建目录成功", False, f"{type(e).__name__}: {e}")
    print()

    # ---- 9b. ★ 删除文件（用户反馈 #5：双端确认）----
    print("9b. 删除文件（★ 双端确认链路）")
    victim = os.path.join(base, "Roms", "to_delete.txt")
    with open(victim, "wb") as f:
        f.write(b"delete me")
    seen_before = auto["seen"]
    try:
        cli.delete("Roms/to_delete.txt")
        check("删除返回成功", True)
        check("★ 文件已从掌机消失", not os.path.exists(victim))
        check("★ 掌机侧弹过确认框（第二道确认）",
              auto["seen"] > seen_before, f"seen={auto['seen']}")
    except Exception as e:
        check("删除文件", False, f"{type(e).__name__}: {e}")

    # 路径穿越：删除也不许跑出卡根
    try:
        cli.delete("../../etc/passwd")
        check("★ 删除拒绝路径穿越", False, "居然成功了！")
    except clipy.Nack as e:
        check("★ 删除拒绝路径穿越", e.reason in (3, 5),
              f"原因码 {e.reason}")
    except Exception as e:
        check("★ 删除拒绝路径穿越", False, f"{type(e).__name__}: {e}")

    # 目标是目录 → 明确 NAK_IS_DIR(6)，不能误删整棵树
    try:
        cli.delete("Roms/GBA")
        check("★ 删除目录被拒绝（防误删整棵树）", False, "居然成功了！")
    except clipy.Nack as e:
        check("★ 删除目录被拒绝（防误删整棵树）", e.reason == 6,
              f"原因码 {e.reason}: {e.msg[:30]}")
    except Exception as e:
        check("★ 删除目录被拒绝（防误删整棵树）", False,
              f"{type(e).__name__}: {e}")
    check("★ 目录仍然完好",
          os.path.isdir(os.path.join(base, "Roms", "GBA")))

    # 文件不存在 → 也应是 NAK，而不是静默成功
    try:
        cli.delete("Roms/not_here_at_all.bin")
        check("删除不存在的文件被拒绝", False, "居然成功了！")
    except clipy.Nack as e:
        check("删除不存在的文件被拒绝", e.reason == 3,
              f"原因码 {e.reason}")
    except Exception as e:
        check("删除不存在的文件被拒绝", False, f"{type(e).__name__}: {e}")

    # ★ 用户在掌机上拒绝删除 → 文件必须还在
    victim2 = os.path.join(base, "Roms", "keep_me.txt")
    with open(victim2, "wb") as f:
        f.write(b"keep")
    auto["allow"] = False
    try:
        cli.delete("Roms/keep_me.txt")
        check("拒绝后删除应失败", False, "居然成功了！")
    except clipy.Nack as e:
        check("★ 拒绝删除返回 NAK", e.reason == 2,
              f"原因码 {e.reason}: {e.msg[:24]}")
        check("★★ 拒绝后文件依然存在（最关键的一条）",
              os.path.exists(victim2))
    except Exception as e:
        check("拒绝后删除应失败", False, f"{type(e).__name__}: {e}")
    auto["allow"] = True
    print()

    # ---- 10. 多块大文件（跨块完整性）----
    print("10. 大文件跨块传输")
    big = os.path.join(base, "_big.bin")
    with open(big, "wb") as f:
        f.write(os.urandom(1024 * 1024 + 7))   # 1MB+ 故意不是整块
    big_md5 = hashlib.md5(open(big, "rb").read()).hexdigest()
    try:
        n = cli.upload(big, "Roms/big.bin")
        dst = os.path.join(base, "Roms", "big.bin")
        got = hashlib.md5(open(dst, "rb").read()).hexdigest()
        check("1MB 文件上传字节数正确", n == 1024 * 1024 + 7, f"{n}")
        check("★ 1MB 文件 md5 一致（跨 17 个块）", got == big_md5)
    except Exception as e:
        check("大文件上传", False, f"{type(e).__name__}: {e}")
    print()

    # ---- 11. ★ 空闲保活（"传完文件连接就断"的回归防线）----
    print("11. 空闲保活与传输后连接存活")
    # 【背景】早期版本掌机侧对整条连接只设 20 秒超时，用户传完一个
    #         文件去翻下一个，超 20 秒连接就被踢掉。
    #         修法有两半：① 掌机把空闲超时放宽到 IDLE_TIMEOUT
    #                     ② PC 端周期性心跳 keepalive()
    #         这里同时验证这两半都还在。
    check("★ 掌机空闲超时是长值（不是 20 秒那种短超时）",
          getattr(netmod, "IDLE_TIMEOUT", 0) >= 300,
          f"IDLE_TIMEOUT={getattr(netmod, 'IDLE_TIMEOUT', None)}")
    check("★ 传输中超时短于空闲超时（能快速发现死链）",
          0 < getattr(netmod, "TRANSFER_TIMEOUT", 0)
          < getattr(netmod, "IDLE_TIMEOUT", 0),
          f"TRANSFER={getattr(netmod, 'TRANSFER_TIMEOUT', None)} "
          f"< IDLE={getattr(netmod, 'IDLE_TIMEOUT', None)}")
    check("★ PC 端有 keepalive() 心跳方法",
          hasattr(cli, "keepalive"))

    # 传一个小文件后，保持空闲并做几次心跳，连接必须一直可用
    probe_host = "Roms/keepalive-probe.txt"
    ka_src = os.path.join(base, "_ka.txt")
    with open(ka_src, "wb") as f:
        f.write(b"keepalive probe")
    try:
        cli.upload(ka_src, probe_host)
        check("保活测试：上传后连接仍可写", True)
    except Exception as e:
        check("保活测试：上传后连接仍可写", False, f"{type(e).__name__}: {e}")

    # 模拟"用户停在那儿不动"：连续心跳 3 轮，每轮之间空转一下
    ka_ok = True
    for _ in range(3):
        time.sleep(0.3)
        if not cli.keepalive():
            ka_ok = False
            break
    check("★ 空闲期间连续心跳全部成功", ka_ok)
    check("★ 心跳之后连接仍然可用（列目录）",
          isinstance(cli.list_dir(""), dict))
    check("★ 传输后再次上传仍然成功（会话未被断开）",
          _try_upload(cli, base, "Roms/after-keepalive.bin"))
    print()

    # ---- 11b. 运行时切可写状态，连接必须活着（用户实测反馈 #2）----
    #
    # 【背景】用户原话：
    #     "现在按了 select 解锁后，两端的连接会断开"
    #
    #   根因有两条，这里各验证一次：
    #     ① 掌机侧：set_writable 本身只是改个 bool，不该碰 socket。
    #        但旧版 PC 端在传输刚结束时并发发起
    #        remote_reload() + _refresh_info() + _heartbeat()，
    #        三个线程抢一条 socket → 应答串台 → 双双卡死 → reset。
    #        修法：Client._io_lock 串行化。
    #        这里用"多线程并发打同一条连接"来复现这个场景。
    #     ② 退出时 net.join() 报 'Event' object is not callable，
    #        根因是 _stop 遮蔽了 Thread._stop()（已在 12 节验证）。
    print("11b. 切可写状态时连接必须存活（用户反馈 #2）")
    try:
        nt.set_writable(True)
        time.sleep(0.2)
        ok_after_w = isinstance(cli.list_dir(""), dict)
        check("★ 掌机上按 SELECT 解锁后，连接仍然可用", ok_after_w)
        nt.set_writable(False)
        time.sleep(0.2)
        check("★ 再按 SELECT 锁定后，连接仍可用",
              isinstance(cli.list_dir(""), dict))
        nt.set_writable(True)
        time.sleep(0.2)
    except Exception as e:
        check("★ 掌机上按 SELECT 解锁后，连接仍然可用", False,
              f"{type(e).__name__}: {e}")

    # 并发打同一条连接 —— 这是"按 SELECT 后断连"的真实触发姿势：
    # 传输刚结束那一瞬间，UI 同时发起 列目录 + 取信息 + 心跳。
    conc_err = []
    conc_ok = [0]

    def _hammer(fn):
        try:
            fn()
            conc_ok[0] += 1
        except Exception as e:      # noqa: BLE001
            conc_err.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=_hammer,
                           args=(lambda: cli.list_dir(""),))
          for _ in range(3)]
    ts += [threading.Thread(target=_hammer,
                            args=(lambda: cli.device_info(),))
           for _ in range(3)]
    ts += [threading.Thread(target=_hammer,
                            args=(lambda: cli.keepalive(),))
           for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)

    check("★★ 多线程并发访问同一条连接不串台（RLock 生效）",
          not conc_err,
          f"10 个并发请求，失败 {len(conc_err)} 个"
          + (f"：{conc_err[:2]}" if conc_err else ""))
    check("  └ 并发之后连接依然健康",
          isinstance(cli.list_dir(""), dict))
    print()

    # ---- 11c. ★ 第六轮反馈 #1：掌机按 SELECT 切读写，PC 必须收到 ----
    #
    # 【背景】用户原话：
    #     "按下 select 键，win11 端这里能检测到，右上角也有相应提示，
    #      但是再按一下 select 键，win11 这边就没有同步状态。"
    #
    #   根因：set_writable() 只改本地 bool，从不通知对端；
    #         而 PC 的 on_notice 只在自己发请求、等应答期间被触发，
    #         掌机空闲时推过来的通知根本没人接。
    #
    #   修法两半，这里各测一次：
    #     ① 掌机 set_writable → 主动推 OP_NOTICE(perm)
    #     ② PC keepalive → 用 GET_DEVICE_INFO 把 writable 顺回来
    print("11c. 第六轮反馈 #1：掌机切读写后 PC 端能同步")
    notices = []
    # ⚠️ 签名是 (kind, data) 两个参数 —— 不是单个 dict。
    #    第一版写成单参数 lambda，回调被 _fire_notice 的 try/except
    #    静默吞掉，表现为"通知一条都没收到"，白查了十分钟。
    cli.on_notice = lambda kind, d: notices.append(dict(d, _kind=kind))

    def _sync_perm(on):
        """
        模拟"掌机按 SELECT"：改状态 + 推通知，
        然后在 PC 侧像真实 UI 那样轮询一下，把通知和心跳都走一遍。
        """
        nt.set_writable(on)
        got = None
        # 掌机是 0.5s 轮询发通知，给够时间
        deadline = time.time() + 4.0
        while time.time() < deadline:
            try:
                cli.device_info()      # 顺带读一次，也把待收的通知带回来
            except Exception:
                pass
            for n in notices:
                if n.get("kind") == "perm" and n.get("writable") == on:
                    got = n
                    break
            if got:
                break
            time.sleep(0.2)
        return got

    try:
        # ⚠️ 必须先明确置到 False 再切 True。
        #    set_writable 只在**状态真的变了**时才发通知
        #    （changed = (old != new)），而 11b 节结束时铺的是 True，
        #    直接 set_writable(True) 是空操作 → 静默不发 → 测试假失败。
        #    这其实正是期望行为：状态没变就不该打扰 PC。
        nt.set_writable(False)
        time.sleep(0.6)
        notices.clear()
        n_on = _sync_perm(True)
        check("★★ 解锁（按 SELECT 切可写）时 PC 收到 perm 通知",
              n_on is not None,
              f"收到 {len(notices)} 条通知" if not n_on else "")
        # 关键回归点：再切回来也必须能收到 —— 用户就是卡在这一步
        notices.clear()
        n_off = _sync_perm(False)
        check("★★ 再按一次 SELECT（切回锁定）PC 也能收到 perm 通知",
              n_off is not None,
              f"收到 {len(notices)} 条通知" if not n_off else "")
        check("★ perm 通知里带 hint 文案",
              bool((n_off or {}).get("hint")) if n_off else False,
              (n_off or {}).get("hint", ""))
        # 状态没变时不该重复打扰 PC
        notices.clear()
        nt.set_writable(False)
        time.sleep(0.6)
        check("★ 状态没变时不重复推通知（避免刷屏）",
              not notices, f"收到 {len(notices)} 条")

        # ② 心跳兜底：即使通知丢了，keepalive 也得把 writable 带回来
        nt.set_writable(True)
        time.sleep(0.1)
        ok = cli.keepalive()
        info = getattr(cli, "info", {}) or {}
        check("★★ keepalive 能把最新 writable 带回来（兜底通道）",
              ok and info.get("writable") is True,
              f"writable={info.get('writable')!r}")
        nt.set_writable(False)
        time.sleep(0.1)
        cli.keepalive()
        info = getattr(cli, "info", {}) or {}
        check("  └ 反向（切回锁定）同样能带回",
              info.get("writable") is False,
              f"writable={info.get('writable')!r}")
        nt.set_writable(True)          # 还原，后面章节还要用
        time.sleep(0.2)
    except Exception as e:
        check("★★ 掌机按 SELECT 切状态时 PC 能收到通知", False,
              f"{type(e).__name__}: {e}")
    cli.on_notice = None
    print()

    # ---- 12. 断线场景 ----
    print("12. 客户端断开")
    try:
        cli.close()
        check("正常 BYE 断开不抛异常", True)
    except Exception as e:
        check("正常 BYE 断开不抛异常", False, str(e))
    time.sleep(0.3)
    check("服务端回到 idle", nt.state in ("idle", "connected"),
          f"state={nt.state}")

    nt.stop()
    # ★★ join 曾经必炸（用户实测反馈 #2 的连带问题）：
    #     NetThread 里 `self._stop = threading.Event()` 把
    #     threading.Thread._stop() 遮蔽了，join 收尾要调它 → TypeError。
    #     这里在**真正 start 过线程之后**再 join，正是旧版会炸的姿势。
    try:
        nt.join(timeout=3)
        check("★★ net.join() 不再报 'Event' object is not callable",
              True)
    except Exception as e:
        check("★★ net.join() 不再报 'Event' object is not callable",
              False, f"{type(e).__name__}: {e}")
    check("网络线程已退出", not nt.is_alive())
    print()

    # 清理
    try:
        shutil.rmtree(base, ignore_errors=True)
    except Exception:
        pass

    print("=" * 66)
    print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 66)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过：端到端协议交互正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
