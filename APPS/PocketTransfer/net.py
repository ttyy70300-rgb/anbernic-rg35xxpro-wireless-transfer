#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer 掌机端 —— 网络层（阶段 2）
=========================================

职责（单文件，便于推送到掌机）：
  1. 协议编解码（4 字节包头 + 负载）—— 与 PC 端契约一致
  2. UDP 广播应答 —— 让 PC 自动发现本机
  3. TCP 服务端 —— 会话、列目录、传输、写操作

【线程模型】
    NetThread        单后台线程，内含两个 socket，用 select 多路复用
       ├── UDP 48211  收广播 → 回 DEVICE_INFO
       └── TCP 48200  收连接 → 处理请求

    为什么要后台线程：
        主线程必须保持 30 FPS 渲染 + 读输入。若在渲染循环里直接
        accept()/recv() 阻塞，UI 会卡死，用户会以为程序挂了。
        所以网络一律丢后台，通过 Event 队列与主线程通信。

    为什么不每连接一个线程：
        机器只有 1GB 内存 + 4 核。本应用是"一次一个 PC"的场景，
        多线程只会带来并发复杂度（尤其写文件时）。串行更安全。

【与主线程的通信】
    主线程 → 网络：通过 Broadcast 消息（新建目录 / 接受传输 / 取消）
    网络 → 主线程：往 self.events 队列推 dict，主线程每帧取

【安全约束（用户硬边界）】
    本模块**只做**：
      - 读目录、读文件（在允许的根目录内）
      - 写文件（仅在用户于掌机上确认后，且仅在允许的根目录内）
    绝不做：改系统配置、写 /tmp/.next、动 /sys、动 /proc、kill 进程。
