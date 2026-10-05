#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
掌机端确认框"按键语义"专项测试（不需要真掌机、不需要显示器）
==============================================================

【为什么单独测这个】
    用户第四轮实测反馈 #1 原话：
      「默认焦点在拒绝按钮上，得手动把焦点移到允许按钮上，再按 A 键，
        才是允许，直接按 A 键，相当于选择了拒绝，实际上实现的是拒绝功能。」

    这是一个**语义级 bug**：旧实现把 A 键当成"确认当前焦点"，
    而默认焦点在「拒绝」上 —— 用户按 A 想允许，实际执行了拒绝。
    在"写文件 / 删文件"这种不可逆操作上，这个错误会直接毁数据。

    所以这一版把 A/B 改成**直接动作键**：不看焦点，A 就是允许、B 就是拒绝。
    本脚本用真按键序列把这个语义钉死，防止以后被改回去。

【怎么做到不依赖显示器的】
    `Confirm` 和 `_handle_confirm` 都不碰 framebuffer，只处理
    (Evt, box, ev) 这三样东西。所以我们绕开 App.__init__ 里
    那些要打开 /dev/fb0 的部分，用 `App.__new__` 造一个空壳，
    只挂上 `_handle_confirm` 需要的那几个属性。

用法：
    python tools/test-confirm-keys.py
