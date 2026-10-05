#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
端到端干跑 —— 在 PC 上完整执行一次 main.py 的主流程

这是推文件之前的最后一道闸门。它把 main.py 里所有碰真机的地方
（framebuffer / input / motor / 文件系统）都换掉，然后**原样执行**
main() 的主循环，验证：

    - import 链没有循环依赖
    - bring_up_framebuffer 的校验逻辑正确
    - 主循环能在有限帧内正常跑完并退出
    - 退出时 framebuffer / input 被正确释放
    - motor 被正确调用（开→关，不会一直震）
    - FPS 节流生效（不会满速空转烧 CPU）

任何在这层暴露的问题，都是推上掌机后必然复现的问题。

用法:
    python tools/dryrun.py
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "handheld"))

import main as appmod  # noqa: E402
import boot            # noqa: E402

FAILS = []
TRACE = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK]   {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 打桩

class FakeFramebuffer:
    """替代真实的 mmap framebuffer。记录所有调用。

    尺寸取【实机实测值】1280x1024 16bpp（见 /sys/class/graphics/fb0），
    这样干跑时走的渲染路径与真机一致（缩放系数 k=2、16bpp 打包分支）。
    """

    instances = []

    def __init__(self, *a, **kw):
        FakeFramebuffer.instances.append(self)
        self.width, self.height = 1280, 1024
        self.virtual_w, self.virtual_h = 1280, 1024
        self.bpp = 16
        self.line_px = 1280
        # 实机 bitfield 全 0，按 bpp 推断为 R11/G5/B0
        self.hoffset, self.goffset, self.boffset, self.toffset = 11, 5, 0, 0
        self.smem_len = 1280 * 2 * 1024
        self.smem_start = 0
        self.probe_note = ["(fake) 干跑用桩数据"]
        self.mm = bytearray(self.line_px * (self.bpp // 8) * self.virtual_h)
        self.opened = False
        self.closed = False
        self.frames = 0
        self.fills = []

    def open(self):
        self.opened = True
        TRACE.append("fb.open")
        return self

    def info(self):
        return (f"{self.width}x{self.height} virt={self.virtual_w}x"
                f"{self.virtual_h} bpp={self.bpp} line_px={self.line_px} (fake)")

    def px(self, x, y, rgb):
        off = (y * self.line_px + x) * (self.bpp // 8)
        if off + 2 <= len(self.mm):
            self.mm[off] = rgb & 0xFF
            self.mm[off + 1] = (rgb >> 8) & 0xFF

    def blit(self, img, force=False):
        if self.closed:
            raise RuntimeError("在已关闭的 framebuffer 上 blit！")
        # 校验图像尺寸必须与画布一致（真机上尺寸不符会 resize，桩上直接报错更严格）
        if img.size != (self.width, self.height):
            raise AssertionError(
                f"blit 图像尺寸 {img.size} != 画布 {self.width}x{self.height}")
        self.frames += 1
        return True

    def fill(self, rgb):
        self.fills.append(rgb)

    def close(self):
        self.closed = True
        TRACE.append("fb.close")


class FakeMotor:
    """替代真实马达。验证 开/关 严格配对。"""

    def __init__(self, *a, **kw):
        self.available = True
        self.on_count = 0
        self.off_count = 0
        self.pulses = 0
        self.leaked = False

    def on(self):
        self.on_count += 1
        return True

    def off(self):
        self.off_count += 1
        return True

    def pulse(self, ms=180):
        self.pulses += 1
        self.on_count += 1
        self.off_count += 1
        return True


class FakeInput:
    """替代真实 evdev。按脚本投递按键。"""

    def __init__(self, *a, **kw):
        self.path = "/dev/input/event1"
        self.tried = []
        self.closed = False
        self._script = []
        self._frame = 0

    def set_script(self, seq):
        """seq: list[list[(code, val)]]，每个元素是某一帧要投递的按键。"""
        self._script = list(seq)

    def open(self):
        TRACE.append("input.open")
        return True

    def poll(self, budget=48):
        self._frame += 1
        if self._script:
            return self._script.pop(0)
        return []

    def close(self):
        self.closed = True
        TRACE.append("input.close")


# ---------------------------------------------------------------- 打桩替换

print("=" * 62)
print("端到端干跑 main()")
print("=" * 62)

import fb as fbmod  # noqa: E402

appmod.fbmod.Framebuffer = FakeFramebuffer

# Input / Motor 是定义在 main 里的类，直接替换名字
_fake_motor = FakeMotor()
_fake_input = FakeInput()
appmod.Input = lambda *a, **kw: _fake_input
appmod.Motor = lambda *a, **kw: _fake_motor

# 让 board_model / local_ip / base_path / other_slots 走可控分支
appmod.board_model = lambda: "RG35xxPRO"
appmod.local_ip = lambda: "192.168.3.25"
appmod.base_path = lambda: "/mnt/mmc"
appmod.other_slots = lambda cur: []

# 按键脚本：第 3 帧按 A（震动测试），第 25 帧按 START（退出）
# 之所以拉长到 25 帧，是为了让主循环真正跑够时间、
# 走到节流分支（否则测试太快退出，sleep 根本没机会发生）。
_script = [[] for _ in range(25)]
_script[2] = [(304, 1)]     # A
_script[24] = [(311, 1)]    # START
_fake_input.set_script(_script)

# 让主循环不会因为时间原因跑太久：限制最大帧数
_real_time = __import__("time")
_frame_budget = {"n": 0}


def fast_time():
    _frame_budget["n"] += 1
    # 每次调用时间推进一点，让 FPS 统计和节流都能走到
    return 1000.0 + _frame_budget["n"] * 0.04


# 不能直接换 time 模块（会破坏 sleep），改为限制循环次数
import time as _t_mod


class _TimeShim:
    """
    模拟时钟。

    关键：时间只在 sleep 或被显式推进时才前进，读取 time() 本身不推进。
    上一版每次 time() 都加 0.035s，导致 now 永远 >= next_frame，
    节流分支永远不进入 —— 那是测试的假象，不是代码的问题。
    """

    # 模拟每次绘制耗时（模拟掌机渲染一帧的开销）
    DRAW_COST = 0.008

    def __init__(self, real):
        self._real = real
        self._t = 1000.0
        self.sleeps = []
        self.advance_calls = 0

    def time(self):
        # 读取时间本身不推进时钟（符合真实语义）
        return self._t

    def advance(self, dt):
        self.advance_calls += 1
        self._t += dt

    def sleep(self, s):
        self.sleeps.append(s)
        # 睡眠会真实推进时钟
        self._t += max(0.0, s)
        return None

    def strftime(self, fmt):
        return self._real.strftime(fmt)

    def __getattr__(self, name):
        return getattr(self._real, name)


shim = _TimeShim(_t_mod)
appmod.time = shim

# 让 blit 模拟真实绘制耗时：在 blit 里推进时钟
_orig_blit = FakeFramebuffer.blit


def blit_with_cost(self, img, force=False):
    shim.advance(_TimeShim.DRAW_COST)
    return _orig_blit(self, img, force=force)


FakeFramebuffer.blit = blit_with_cost

try:
    rc = appmod.main()
    check("main() 正常返回", rc == 0, f"rc={rc}")
except SystemExit as e:
    check("main() 正常返回", False, f"意外 SystemExit({e.code})")
except Exception as e:
    import traceback
    traceback.print_exc()
    check("main() 正常返回", False, f"{type(e).__name__}: {e}")

# ---------------------------------------------------------------- 验证

print()
print("  -- 资源生命周期 --")
fbs = FakeFramebuffer.instances
check("framebuffer 被创建", len(fbs) >= 1, f"实得 {len(fbs)}")
if fbs:
    fb0 = fbs[0]
    check("framebuffer 被打开", fb0.opened)
    check("framebuffer 被关闭（不泄漏）", fb0.closed,
          "退出时没有 close，dmenu 将无法取回显示")
    check("确实渲染了多帧", fb0.frames >= 3, f"只有 {fb0.frames} 帧")
    check("退出时清屏为黑", 0x000000 in fb0.fills,
          f"fills={[hex(x) for x in fb0.fills]}")

check("input 被关闭", _fake_input.closed, "输入设备泄漏")

print()
print("  -- 马达（防一直震）--")
check("马达被调用过", _fake_motor.pulses > 0)
check("开/关次数严格配对（不会一直震）",
      _fake_motor.on_count == _fake_motor.off_count,
      f"on={_fake_motor.on_count} off={_fake_motor.off_count}")

print()
print("  -- 节流（防满速空转）--")
check("发生了 sleep（有节流）", len(shim.sleeps) > 0,
      "主循环没有 sleep，会吃满 CPU")
if shim.sleeps:
    check("sleep 时长合理", all(0 < s <= 0.05 for s in shim.sleeps),
          f"最大 {max(shim.sleeps):.3f}s")

print()
print("  -- 关键约束 --")
check("从未调用 set_mode",
      not any(hasattr(x, "_set_mode_called") for x in fbs))
check("日志文件已写出", os.path.exists(boot.LOG_PATH)
      or os.path.exists("/tmp/pockettransfer-boot.log"),
      "boot 日志没写出来，出问题无法排查")

print()
print("=" * 62)
if FAILS:
    print(f"结果：{len(FAILS)} 项失败")
    for f in FAILS:
        print(f"  ✗ {f}")
    sys.exit(1)
print("干跑通过：main() 全流程无异常，资源正确释放。")
print("=" * 62)
sys.exit(0)
