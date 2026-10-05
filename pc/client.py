#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer —— 掌机客户端（PC 端）
=====================================

职责：把协议细节全部封装掉，给 UI 层一个「同步方法调用」的接口。

    cli = Client("192.168.3.25")
    cli.connect()
    info = cli.device_info()
    items = cli.list_dir("Roms")
    cli.download("Roms/a.gba", r"D:\a.gba", on_progress=cb)
    cli.upload(r"D:\b.gba", "Roms/b.gba", on_progress=cb)
    cli.mkdir("Roms/NewDir")
    cli.close()

【关于阻塞与 UI】
    tkinter 是单线程事件循环。所有网络调用都是阻塞的，
    所以调用方必须把耗时操作丢到后台线程，再用 after() 回主线程更新 UI。
    本模块自己不创建线程 —— 保持"纯粹"，由 UI 层决定并发策略。

【写操作的三道门】
    1. 掌机必须已解锁（SELECT 键），否则 NAK_LOCKED
    2. 掌机屏幕会弹确认框，用户按 A 允许 / B 拒绝（最长 60 秒）
    3. 路径必须落在卡根内（掌机侧 safe_join 校验）
    这三道门全在掌机侧执行，PC 端无法绕过 —— 这是设计意图。
"""

import os
import socket
import threading
import time

from protocol import (
    HDR, OP_ACK, OP_BYE, OP_DEVICE_INFO, OP_DIR_LISTING, OP_ERROR,
    OP_FILE_CHUNK, OP_FILE_END, OP_GET_DEVICE_INFO, OP_GET_FILE,
    OP_LIST_DIR, OP_MKDIR_BEGIN, OP_DELETE_BEGIN, OP_NAK, OP_NOTICE,
    OP_PING, OP_PONG,
    OP_PUT_FILE_BEGIN, OP_PUT_FILE_CHUNK, MAGIC, CHUNK, TCP_PORT,
    NAK_TEXT, Nack, fmt_mtime, human_size, pack_frame, pack_json,
    unpack_header,
)


class Client:
    """
    同步 TCP 客户端。

    ★★ 关于线程安全（这里踩过一个大坑）
        协议是"一问一答"式的：发一个请求，读若干条消息直到拿到应答。
        如果**两个线程同时**在这条 socket 上收发，应答会串台 ——
        A 线程在等 DIR_LISTING，却把 B 线程要的 DEVICE_INFO 读走了，
        于是 A 一直等、B 也一直等，双方卡死，最后掌机侧看到
        `Connection reset by peer`。

        真实触发场景：传输完成后 UI 同时发起
            ① remote_reload()  → LIST_DIR
            ② _refresh_info()  → GET_DEVICE_INFO
            ③ _heartbeat()     → PING
        三个后台线程抢一条 socket，于是"传完文件连接就断"。

        解决：所有"发请求 + 读应答"的公开方法都拿同一把 `_io_lock`，
        串行执行。传输（download/upload）也在锁内 —— 这样传输期间
        心跳/刷新会自动排队而不是插进来搅乱流。
    """

    def __init__(self, host, port=TCP_PORT, timeout=15.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.info = {}
        # ★ 串行化所有 socket 请求，避免多线程应答串台
        #   （RLock：download/upload 内部会调 _recv_until，可能重入）
        self._io_lock = threading.RLock()
        # ★ 掌机"需要人动手"时的回调：on_notice(kind, data)
        #   典型是拿它弹提示："请在掌机上按 A 确认"。
        #   由 UI 层设置；模型层只负责在有 NOTICE 时叫一声。
        self.on_notice = None

    # ---------------- 连接管理 ----------------

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

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    @property
    def connected(self):
        return self.sock is not None

    # ---------------- 底层收发 ----------------

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            d = self.sock.recv(n - len(buf))
            if not d:
                raise ConnectionError("掌机断开了连接")
            buf += d
        return buf

    def _recv(self):
        hdr = self._exact(HDR.size)
        op, length = unpack_header(hdr)
        return op, (self._exact(length) if length else b"")

    def _send(self, op, payload=b""):
        self.sock.sendall(pack_frame(op, payload))

    def _sendj(self, op, obj):
        self.sock.sendall(pack_json(op, obj))

    # ---------------- 语义化的请求/应答 ----------------

    def _recv_until(self, expect, timeout=None):
        """
        读消息直到 op ∈ expect。
        若先收到 NAK/ERROR 则抛 Nack。
        其它无关消息（如传输中的 FILE_CHUNK）直接返回给调用方处理。

        ★ OP_NOTICE 是"临时通知"，不改变流程，只转给 on_notice 回调
          （比如提示用户去掌机上按键），然后继续等真正的应答。
        """
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            while True:
                op, payload = self._recv()
                if op in expect:
                    return op, payload
                if op == OP_NOTICE:
                    self._fire_notice(payload)
                    continue
                if op == OP_NAK:
                    import json as _j
                    d = _j.loads(payload.decode("utf-8") or "{}")
                    raise Nack(d.get("reason", 0), d.get("msg"))
                if op == OP_ERROR:
                    raise RuntimeError(
                        payload.decode("utf-8", "replace") or "掌机内部错误")
        finally:
            if timeout is not None:
                self.sock.settimeout(self.timeout)

    def _fire_notice(self, payload):
        """把掌机的临时通知交给 UI 回调（回调异常绝不能影响传输）。"""
        if not self.on_notice:
            return
        import json as _j
        try:
            d = _j.loads(payload.decode("utf-8") or "{}")
        except Exception:
            d = {}
        try:
            self.on_notice(d.get("kind", "notice"), d)
        except Exception:
            pass

    def _expect_ack(self, what):
        op, payload = self._recv_until({OP_ACK})
        import json as _j
        try:
            return _j.loads(payload.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---------------- 公开 API ----------------
    #
    # ★ 每一个"发请求 + 读应答"的方法都整段加 self._io_lock，
    #   保证同一时刻只有一个线程在这条 socket 上说话。
    #   详见类 docstring 里"线程安全"那段。

    def ping(self, payload=b"ping"):
        with self._io_lock:
            self._send(OP_PING, payload)
            op, data = self._recv_until({OP_PONG})
            return data

    def device_info(self):
        """取掌机信息（型号/固件/IP/卡根/权限/统计）。"""
        with self._io_lock:
            self._send(OP_GET_DEVICE_INFO)
            op, payload = self._recv_until({OP_DEVICE_INFO})
            import json as _j
            self.info = _j.loads(payload.decode("utf-8") or "{}")
            return self.info

    def keepalive(self):
        """
        心跳保活。UI 空闲定时器会周期性调它。

        ★ 为什么需要：
            掌机侧对空闲连接有超时兜底（IDLE_TIMEOUT，10 分钟）。
            做这个心跳是为了把"连接还活着"这件事持续告诉对方，
            让用户长时间不动手也不会掉线 —— 这正是"传完一个文件
            连接就断"那个问题的另一半解法（另一半是把超时调大）。

        ★ 2026-10-05 改为用 GET_DEVICE_INFO 而不是 PING：
            原因 —— 用户在掌机上按 SELECT 切读写时，PC 常常是**空闲**的，
            不会主动读 socket，于是那条状态变更通知没人接。
            借心跳这次往返顺便把设备信息（含 writable）读回来，
            一次往返同时完成"保活"和"权限同步"两件事，
            不需要再单开一个轮询定时器。

            期间收到的 OP_NOTICE 会照常转给 on_notice 回调
            （由 _recv_until 负责），所以掌机主动推的 perm 通知
            也不会丢。

        返回 True 表示连接仍然健康。
        """
        # ★ 用非阻塞方式拿锁：若此刻有传输在跑，直接跳过这次心跳，
        #   而不是排队等着（心跳迟到几秒无所谓，插队才有害）。
        if not self._io_lock.acquire(blocking=False):
            return True
        try:
            self._send(OP_GET_DEVICE_INFO)
            op, payload = self._recv_until({OP_DEVICE_INFO}, timeout=10.0)
            import json as _j
            self.info = _j.loads(payload.decode("utf-8") or "{}")
            return True
        except Exception:
            return False
        finally:
            self._io_lock.release()

    def list_dir(self, path=""):
        """
        列目录。返回 dict：
            {"path": 相对路径, "abs": 绝对路径, "items": [...]}
        item = {"name", "dir", "size", "mtime"}
        """
        with self._io_lock:
            self._sendj(OP_LIST_DIR, {"path": path})
            op, payload = self._recv_until({OP_DIR_LISTING})
            import json as _j
            return _j.loads(payload.decode("utf-8") or "{}")

    def download(self, remote_path, local_path, on_progress=None,
                 should_cancel=None):
        """
        从掌机下载文件到本地。

        on_progress(got, total)  —— 每块回调一次（在调用线程里同步执行）
        should_cancel()          —— 返回 True 时中止（每块查一次）

        返回实际写入的字节数。
        """
        with self._io_lock:
            return self._download_locked(remote_path, local_path,
                                         on_progress, should_cancel)

    def _download_locked(self, remote_path, local_path, on_progress,
                         should_cancel):
        self._sendj(OP_GET_FILE, {"path": remote_path})
        op, payload = self._recv_until({OP_ACK})
        import json as _j
        meta = _j.loads(payload.decode("utf-8") or "{}")
        total = int(meta.get("size") or 0)
        name = meta.get("name") or os.path.basename(remote_path)

        # 先写临时文件，收完再改名 —— 中断不会留下半个"看起来正常"的文件
        tmp = local_path + ".part"
        got = 0
        try:
            with open(tmp, "wb") as f:
                while True:
                    if should_cancel and should_cancel():
                        raise InterruptedError("用户取消")
                    op, payload = self._recv()
                    if op == OP_FILE_CHUNK:
                        # 前 4 字节是块序号，校验一下连续性
                        if len(payload) < 4:
                            raise ValueError("数据块过短")
                        seq = int.from_bytes(payload[:4], "big")
                        if seq != got:
                            raise ValueError(
                                f"块序号断裂: 期望 {got} 收到 {seq}")
                        data = payload[4:]
                        f.write(data)
                        got += len(data)
                        if on_progress:
                            on_progress(got, total)
                    elif op == OP_FILE_END:
                        break
                    elif op == OP_NOTICE:
                        # 传输中夹带的临时通知：转出去，继续收
                        self._fire_notice(payload)
                    elif op == OP_NAK:
                        d = _j.loads(payload.decode("utf-8") or "{}")
                        raise Nack(d.get("reason", 0), d.get("msg"))
                    else:
                        raise ValueError(f"传输中收到意外消息 0x{op:04X}")
                f.flush()
                os.fsync(f.fileno())
            if total and got != total:
                raise ValueError(f"大小不符: 期望 {total} 收到 {got}")
            os.replace(tmp, local_path)
            return got
        except BaseException:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise

    def upload(self, local_path, remote_path, on_progress=None,
               should_cancel=None):
        """
        上传文件到掌机。

        ⚠️ 掌机侧会弹确认框并**阻塞等待用户按键**（最长 60 秒），
           所以这个调用的首个 ACK 可能来得很慢 —— UI 上要给出提示。

        返回实际发送的字节数。
        """
        with self._io_lock:
            return self._upload_locked(local_path, remote_path,
                                       on_progress, should_cancel)

    def _upload_locked(self, local_path, remote_path, on_progress,
                       should_cancel):
        size = os.path.getsize(local_path)
        name = os.path.basename(local_path)

        self._sendj(OP_PUT_FILE_BEGIN,
                    {"path": remote_path, "name": name, "size": size})
        # 这里可能等 60 秒 —— 用户要在掌机上按 A 确认
        self._recv_until({OP_ACK}, timeout=75.0)

        sent = 0
        try:
            with open(local_path, "rb") as f:
                while True:
                    if should_cancel and should_cancel():
                        raise InterruptedError("用户取消")
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    # 同样带块序号，与下载对称
                    self._send(OP_PUT_FILE_CHUNK,
                               sent.to_bytes(4, "big") + chunk)
                    sent += len(chunk)
                    if on_progress:
                        on_progress(sent, size)
            self._sendj(OP_FILE_END,
                        {"name": name, "size": sent, "sum": str(sent)})
            # 掌机收满后会回 ACK put_done
            try:
                self._recv_until({OP_ACK}, timeout=30.0)
            except Nack:
                raise
            return sent
        except BaseException:
            raise

    def mkdir(self, remote_path):
        """在掌机新建目录（需掌机确认）。"""
        with self._io_lock:
            self._sendj(OP_MKDIR_BEGIN, {"path": remote_path})
            self._recv_until({OP_ACK}, timeout=75.0)
            return True

    def delete(self, remote_path):
        """
        删除掌机上的文件（需掌机确认）。

        ★ 与 mkdir 同构，但超时给得更宽：
          掌机侧会弹出模态框等用户按 A/B，最长 60 秒。
          这里给 75 秒，留出网络往返 + 掌机渲染的余量，
          避免 PC 提前超时断开、而用户其实还在掌机上看那个框。
        """
        with self._io_lock:
            self._sendj(OP_DELETE_BEGIN, {"path": remote_path})
            self._recv_until({OP_ACK}, timeout=75.0)
            return True


# ---------------------------------------------------------------------------
# 自动发现（UDP 广播）
# ---------------------------------------------------------------------------

def discover(timeout=2.0, port=None):
    """
    广播找掌机。返回 [info, ...]，info 里含 ip / model / tcp_port。

    为什么用广播而不是 mDNS：
        掌机固件未必有 avahi；广播 + JSON 是零依赖且够用的方案。
        广播地址 255.255.255.255 在多数家用路由器下可用。
    """
    import json
    from protocol import UDP_BEACON_PORT
    port = port or UDP_BEACON_PORT

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.4)
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
            continue
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