"""
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "handheld"))

PASS, FAIL = [], []


def check(name, cond, note=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[!!]'}  {name}" + (f"  —— {note}" if note else ""))


import ui  # noqa: E402


def make_app():
    """
    造一个只够跑 `_handle_confirm` 的 App 空壳。

    ★ 不能用 App(...) 正常构造：__init__ 会去找 framebuffer / 字体 / 输入设备，
      在没有真掌机（也没接屏幕）的机器上会直接抛异常。
      这里只补 `_handle_confirm` 会碰到的属性。
    """
    app = ui.App.__new__(ui.App)
    app.confirm = None
    app.dirty = False
    app.logs = []
    app.toasts = []
    app.confirm_log = []          # 专记"按键 → 决定"的轨迹

    def log_only(m):
        app.logs.append(m)
        app.confirm_log.append(m)

    def toast(m):
        app.toasts.append(m)

    app.log_only = log_only
    app.toast = toast
    return app


def new_confirm(app, action="put"):
    """装一个待确认的 Confirm，返回 (box, ev)。"""
    box = {"ok": False}
    ev = threading.Event()
    app.confirm = ui.Confirm(action, "/root/Roms/a.gba", "a.gba", 36864, ev, box)
    return box, ev


def key(app, name):
    """模拟按下一个键。"""
    app.handle_pre = None
    ui.App.handle  # 引用一下，避免 linter 说没用
    return ui.Evt("key", name)


def axis(app, name):
    return ui.Evt("axis", name)


def press(app, name):
    """直接调 _handle_confirm —— 这是 handle() 在 confirm 存在时的唯一分支。"""
    app._handle_confirm(ui.Evt("key", name))


def press_axis(app, name):
    app._handle_confirm(ui.Evt("axis", name))


def main():
    print("=" * 66)
    print("  掌机端确认框 · 按键语义专项测试（第四轮反馈 #1）")
    print("=" * 66)

    # ---- 1. 核心语义：A 就是允许，B 就是拒绝 ----
    print("1. 直接动作键（★ 本轮修复的核心）")

    app = make_app()
    box, ev = new_confirm(app)
    press(app, "A")
    check("★ 直接按 A → 允许（box['ok'] 为真）", box["ok"] is True,
          f"box={box}")
    check("★ 按 A 后事件被 set（网络线程能解除阻塞）", ev.is_set())
    check("★ 按 A 后 confirm 被清空", app.confirm is None)

    app = make_app()
    box, ev = new_confirm(app)
    press(app, "B")
    check("★ 直接按 B → 拒绝（box['ok'] 为假）", box["ok"] is False,
          f"box={box}")
    check("★ 按 B 后事件被 set", ev.is_set())
    check("★ 按 B 后 confirm 被清空", app.confirm is None)

    print()

    # ---- 2. 回归测试：不移动焦点时按 A 必须还是允许 ----
    print("2. 回归：默认焦点状态下按 A（旧版就是在这里出的错）")
    app = make_app()
    box, ev = new_confirm(app)
    check("新框默认高亮在「允许」上（纯视觉）",
          app.confirm.picked == 1, f"picked={app.confirm.picked}")
    press(app, "A")
    check("★★ 默认状态下按 A → 允许（旧版这里会变成拒绝）",
          box["ok"] is True, f"box={box}")
    print()

    # ---- 3. 移动高亮不能改变 A/B 的结果 ----
    print("3. 移动高亮只改视觉，不影响按键结果")
    for move in ("左", "右"):
        app = make_app()
        box, ev = new_confirm(app)
        before = app.confirm.picked
        press_axis(app, move)
        after = app.confirm.picked
        check(f"按「{move}」切换了高亮位置",
              after != before, f"{before} → {after}")
        check(f"按「{move}」后 confirm 没有被消费（还在等决定）",
              app.confirm is not None)
        check(f"按「{move}」没有提前回答",
              box["ok"] is False and not ev.is_set())

    # 高亮移到「拒绝」之后按 A，仍然必须是允许
    app = make_app()
    box, ev = new_confirm(app)
    if app.confirm.picked == 1:
        press_axis(app, "左")       # 高亮挪到「拒绝」
    check("高亮已被挪到「拒绝」侧",
          app.confirm.picked == 0, f"picked={app.confirm.picked}")
    press(app, "A")
    check("★★ 高亮在「拒绝」时按 A，结果仍是允许（A 不做焦点确认）",
          box["ok"] is True, f"box={box}")

    # 反向：高亮在「允许」时按 B，必须拒绝
    app = make_app()
    box, ev = new_confirm(app)
    check("高亮在「允许」侧",
          app.confirm.picked == 1)
    press(app, "B")
    check("★★ 高亮在「允许」时按 B，结果仍是拒绝",
          box["ok"] is False, f"box={box}")
    print()

    # ---- 4. START / MENUF 视为拒绝 ----
    print("4. START / MENUF 语义 = 取消 = 拒绝")
    for name in ("START", "MENUF"):
        app = make_app()
        box, ev = new_confirm(app)
        press(app, name)
        check(f"按 {name} → 拒绝", box["ok"] is False, f"box={box}")
        check(f"按 {name} 后 confirm 清空", app.confirm is None)
    print()

    # ---- 5. delete 动作的文案 ----
    print("5. delete 动作的确认框文案（第四轮反馈 #4）")
    app = make_app()
    box, ev = new_confirm(app, action="delete")
    c = app.confirm
    check("标题点明「删除」", "删除" in c.title(), f"{c.title()!r}")
    lines = c.lines()
    check("★ 文案含「不可恢复」警示",
          any("不可恢复" in ln for ln in lines), f"{lines}")
    check("文案含文件名", any("a.gba" in ln for ln in lines), f"{lines}")

    app = make_app()
    box, ev = new_confirm(app, action="mkdir")
    check("mkdir 标题点明「新建目录」", "新建目录" in app.confirm.title())

    app = make_app()
    box, ev = new_confirm(app, action="put")
    check("put 标题点明「写入文件」", "写入文件" in app.confirm.title())
    check("put 文案含大小",
          any("KB" in ln or "B" in ln for ln in app.confirm.lines()),
          f"{app.confirm.lines()}")
    print()

    # ---- 6. 底部帮助栏文案 ----
    print("6. 底部帮助栏（弹确认框时）")
    app = make_app()
    box, ev = new_confirm(app)
    try:
        hint = app.help_text()
    except AttributeError:
        hint = None
    if hint is None:
        # 方法名可能不同，退化成源码断言
        src = open(os.path.join(ROOT, "handheld", "ui.py"),
                   encoding="utf-8").read()
        check("★ 帮助栏文案为「A 允许   B 拒绝」（不含「选择」）",
              '"A 允许   B 拒绝"' in src, "源码断言")
        check("旧文案「←→ 选择」已移除（在 confirm 分支里）",
              'return "←→ 选择   A 允许   B 拒绝"' not in src)
    else:
        check("★ 帮助栏文案为「A 允许   B 拒绝」",
              hint == "A 允许   B 拒绝", f"{hint!r}")
    print()

    print("=" * 66)
    print(f"  结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 66)
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        return 1
    print("全部通过：确认框是直接动作键语义，不会再「按 A 变拒绝」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
