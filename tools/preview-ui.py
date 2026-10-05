#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
掌机端 UI 预览 —— 把界面画成 PNG，推掌机之前先看效果
======================================================

【为什么需要这个】
    推一轮掌机的成本很高（改代码 → SSH 推送 → 掌机上点菜单 → 肉眼比对
    → 发现比例不对 → 再来一轮）。本机渲染一张图只要一秒。

【关键：怎么在没有设备的前提下渲染】
    `App.render()` 只依赖 `f` 上的几个**属性**（width/height/偏移量），
    不碰真实 framebuffer。所以造一个假对象塞进去就能出图。
    用 `App.__new__` 绕过 `__init__` 里那些打开设备的部分。

用法:
    python tools/preview-ui.py
产出:
    RG35XX-Transfer/preview/*.png
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "handheld"))

import ui  # noqa: E402

OUT = os.path.join(ROOT, "preview")
os.makedirs(OUT, exist_ok=True)


class FakeFB:
    """只提供 render() 需要的几何属性，不碰任何真实设备。"""

    def __init__(self, w, h):
        self.width, self.height = w, h
        self.hoffset, self.goffset, self.boffset = 16, 8, 0


class FakeFonts:
    """
    字体。

    真机上用的是原厂 9.6MB 中文字库；本机没有那个文件，
    退回 PIL 默认位图字体也能验证**版面比例**（这是我们最想先确认的）。
    文字会小一些、中文可能显示成方框 —— 不影响判断分区是否合理。
    """

    def __init__(self):
        from PIL import ImageFont
        self.source = "(PIL 默认字体 —— 仅验证版面，真机字体更大)"
        self.small = ImageFont.load_default()
        self.body = self.small
        self.head = self.small


def build_app(w=640, h=480, page="home", log=None, st_extra=None):
    """造一个能渲染的 App 空壳，并把堆栈指到指定页面。"""
    app = ui.App.__new__(ui.App)
    app.f = FakeFB(w, h)
    app.fonts = FakeFonts()
    app.k = max(0.5, min(4.0, w / 640.0))
    app.fb_w, app.fb_h = w, h
    app.fbo = (16, 8, 0)

    app.st = {
        "mode": ui.MODE_BROWSE, "model": "RG35xxPRO", "base": "/mnt/mmc",
        "ip": "192.168.3.25", "port": 48200, "udp_port": 48211,
        "input": "/dev/input/event1", "motor": True, "font": "default.ttf",
        "writable": True, "sent": 12 * 1024 * 1024, "recv": 4_700_000,
        "fps": 59.4, "backend": "sdl", "sdl_ver": "2.0.20",
        "fw": "20260522", "peer": "192.168.3.10",
    }
    if st_extra:
        app.st.update(st_extra)

    app.net = FakeNet(state="connected", peer="192.168.3.10")
    app.log_lines = list(log or [])
    app.toasts = []
    app.confirm = None
    app.quit = False
    app.dirty = False
    app.axis_state = {}
    app._tw_cache = {}

    app.pages = {
        "home": ui.HomePage(app),
        "status": ui.StatusPage(app),
        "browser": ui.BrowserPage(app),
        "net": ui.NetPage(app),
        "log": ui.LogPage(app),
        "diag": ui.DiagPage(app),
    }
    app.stack = [app.pages[page]]
    return app


class FakeNet:
    def __init__(self, state="idle", peer=None):
        self.state = state
        self.peer = peer
        self.udp_seen = 3


SAMPLE_LOG = [
    "[18:27:10] 自动扫描已开启：每 7 秒一次，5 分钟后自动停止",
    "[18:27:12] 自动扫描 第 1 次 …（剩余 4:58）",
    "[18:27:19] 自动扫描 第 2 次 …（剩余 4:51）",
    "[18:27:26] 自动扫描 第 3 次 …（剩余 4:44）",
    "[18:28:02] 自动扫描 第 9 次 …（剩余 4:08）",
    "[18:29:35] ✓ 发现 RG35xxPRO @ 192.168.3.10",
    "[18:29:41] PC 已连接：192.168.3.10",
    "[18:30:12] 接收 rom.gba (36.0KB)",
    "[18:30:13] 接收完成 rom.gba (36.0KB)",
    "[18:31:02] 待确认: PC 请求写入文件 saves/a.sav",
    "[18:31:09] 确认框 → 允许（put saves/a.sav）",
    "[18:32:40] 待确认: PC 请求删除文件 Roms/old.gba",
    "[18:32:47] 确认框 → 拒绝（delete Roms/old.gba）",
]


CASES = [
    ("首页_已连接_640x480", "home", 640, 480, SAMPLE_LOG, None),
    ("首页_未连接_640x480", "home", 640, 480, SAMPLE_LOG[:5],
     {"writable": False, "sent": 0, "recv": 0}),
    ("首页_空日志_640x480", "home", 640, 480, [],
     {"writable": True, "sent": 0, "recv": 0}),
    ("状态页_640x480", "status", 640, 480, SAMPLE_LOG, None),
    ("文件页_640x480", "browser", 640, 480, SAMPLE_LOG, None),
]

print("=" * 60)
print("  掌机端 UI 预览渲染")
print("=" * 60)

# 未连接的首页要单独把 net 设成 idle
for name, page, w, h, log, extra in CASES:
    app = build_app(w, h, page, log, extra)
    if "未连接" in name:
        app.net = FakeNet(state="idle", peer=None)
        app.st["peer"] = None
    img = app.render()
    p = os.path.join(OUT, f"{name}.png")
    img.save(p)
    print(f"  已生成 {p}  ({img.size[0]}x{img.size[1]})")

print()
print(f"预览图目录: {OUT}")
print("注意：本机无原厂中文字库，文字用 PIL 默认字体渲染，")
print("      看**版面分区和比例**是准的，字的实际大小请以真机为准。")
