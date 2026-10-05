#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
两端协议一致性校验
==================

PC 端 pc/protocol.py 与掌机端 handheld/net.py 的协议常量必须完全一致。
任何一方改了而另一方没跟上，就会出现「连上了但一说话就崩」这类
最难查的问题。这个脚本把两边都导入，逐项比对。

做法：用 ast 解析源文件取出赋值常量（不 import net.py，
      因为它 import 了一堆只在 Linux 上有的东西）。

用法：
    python tools/test-protocol-sync.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PASS, FAIL = [], []


def check(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[!!]'}  {name}" + (f"  —— {note}" if note else ""))


def _safe_eval(node, prev):
    """
    对常量右值做安全求值。

    ast.literal_eval 只认**字面量**，遇到 `64 * 1024` 这种表达式就放弃。
    但协议常量里这种写法很常见（可读性好），所以这里额外处理
    四则运算和一元负号 —— 只允许数字，不执行任何名字查找/调用。
    """
    try:
        return ast.literal_eval(node)
    except Exception:
        pass
    if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Div,
                      ast.Mod, ast.LShift, ast.RShift, ast.BitOr, ast.BitAnd)):
        try:
            a = _safe_eval(node.left, prev)
            b = _safe_eval(node.right, prev)
            op = node.op
            if isinstance(op, ast.Add):
                return a + b
            if isinstance(op, ast.Sub):
                return a - b
            if isinstance(op, ast.Mult):
                return a * b
            if isinstance(op, ast.FloorDiv):
                return a // b
            if isinstance(op, ast.Div):
                return a / b
            if isinstance(op, ast.Mod):
                return a % b
            if isinstance(op, ast.LShift):
                return a << b
            if isinstance(op, ast.RShift):
                return a >> b
            if isinstance(op, ast.BitOr):
                return a | b
            if isinstance(op, ast.BitAnd):
                return a & b
        except Exception:
            return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _safe_eval(node.operand, prev)
        return -v if isinstance(v, (int, float)) else None
    return None


def grab_constants(path):
    """从源文件里抽取所有模块级大写常量赋值。"""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    out = {}
    # 两遍：先把纯字面量收进来，供依赖前值的表达式求值用
    for _pass in (1, 2):
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if not isinstance(t, ast.Name):
                    continue
                if not t.id.isupper():
                    continue
                v = _safe_eval(node.value, out)
                if v is not None or t.id in out:
                    out[t.id] = v
    return out


def grab_struct(path, name):
    """抽取 name = struct.Struct("fmt") 的格式串。"""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == name:
                v = node.value
                if (isinstance(v, ast.Call) and
                        getattr(v.func, "attr", "") == "Struct" and v.args):
                    try:
                        return ast.literal_eval(v.args[0])
                    except Exception:
                        pass
    return None


# 必须完全一致的键
MUST_MATCH = [
    "MAGIC", "TCP_PORT", "UDP_BEACON_PORT", "MAX_PAYLOAD", "CHUNK",
    "OP_PING", "OP_PONG", "OP_GET_DEVICE_INFO", "OP_DEVICE_INFO",
    "OP_LIST_DIR", "OP_DIR_LISTING", "OP_GET_FILE", "OP_FILE_CHUNK",
    "OP_FILE_END", "OP_PUT_FILE_BEGIN", "OP_PUT_FILE_CHUNK",
    "OP_MKDIR_BEGIN", "OP_ACK", "OP_NAK", "OP_ERROR", "OP_BYE",
    "NAK_LOCKED", "NAK_NO_CONFIRM", "NAK_DENIED", "NAK_BUSY", "NAK_BAD",
]


