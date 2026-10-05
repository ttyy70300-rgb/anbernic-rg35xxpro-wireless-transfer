#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
掌机端首页结构测试（不需要真掌机、不需要显示器）
=================================================

验三件事：

  1. **版面比例** —— 用户要求"上面一多半，下面一小半"。
     比例算错是这类改动最容易出、又最容易被忽视的问题
     （推上去才发现下半被压扁）。

  2. **内容与兜底** —— 左栏字段齐全；日志筛选能命中；
     空日志时不能留白框（否则用户以为功能坏了）。

  3. ★ **按键表防漂移** —— 从各 `Page.handle()` 的源码里提取
     "认过的键"，和 `KEY_HELP` 声明比对。
     这是本文件最重要的部分：本项目已经因为
     "界面说明与实际行为不一致"出过一次严重事故
     （第四轮确认框：界面写「允许(A)」，按 A 实际拒绝）。
     说明和实现必须是同一份事实。

用法：
    python tools/test-home-page.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "handheld"))

PASS, FAIL = [], []


def check(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[!!]'}  {name}" + (f"  —— {note}" if note else ""))


import ui  # noqa: E402

from PIL import Image, ImageDraw, ImageFont  # noqa: E402


class FakeFB:
    def __init__(self, w=640, h=480):
        self.width, self.height = w, h
        self.hoffset, self.goffset, self.boffset = 16, 8, 0


class FakeFonts:
    """
    用 PIL 默认字体。

    ★ 这里有个必须说清的局限：本机没有原厂 9.6MB 中文字库，
      默认字体比真机**小得多**。所以本文件**只断言结构**，
      不断言"文字放得下" —— 那种断言在本机会假通过、到真机才失败。
    """

    def __init__(self):
        self.source = "(PIL 默认)"
        f = ImageFont.load_default()
        self.small = self.body = self.head = f


class FakeNet:
    def __init__(self, state="idle", peer=None):
        self.state = state
        self.peer = peer
        self.udp_seen = 0


def build_app(page="home", log=None, writable=True):
    app = ui.App.__new__(ui.App)
    app.f = FakeFB()
    app.fonts = FakeFonts()
    app.k = 1.0
    app.fb_w, app.fb_h = 640, 480
    app.fbo = (16, 8, 0)
    app.st = {
        "mode": ui.MODE_BROWSE, "model": "RG35xxPRO", "base": "/mnt/mmc",
        "ip": "192.168.3.25", "port": 48200, "udp_port": 48211,
        "input": "/dev/input/event1", "motor": True, "font": "default.ttf",
        "writable": writable, "sent": 0, "recv": 0, "fps": 59.0,
        "backend": "sdl", "sdl_ver": "2.0.20", "fw": "20260522",
        "peer": None,
    }
    app.net = None
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


def main():
    print("=" * 66)
    print("  掌机端首页结构测试")
    print("=" * 66)

    # ---- 1. 页面注册与默认落点 ----
    print("1. 首页注册与开机落点")
    check("★ PAGE_ORDER 含 home 且排第一",
          ui.PAGE_ORDER and ui.PAGE_ORDER[0] == "home",
          f"{ui.PAGE_ORDER}")
    check("★ PAGE_LABEL 有首页", ui.PAGE_LABEL.get("home") == "首页")
    check("标签栏共 5 个页签", len(ui.PAGE_ORDER) == 5,
          f"{len(ui.PAGE_ORDER)}")
    check("★ 默认落点是首页（开机第一眼看到仪表盘）",
          build_app().stack[0].name == "home")
    check("HomePage 已注册进 pages 字典",
          "home" in build_app().pages)
    check("HomePage 有 title", ui.HomePage.title == "首页")
    print()

    # ---- 2. 版面比例（★ 用户核心要求）----
    print("2. 版面比例：上面一多半，下面一小半（★ 用户核心要求）")
    app = build_app()
    d = ImageDraw.Draw(Image.new("RGB", (640, 480)))
    ti = app.app_ti(d, app.fonts)
    content_y = ti["bar_h"] + ti["tab_h"]
    content_h = 480 - content_y - ti["foot_h"]
    inner_h = content_h - 8            # render() 里给页面留的内边距
    top_h = ui.HomePage.top_height(inner_h)
    bot_h = inner_h - top_h
    check("★ 上半明确大于下半（不是 50:50）", top_h > bot_h,
          f"上 {top_h} / 下 {bot_h}")
    ratio = top_h / float(inner_h)
    check("★ 上半占比落在 55%~68%（'一多半'的合理区间）",
          0.55 <= ratio <= 0.68, f"{ratio:.1%}")
    check("TOP_RATIO 常量存在且被引用",
          abs(ui.TOP_RATIO - ratio) < 0.02,
          f"TOP_RATIO={ui.TOP_RATIO}")
    check("下半高度足够放下 3 行按键说明",
          bot_h >= app.line_h(app.fonts.body) * 3 + 30,
          f"下 {bot_h}px")
    check("上半高度足够放下状态明细 5 行",
          top_h >= app.line_h(app.fonts.body) * 5 + 30,
          f"上 {top_h}px")
    print()

    # ---- 3. 左栏数据字段 ----
    print("3. 左栏连接状态：字段来源全部现成")
    src = open(os.path.join(ROOT, "handheld", "ui.py"), encoding="utf-8").read()
    home_src = src.split("class HomePage")[1].split("class StatusPage")[0]
    for field, why in [
        ('st.get("ip")', "本机 IP"),
        ("net.peer", "对端"),
        ('st.get("writable")', "权限"),
        ('st.get("base")', "卡根"),
        ("st.get('sent'", "已发送累计"),
        ("st.get('recv'", "已接收累计"),
    ]:
        check(f"  └ 左栏用了 {why}（{field}）", field in home_src)
    check("★ 左栏没有新增状态字段（全靠现成数据）",
          "self._new_state" not in home_src)
    check("状态有圆点 + 大字（一眼看出连没连上）",
          "ellipse" in home_src and "已连接" in home_src)
    print()

    # ---- 4. 右栏日志筛选与兜底 ----
    print("4. 右栏关键操作日志：筛选 + 兜底 + 滚动")
    app = build_app(log=[
        "[10:00:00] 无关紧要的一行",
        "[10:00:01] PC 已连接：192.168.3.10",
        "[10:00:02] 又一行无关的",
        "[10:00:03] 确认框 → 拒绝（delete a.gba）",
    ])
    hp = app.pages["home"]
    keys = hp.key_lines()
    check("★ 筛选命中「已连接」", any("已连接" in k for k in keys), f"{keys}")
    check("★ 筛选命中「拒绝」", any("拒绝" in k for k in keys))
    check("★ 无关行被滤掉", not any("无关紧要" in k for k in keys), f"{keys}")

    # 全是无关行时 → 兜底显示最近几条，不能空框
    app2 = build_app(log=["[10:00:00] 无关 A", "[10:00:01] 无关 B",
                          "[10:00:02] 无关 C"])
    got = app2.pages["home"].key_lines()
    check("★★ 无关键词命中时兜底显示最近几条（不留空框）",
          len(got) > 0, f"{got}")

    # 完全没日志 → 返回空，由绘制层显示"暂无记录"
    app3 = build_app(log=[])
    check("完全无日志时返回空列表（交给绘制层提示）",
          app3.pages["home"].key_lines() == [])

    # 缓存：长度不变应复用
    app4 = build_app(log=["[10:00:00] 已连接 x"])
    hp4 = app4.pages["home"]
    a = hp4.key_lines()
    b = hp4.key_lines()
    check("筛选结果有缓存（同长度不重算）", a is b)

    # 滚动
    app5 = build_app(log=[f"[10:00:{i:02d}] 已连接 {i}" for i in range(30)])
    hp5 = app5.pages["home"]
    check("初始 scroll=0（跟随最新）", hp5.scroll == 0)
    hp5.handle(ui.Evt("axis", "上"))
    check("按上 scroll 增加", hp5.scroll == 1)
    hp5.handle(ui.Evt("axis", "下"))
    check("按下 scroll 回落", hp5.scroll == 0)
    hp5.handle(ui.Evt("axis", "下"))
    check("★ scroll 不会变成负数", hp5.scroll == 0)
    print()

    # ---- 5. 首页按键行为 ----
    print("5. 首页按键行为")
    app6 = build_app()
    hp6 = app6.pages["home"]
    app6.push = lambda p: app6.stack.append(p)
    consumed = hp6.handle(ui.Evt("key", "A"))
    check("★ A 跳到日志页（下钻看全部）",
          consumed and app6.stack[-1].name == "log",
          f"栈顶={app6.stack[-1].name}")
    app7 = build_app()
    hp7 = app7.pages["home"]
    called = {"n": 0}
    app7.open_log_file = lambda: called.__setitem__("n", called["n"] + 1)
    hp7.handle(ui.Evt("key", "X"))
    check("★ X 调用 open_log_file（打开日志文件）", called["n"] == 1)
    app8 = build_app()
    check("未定义的键不被消费（交给全局处理）",
          app8.pages["home"].handle(ui.Evt("key", "Y")) is False)
    print()

    # ---- 6. ★★ 按键表防漂移（本项目出过事故的地方）----
    print("6. ★★ 按键表防漂移：说明必须与实现一致")
    check("KEY_HELP 覆盖 PAGE_ORDER 里每个页面",
          set(ui.PAGE_ORDER) <= set(ui.KEY_HELP),
          f"缺 {set(ui.PAGE_ORDER) - set(ui.KEY_HELP)}")
    check("FOOTER_HINT 覆盖 PAGE_ORDER 里每个页面",
          set(ui.PAGE_ORDER) <= set(ui.FOOTER_HINT))
    check("diag 页也有底栏提示",
          "diag" in ui.FOOTER_HINT)

    # ★ 从源码里提取每个 Page.handle() 认过的键名，和 KEY_HELP 比对。
    #   思路：各页 handle() 里出现的 "key" 字面量，就是它认的键。
    #   这是"说明 ↔ 实现"一致性的机械校验。
    KNOWN_KEYS = ("A", "B", "X", "Y", "L1", "R1", "L2", "R2",
                  "SELECT", "START", "MENUF", "V+", "V-")
    body = src
    # 切出每个页面类
    segments = {}
    cls_names = ["HomePage", "StatusPage", "BrowserPage", "NetPage",
                 "LogPage", "DiagPage"]
    for i, cn in enumerate(cls_names):
        if f"class {cn}" not in body:
            continue
        seg = body.split(f"class {cn}")[1]
        for nxt in cls_names[i + 1:]:
            if f"class {nxt}" in seg:
                seg = seg.split(f"class {nxt}")[0]
        if "class Confirm" in seg:
            seg = seg.split("class Confirm")[0]
        segments[cn] = seg

    key_to_cls = {"home": "HomePage", "status": "StatusPage",
                  "browser": "BrowserPage", "net": "NetPage",
                  "log": "LogPage", "diag": "DiagPage"}
    total_missing = 0
    for page, cn in key_to_cls.items():
        seg = segments.get(cn, "")
        if "def handle" not in seg:
            continue
        hseg = seg.split("def handle")[1].split("def ")[0]
        # 提取 handle() 里比较过的键名
        used = set()
        for m in re.finditer(r'"([^"]+)"', hseg):
            tok = m.group(1)
            if tok in KNOWN_KEYS:
                used.add(tok)
        declared = set()
        for keys, _desc in ui.KEY_HELP.get(page, []):
            for tok in re.split(r"[ /]+", keys):
                if tok in KNOWN_KEYS:
                    declared.add(tok)
        missing = used - declared
        # 全局键由 App.handle 统一处理，各页 handle 不必声明
        missing -= {"START", "MENUF", "SELECT", "L2", "R2"}
        if missing:
            total_missing += 1
        check(f"  └ {page} 页：handle() 认的键都在 KEY_HELP 里",
              not missing,
              f"说明里缺 {sorted(missing)}" if missing else f"认 {sorted(used)}")

    check("★★ 没有任何页面出现「实现有、说明没有」的键",
          total_missing == 0,
          "★ 这正是第四轮确认框事故的同类问题：说明与实现脱节")

    check("★ 底栏提示已改为从表里读（不再各处硬编码）",
          "FOOTER_HINT.get(" in body and "_footer_hint" in body)
    # 只看 _footer_hint 函数内部 —— 表里有相同词组是正常的，
    # 要断言的是"函数体里不再有手写分支"。
    fh_body = body.split("def _footer_hint")[1].split("def ")[0]
    check("_footer_hint 函数体里没有手写页面分支",
          "if page == " not in fh_body and '"browser"' not in fh_body,
          "改为查表后，函数体里不该再有 if page == … 分支")
    print()

    # ---- 7. 渲染不抛异常 ----
    print("7. 各页渲染冒烟（含边界数据）")
    cases = [
        ("首页-有日志", "home", ["[10:00:00] 已连接 1.2.3.4"], True),
        ("首页-空日志", "home", [], True),
        ("首页-只读", "home", ["[10:00:00] 已连接 1.2.3.4"], False),
        ("状态页", "status", [], True),
        ("日志页", "log", [], True),
    ]
    for name, page, log, writable in cases:
        try:
            a = build_app(page, log, writable)
            if page == "home" and "1.2.3.4" in (log[0] if log else ""):
                a.net = FakeNet(state="connected", peer="192.168.3.10")
            img = a.render()
            ok = img.size == (640, 480)
            err = "" if ok else f"尺寸 {img.size}"
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        check(f"  └ {name} 渲染成功", ok, err)

    # 日志很长也不崩
    try:
        a = build_app("home", [f"[10:00:{i:02d}] 已连接 {i}" for i in range(400)])
        a.net = FakeNet(state="connected", peer="1.2.3.4")
        a.render()
        check("  └ 400 条日志仍能渲染", True)
    except Exception as e:
        check("  └ 400 条日志仍能渲染", False, f"{type(e).__name__}: {e}")
    print()

    print("=" * 66)
    print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 66)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过：首页版面、内容、按键表一致性都符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
