#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轴映射表语义验证（纯逻辑，不需要真机）

真机实测事实（2026-10-05 引导式标定照片读数）
=============================================
十字键 (D-Pad)：
    ABS_HAT0X(16)  值 ±1   -1=左  +1=右
    ABS_HAT0Y(17)  值 ±1   -1=上  +1=下

模拟摇杆（左摇杆=轴2/3，右摇杆=轴4/5，成对分配）：
    LU code=3 peak=-4092    LD code=3 peak=+4071
    LL code=2 peak=-3792    LR code=2 peak=+4096
    RU code=5 peak=-3675    RD code=5 peak=+4096
    RL code=4 peak=-4096    RR code=4 peak=+3309

    规律：2/3 一对 = 左摇杆，4/5 一对 = 右摇杆；
          每对里 code 小的 = 水平(H)，code 大的 = 垂直(V)；
          全部"负 = 上/左，正 = 下/右"。

本脚本验证 AXIS_ROLE 的**结构与关键不变量**，并把标定实测值固化为回归基线。
特别强调：HAT（十字键）死区必须为 0 —— 否则 ±1 会被当回中丢掉。

用法：
    python tools/test-axis-map.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "handheld"))

PASS, FAIL = [], []


def check(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[!!]'}  {name}" + (f"  —— {note}" if note else ""))


# ★ 2026-10-05 真机标定实测基线（照片读数，勿轻易改）
CALIB_BASELINE = {
    #  步骤          code   peak  期望方向
    "LU": (3, -4092, "上"),
    "LD": (3, +4071, "下"),
    "LL": (2, -3792, "左"),
    "LR": (2, +4096, "右"),
    "RU": (5, -3675, "上"),
    "RD": (5, +4096, "下"),
    "RL": (4, -4096, "左"),
    "RR": (4, +3309, "右"),
}

CALIB_LABEL = {
    "LU": "左摇杆 上", "LD": "左摇杆 下", "LL": "左摇杆 左", "LR": "左摇杆 右",
    "RU": "右摇杆 上", "RD": "右摇杆 下", "RL": "右摇杆 左", "RR": "右摇杆 右",
}



def main():
    import main as m

    print("=" * 62)
    print("  轴映射表语义验证")
    print("=" * 62)
    print(f"  摇杆死区 = {m.STICK_DEADZONE}")
    print(f"  HAT  死区 = {m.HAT_DEADZONE}")
    print(f"  映射表 = {m.AXIS_ROLE}")
    print()

    # ---- 1. ★ HAT 死区必须为 0（十字键根因）----
    print("1. ★ HAT（十字键）死区检查")
    check("HAT_DEADZONE == 0",
          m.HAT_DEADZONE == 0,
          "★ 十字键值是 ±1；死区若为 800 会被当回中全部丢弃"
          "（这是'方向键无反应'的第二根因）")
    check("code 16 用 HAT 死区",
          m.AXIS_ROLE[16][2] == m.HAT_DEADZONE)
    check("code 17 用 HAT 死区",
          m.AXIS_ROLE[17][2] == m.HAT_DEADZONE)
    check("code 16 能识别 ±1",
          m.axis_direction(16, -1) == "左" and
          m.axis_direction(16, +1) == "右",
          f"实际 -1→{m.axis_direction(16, -1)} +1→{m.axis_direction(16, +1)}")
    check("code 17 能识别 ±1",
          m.axis_direction(17, -1) == "上" and
          m.axis_direction(17, +1) == "下",
          f"实际 -1→{m.axis_direction(17, -1)} +1→{m.axis_direction(17, +1)}")
    check("HAT 值为 0 时回中",
          m.axis_direction(16, 0) is None and
          m.axis_direction(17, 0) is None)
    print()

    # ---- 2. 摇杆死区 ----
    print("2. 摇杆死区检查")
    check("摇杆死区内回中",
          m.axis_direction(4, 0) is None and
          m.axis_direction(4, m.STICK_DEADZONE) is None)
    check("摇杆超出死区可触发",
          m.axis_direction(4, m.STICK_DEADZONE + 1) is not None)
    check("未知轴码返回 None", m.axis_direction(99, 3700) is None)
    print()

    # ---- 3. 表结构完整性 ----
    print("3. 映射表结构")
    ok = True
    bad = []
    for code, trio in m.AXIS_ROLE.items():
        if not (isinstance(trio, tuple) and len(trio) == 3):
            ok = False
            bad.append(f"{code}→{trio}")
            continue
        kind, sign, dz = trio
        if kind not in ("H", "V") or sign not in (-1, 1) or dz < 0:
            ok = False
            bad.append(f"{code}→{trio}")
    check("每项都是 (role, sign, deadzone) 且取值合法", ok,
          ",".join(bad) if bad else "")
    print()

    # ---- 4. 值域全覆盖 ----
    print("4. 值域全覆盖（不允许出现未映射方向）")
    ok = True
    bad = []
    for code in m.AXIS_ROLE:
        for v in (-3800, -1500, -1, 0, +1, 1500, 3800):
            d = m.axis_direction(code, v)
            if d not in (None, "上", "下", "左", "右"):
                ok = False
                bad.append(f"c{code}v{v}→{d}")
    check("所有轴的返回值都在合法集合内", ok, ",".join(bad) if bad else "")
    print()

    # ---- 5. 四个方向都能触达 ----
    print("5. 导航可达性")
    got = set()
    for code in m.AXIS_ROLE:
        for v in (-3800, -1, +1, 3800):
            d = m.axis_direction(code, v)
            if d:
                got.add(d)
    check("上/下/左/右 四向均可达成",
          got == {"上", "下", "左", "右"},
          f"实际 {sorted(got)}")
    print()

    # ---- 6. 死区分轴生效（HAT 不被大死区影响）----
    print("6. 死区分轴验证（关键不变量）")
    check("HAT ±1 有效（大死区不影响它）",
          m.axis_direction(16, 1) is not None and
          m.axis_direction(17, 1) is not None,
          "如果 HAT 和摇杆共用死区，这里会是 None")
    check("摇杆 ±1 无效（小抖动不该触发）",
          m.axis_direction(4, 1) is None and
          m.axis_direction(4, -1) is None,
          "摇杆有噪声，必须用大死区")
    print()

    # ---- 7. ★ 标定实测回归（核心：8 个方向必须完全对上）----
    print("7. ★ 标定实测回归（2026-10-05 照片读数）")
    all_ok = True
    for key, (code, peak, want_dir) in CALIB_BASELINE.items():
        got = m.axis_direction(code, peak)
        ok = (got == want_dir)
        if not ok:
            all_ok = False
        check(f"{key} {CALIB_LABEL[key]}: c{code} {peak:+d} → {want_dir}",
              ok, f"实际 {got}")
    check("★ 8 个摇杆方向全部与真机标定一致", all_ok)
    print()

    # ---- 8. 轴码配对规律（左杆 2/3，右杆 4/5）----
    print("8. 轴码配对规律")
    check("左摇杆 = 轴 2(H) / 3(V)",
          m.AXIS_ROLE[2][0] == "H" and m.AXIS_ROLE[3][0] == "V",
          f"实际 2→{m.AXIS_ROLE[2][0]} 3→{m.AXIS_ROLE[3][0]}")
    check("右摇杆 = 轴 4(H) / 5(V)",
          m.AXIS_ROLE[4][0] == "H" and m.AXIS_ROLE[5][0] == "V",
          f"实际 4→{m.AXIS_ROLE[4][0]} 5→{m.AXIS_ROLE[5][0]}")
    check("规律：每对里 code 小的为 H，大的为 V",
          m.AXIS_ROLE[2][0] == "H" and m.AXIS_ROLE[3][0] == "V" and
          m.AXIS_ROLE[4][0] == "H" and m.AXIS_ROLE[5][0] == "V")
    check("全部摇杆轴 sign 均为 +1（负=上/左，正=下/右）",
          all(m.AXIS_ROLE[c][1] == 1 for c in (2, 3, 4, 5)),
          f"实际 {[m.AXIS_ROLE[c][1] for c in (2, 3, 4, 5)]}")
    print()

    # ---- 9. 摇杆峰值远大于死区（噪声裕量）----
    print("9. 死区裕量检查")
    peaks = [abs(p) for (_c, p, _d) in CALIB_BASELINE.values()]
    check("摇杆最小峰值 > 死区 3 倍",
          min(peaks) > m.STICK_DEADZONE * 3,
          f"最小峰值 {min(peaks)} vs 死区 {m.STICK_DEADZONE}")
    print()

    print("=" * 62)
    print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 62)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过：轴映射表结构正确。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
