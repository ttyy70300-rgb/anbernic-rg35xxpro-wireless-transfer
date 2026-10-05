#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
input_event 结构体解包验证（离线，不需要真机）

背景
====
2026-10-05 真机实测发现：所有 ABS 事件的 value 都是 +1 而非 ±3700，
且十字方向键完全没有事件。

根因：用了 struct.unpack("llHHI", raw) 解 24 字节的 input_event。
      Python 的 struct **无对齐**，'l' 恒为 4 字节：
          "llHHI" = 4+4+2+2+4 = 16 字节   ← 少了 8 字节！
      而 64 位 Linux 的 struct input_event 是：
          struct timeval { long tv_sec; long tv_usec; }  → 8 + 8
          __u16 type; __u16 code; __s32 value;          → 2 + 2 + 4
          共 24 字节
      ⇒ 必须用 "qqHHi"

本脚本用构造的字节流证明这个结论。

用法：
    python tools/test-input-struct.py
"""
import struct
import sys

PASS, FAIL = [], []


def check(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[!!]'}  {name}" + (f"  —— {note}" if note else ""))


def main():
    print("=" * 66)
    print("  input_event 结构体解包验证")
    print("=" * 66)

    # ---- 1. 格式宽度 ----
    print("\n1. struct 格式宽度（Python 无对齐）")
    check("'l' 是 4 字节（不是 C 的 long）", struct.calcsize("l") == 4,
          f"实际 {struct.calcsize('l')}")
    check("'q' 是 8 字节", struct.calcsize("q") == 8)
    check("llHHI = 16 字节（错误格式）", struct.calcsize("llHHI") == 16,
          f"实际 {struct.calcsize('llHHI')}")
    check("qqHHi = 24 字节（正确格式）", struct.calcsize("qqHHi") == 24,
          f"实际 {struct.calcsize('qqHHi')}")
    check("QQHHi = 24 字节（无符号计时也可）",
          struct.calcsize("QQHHi") == 24)

    # ---- 2. 真实轴事件解包 ----
    print("\n2. 真实轴事件的解包结果")
    # 左摇杆向上：ABS_Z(2) = -3700
    real = struct.pack("qqHHi", 1759600000, 123456, 3, 2, -3700)
    check("构造的字节流是 24 字节", len(real) == 24)

    s, us, t, c, v = struct.unpack("qqHHi", real)
    check("qqHHi: type == 3 (EV_ABS)", t == 3, f"实际 {t}")
    check("qqHHi: code == 2 (ABS_Z)", c == 2, f"实际 {c}")
    check("qqHHi: value == -3700（负数保真）", v == -3700, f"实际 {v}")

    # 错误的格式
    t2, c2, v2 = struct.unpack("llHHI", real[:16])[2:]
    check("llHHI: type ≠ 3（错位，事件会被丢弃）", t2 != 3,
          f"实际读到 type={t2}")
    check("llHHI: value ≠ -3700（真实值丢失）", v2 != -3700,
          f"实际读到 value={v2}")

    # ---- 3. 按键事件解包 ----
    print("\n3. 按键事件（A = code 304）")
    key = struct.pack("qqHHi", 1759600000, 999, 1, 304, 1)
    s, us, t, c, v = struct.unpack("qqHHi", key)
    check("qqHHi: type == 1 (EV_KEY)", t == 1)
    check("qqHHi: code == 304 (A)", c == 304)
    check("qqHHi: value == 1 (按下)", v == 1)

    t2, c2, v2 = struct.unpack("llHHI", key[:16])[2:]
    check("llHHI: code ≠ 304（按键也会读错）", c2 != 304,
          f"实际读到 code={c2}")

    # ---- 4. 值域保真（正负都要）----
    print("\n4. 值域保真（±3700 全范围）")
    ok = True
    bad = []
    for code in (2, 3, 4, 5):
        for val in (-3700, -1800, 0, 1800, 3700):
            buf = struct.pack("qqHHi", 1759600000, 0, 3, code, val)
            _s, _u, _t, gc, gv = struct.unpack("qqHHi", buf)
            if gc != code or gv != val:
                ok = False
                bad.append(f"c{code}v{val}→c{gc}v{gv}")
    check("所有 code/value 组合往返无损", ok, ",".join(bad) if bad else "")

    # ---- 5. 反复解包（模拟连续读取）----
    print("\n5. 连续多事件流解包（模拟 os.read 一次读多个）")
    stream = b""
    events = [(3, 4, 3700), (3, 4, 0), (1, 304, 1), (1, 304, 0),
              (3, 2, -3700)]
    for i, (t, c, v) in enumerate(events):
        stream += struct.pack("qqHHi", 1759600000, i, t, c, v)
    check("流长度 = 事件数 × 24", len(stream) == 24 * len(events),
          f"实际 {len(stream)}")

    got = []
    for i in range(len(stream) // 24):
        chunk = stream[i * 24:(i + 1) * 24]
        _s, _u, t, c, v = struct.unpack("qqHHi", chunk)
        got.append((t, c, v))
    check("逐条解包结果与写入一致", got == events, f"读到 {got}")

    # ---- 6. read 长度必须与格式匹配 ----
    print("\n6. 读取长度检查")
    check("os.read 长度 24 与 qqHHi 匹配", 24 == struct.calcsize("qqHHi"),
          "读取长度 ≠ 结构长度会导致错位/半包")

    print()
    print("=" * 66)
    print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 66)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过：结构体解包正确。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