def main():
    print("=" * 64)
    print("  协议一致性校验（pc/protocol.py  vs  handheld/net.py）")
    print("=" * 64)

    p_pc = os.path.join(ROOT, "pc", "protocol.py")
    p_hh = os.path.join(ROOT, "handheld", "net.py")
    for p in (p_pc, p_hh):
        if not os.path.exists(p):
            print(f"!! 找不到 {p}")
            return 1

    pc = grab_constants(p_pc)
    hh = grab_constants(p_hh)

    print(f"  PC  端常量 {len(pc)} 个 / 掌机端常量 {len(hh)} 个\n")

    # ---- 1. 逐项比对 ----
    print("1. 关键常量逐项比对")
    mism = []
    for k in MUST_MATCH:
        a, b = pc.get(k, "<缺失>"), hh.get(k, "<缺失>")
        if a != b:
            mism.append(f"{k}: PC={a} 掌机={b}")
    check(f"{len(MUST_MATCH)} 个关键常量完全一致", not mism,
          "; ".join(mism[:4]) if mism else "")
    print()

    # ---- 2. 包头格式 ----
    print("2. 包头 struct 格式")
    f_pc = grab_struct(p_pc, "HDR")
    f_hh = grab_struct(p_hh, "HDR")
    check('两端 HDR 格式串一致 且 为 ">HHI"',
          f_pc == f_hh == ">HHI", f"PC={f_pc!r} 掌机={f_hh!r}")

    # 包头实际字节数必须等于 2+2+4
    import struct as _s
    check("包头字节数为 8", _s.calcsize(f_hh) == 8,
          f"实际 {_s.calcsize(f_hh)}")
    print()

    # ---- 3. 操作码无冲突 ----
    print("3. 操作码唯一性（防手误写重）")
    ops = {k: v for k, v in hh.items() if k.startswith("OP_")}
    seen = {}
    dup = []
    for k, v in ops.items():
        if v in seen:
            dup.append(f"{seen[v]} 与 {k} 都是 {v:#06x}")
        seen[v] = k
    check(f"{len(ops)} 个操作码互不相同", not dup,
          "; ".join(dup) if dup else "")
    print()

    # ---- 4. NAK 原因码唯一 + PC 端有人话 ----
    print("4. NAK 原因码")
    naks = {k: v for k, v in hh.items() if k.startswith("NAK_")}
    seen2 = {}
    dup2 = []
    for k, v in naks.items():
        if v in seen2:
            dup2.append(f"{seen2[v]} 与 {k} 都是 {v}")
        seen2[v] = k
    check(f"{len(naks)} 个原因码互不相同", not dup2,
          "; ".join(dup2) if dup2 else "")

    # PC 端 protocol.py 里应有 NAK_TEXT 覆盖所有原因码
    src_pc = open(p_pc, "r", encoding="utf-8").read()
    missing = [k for k in naks if k.split("_", 1)[1] not in src_pc.upper()]
    check("PC 端 NAK_TEXT 覆盖全部原因码",
          "NAK_TEXT" in src_pc and "NAK_LOCKED" in src_pc and
          "NAK_NO_CONFIRM" in src_pc and "NAK_DENIED" in src_pc and
          "NAK_BUSY" in src_pc and "NAK_BAD" in src_pc)
    print()

    # ---- 5. 分块大小合理性 ----
    print("5. 分块与上限")
    check("CHUNK 是 64KB", hh.get("CHUNK") == 64 * 1024)
    check("CHUNK < MAX_PAYLOAD", hh.get("CHUNK", 0) < hh.get("MAX_PAYLOAD", 0),
          f"{hh.get('CHUNK')} < {hh.get('MAX_PAYLOAD')}")
    check("MAX_PAYLOAD 能容纳一块（含 4 字节序号）",
          hh.get("CHUNK", 0) + 4 <= hh.get("MAX_PAYLOAD", 0))
    print()

    # ---- 6. 端口 ----
    print("6. 端口")
    check("TCP 端口在 1024~65535", 1024 < hh.get("TCP_PORT", 0) < 65536,
          f"{hh.get('TCP_PORT')}")
    check("UDP 端口在 1024~65535", 1024 < hh.get("UDP_BEACON_PORT", 0) < 65536,
          f"{hh.get('UDP_BEACON_PORT')}")
    check("TCP 与 UDP 端口不同", hh.get("TCP_PORT") != hh.get("UDP_BEACON_PORT"))
    print()

    print("=" * 64)
    print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 64)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过：两端协议完全一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
