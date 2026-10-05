#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer —— 协议层（PC 端）
=================================

⚠️ 本文件与 handheld/net.py 的协议常量**必须逐字一致**。
   任何一方改动都要同步另一方，并跑 tools/test-protocol-sync.py 校验。

协议格式
--------
所有消息 = 4 字节包头 + 负载

    包头（大端）: MAGIC(2) | OP(2) | LENGTH(4)
    MAGIC  = 0x504B  ('P','K' → PocketTransfer)
    LENGTH = 负载字节数

负载分两类：
    JSON   —— 控制类消息（列目录、设备信息、ACK/NAK）
    二进制 —— 文件数据块（前 4 字节是块序号，后面是原始数据）

为什么要自己定协议而不是用 HTTP：
    掌机只有 1GB 内存、Python 3.10，起 HTTP 服务要多拉一堆东西。
    这个协议全部用 struct + socket 实现，无第三方依赖，启动快。

安全模型（掌机掌握最终决定权）
    写操作（上传文件 / 新建目录）必须由**掌机上的物理按键**确认。
    PC 端只能发起请求，然后阻塞等待掌机应答（最长 60 秒）。
    只读锁定状态下，任何写请求都会被掌机直接 NAK。
"""

import json
import struct

# ---------------------------------------------------------------------------
# 协议常量（★ 与 handheld/net.py 保持一致）
# ---------------------------------------------------------------------------

MAGIC = 0x504B
HDR = struct.Struct(">HHI")          # MAGIC(2) TYPE(2) LENGTH(4) 大端

TCP_PORT = 48200
UDP_BEACON_PORT = 48211

MAX_PAYLOAD = 16 * 1024 * 1024       # 单包上限 16MB
CHUNK = 64 * 1024                    # 文件分块 64KB

# 操作码
OP_PING = 0x0001
OP_PONG = 0x0002
OP_GET_DEVICE_INFO = 0x0003
OP_DEVICE_INFO = 0x0004
OP_LIST_DIR = 0x0010
OP_DIR_LISTING = 0x0011
OP_GET_FILE = 0x0012             # 请求下载（PC → 掌机）
OP_FILE_CHUNK = 0x0013             # 下载数据块（掌机 → PC）
OP_FILE_END = 0x0014             # 下载结束 / 上传结束
OP_PUT_FILE_BEGIN = 0x0020      # 请求上传（PC → 掌机，需掌机确认）
OP_PUT_FILE_CHUNK = 0x0021
OP_MKDIR_BEGIN = 0x0030      # 请求新建目录（需掌机确认）
OP_DELETE_BEGIN = 0x0031     # ★ 请求删除文件（需掌机确认，不可逆）
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

# NAK 原因码 → 人话
NAK_LOCKED = 1      # 写操作被拒绝：掌机未解锁
NAK_NO_CONFIRM = 2      # 写操作被拒绝：用户在掌机上点了拒绝
NAK_DENIED = 3      # 写操作被拒绝：路径不允许
NAK_BUSY = 4      # 网络忙
NAK_BAD = 5      # 请求不合法
NAK_IS_DIR = 6      # ★ 目标是目录：本版本只支持删文件，不支持删目录

NAK_TEXT = {
    NAK_LOCKED: "掌机处于只读状态，请在掌机上按 SELECT 解锁后重试",
    NAK_NO_CONFIRM: "你在掌机上拒绝了本次操作",
    NAK_DENIED: "掌机拒绝了该路径（越界或无权限）",
    NAK_BUSY: "掌机正忙，请稍后重试",
    NAK_BAD: "请求格式不合法",
    NAK_IS_DIR: "目标是文件夹，当前版本只支持删除文件（请先进入文件夹逐个删除）",
}

# ---------------------------------------------------------------------------
# ★ OP_NOTICE 的 kind 取值
# ---------------------------------------------------------------------------
# 这是"掌机需要人动手时，PC 端要说人话"的通道。
# 掌机在弹出确认框之前先发一条 NOTICE，PC 端据此提示操作者
# "去掌机上按 A"，否则用户只会看到一个卡住的进度条。
NOTICE_CONFIRM = "confirm"      # 掌机正弹确认框，等用户按 A/B（60 秒）
NOTICE_LOCKED = "locked"        # 掌机只读，需要先按 SELECT 解锁
NOTICE_BUSY = "busy"            # 掌机正在处理上一个请求
NOTICE_PERM = "perm"            # ★ 掌机主动上报读写状态已变更（按了 SELECT）


class Nack(Exception):
    """掌机明确拒绝。带原因码，便于 UI 提示人话。"""

    def __init__(self, reason, msg=""):
        self.reason = reason
        self.msg = msg or NAK_TEXT.get(reason, f"被拒绝（原因码 {reason}）")
        super().__init__(self.msg)


# ---------------------------------------------------------------------------
# 编解码
# ---------------------------------------------------------------------------

def pack_frame(op, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return HDR.pack(MAGIC, op, len(payload)) + payload


def pack_json(op, obj):
    return pack_frame(op, json.dumps(obj, ensure_ascii=False))


def unpack_header(hdr):
    """返回 (op, length)；magic 不对抛 ValueError。"""
    magic, op, length = HDR.unpack(hdr)
    if magic != MAGIC:
        raise ValueError(f"magic 不匹配: {magic:#06x}")
    if length > MAX_PAYLOAD:
        raise ValueError(f"负载过大: {length}")
    return op, length


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


def fmt_mtime(ts):
    """时间戳 → 'YYYY-MM-DD HH:MM'；非法返回空串。"""
    if not ts:
        return ""
    import time as _t
    try:
        return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(ts))
    except Exception:
        return ""
