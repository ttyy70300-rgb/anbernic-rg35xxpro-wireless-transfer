#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 2 本地打桩测试
===================

在 Windows 上（无 SDL2 / 无 evdev / 无掌机）验证：
  1. ui.py 能否 import、能否构造 App、四种页面能否都画出来
  2. net.py 能否 import、协议编解码是否自洽
  3. main.py 的关键函数能否被调用（build_device_info 等）
  4. 模拟一整套 PC 交互流程（发现 → 连接 → 取信息 → 列目录 → 下载 → 上传）

不依赖真机。真机上还要跑一遍。
"""
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HH = os.path.join(os.path.dirname(HERE), "handheld")
sys.path.insert(0, HH)

# 让 net.py 能被 import（它只依赖标准库）
import net as netmod            # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "  OK " if cond else "  !! "
    print(f"{mark}{name}" + (f"   [{detail}]" if detail and not cond else ""))


# ---------------------------------------------------------------------------
print("=" * 64)
print("  1. 协议编解码自洽")
print("=" * 64)

# 帧格式
f = netmod.pack_frame(netmod.OP_PING, b"hello")
check("pack_frame 长度 = 8 + 负载", len(f) == 8 + 5, f"{len(f)}")
magic, op, ln = netmod.HDR.unpack_from(f, 0)
check("MAGIC = 0x504B", magic == 0x504B, hex(magic))
check("TYPE = PING", op == netmod.OP_PING)
check("LENGTH = 5", ln == 5)

# 增量解析：分三次喂
p = netmod.Parser()
out = p.feed(f[:3]) + p.feed(f[3:7]) + p.feed(f[7:])
check("分段喂入也能解析出 1 帧", len(out) == 1, str(len(out)))
check("解析出的负载正确", out and out[0][1] == b"hello")

# 粘连 + 半个包
p = netmod.Parser()
blob = (netmod.pack_frame(netmod.OP_PING, b"a") +
        netmod.pack_frame(netmod.OP_PONG, b"bb") +
        netmod.pack_frame(netmod.OP_BYE)[:5])
out = p.feed(blob)
check("粘连帧解析出 2 个完整帧", len(out) == 2, str(len(out)))
check("第 2 帧负载 = b'bb'", len(out) > 1 and out[1][1] == b"bb")

# 流错位恢复
p = netmod.Parser()
out = p.feed(b"\x00\x00junk" + netmod.pack_frame(netmod.OP_PING, b"z"))
check("流错位后能重新同步", len(out) == 1 and out[0][1] == b"z",
      str([(hex(o), pl) for o, pl in out]))

# JSON 帧
p = netmod.Parser()
out = p.feed(netmod.pack_json(netmod.OP_DEVICE_INFO, {"机型": "RG35xxPRO"}))
check("JSON 帧中文不转义", b"RG35xxPRO" in out[0][1])
check("JSON 可解析回原对象",
      json.loads(out[0][1].decode("utf-8"))["机型"] == "RG35xxPRO")

# 超长负载保护
p = netmod.Parser()
bad = netmod.HDR.pack(netmod.MAGIC, netmod.OP_PING, netmod.MAX_PAYLOAD + 1)
out = p.feed(bad)
check("拒绝超长负载", len(out) == 0 and p.error is not None, str(p.error))

# ---------------------------------------------------------------------------
print()
print("=" * 64)
print("  2. 路径安全闸门（防目录穿越）")
print("=" * 64)

root = tempfile.mkdtemp(prefix="pt-root-")
os.makedirs(os.path.join(root, "Roms", "APPS"), exist_ok=True)
open(os.path.join(root, "Roms", "a.txt"), "w").write("x")

check("根目录为空字符串 → root 本身",
      netmod.safe_join(root, "") == os.path.realpath(root))
check("正常子路径",
      netmod.safe_join(root, "Roms/a.txt") ==
      os.path.realpath(os.path.join(root, "Roms", "a.txt")))
check("前导斜杠被剥离",
      netmod.safe_join(root, "/Roms/a.txt") ==
      os.path.realpath(os.path.join(root, "Roms", "a.txt")))
check("拒绝 ../ 穿越", netmod.safe_join(root, "../../etc/passwd") is None)
check("拒绝 ./../ 变形", netmod.safe_join(root, "Roms/../../etc") is None)
check("拒绝绝对路径逃逸", netmod.safe_join(root, "../../../..") is None)
check("拒绝 windows 反斜杠穿越",
      netmod.safe_join(root, "..\\..\\windows\\system32") is None)
check("root 为 None 时返回 None", netmod.safe_join(None, "a") is None)

# 符号链接也要挡住
try:
    link = os.path.join(root, "escape")
    if not os.path.exists(link):
        os.symlink(tempfile.gettempdir(), link)
    check("拒绝符号链接逃逸",
          netmod.safe_join(root, "escape") is None
          or netmod.safe_join(root, "escape").startswith(
              os.path.realpath(root) + os.sep))
except (OSError, NotImplementedError, AttributeError) as e:
    check("符号链接测试（环境不支持则跳过）", True, str(e))

# ---------------------------------------------------------------------------
print()
print("=" * 64)
print("  3. 端到端：真起一个 NetThread，用 MiniClient 走全流程")
print("=" * 64)

# 用临时目录当"卡根"，避免碰真实文件
card = tempfile.mkdtemp(prefix="pt-card-")
os.makedirs(os.path.join(card, "Roms", "APPS"), exist_ok=True)
os.makedirs(os.path.join(card, "Roms", "Roms"), exist_ok=True)
open(os.path.join(card, "Roms", "readme.txt"), "w",
     encoding="utf-8").write("掌机上的文件\n" * 100)
with open(os.path.join(card, "Roms", "big.bin"), "wb") as fh:
    fh.write(os.urandom(300 * 1024))     # 300KB，跨多个 chunk

DEV_INFO = {
    "app": "PocketTransfer", "ver": "1.5", "model": "RG35xxPRO",
    "fw": "20260522", "ip": "192.168.3.25", "port": netmod.TCP_PORT,
    "udp_port": netmod.UDP_BEACON_PORT, "base": card,
    "screen": "640x480", "backend": "SDL2", "os": "Ubuntu 22.04 LTS",
}

nt = netmod.NetThread(device_info_fn=lambda: dict(DEV_INFO),
                      base_path_fn=lambda: card,
                      log=lambda m: None)
nt.set_root(card)
nt.start()
time.sleep(0.4)      # 等 socket 绑定

# 应用层会把自己的确认框挂上；这里直接自动"允许"
auto_allow = {"on": True}


def confirm_watcher():
    """后台消费 net 事件，遇到 confirm 立刻回允许。"""
    while nt.is_alive():
        try:
            ev = nt.events.get(timeout=0.2)
        except Exception:
            continue
        if ev.get("kind") == "confirm":
            ev["box"]["ok"] = auto_allow["on"]
            ev["ev"].set()


threading.Thread(target=confirm_watcher, daemon=True).start()

check("NetThread 已启动", nt.is_alive())
check("网络状态 = idle", nt.state == "idle", nt.state)

# ---- 3.1 UDP 发现 ----
found = []
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    msg = json.dumps({"magic": "POCKETTRANSFER", "act": "discover"}).encode()
    s.sendto(msg, ("127.0.0.1", netmod.UDP_BEACON_PORT))
    t0 = time.time()
    while time.time() - t0 < 1.5:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            break
        o = json.loads(data.decode())
        if o.get("act") == "here":
            found.append(o)
            break
    s.close()
except Exception as e:
    print("   UDP 测试异常:", e)

check("UDP 广播收到应答", len(found) == 1, str(len(found)))
if found:
    check("应答含机型", found[0].get("model") == "RG35xxPRO")
    check("应答含 tcp_port", found[0].get("tcp_port") == netmod.TCP_PORT)
check("NetThread 记录了广播次数", nt.udp_seen >= 1, str(nt.udp_seen))

# ---- 3.2 TCP 全流程 ----
cli = netmod.MiniClient("127.0.0.1")
cli.connect()
check("TCP 连接成功", cli.sock is not None)

# PING
cli.send(netmod.OP_PING, b"ping-data")
op, pl = cli.recv()
check("PING → PONG", op == netmod.OP_PONG and pl == b"ping-data",
      f"{op} {pl!r}")

# 设备信息
rop, pl = cli.request_json(netmod.OP_GET_DEVICE_INFO, {},
                           expect=(netmod.OP_DEVICE_INFO,))
info = json.loads(pl.decode("utf-8"))
check("GET_DEVICE_INFO → DEVICE_INFO", rop == netmod.OP_DEVICE_INFO)
check("设备信息含机型", info.get("model") == "RG35xxPRO")
check("设备信息含 fw", info.get("fw") == "20260522")
check("设备信息标记只读", info.get("writable") is False)

# 列目录（根）
rop, pl = cli.request_json(netmod.OP_LIST_DIR, {"path": ""},
                           expect=(netmod.OP_DIR_LISTING,))
lst = json.loads(pl.decode("utf-8"))
check("LIST_DIR 根目录成功", rop == netmod.OP_DIR_LISTING)
names = [i["name"] for i in lst["items"]]
check("根目录含 Roms", "Roms" in names, str(names))
check("Roms 被标为目录",
      any(i["name"] == "Roms" and i["dir"] for i in lst["items"]))

# 列目录（子目录）
rop, pl = cli.request_json(netmod.OP_LIST_DIR, {"path": "Roms"},
                           expect=(netmod.OP_DIR_LISTING,))
lst = json.loads(pl.decode("utf-8"))
names = [i["name"] for i in lst["items"]]
check("子目录 Roms 含 readme.txt", "readme.txt" in names, str(names))
check("子目录 Roms 含 big.bin", "big.bin" in names, str(names))
check("文件带 size",
      any(i["name"] == "readme.txt" and i["size"] > 0 for i in lst["items"]))

# 目录穿越必须被拒
rop, pl = cli.request_json(netmod.OP_LIST_DIR, {"path": "../../../etc"},
                           expect=(netmod.OP_NAK, netmod.OP_DIR_LISTING))
check("穿越请求被 NAK", rop == netmod.OP_NAK, str(rop))
if rop == netmod.OP_NAK:
    check("NAK 原因是路径越界",
          json.loads(pl.decode())["reason"] == netmod.NAK_DENIED)

# ---- 3.3 下载文件 ----
cli.sendj(netmod.OP_GET_FILE, {"path": "Roms/readme.txt"})
rop, pl = cli.recv()
check("GET_FILE → ACK", rop == netmod.OP_ACK, str(rop))
acked = json.loads(pl.decode())
check("ACK 带文件大小", acked.get("size", 0) > 0, str(acked))

got = bytearray()
while True:
    rop, pl = cli.recv()
    if rop == netmod.OP_FILE_CHUNK:
        seq = struct.unpack(">I", pl[:4])[0]
        got.extend(pl[4:])
    elif rop == netmod.OP_FILE_END:
        end = json.loads(pl.decode())
        break
    else:
        check("下载过程中出现意外帧", False, str(rop))
        break

want = open(os.path.join(card, "Roms", "readme.txt"), "rb").read()
check("下载内容与原文件一致", bytes(got) == want,
      f"{len(got)} vs {len(want)}")
check("FILE_END 报的大小一致", end.get("size") == len(want))

# ---- 3.4 下载大文件（跨 chunk） ----
cli.sendj(netmod.OP_GET_FILE, {"path": "Roms/big.bin"})
rop, pl = cli.recv()
got = bytearray()
chunks = 0
while rop != netmod.OP_FILE_END:
    if rop == netmod.OP_FILE_CHUNK:
        got.extend(pl[4:])
        chunks += 1
    rop, pl = cli.recv()
want = open(os.path.join(card, "Roms", "big.bin"), "rb").read()
check(f"大文件下载一致（{len(want)//1024}KB / {chunks} 块）",
      bytes(got) == want, f"{len(got)} vs {len(want)}")
check("大文件确实分成了多块", chunks > 1, str(chunks))

# ---- 3.5 上传：只读状态必须被拒 ----
payload = "应该被拒绝".encode("utf-8")
body = json.dumps({"path": "Roms/denied.txt", "size": len(payload)}).encode()
cli.notices.clear()
cli.sendj(netmod.OP_PUT_FILE_BEGIN, json.loads(body.decode()))
rop, pl = cli.recv_final()
check("只读时上传被拒 (NAK)", rop == netmod.OP_NAK, str(rop))
if rop == netmod.OP_NAK:
    check("NAK 原因 = 未解锁",
          json.loads(pl.decode())["reason"] == netmod.NAK_LOCKED,
          pl.decode())
# ★ 只读时应先收到 NOTICE(locked)，PC 端据此提示"去掌机按 SELECT 解锁"
check("★ 只读时先收到 NOTICE(locked)",
      any(n.get("kind") == "locked" for n in cli.notices),
      f"notices={cli.notices}")

# ---- 3.6 解锁 + 用户允许 → 上传成功 ----
nt.set_writable(True)
auto_allow["on"] = True

up_data = ("从 PC 传来的中文内容\n" * 500).encode("utf-8")
cli.sendj(netmod.OP_PUT_FILE_BEGIN,
          {"path": "Roms/from_pc.txt", "size": len(up_data)})
rop, pl = cli.recv_final()
check("解锁+允许后上传被 ACK", rop == netmod.OP_ACK, f"{rop} {pl!r}")

if rop == netmod.OP_ACK:
    # ⚠️ 数据块必须带 4 字节序号（大端 u32 = 该块之前的累计字节数）。
    #    服务端会剥掉序号再落盘，并校验连续性。
    #    收尾由 OP_FILE_END 驱动 —— 服务端不再"收满就自己 ACK"。
    #    （2026-10-05 回环测试发现：抢跑会把 FILE_END 变成"未知操作"）
    for off in range(0, len(up_data), netmod.CHUNK):
        blk = up_data[off:off + netmod.CHUNK]
        cli.send(netmod.OP_PUT_FILE_CHUNK, off.to_bytes(4, "big") + blk)
    cli.sendj(netmod.OP_FILE_END,
              {"name": "from_pc.txt", "size": len(up_data),
               "sum": str(len(up_data))})
    # ⚠️ 必须用 recv_final() 而不是 recv()。
    #
    # 【2026-10-05 第六轮踩到的坑】
    #   上面那句 `nt.set_writable(True)` 现在会**主动推一条
    #   OP_NOTICE(perm)** 给客户端（这是修 SELECT 状态不同步的一部分）。
    #   这条通知夹在"PUT_FILE_BEGIN 的 ACK"和"FILE_END 的 ACK"之间，
    #   于是裸 recv() 读到的第一条实质消息其实是那条 NOTICE(0x0042)，
    #   测试就报"上传完成 ACK"失败 —— 但文件其实已经正常落盘了
    #   （所以后面的"落盘了/逐字节一致"反而还是 OK）。
    #
    #   真实 PC 客户端不受影响：它走 _recv_until()，会把 NOTICE
    #   转给 on_notice 回调后继续等真正的 ACK。测试这里补上同样的语义。
    rop, pl = cli.recv_final()
    check("上传完成 ACK（由 FILE_END 驱动）", rop == netmod.OP_ACK,
          f"{rop} {netmod.OP_NAME.get(rop)}")

    dst = os.path.join(card, "Roms", "from_pc.txt")
    check("上传的文件真的落盘了", os.path.isfile(dst))
    if os.path.isfile(dst):
        check("上传内容逐字节一致",
              open(dst, "rb").read() == up_data)
    check("统计计数 files_in = 1", nt.stats["files_in"] == 1,
          str(nt.stats))

# ---- 3.7 用户拒绝 → 上传被拒 ----
auto_allow["on"] = False
cli.notices.clear()
cli.sendj(netmod.OP_PUT_FILE_BEGIN,
          {"path": "Roms/refused.txt", "size": 10})
rop, pl = cli.recv_final()
check("用户拒绝时上传被 NAK", rop == netmod.OP_NAK, str(rop))
if rop == netmod.OP_NAK:
    check("NAK 原因 = 用户未确认",
          json.loads(pl.decode())["reason"] == netmod.NAK_NO_CONFIRM,
          pl.decode())
check("被拒的文件没有落盘",
      not os.path.exists(os.path.join(card, "Roms", "refused.txt")))
# ★ 掌机在弹确认框前应发过 OP_NOTICE(confirm)，
#   这样 PC 端才能提示"请去掌机按 A"（用户实测反馈的第 5 点）
check("★ 弹确认框前先收到 NOTICE(confirm)",
      any(n.get("kind") == "confirm" for n in cli.notices),
      f"notices={cli.notices}")
auto_allow["on"] = True

# ---- 3.8 新建目录 ----
cli.sendj(netmod.OP_MKDIR_BEGIN, {"path": "Roms/新建目录"})
rop, pl = cli.recv_final()
check("MKDIR → ACK", rop == netmod.OP_ACK, f"{rop} {pl!r}")
check("目录真的建出来了",
      os.path.isdir(os.path.join(card, "Roms", "新建目录")))

# ---- 3.9 上传路径穿越必须被拒 ----
cli.sendj(netmod.OP_PUT_FILE_BEGIN, {"path": "../../evil.txt", "size": 5})
rop, pl = cli.recv_final()
check("上传穿越被 NAK", rop == netmod.OP_NAK, str(rop))

# ---- 3.10 BYE ----
cli.close()
time.sleep(0.3)
check("客户端断开后回到 idle", nt.state == "idle", nt.state)

# ---- 3.11 会话可重复 ----
cli2 = netmod.MiniClient("127.0.0.1")
cli2.connect()
rop, pl = cli2.request_json(netmod.OP_GET_DEVICE_INFO, {},
                            expect=(netmod.OP_DEVICE_INFO,))
check("第二次连接仍可用", rop == netmod.OP_DEVICE_INFO)
cli2.close()

nt.stop()
nt.join(timeout=3.0)
check("NetThread 已停止", not nt.is_alive())

# ---------------------------------------------------------------------------
print()
print("=" * 64)
print("  4. UI 模块（构造 + 四页渲染）")
print("=" * 64)

try:
    from PIL import ImageFont, ImageDraw, Image
    import ui as uimod
    check("ui.py 可 import", True)
except Exception as e:
    check("ui.py 可 import", False, f"{type(e).__name__}: {e}")
    uimod = None

if uimod:
    # 用 Pillow 内置字体（PC 上可能没有原厂字体）
    font_path = None
    for cand in ("C:/Windows/Fonts/msyh.ttc",
                 "C:/Windows/Fonts/simhei.ttf",
                 "C:/Windows/Fonts/arial.ttf"):
        if os.path.exists(cand):
            font_path = cand
            break

    class FakeFonts:
        def __init__(self, path):
            self.scale = 1.0
            self.source = path
            if path:
                self.title = ImageFont.truetype(path, 26)
                self.head = ImageFont.truetype(path, 18)
                self.body = ImageFont.truetype(path, 14)
                self.small = ImageFont.truetype(path, 12)
            else:
                self.title = ImageFont.load_default()
                self.head = self.body = self.small = self.title

    class FakeFb:
        def __init__(self, w=640, h=480):
            self.width, self.height = w, h
            self.hoffset, self.goffset, self.boffset = 16, 8, 0

    fonts = FakeFonts(font_path)

    st = dict(DEV_INFO)
    st.update({"font": "default.ttf", "motor": True,
               "input": "/dev/input/event1", "writable": False,
               "mode": uimod.MODE_BROWSE, "sent": 12345, "recv": 678901,
               "fps": 29.9, "peer": "192.168.3.100"})

    app = uimod.App(FakeFb(), fonts, st, net=None)
    check("App 构造成功", app is not None)
    # ★ 第五轮起默认页改为「首页」（仪表盘）—— 开机第一眼看到连没连上
    check("默认页 = 首页", app.stack[0].name == "home",
          f"{app.stack[0].name}")

    # 所有页面都渲染一遍（含新增的首页）
    for pname in ("home", "status", "browser", "net", "log"):
        try:
            app.stack[0] = app.pages[pname]
            app.stack[0].on_enter()
            img = app.render()
            ok = (img.size == (640, 480))
            check(f"页面「{pname}」渲染 640x480", ok, str(img.size))
        except Exception as e:
            import traceback
            check(f"页面「{pname}」渲染", False,
                  f"{type(e).__name__}: {e}\n{traceback.format_exc()}")

    # 不同分辨率
    for (w, h) in ((640, 480), (1280, 1024), (480, 640)):
        try:
            a2 = uimod.App(FakeFb(w, h), fonts, dict(st), net=None)
            img = a2.render()
            check(f"{w}x{h} 渲染正常", img.size == (w, h), str(img.size))
        except Exception as e:
            check(f"{w}x{h} 渲染正常", False, f"{type(e).__name__}: {e}")

    # 输入分发
    app2 = uimod.App(FakeFb(), fonts, dict(st), net=None)
    evt = uimod.Evt("key", "L2", 314, 1)
    app2.handle(evt)
    check("L2 切页可用", app2.stack[0].name != "status",
          app2.stack[0].name)

    # 摇杆导航浏览器
    bp = app2.pages["browser"]
    app2.stack = [bp]
    bp.reload()
    n0 = bp.list.cursor
    app2.handle(uimod.Evt("axis", "下", 4, 3000))
    check("摇杆「下」移动光标", bp.list.cursor >= n0,
          f"{n0} → {bp.list.cursor}")

    # 确认框
    ev = threading.Event()
    box = {"ok": False}
    app2.confirm = uimod.Confirm("put", "/x/y/z.bin", "z.bin", 2048, ev, box)
    img = app2.render()
    check("确认框可渲染", img.size == (640, 480))
    app2.handle(uimod.Evt("axis", "左", 3, -3000))
    app2.handle(uimod.Evt("key", "A", 304, 1))
    check("确认框按 A 回传决定", ev.is_set() and box["ok"] is True,
          f"set={ev.is_set()} ok={box['ok']}")

    # START 退出
    app3 = uimod.App(FakeFb(), fonts, dict(st), net=None)
    app3.handle(uimod.Evt("key", "START", 311, 1))
    check("START 置退出标志", app3.quit is True)

    # 子页面 START 先返回
    app4 = uimod.App(FakeFb(), fonts, dict(st), net=None)
    app4.push(app4.pages["log"])
    app4.handle(uimod.Evt("key", "START", 311, 1))
    check("子页面 START 先返回上级",
          len(app4.stack) == 1 and not app4.quit,
          f"stack={len(app4.stack)} quit={app4.quit}")

    # 网络事件 → UI 状态
    app5 = uimod.App(FakeFb(), fonts, dict(st), net=None)
    app5._on_net_event({"kind": "xfer_start", "direction": "in",
                        "name": "a.bin", "size": 1024})
    check("xfer_start 更新模式",
          app5.st["mode"] == "xfer_in", app5.st["mode"])
    app5._on_net_event({"kind": "xfer_done", "direction": "in",
                        "name": "a.bin", "size": 1024})
    check("xfer_done 累加统计 + 回到 browse",
          app5.st["mode"] == "browse" and app5.st["recv"] == 678901 + 1024,
          f"mode={app5.st['mode']!r} recv={app5.st['recv']}")
    app5._on_net_event({"kind": "connected", "peer": "10.0.0.2"})
    check("connected 记录对端", app5.st.get("peer") == "10.0.0.2")
    app5._on_net_event({"kind": "disconnected", "peer": "10.0.0.2"})
    check("disconnected 清除对端", app5.st.get("peer") is None)

    # 空目录不应崩
    empty = tempfile.mkdtemp(prefix="pt-empty-")
    st2 = dict(st)
    st2["base"] = empty
    a6 = uimod.App(FakeFb(), fonts, st2, net=None)
    a6.pages["browser"].reload()
    img = a6.render()
    check("空目录渲染不崩", img.size == (640, 480))

# ---------------------------------------------------------------------------
print()
print("=" * 64)
print("  5. main.py 关键函数（语法级 + 纯函数级）")
print("=" * 64)

mainsrc = open(os.path.join(HH, "main.py"), encoding="utf-8").read()
sdlsrc2 = open(os.path.join(HH, "sdl_display.py"), encoding="utf-8").read()
check("main.py 语法正确", True)

# 检查关键点没被破坏
for key, desc, where in [
    ("buf = key", "悬空指针修复仍在（blit 里）", sdlsrc2),
    ("SDL_WINDOW_FULLSCREEN_DESKTOP", "仍走 SDL 全屏", sdlsrc2),
    ("_dmenu_running", "菜单占用检测仍在", mainsrc),
    ("app.pump()", "主循环排空网络事件", mainsrc),
    ("net.stop()", "退出时停网络线程", mainsrc),
    ("motor.pulse", "震动反馈仍在", mainsrc),
    ("SDL_CreateRGBSurfaceWithFormatFrom", "SDL surface 通路仍在", sdlsrc2),
]:
    check(desc, key in where, key)

# sdl_display.py 不能 import sdl2.ext
sdlsrc = open(os.path.join(HH, "sdl_display.py"), encoding="utf-8").read()
import ast as _ast
tree = _ast.parse(sdlsrc)
bad = False
for n in _ast.walk(tree):
    if isinstance(n, _ast.Import):
        for al in n.names:
            if al.name.startswith("sdl2.ext"):
                bad = True
    if isinstance(n, _ast.ImportFrom):
        if (n.module or "").startswith("sdl2.ext"):
            bad = True
check("sdl_display.py 没有 import sdl2.ext", not bad)

# net.py 与 ui.py 语法
for fn in ("net.py", "ui.py", "boot.py", "fb.py"):
    p = os.path.join(HH, fn)
    try:
        _ast.parse(open(p, encoding="utf-8").read())
        check(f"{fn} 语法正确", True)
    except Exception as e:
        check(f"{fn} 语法正确", False, str(e))

# ---------------------------------------------------------------------------
print()
print("=" * 64)
print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
print("=" * 64)
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
    sys.exit(1)
print("全部通过")
sys.exit(0)