"""
import errno
import json
import os
import queue
import select
import socket
import struct
import threading
import time

# ---------------------------------------------------------------------------
# 协议常量（必须与 PC 端一致）
# ---------------------------------------------------------------------------

MAGIC = 0x504B
HDR = struct.Struct(">HHI")          # MAGIC(2) TYPE(2) LENGTH(4) 大端

TCP_PORT = 48200
UDP_BEACON_PORT = 48211

MAX_PAYLOAD = 16 * 1024 * 1024       # 单包上限 16MB
CHUNK = 64 * 1024                    # 文件分块 64KB

# ---------------------------------------------------------------------------
# ★★ 会话空闲超时（这是"传完文件连接就断"那个 bug 的正解）
# ---------------------------------------------------------------------------
# 早期版本对整条连接只设一个 20 秒超时：
#     conn.settimeout(20.0)   # 单次读超时，防止半开连接永久占线
# 结果是：**空闲 20 秒就断**。用户传完一个文件，去盘里翻下一个，
# 超过 20 秒没动作，下一次 recv 就 socket.timeout → 主循环 break → 连接没了。
#
# 正确做法是把"空闲"和"传输中"分开：
#   - 空闲态：可以很长（用户在思考/翻文件），用 IDLE_TIMEOUT 兜底半开连接
#   - 传输中：每个数据块之间不该有长间隔，用 TRANSFER_TIMEOUT 快速发现对端没了
# 另外 PC 端还会周期性发 PING 保活，正常情况下永远碰不到 IDLE_TIMEOUT。
IDLE_TIMEOUT = 600.0        # 空闲时允许的最大静默：10 分钟
TRANSFER_TIMEOUT = 30.0     # 传输过程中单次读的最大间隔：30 秒

# 操作码
OP_PING = 0x0001
OP_PONG = 0x0002
OP_GET_DEVICE_INFO = 0x0003
OP_DEVICE_INFO = 0x0004
OP_LIST_DIR = 0x0010
OP_DIR_LISTING = 0x0011
OP_GET_FILE = 0x0012             # 请求下载
OP_FILE_CHUNK = 0x0013             # 下载数据块
OP_FILE_END = 0x0014             # 下载结束 / 上传结束
OP_PUT_FILE_BEGIN = 0x0020      # 请求上传（需掌机确认）
OP_PUT_FILE_CHUNK = 0x0021
OP_MKDIR_BEGIN = 0x0030      # 请求新建目录（需确认）
OP_DELETE_BEGIN = 0x0031     # ★ 请求删除文件（需确认，不可逆）
OP_ACK = 0x0040             # 通用确认
OP_NAK = 0x0041             # 通用拒绝
OP_NOTICE = 0x0042          # ★ 临时通知（掌机 → PC）：如"等你确认"、"请解锁"
OP_ERROR = 0x00F0
OP_BYE = 0x00FF

OP_NAME = {
    OP_PING: "PING", OP_PONG: "PONG",
    OP_GET_DEVICE_INFO: "GET_DEVICE_INFO", OP_DEVICE_INFO: "DEVICE_INFO",
    OP_LIST_DIR: "LIST_DIR", OP_DIR_LISTING: "DIR_LISTING",
    OP_GET_FILE: "GET_FILE", OP_FILE_CHUNK: "FILE_CHUNK",
    OP_FILE_END: "FILE_END", OP_PUT_FILE_BEGIN: "PUT_FILE_BEGIN",
    OP_PUT_FILE_CHUNK: "PUT_FILE_CHUNK", OP_MKDIR_BEGIN: "MKDIR_BEGIN",
    OP_DELETE_BEGIN: "DELETE_BEGIN",
    OP_ACK: "ACK", OP_NAK: "NAK", OP_NOTICE: "NOTICE",
    OP_ERROR: "ERROR", OP_BYE: "BYE",
}

# OP_NOTICE 的 kind —— ★ 与 pc/protocol.py 保持一致
NOTICE_CONFIRM = "confirm"      # 掌机正弹确认框，等用户按 A/B（60 秒）
NOTICE_LOCKED = "locked"        # 掌机被用户主动切成只读（按 SELECT）
NOTICE_BUSY = "busy"            # 掌机正在处理上一个请求
NOTICE_PERM = "perm"            # ★ 掌机主动上报读写状态已变更（按了 SELECT）

# NAK 原因码
NAK_LOCKED = 1      # 写操作被拒绝：用户已把掌机切成只读
NAK_NO_CONFIRM = 2      # 写操作被拒绝：用户未确认
NAK_DENIED = 3      # 写操作被拒绝：路径不允许
NAK_BUSY = 4      # 网络忙
NAK_BAD = 5      # 请求不合法
NAK_IS_DIR = 6      # ★ 目标是目录：本版本只支持删文件，不支持删目录


def pack_frame(op, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return HDR.pack(MAGIC, op, len(payload)) + payload


def pack_json(op, obj):
    return pack_frame(op, json.dumps(obj, ensure_ascii=False))


class Parser:
    """TCP 是字节流，必须自己攒够包头再切负载。"""

    def __init__(self):
        self.buf = bytearray()
        self.error = None

    def feed(self, data):
        self.buf.extend(data)
        out = []
        while True:
            if len(self.buf) < HDR.size:
                break
            magic, op, length = HDR.unpack_from(self.buf, 0)
            if magic != MAGIC:
                self.buf = self.buf[1:]       # 重新同步
                self.error = "magic 不匹配"
                continue
            if length > MAX_PAYLOAD:
                self.error = f"负载过大 {length}"
                self.buf.clear()
                break
            if len(self.buf) < HDR.size + length:
                break
            payload = bytes(self.buf[HDR.size:HDR.size + length])
            del self.buf[:HDR.size + length]
            out.append((op, payload))
        return out


# ---------------------------------------------------------------------------
# 安全：路径校验
# ---------------------------------------------------------------------------

def safe_join(root, rel):
    """
    把客户端给的相对路径拼到 root 下，并确保结果**没有跑出 root**。

    这是唯一的路径安全闸门。客户端可以发 "../../../etc/passwd"，
    必须在这里挡住。用 realpath 而不是 normpath，因为符号链接
    也要一并解析（/mnt/mmc 下如果有指向 /etc 的软链，normpath 挡不住）。

    返回绝对路径；不合法返回 None。
    """
    if root is None:
        return None
    if rel is None:
        rel = ""
    rel = str(rel).replace("\\", "/").strip()
    # 去掉前导 /，避免 os.path.join 丢弃 root
    while rel.startswith("/"):
        rel = rel[1:]
    full = os.path.realpath(os.path.join(root, rel))
    rootr = os.path.realpath(root)
    if full == rootr:
        return full
    if not full.startswith(rootr + os.sep):
        return None
    return full


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)}B"
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return "?"


# ---------------------------------------------------------------------------
# 网络线程
# ---------------------------------------------------------------------------


class NetThread(threading.Thread):
    """
    后台网络线程。

    对外接口：
        start()                      启动
        stop()                       请求停止并等待退出
        events                       主线程每帧 drain 的队列
        send_command(dict)           主线程向网络下发指令
        set_writable(bool)           主线程改变"是否允许写"状态
        set_root(path)               主线程设置当前允许访问的根目录
    """

    def __init__(self, device_info_fn, base_path_fn, log=None):
        super().__init__(daemon=True, name="pt-net")
        self.device_info_fn = device_info_fn   # callable() -> dict
        self.base_path_fn = base_path_fn     # callable() -> str
        self.log = log or (lambda m: None)

        self.events = queue.Queue()
        self._cmds = queue.Queue()

        # ★★ 千万不要把属性命名为 `_stop`（血泪教训）
        #
        #   `threading.Thread` 内部有个私有方法叫 `_stop()`，
        #   `_wait_for_tstate_lock()` 在 join 收尾时会调用它：
        #
        #       def _wait_for_tstate_lock(self, block=True, timeout=-1):
        #           ...
        #           elif lock.acquire(block, timeout):
        #               lock.release()
        #               self._stop()          # ← 调的就是这个名字
        #
        #   一旦我们在子类里写 `self._stop = threading.Event()`，
        #   就把那个方法**遮蔽**了。于是 `net.join(timeout=2.0)` 走到
        #   最后一步时爆炸：
        #
        #       TypeError: 'Event' object is not callable
        #
        #   表现：退出时收尾日志只留一行"停止网络线程异常"，
        #        而且**只在真正跑过线程之后**才触发（没 start 过就不进
        #        那条分支），所以本机单测怎么跑都不复现，
        #        一到掌机上就中招。掌机 Python 3.10 / 本机 3.13 都有此行为。
        #
        #   修复：改名 `_stop_evt`，把 `_stop` 这个名字还给 CPython。
        self._stop_evt = threading.Event()

        self._root = None
        self._writable = False
        self._lock = threading.Lock()

        # ⚠️ 必须在这里初始化！
        # _serve() 里有一句裸读 `if not self._put_ctx:`，
        # 若未在 __init__ 定义，任何客户端发来 PUT_FILE_BEGIN 之前的
        # 第一次 PUT_FILE_CHUNK 都会 AttributeError。
        # 用 getattr(..., None) 是防御性写法，但根因要靠这里补上。
        self._put_ctx = None
        self._calib_ctx = None      # 预留：其它需要跨消息维持的上下文

        self.state = "starting"
        self.peer = None
        self.stats = {"sent": 0, "recv": 0, "files_out": 0, "files_in": 0}
        self.last_error = None
        self.udp_seen = 0

        self._udp = None
        self._tcp = None

    # ---------------- 主线程侧接口 ----------------

    def set_root(self, path):
        with self._lock:
            self._root = path

    def set_writable(self, on):
        """
        改变"是否允许 PC 写入"状态。

        ★ 关键：必须**主动通知 PC**。
          在此之前这里只改本地标志，PC 只在连接时读过一次 writable，
          于是用户在掌机上按 SELECT 切了状态，PC 面板纹丝不动 ——
          用户实测反馈的原话就是"再按一下 select 键，win11 这边就没有同步状态"。

          通知走 OP_NOTICE(kind=perm)，复用已有的单向上报通道，
          PC 端 `_on_notice` 收到后刷新权限标签。
        """
        with self._lock:
            changed = (self._writable != bool(on))
            self._writable = bool(on)
            cur = self._writable
        if changed:
            self.notify_peer({
                "kind": "perm",
                "writable": cur,
                "hint": ("掌机已设为可写" if cur else
                         "掌机已在掌机上锁定，写入会被拒绝"),
            })

    def notify_peer(self, data):
        """
        让会话线程往当前 TCP 连接推一条 OP_NOTICE。

        为什么走命令队列而不是直接抓 socket 发：
            socket 归会话线程独占，从别的线程写会和控制帧交错、
            造成协议错位。这里只投递意图，由会话线程在自己的
            循环里发出去（_serve 每轮 POLL_S 检查一次队列）。
        连接不存在时静默丢弃 —— 没连上就没人需要知道。
        """
        self.send_command({"act": "notify", "data": data})

    def is_writable(self):
        with self._lock:
            return self._writable

    def send_command(self, cmd):
        self._cmds.put(cmd)

    def stop(self):
        self._stop_evt.set()

    def _drain_commands(self):
        out = []
        while True:
            try:
                out.append(self._cmds.get_nowait())
            except queue.Empty:
                break
        return out

    def _emit(self, kind, **kw):
        kw["kind"] = kind
        kw["t"] = time.strftime("%H:%M:%S")
        try:
            self.events.put_nowait(kw)
        except queue.Full:
            pass

    # ---------------- 线程主体 ----------------

    def run(self):
        try:
            self._setup()
        except Exception as e:
            self.state = "error"
            self.last_error = f"网络初始化失败: {e}"
            self.log(f"[net] {self.last_error}")
            self._emit("net_error", msg=self.last_error)
            return

        self.state = "idle"
        self.log(f"[net] 已监听 TCP:{TCP_PORT} UDP:{UDP_BEACON_PORT}")

        while not self._stop_evt.is_set():
            # ---- 处理主线程指令 ----
            for c in self._drain_commands():
                try:
                    self._handle_command(c)
                except Exception as e:
                    self.log(f"[net] 指令处理异常 {c}: {e}")

            try:
                r, _, _ = select.select([self._udp, self._tcp], [], [], 0.10)
            except (OSError, ValueError):
                break

            if self._udp in r:
                try:
                    self._handle_udp()
                except Exception as e:
                    self.log(f"[net] UDP 处理异常: {e}")

            if self._tcp in r:
                try:
                    self._handle_tcp_accept()
                except Exception as e:
                    self.log(f"[net] TCP accept 异常: {e}")

        self._teardown()
        self.log("[net] 网络线程已退出")

    def _setup(self):
        # ---- UDP 广播应答 ----
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        u.bind(("0.0.0.0", UDP_BEACON_PORT))
        u.setblocking(False)
        self._udp = u

        # ---- TCP 服务 ----
        t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        t.bind(("0.0.0.0", TCP_PORT))
        t.listen(2)
        t.setblocking(False)
        self._tcp = t

    def _teardown(self):
        for s in (self._udp, self._tcp):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        self._udp = None
        self._tcp = None

    def _handle_command(self, c):
        act = c.get("act")
        if act == "root":
            self.set_root(c.get("path"))
        elif act == "writable":
            self.set_writable(c.get("on"))
            self.log(f"[net] 可写状态 → {self.is_writable()}")
        # 其它指令在这里扩展（如主动断开）

    # ---------------- UDP ----------------

    def _handle_udp(self):
        while True:
            try:
                data, addr = self._udp.recvfrom(4096)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return
                raise
            if not data:
                return
            try:
                obj = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if obj.get("magic") != "POCKETTRANSFER":
                continue
            if obj.get("act") not in ("discover", "who"):
                continue

            self.udp_seen += 1
            info = dict(self.device_info_fn() or {})
            info["magic"] = "POCKETTRANSFER"
            info["act"] = "here"
            info["tcp_port"] = TCP_PORT
            info["writable"] = self.is_writable()
            try:
                self._udp.sendto(json.dumps(info, ensure_ascii=False)
                                 .encode("utf-8"), addr)
            except Exception as e:
                self.log(f"[net] UDP 回复失败: {e}")
            # 首次被发现时通知主线程（UI 上可以显示"PC 已找到")
            if self.udp_seen == 1:
                self._emit("discovered", peer=f"{addr[0]}")

    # ---------------- TCP ----------------

    def _handle_tcp_accept(self):
        try:
            conn, addr = self._tcp.accept()
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            raise
        conn.settimeout(IDLE_TIMEOUT)   # ★ 空闲超时，不是 20 秒的"单次读超时"
        # 说明：_serve 内部会在进入"传输中"时把超时切成 TRANSFER_TIMEOUT，
        #      传完再切回 IDLE_TIMEOUT。见 _serve 里的 _set_timeout。
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        # 开启 TCP keepalive 兜底：万一中间设备静默丢弃连接（比如 WiFi 漫游），
        # 内核会自己探测并在死链上关掉 socket，不至于永久占着。
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
        except Exception:
            pass
        self.log(f"[net] TCP 连接来自 {addr[0]}:{addr[1]}")
        self._emit("connected", peer=addr[0])
        self.state = "connected"
        try:
            self._serve(conn, addr)
        except Exception as e:
            self.log(f"[net] 会话异常: {type(e).__name__}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self.state = "idle"
            self._emit("disconnected", peer=addr[0])
            self.log(f"[net] 会话结束 {addr[0]}")

    def _serve(self, conn, addr):
        parser = Parser()
        conn.setblocking(True)
        state = {"timeout": IDLE_TIMEOUT}

        def _set_timeout(sec):
            """
            切换本会话的读超时。

            ★ 为什么要能切：
                空闲时用户可能长时间不动（传完一个文件去翻下一个），
                此时该耐心等；一旦进入传输，每个数据块之间不该有长间隔，
                此时该严格。用一个固定值必然要么误杀空闲、要么放过死链。
            """
            if sec == state["timeout"]:
                return
            state["timeout"] = sec
            try:
                conn.settimeout(sec)
            except Exception:
                pass

        def send(op, payload=b""):
            conn.sendall(pack_frame(op, payload))

        def sendj(op, obj):
            conn.sendall(pack_json(op, obj))

        def recv_exact(n):
            buf = b""
            while len(buf) < n:
                try:
                    d = conn.recv(n - len(buf))
                except socket.timeout:
                    raise
                if not d:
                    raise ConnectionError("对端关闭")
                buf += d
            return buf

        def recv_msg():
            hdr = recv_exact(HDR.size)
            magic, op, length = HDR.unpack(hdr)
            if magic != MAGIC:
                raise ValueError(f"magic 错误 {magic:#x}")
            if length > MAX_PAYLOAD:
                raise ValueError(f"负载过大 {length}")
            return op, (recv_exact(length) if length else b"")

        # ★ 主循环轮询节奏
        #
        # 【为什么不能直接用 IDLE_TIMEOUT 阻塞在 recv 上】
        #   掌机按 SELECT 切可写状态时，连接通常是**空闲**的 ——
        #   会话线程正阻塞在 recv_msg() 上等 600 秒。
        #   如果只在"收到包之后"才排空命令队列，这条状态变更
        #   就得等到下一次数据往来才能送达 PC。
        #   用户按下 SELECT 后 PC 端面板纹丝不动，就是这个原因。
        #
        # 【做法】用较短的轮询超时，超时后回到循环开头排空命令队列，
        #   队列空且总空闲时间没到才真的判超时。
        POLL_S = 0.5
        idle_deadline = None      # 连续无数据多久算空闲超时

        def _drain_pending():
            """把主线程塞进队列的命令发出去（状态变更等主动通知）。"""
            for c in self._drain_commands():
                if c.get("act") == "notify":
                    try:
                        sendj(OP_NOTICE, c.get("data") or {})
                    except Exception as e:
                        self.log(f"[net] 主动通知失败: {e}")
                        return False
                else:
                    self._handle_command(c)
            return True

        while not self._stop_evt.is_set():
            # ★ 每轮回合先看有没有主线程塞进来的主动通知
            try:
                conn.settimeout(POLL_S)
            except Exception:
                pass
            if not _drain_pending():
                break

            try:
                op, payload = recv_msg()
            except socket.timeout:
                # 轮询超时 ≠ 空闲超时。累积无数据时长，到点才真断。
                now = time.time()
                if idle_deadline is None:
                    idle_deadline = now + IDLE_TIMEOUT
                if now >= idle_deadline:
                    self.log(f"[net] 空闲超时（{IDLE_TIMEOUT:.0f}s），关闭连接")
                    break
                continue
            except (ConnectionError, ValueError, OSError) as e:
                self.log(f"[net] 连接结束: {e}")
                break

            idle_deadline = None      # 收到东西 → 重新开始计空闲

            name = OP_NAME.get(op, f"0x{op:04X}")

            # ★ 除传输数据块外，任何消息都说明连接是活的 → 回到空闲超时
            #   （数据块由收发函数自己管超时，不在这里切）
            if op not in (OP_PUT_FILE_CHUNK, OP_FILE_CHUNK):
                _set_timeout(IDLE_TIMEOUT)

            if op == OP_PING:
                send(OP_PONG, payload)

            elif op == OP_GET_DEVICE_INFO:
                d = dict(self.device_info_fn() or {})
                d["writable"] = self.is_writable()
                d["root"] = self._root or ""
                d["stats"] = dict(self.stats)
                sendj(OP_DEVICE_INFO, d)

            elif op == OP_LIST_DIR:
                self._do_list_dir(sendj, payload)

            elif op == OP_GET_FILE:
                self._do_get_file(send, sendj, payload, _set_timeout)

            elif op == OP_PUT_FILE_BEGIN:
                self._do_put_begin(sendj, payload, _set_timeout)

            elif op == OP_PUT_FILE_CHUNK:
                # 传输中：每块之间用严格超时，及时发现对端消失
                _set_timeout(TRANSFER_TIMEOUT)
                self._do_put_chunk(recv_exact, sendj, payload)

            elif op == OP_FILE_END:
                # 上传的收尾信号。注意：下载方向下 FILE_END 是**服务端发出**
                # 的，客户端不会反发给我们，所以这里只可能是上传结束。
                _set_timeout(IDLE_TIMEOUT)
                self._do_put_end(sendj, payload)

            elif op == OP_MKDIR_BEGIN:
                self._do_mkdir(sendj, payload)

            elif op == OP_DELETE_BEGIN:
                # ★ 删除是不可逆操作，本方法内部会阻塞到用户在掌机上按 A/B
                self._do_delete(sendj, payload)

            elif op == OP_BYE:
                self.log("[net] 客户端道别")
                break

            else:
                self.log(f"[net] 未知操作码 {name}")
                sendj(OP_NAK, {"reason": NAK_BAD, "msg": f"未知操作 {name}"})

    # ---------------- 各操作实现 ----------------

    def _do_list_dir(self, sendj, payload):
        try:
            req = json.loads(payload.decode("utf-8") or "{}")
        except Exception:
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "请求不是合法 JSON"})
            return
        root = self._root
        if not root:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "卡片未就绪"})
            return
        target = safe_join(root, req.get("path", ""))
        if target is None:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "路径越界"})
            return
        if not os.path.isdir(target):
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "不是目录"})
            return

        items = []
        try:
            for nm in sorted(os.listdir(target),
                             key=lambda s: (not os.path.isdir(
                                 os.path.join(target, s)), s.lower())):
                full = os.path.join(target, nm)
                isdir = os.path.isdir(full)
                try:
                    stt = os.stat(full)
                    size, mt = stt.st_size, int(stt.st_mtime)
                except Exception:
                    size, mt = 0, 0
                items.append({
                    "name": nm,
                    "dir": isdir,
                    "size": 0 if isdir else size,
                    "mtime": mt,
                })
                if len(items) >= 4000:
                    break
        except PermissionError:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "无权限读取"})
            return

        rel = os.path.relpath(target, os.path.realpath(root))
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        sendj(OP_DIR_LISTING, {
            "path": rel,
            "abs": target,
            "items": items,
        })
        self.log(f"[net] LIST {rel or '/'} → {len(items)} 项")

    def _do_get_file(self, send, sendj, payload, set_timeout=None):
        try:
            req = json.loads(payload.decode("utf-8") or "{}")
        except Exception:
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "请求不合法"})
            return
        target = safe_join(self._root, req.get("path", ""))
        if target is None:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "路径越界"})
            return
        if not os.path.isfile(target):
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "不是文件"})
            return
        size = os.path.getsize(target)
        name = os.path.basename(target)
        self.log(f"[net] GET_FILE {name} ({human_size(size)})")
        self._emit("xfer_start", direction="out", name=name, size=size)

        sendj(OP_ACK, {"op": "get_file", "name": name, "size": size})
        sent = 0
        try:
            with open(target, "rb") as f:
                while not self._stop_evt.is_set():
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    # 每块带序号 + 数据；序号从 0 递增
                    seq = sent
                    send(OP_FILE_CHUNK,
                         struct.pack(">I", seq) + chunk)
                    sent += len(chunk)
                    self.stats["sent"] += len(chunk)
        except (BrokenPipeError, ConnectionError, OSError) as e:
            self.log(f"[net] 发送中断: {e}")
            self._emit("xfer_abort", direction="out", name=name)
            return
        finally:
            # 不管成功失败，回到空闲超时（会话继续可用）
            if set_timeout:
                set_timeout(IDLE_TIMEOUT)

        sendj(OP_FILE_END, {"name": name, "size": sent,
                            "sum": f"{sent}"})
        self.stats["files_out"] += 1
        self.log(f"[net] 发送完成 {name} {human_size(sent)}")
        self._emit("xfer_done", direction="out", name=name, size=sent)

    def _do_put_begin(self, sendj, payload, set_timeout=None):
        """
        客户端请求上传。**必须由用户在本机确认**，这里只做前置校验
        并记录上下文，ACK 由主线程确认后下发。
        """
        self._put_ctx = None
        try:
            req = json.loads(payload.decode("utf-8") or "{}")
        except Exception:
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "请求不合法"})
            return
        if not self.is_writable():
            # ★ 先给 PC 一条 NOTICE，让它能把"请去掌机按 SELECT 解锁"
            #   直接显示出来，而不是只看到一个冷冰冰的拒绝。
            #
            # 说明（2026-10-05 用户反馈 #1 之后）：
            #   掌机现在**默认就是可写**，正常情况走不到这里。
            #   只有用户主动按了 SELECT 把掌机切成只读，才会被拦下。
            #   所以这里的提示语是"你把它锁住了"，而不是"你还没解锁"。
            try:
                sendj(OP_NOTICE, {"kind": NOTICE_LOCKED,
                                  "hint": "掌机已被临时锁定，"
                                          "请在掌机上按 SELECT 解锁后重试"})
            except Exception:
                pass
            sendj(OP_NAK, {"reason": NAK_LOCKED,
                           "msg": "掌机处于只读状态，请按 SELECT 解锁"})
            self._emit("write_denied", why="locked", path=req.get("path", ""))
            return
        target = safe_join(self._root, req.get("path", ""))
        if target is None:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "目标路径越界"})
            return
        name = os.path.basename(target)
        size = int(req.get("size") or 0)

        # 交给主线程弹确认框；网络线程在此阻塞等待用户决定
        decision = self._request_confirm("put", target, name, size,
                                         sendj=sendj)
        if decision is None or not decision.get("ok"):
            sendj(OP_NAK, {"reason": NAK_NO_CONFIRM,
                           "msg": "用户在掌机上拒绝了本次写入"})
            self._emit("write_denied", why="rejected", path=target)
            return
        # 用户同意：准备接收
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except Exception as e:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": f"无法创建目录: {e}"})
            return

        self._put_ctx = {
            "target": target,
            "name": name,
            "expect": size,
            "got": 0,
            "fh": None,
        }
        try:
            self._put_ctx["fh"] = open(target, "wb")
        except Exception as e:
            self._put_ctx = None
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": f"无法写入: {e}"})
            return

        self.log(f"[net] 用户已确认上传 {target} ({human_size(size)})")
        self._emit("xfer_start", direction="in", name=name, size=size)
        sendj(OP_ACK, {"op": "put_file", "name": name, "size": size})

    def _do_put_chunk(self, recv_exact, sendj, payload):
        """
        收一个上传数据块。

        【数据块布局】
            前 4 字节 = 块序号（大端 u32，等于该块之前的累计字节数）
            其余     = 原始文件数据

        ⚠️⚠️ 这里最容易踩的坑：**序号必须剥掉再写盘**。
            早期版本直接把整个 payload 写进文件，于是每 64KB 就
            多混进 4 字节序号 —— 文件大小"看着对"（因为客户端报的
            是纯数据长度），但 md5 必然不符。回环测试逮住了这个 bug。

        【收尾由谁驱动】
            由客户端发来的 OP_FILE_END 驱动，**不是**在收到第
            expect 字节时就自行收尾。原因见 _do_put_end 注释。
        """
        ctx = getattr(self, "_put_ctx", None)
        if not ctx:
            # 已经收尾（或从未开始）：这是"拒绝后仍在途"的数据块。
            # 回一个 NAK 让客户端尽早停下，但不当作致命错误。
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "未开始上传"})
            return False

        if len(payload) < 4:
            self.log("[net] 上传数据块过短（缺少序号）")
            self._finish_put(ok=False)
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "数据块缺少序号"})
            return False

        seq = int.from_bytes(payload[:4], "big")
        data = payload[4:]

        # 序号校验：应当等于已收字节数。不连续说明流错乱了，宁可失败。
        if seq != ctx["got"]:
            self.log(f"[net] 块序号断裂: 期望 {ctx['got']} 收到 {seq}")
            self._finish_put(ok=False)
            sendj(OP_NAK, {"reason": NAK_BAD,
                           "msg": f"块序号断裂 {ctx['got']}≠{seq}"})
            return False

        try:
            ctx["fh"].write(data)
            ctx["got"] += len(data)
            self.stats["recv"] += len(data)
        except Exception as e:
            self.log(f"[net] 写文件失败: {e}")
            self._finish_put(ok=False, msg=str(e))
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": f"写入失败: {e}"})
            return False

        # 收满了也不在这里收尾 —— 等 FILE_END，保证协议对称且不丢消息。
        return True

    def _do_put_end(self, sendj, payload):
        """
        客户端宣告上传结束。**收尾的唯一入口。**

        【为什么不在收满 expect 字节时自行收尾】
            早期版本在 _do_put_chunk 里收满就 _finish_put + ACK，
            结果客户端紧接着发的 OP_FILE_END 落到了 _serve 主循环，
            被当成"未知操作 FILE_END"回了一个 NAK。

            更糟的是在**用户拒绝**场景：客户端仍在把 FILE_END 发完，
            服务端却已经回 NAK 收场，双方对"会话进行到哪"认知错位。

            正确做法：收满只是"数据到齐"，结束由显式的 FILE_END 宣告。
            这样双方状态机严格对称，任何一方都不会抢跑。
        """
        ctx = getattr(self, "_put_ctx", None)
        if not ctx:
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "未开始上传"})
            return False
        expect = ctx.get("expect") or 0
        got = ctx["got"]
        if expect and got != expect:
            self.log(f"[net] 上传大小不符: 期望 {expect} 收到 {got}")
            self._finish_put(ok=False)
            sendj(OP_NAK, {"reason": NAK_BAD,
                           "msg": f"大小不符 期望{expect} 收到{got}"})
            return False
        name, size = ctx["name"], got
        self._finish_put(ok=True)
        sendj(OP_ACK, {"op": "put_done", "name": name, "size": size})
        return True

    def _finish_put(self, ok, msg=""):
        ctx = getattr(self, "_put_ctx", None)
        if not ctx:
            return
        try:
            if ctx["fh"]:
                ctx["fh"].flush()
                os.fsync(ctx["fh"].fileno())
                ctx["fh"].close()
        except Exception:
            pass
        if ok:
            self.stats["files_in"] += 1
            self.log(f"[net] 接收完成 {ctx['name']} "
                     f"{human_size(ctx['got'])}")
            self._emit("xfer_done", direction="in", name=ctx["name"],
                       size=ctx["got"])
        else:
            try:
                os.unlink(ctx["target"])
            except Exception:
                pass
            self._emit("xfer_abort", direction="in", name=ctx["name"])
        self._put_ctx = None

    def _do_mkdir(self, sendj, payload):
        try:
            req = json.loads(payload.decode("utf-8") or "{}")
        except Exception:
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "请求不合法"})
            return
        if not self.is_writable():
            try:
                sendj(OP_NOTICE, {"kind": NOTICE_LOCKED,
                                  "hint": "掌机已被临时锁定，"
                                          "请在掌机上按 SELECT 解锁后重试"})
            except Exception:
                pass
            sendj(OP_NAK, {"reason": NAK_LOCKED, "msg": "掌机只读，请先解锁"})
            return
        target = safe_join(self._root, req.get("path", ""))
        if target is None:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "路径越界"})
            return
        name = os.path.basename(target)
        decision = self._request_confirm("mkdir", target, name, 0,
                                         sendj=sendj)
        if decision is None or not decision.get("ok"):
            sendj(OP_NAK, {"reason": NAK_NO_CONFIRM, "msg": "用户拒绝"})
            return
        try:
            os.makedirs(target, exist_ok=True)
        except Exception as e:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": f"创建失败: {e}"})
            return
        self.log(f"[net] 新建目录 {target}")
        self._emit("mkdir_done", path=target)
        sendj(OP_ACK, {"op": "mkdir", "path": target})

    def _do_delete(self, sendj, payload):
        """
        删除掌机上的**文件**（不删目录）。

        ★ 用户反馈 #5 要求"双端确认"：PC 端弹窗是第一道，
          这里是第二道 —— 复用 `_request_confirm`，会在掌机上
          弹出与"写入确认"同款模态框，用户按 A 才真的删。

        安全检查顺序（沿用全项目的三道门）：
            ① 路径越界检查（`safe_join`）
            ② 目标是目录 → 明确拒绝（本版本不支持删目录，避免误删整棵树）
            ③ 物理确认（阻塞，用户按 A 才继续）

        注意：删除**不需要** `is_writable()` 解锁。这是"操作手续"，
        不是"安全边界"—— 用户在掌机上按 A 才是真正的最终授权。
        """
        try:
            req = json.loads(payload.decode("utf-8") or "{}")
        except Exception:
            sendj(OP_NAK, {"reason": NAK_BAD, "msg": "请求不合法"})
            return

        rel = req.get("path", "")
        target = safe_join(self._root, rel)
        if target is None:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "路径越界"})
            return

        # 目录单独拦一道：删除目录涉及递归，风险面比删文件大得多，
        # 这一版不做，给 PC 一个能直接展示给人看的原因码。
        if os.path.isdir(target):
            sendj(OP_NAK, {"reason": NAK_IS_DIR,
                           "msg": "目标是文件夹，本版本只支持删除文件"})
            return
        if not os.path.exists(target):
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "文件不存在"})
            return
        if not os.path.isfile(target):
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": "不是普通文件"})
            return

        name = os.path.basename(target)
        try:
            size = os.path.getsize(target)
        except Exception:
            size = 0

        # ★ 第二道确认：阻塞等用户按 A（掌机弹同款确认框）
        decision = self._request_confirm("delete", target, name, size,
                                         sendj=sendj)
        if decision is None or not decision.get("ok"):
            sendj(OP_NAK, {"reason": NAK_NO_CONFIRM, "msg": "用户拒绝"})
            return

        try:
            os.remove(target)
        except Exception as e:
            sendj(OP_NAK, {"reason": NAK_DENIED, "msg": f"删除失败: {e}"})
            return

        self.log(f"[net] 已删除 {target}")
        self._emit("delete_done", path=target)
        sendj(OP_ACK, {"op": "delete", "path": target})

    # ---------------- 确认机制（掌机掌握决定权） ----------------

    def _request_confirm(self, action, target, name, size, sendj=None):
        """
        向主线程弹确认框，并**阻塞等待**用户决定。

        这是"掌机掌握最终决定权"的实现点：
        PC 只能发起请求，能不能落地由掌机上的物理按键决定。

        ★ 在阻塞之前先给 PC 发一条 OP_NOTICE(kind=confirm)：
          PC 端收到后就能立刻提示"去掌机按 A"，而不是干等 60 秒
          看着进度条卡住、以为程序死了。

        返回 {"ok": bool}；被取消/超时返回 None。
        """
        # 先通知 PC：马上要在掌机上弹确认框了
        if sendj is not None:
            try:
                sendj(OP_NOTICE, {
                    "kind": NOTICE_CONFIRM,
                    "action": action,
                    "name": name,
                    "size": size,
                    "timeout": 60,
                    "hint": "请在掌机上按 A 允许 / 按 B 拒绝",
                })
            except Exception as e:
                self.log(f"[net] 发送 NOTICE 失败: {e}")

        ev = threading.Event()
        box = {"ok": False}
        self._emit("confirm", action=action, target=target, name=name,
                   size=size, ev=ev, box=box)
        # 最多等 60 秒 —— 用户可能在忙别的
        if not ev.wait(60.0):
            self.log("[net] 确认超时，视为拒绝")
            self._emit("toast", msg="确认超时，已拒绝")
            return None
        return box


# ---------------------------------------------------------------------------
# 供 PC 端复用的最小客户端（也用于本机自测）
# ---------------------------------------------------------------------------


class MiniClient:
    """同步 TCP 客户端。tools/ 下的联调脚本用它。"""

    def __init__(self, host, port=TCP_PORT, timeout=10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.notices = []       # recv_final() 跳过的 OP_NOTICE，备查

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return self

    def close(self):
        try:
            if self.sock:
                self.sock.sendall(pack_frame(OP_BYE))
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def send(self, op, payload=b""):
        self.sock.sendall(pack_frame(op, payload))

    def sendj(self, op, obj):
        self.sock.sendall(pack_json(op, obj))

    def recv(self):
        hdr = self._exact(HDR.size)
        magic, op, length = HDR.unpack(hdr)
        if magic != MAGIC:
            raise ValueError(f"bad magic {magic:#x}")
        return op, (self._exact(length) if length else b"")

    def recv_final(self):
        """
        读一条"实质"消息，自动跳过 OP_NOTICE 这类中间通知。

        ★ 为什么要单独一个方法：
            掌机在弹确认框前会先发 OP_NOTICE（kind=confirm），
            这是**信息性**的，不是应答。写操作的真正结果（ACK/NAK）
            在它后面。联调脚本和测试若只 recv() 一次，
            会先拿到 NOTICE 而误判。用 recv_final() 拿到最终结果，
            同时把跳过的 NOTICE 收集到 self.notices 里备查。
        """
        while True:
            op, payload = self.recv()
            if op == OP_NOTICE:
                try:
                    self.notices.append(json.loads(payload.decode("utf-8")))
                except Exception:
                    self.notices.append({"raw": payload[:80]})
                continue
            return op, payload

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            d = self.sock.recv(n - len(buf))
            if not d:
                raise ConnectionError("对端关闭")
            buf += d
        return buf

    def request_json(self, op, obj, expect=None):
        self.sendj(op, obj)
        while True:
            rop, payload = self.recv()
            if rop == OP_NOTICE:
                # 中间通知（如"该确认了"），跳过继续等实质应答
                try:
                    self.notices.append(json.loads(payload.decode("utf-8")))
                except Exception:
                    pass
                continue
            if expect is None or rop in expect:
                return rop, payload
            if rop in (OP_NAK, OP_ERROR):
                return rop, payload


def discover(timeout=2.0, port=UDP_BEACON_PORT):
    """UDP 广播找掌机，返回列表 [info, ...]。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    msg = json.dumps({"magic": "POCKETTRANSFER", "act": "discover",
                      "from": "pockettransfer-pc"}).encode("utf-8")
    found = {}
    try:
        s.sendto(msg, ("255.255.255.255", port))
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            break
        except Exception:
            break
        try:
            obj = json.loads(data.decode("utf-8"))
        except Exception:
            continue
        if obj.get("act") == "here":
            obj["ip"] = addr[0]
            found[addr[0]] = obj
    s.close()
    return list(found.values())
