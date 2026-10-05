#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer 掌机端 —— UI 框架（阶段 2）
==========================================

把原来"一屏到底"的绘制改成**页面栈 + 焦点导航**，为文件浏览器腾出结构。

【设计】
    Page       基类：handle(evt) 处理输入，draw(d, ctx) 绘制内容区
    App        页面栈 + 全局状态 + 输入分发 + 模态确认框
    Widgets    复用的小部件（列表、状态栏、提示条、确认框）

【为什么不用现成 GUI 框架】
    这台机器只有 1GB 内存，且原厂 5 个应用全是"PIL 画图 + 裸 evdev 输入"
    的极简模式。引入 tkinter/Qt 既装不上也没必要。
    而且 PIL 的中文字形渲染已经验证可用（原厂字体 9.6MB 全字库）。

【输入约定（来自实机实测）】
    按键 EV_KEY：A/B/Y/X/L1/R1/L2/R2/SELECT/START/MENUF/V+/V-
    方向    EV_ABS：摇杆 ABS_RX/ABS_RY，值域 ±3700，死区 800
    → 方向键是"模拟量"，必须做去重（保持推着不动只触发一次）
"""
import os
import time

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# 配色
# ---------------------------------------------------------------------------

C_BG = (14, 16, 22)
C_BAR = (0, 104, 176)
C_BAR_D = (0, 62, 104)
C_CARD = (26, 30, 40)
C_CARD_SEL = (34, 62, 92)
C_CARD_LN = (52, 60, 76)
C_FG = (236, 239, 245)
C_DIM = (126, 134, 150)
C_ACC = (0, 206, 255)
C_OK = (58, 208, 128)
C_WARN = (255, 198, 58)
C_ERR = (236, 92, 92)
C_DIR = (255, 214, 88)

# 按键配色 —— 与实机按键颜色一致，用户不用记"哪个是 A"。
# 键名会按这张表着色，"摇杆/L1/R1/START"这类没有物理颜色的用中性灰。
KEY_COLOR = {
    "A": (74, 158, 255),        # 蓝
    "B": (236, 92, 92),         # 红
    "Y": (255, 198, 58),        # 黄
    "X": (167, 139, 250),       # 紫
    "L1": (140, 148, 164), "R1": (140, 148, 164),
    "L2": (140, 148, 164), "R2": (140, 148, 164),
    "SELECT": (126, 134, 150), "START": (126, 134, 150),
    "MENUF": (126, 134, 150),
}
C_KEY_DEF = (140, 148, 164)     # 默认键名颜色

MODE_BROWSE = "browse"
MODE_XFER_OUT = "xfer_out"
MODE_XFER_IN = "xfer_in"

# 根页面（标签）顺序 —— 渲染标签栏、L2/R2 切页、默认焦点都用它，
# 只此一处定义，避免三处列表各写一遍导致顺序不一致。
PAGE_ORDER = ["home", "status", "browser", "net", "log"]

PAGE_LABEL = {"home": "首页", "status": "状态", "browser": "文件",
              "net": "网络", "log": "日志"}

# ---------------------------------------------------------------------------
# ★ 按键说明表（单一事实来源）
# ---------------------------------------------------------------------------
# 【为什么要建这张表】
#   在此之前，按键说明散落在**两处**：
#     ① `App._footer_hint()` 里的一段字符串
#     ② 各 `Page.handle()` 里的分支实现
#   两者没有任何机械关联，改了一处忘了另一处，用户就会看到
#   "界面上写着按 X 能刷新，按下去没反应"这种问题。
#
#   本项目已经因为"界面说明与实际行为不一致"出过一次严重事故
#   （第四轮：确认框写着「允许(A)」，按 A 实际执行拒绝）。
#   所以这里把说明收敛成数据，并用测试断言
#   "每个 handle() 里认过的键，都必须在表里声明"。
#
# 【格式】(显示用键名, 说明)
#   键名可以是 "A"、"L1 / R1"、"摇杆上下" 这样的组合写法，
#   着色时按第一个 token 查 KEY_COLOR。
KEY_HELP = {
    "home": [
        ("摇杆上下", "滚动日志"),
        ("A", "查看完整日志"),
        ("X", "打开日志文件"),
        ("L2 / R2", "切换页面"),
        ("SELECT", "锁定 / 解锁"),
        ("START", "退出程序"),
    ],
    "status": [
        ("L2 / R2", "切换页面"),
        ("X", "查看日志"),
        ("Y", "输入诊断"),
        ("SELECT", "锁定 / 解锁"),
        ("START", "退出程序"),
    ],
    "browser": [
        ("摇杆上下", "选择文件"),
        ("A", "进入目录 / 看文件"),
        ("B", "返回上级"),
        ("Y", "刷新列表"),
        ("L1 / R1", "翻页 ±5"),
        ("SELECT", "锁定 / 解锁"),
        ("START", "退出程序"),
    ],
    "net": [
        ("A / Y", "扫描局域网"),
        ("X", "锁定 / 解锁"),
        ("L2 / R2", "切换页面"),
        ("START", "返回 / 退出"),
    ],
    "log": [
        ("摇杆上下", "滚动"),
        ("A", "写入日志文件"),
        ("L2 / R2", "切换页面"),
        ("START", "返回 / 退出"),
    ],
    "diag": [
        ("A", "开始引导式标定"),
        ("Y", "清空记录"),
        ("X", "跳过当前步（标定中）"),
        ("B", "退出标定"),
        ("START", "返回"),
    ],
}

# 底栏提示 —— 每页一句话，比首页那张表更简（底栏只有一行）
FOOTER_HINT = {
    "home": "摇杆滚日志  A 看全部  X 存日志  L2/R2 切页  START 退出",
    "status": "R2 下一页 / L2 上一页   Y 输入诊断   X 看日志   START 退出",
    "browser": ("摇杆选择  A 进入  B 返回  Y 刷新  "
                "L1/R1 翻页  START 退出"),
    "net": "A/Y 扫描  X 锁定/解锁  START 返回",
    "log": "摇杆滚动  A 写日志  START 返回",
    "diag": "A 引导式标定   Y 清空   START 返回",
}

# 诊断页展示用：轴码 → 名字（仅显示，不参与逻辑）
# ★ 2026-10-05 真机标定后的语义（左杆=2/3，右杆=4/5）：
#     2/3 → 左摇杆 水平/垂直
#     4/5 → 右摇杆 水平/垂直
AXIS_ROLE_HINT = {
    0: "X", 1: "Y",
    2: "L#H", 3: "L#V",        # 左摇杆 水平 / 垂直
    4: "R#H", 5: "R#V",        # 右摇杆 水平 / 垂直
    16: "HAT0X", 17: "HAT0Y", 18: "HAT1X", 19: "HAT1Y",
}

MODE_LABEL = {
    MODE_BROWSE: "待机",
    MODE_XFER_OUT: "发送中",
    MODE_XFER_IN: "接收中",
}


# ---------------------------------------------------------------------------
# 事件（按键/方向被规范化成这个结构）
# ---------------------------------------------------------------------------

class Evt:
    __slots__ = ("kind", "name", "code", "value", "stamp", "etype")

    def __init__(self, kind, name, code=0, value=0, etype=None):
        self.kind = kind          # "key" | "axis" | "raw"
        self.name = name          # "A" / "左" / "START" ...
        self.code = code
        self.value = value
        self.etype = etype        # 原始 evdev type（仅 "raw" 用）
        self.stamp = time.strftime("%H:%M:%S")

    def __repr__(self):
        return f"<Evt {self.kind} {self.name} {self.value:+d}>"


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------

def ellipsize(d, text, font, max_w):
    """按像素宽度截断，超长加省略号。中文按字形宽度自然处理。"""
    if max_w <= 0:
        return ""
    if d.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    ew = d.textlength(ell, font=font)
    while text and d.textlength(text, font=font) + ew > max_w:
        text = text[:-1]
    return text + ell if text else ell


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return "?"


# ---------------------------------------------------------------------------
# 小部件
# ---------------------------------------------------------------------------

class ListView:
    """
    可滚动的纵向列表。

    关键行为：
      - 光标移动时自动滚动，保证选中项始终可见
      - 滚动带"上下文"（cursor 在视窗内尽量保持同一相对位置）
    """

    def __init__(self, items=None):
        self.items = items or []
        self.cursor = 0
        self.scroll = 0

    def set_items(self, items, keep_cursor=False):
        old = self.items
        self.items = items or []
        if not keep_cursor or not self.items:
            self.cursor = 0
            self.scroll = 0
        else:
            self.cursor = min(self.cursor, len(self.items) - 1)
        if old != self.items:
            self.scroll = max(0, min(self.scroll, max(0,
                                                       len(self.items) - 1)))

    def move(self, delta):
        if not self.items:
            return False
        n = len(self.items)
        new = max(0, min(n - 1, self.cursor + delta))
        if new == self.cursor:
            return False
        self.cursor = new
        return True

    def current(self):
        if 0 <= self.cursor < len(self.items):
            return self.items[self.cursor]
        return None

    def ensure_visible(self, visible_rows):
        """由 draw 调用，保证 cursor 在视窗内。"""
        if visible_rows <= 0:
            self.scroll = 0
            return
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + visible_rows:
            self.scroll = self.cursor - visible_rows + 1
        self.scroll = max(0, min(self.scroll,
                                 max(0, len(self.items) - visible_rows)))


# ---------------------------------------------------------------------------
# 页面基类
# ---------------------------------------------------------------------------

class Page:
    name = "page"
    title = ""

    def __init__(self, app):
        self.app = app

    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def handle(self, evt):
        """返回 True 表示事件已被消费。"""
        return False

    def draw(self, d, x, y, w, h, fonts):
        """在指定区域绘制内容。"""
        pass

    # 便利访问
    @property
    def st(self):
        return self.app.st


# ---------------------------------------------------------------------------
# 页面 0：★ 首页（三段式仪表盘）
# ---------------------------------------------------------------------------
#
# 【版面】用户要求「上面一多半，下面一小半」：
#
#   ┌────────────────────────────────────────────┐
#   │ 标题条 / 标签栏（复用现有）                  │
#   ├──────────────────┬─────────────────────────┤
#   │ 连接状态          │ 关键操作                │  ← 上半 62%（左右各半）
#   │ ● 已连接          │ 18:29:41 已连接 …       │
#   │ IP / 对端 / 权限  │ 18:29:35 发现 …         │
#   │ 卡根 / 收发       │ …可滚动                 │
#   ├──────────────────┴─────────────────────────┤
#   │ 按键说明（两列，读 KEY_HELP）                │  ← 下半 38%
#   ├────────────────────────────────────────────┤
#   │ 底栏                                        │
#   └────────────────────────────────────────────┘
#
# 【为什么左上角放"状态圆点 + 大字"】
#   掌机常放在桌上斜看，屏幕又小。"连没连上"是最高频的问题，
#   必须做到扫一眼就知道，不需要读字。
#
# 【为什么右栏复用 log_lines 而不是另存一份】
#   两处读同一个列表，就不可能不一致。日志页保留全部内容，
#   首页只做筛选 → 自然形成"摘要 → 详情"的层次。

# 关键事件关键词 —— 命中即认为值得出现在首页。
# 覆盖：连接生命周期 / 发现 / 传输结果 / 确认结果 / 错误。
KEY_WORDS = (
    "已连接", "已断开", "发现", "自动扫描",
    "待确认", "已允许", "已拒绝", "拒绝",
    "完成", "失败", "异常", "错误",
    "新建目录", "已删除", "中断",
)

# 上下两半的高度比例。用户要求"上面一多半，下面一小半"。
# 62 : 38 —— 比 50:50 明显偏上，又不至于把按键说明压得放不下。
TOP_RATIO = 0.62


class HomePage(Page):
    name = "home"
    title = "首页"

    def __init__(self, app):
        super().__init__(app)
        self.scroll = 0            # 日志向上滚了多少条；0 = 跟随最新
        self._cache_key = None     # (log_lines 长度) → 复用筛选结果
        self._cache_val = []

    # ---------- 数据 ----------

    def key_lines(self):
        """
        从 log_lines 里筛出"关键操作"。

        ★ 兜底：若 500 行日志里一条关键词都没命中，就退回显示最近几条。
          否则框里空空如也，用户会以为功能坏了。
        """
        n = len(self.app.log_lines)
        if self._cache_key == n:
            return self._cache_val
        hits = [ln for ln in self.app.log_lines if any(w in ln for w in KEY_WORDS)]
        if not hits:
            hits = self.app.log_lines[-3:]
        # 只留最近的 60 条，省内存也够滚动
        self._cache_val = hits[-60:]
        self._cache_key = n
        return self._cache_val

    # ---------- 输入 ----------

    def handle(self, evt):
        # 摇杆上下滚日志
        if evt.kind == "axis":
            if evt.name == "上":
                self.scroll += 1
                return True
            if evt.name == "下":
                self.scroll = max(0, self.scroll - 1)
                return True
            return False

        n = evt.name
        if n == "A":
            # 下钻：去看完整日志。清空是破坏性操作，不该出现在首页。
            self.app.push(self.app.pages["log"])
            return True
        if n == "X":
            self.app.open_log_file()
            return True
        return False

    # ---------- 绘制 ----------

    def draw(self, d, x, y, w, h, fonts):
        """
        上半画完，下半交给 draw_key_help。
        用统一的辅助函数算高度，保证两半的切分点和"上多下少"比例一致。
        """
        top_h = self.top_height(h)
        self._draw_info(d, x, y, w, top_h, fonts)
        self._draw_keys(d, x, y + top_h, w, h - top_h, fonts)

    @staticmethod
    def top_height(h):
        """上半高度。留出两半之间的间距，避免两块贴在一起。"""
        return max(80, int(h * TOP_RATIO))

    def _draw_info(self, d, x, y, w, h, fonts):
        """上半：左栏连接状态 + 右栏关键操作日志。"""
        k = self.app.k
        gap = max(6, int(10 * k))
        col_w = (w - gap) // 2
        self._draw_status_card(d, x, y, col_w, h, fonts)
        self._draw_log_card(d, x + col_w + gap, y, w - col_w - gap, h, fonts)

    # ---- 左栏：连接状态 ----

    def _draw_status_card(self, d, x, y, w, h, fonts):
        st = self.st
        net = self.app.net
        f_title = fonts.small
        f_body = fonts.body
        f_big = fonts.head
        k = self.app.k
        pad = max(6, int(10 * k))
        lh = self.app.line_h(f_body)

        d.rectangle([x, y, x + w, y + h], fill=C_CARD, outline=C_CARD_LN)
        d.text((x + pad, y + pad), "连接状态", font=f_title, fill=C_ACC)

        # --- 状态圆点 + 大字 ---
        connected = bool(net and net.state == "connected")
        xfer = st.get("mode", MODE_BROWSE) in (MODE_XFER_OUT, MODE_XFER_IN)
        if xfer:
            dot, word, col = C_ACC, "传输中", C_ACC
        elif connected:
            dot, word, col = C_OK, "已连接", C_OK
        else:
            dot, word, col = C_DIM, "等待连接", C_DIM

        cy = y + pad + lh + max(4, int(6 * k))
        r = max(3, int(5 * k))
        cxx = x + pad + r
        cyy = cy + self.app.line_h(f_big) // 2
        d.ellipse([cxx - r, cyy - r, cxx + r, cyy + r], fill=dot)
        d.text((x + pad + r * 2 + max(4, int(6 * k)), cy), word,
               font=f_big, fill=col)
        cy += self.app.line_h(f_big) + max(2, int(4 * k))

        # --- 明细行 ---
        peer = (net.peer if net and net.peer else None) or "—"
        base = st.get("base") or "(未设置)"
        rows = [
            ("本机", st.get("ip") or "?"),
            ("对端", peer),
            ("权限", "RW 可写" if st.get("writable") else "RO 只读"),
            ("卡根", base),
            ("收发", f"↑{human_size(st.get('sent', 0))}  "
                     f"↓{human_size(st.get('recv', 0))}"),
        ]
        lab_w = self.app.tw(d, "本机", f_body) + max(6, int(9 * k))
        for label, val in rows:
            if cy + lh > y + h - pad:
                break
            d.text((x + pad, cy), label, font=f_body, fill=C_DIM)
            if label == "权限":
                vcol = C_WARN if st.get("writable") else (215, 232, 250)
            else:
                vcol = C_FG
            d.text((x + pad + lab_w, cy),
                   ellipsize(d, str(val), f_body, w - pad * 2 - lab_w),
                   font=f_body, fill=vcol)
            cy += lh

    # ---- 右栏：关键操作日志 ----

    def _draw_log_card(self, d, x, y, w, h, fonts):
        f_title = fonts.small
        f = fonts.small
        k = self.app.k
        pad = max(6, int(10 * k))
        lh = self.app.line_h(f)

        d.rectangle([x, y, x + w, y + h], fill=C_CARD, outline=C_CARD_LN)
        lines = self.key_lines()

        # 标题带上条数，让用户知道"这是筛选过的"
        head = "关键操作" if lines else "关键操作"
        d.text((x + pad, y + pad), head, font=f_title, fill=C_ACC)

        top = y + pad + self.app.line_h(f_title) + max(2, int(4 * k))
        rows = max(1, int((y + h - pad - top) // lh))

        inner_x = x + pad
        inner_w = w - pad * 2
        if not lines:
            d.text((inner_x, top), "暂无记录", font=f, fill=C_DIM)
            return

        # scroll 是从底部往上数的偏移；0 = 跟随最新
        end = max(0, len(lines) - self.scroll)
        start = max(0, end - rows)
        shown = lines[start:end]

        cy = top
        for ln in shown:
            if cy + lh > y + h - pad:
                break
            col = C_FG
            low = ln.lower()
            if ("拒绝" in ln or "失败" in ln or "异常" in ln
                    or "错误" in ln or "中断" in ln):
                col = C_ERR
            elif ("已连接" in ln or "完成" in ln or "发现" in ln
                    or "已允许" in ln):
                col = C_OK
            d.text((inner_x, cy), ellipsize(d, ln, f, inner_w),
                   font=f, fill=col)
            cy += lh

        # 不在跟随最新时给个提示，否则用户会以为卡住了
        if self.scroll:
            tag = "▲ 已暂停跟随"
            tw = self.app.tw(d, tag, f)
            d.text((x + w - tw - pad, y + pad), tag, font=f, fill=C_WARN)

    # ---- 下半：按键说明 ----

    def _draw_keys(self, d, x, y, w, h, fonts):
        """读 KEY_HELP 画按键说明，两列排布。"""
        k = self.app.k
        pad = max(6, int(10 * k))
        f_title = fonts.small
        f = fonts.body

        d.rectangle([x, y, x + w, y + h], fill=C_CARD, outline=C_CARD_LN)
        d.text((x + pad, y + pad), "按键说明", font=f_title, fill=C_WARN)

        entries = KEY_HELP.get(self.name, [])
        top = y + pad + self.app.line_h(f_title) + max(2, int(4 * k))
        lh = self.app.line_h(f)
        rows = max(1, int((y + h - pad - top) // lh))

        # 两列：先把条目按行放满，再决定每列放几条
        per_col = max(1, (len(entries) + 1) // 2)
        gap = max(6, int(10 * k))
        col_w = (w - pad * 2 - gap) // 2
        key_w = max(50, int(64 * k))     # 键名列固定宽度，说明列对齐

        for i, (keys, desc) in enumerate(entries[:rows * 2]):
            col = i // per_col
            row = i % per_col
            ex = x + pad + col * (col_w + gap)
            ey = top + row * lh
            if ey + lh > y + h - pad:
                continue

            # 键名：按第一个 token 取色（"A / Y" 取 A 的蓝）
            token = keys.split()[0] if keys else ""
            kcol = KEY_COLOR.get(token, C_KEY_DEF)
            d.text((ex, ey), ellipsize(d, keys, f, key_w - 4),
                   font=f, fill=kcol)
            d.text((ex + key_w, ey),
                   ellipsize(d, desc, f, col_w - key_w),
                   font=f, fill=C_FG)


# ---------------------------------------------------------------------------
# 页面 1：状态页（原「一屏到底」的信息页，保留）
# ---------------------------------------------------------------------------

class StatusPage(Page):
    name = "status"
    title = "状态"

    def handle(self, evt):
        if evt.name == "X":
            self.app.push(self.app.pages["log"])
            return True
        if evt.name == "Y" and evt.kind == "key":
            # 输入诊断页入口。放在状态页是刻意的：
            # 这是"首页"，用户一进来就能按到，不用记组合键。
            self.app.push(self.app.pages["diag"])
            return True
        return False

    def draw(self, d, x, y, w, h, fonts):
        st = self.st
        f_body = fonts.body
        f_small = fonts.small
        k = self.app.k
        pad = max(6, int(10 * k))
        lh = self.app.line_h(f_body)
        vp = max(4, int(6 * k))

        cards = []

        # 卡片 1：显示
        backend = st.get("backend", "?")
        sdl = st.get("sdl_ver", "?")
        cards.append([
            f"显示 {self.app.fb_w}x{self.app.fb_h}   {backend}   R{self.app.fbo[0]}G{self.app.fbo[1]}B{self.app.fbo[2]}",
            f"SDL2 {sdl}   画布缩放 {k:.2f}",
            f"机型 {st.get('model', '?')}   固件 {st.get('fw', '?')}",
        ])

        # 卡片 2：网络
        net = self.app.net
        if net and net.state in ("idle", "connected"):
            netline = (f"UDP {net.state}   广播应答 {net.udp_seen} 次")
        else:
            netline = "网络未启动"
        peer = f"   对端 {net.peer}" if (net and net.peer) else ""
        cards.append([
            f"IP {st.get('ip', '?')}   端口 {st.get('port', '?')}",
            netline + peer,
            f"卡根 {st.get('base') or '(未设置)'}   "
            f"{'可写' if st.get('writable') else '只读'}",
        ])

        # 卡片 3：硬件
        cards.append([
            f"输入 {st.get('input', '-')}",
            f"马达 {'可用' if st.get('motor') else '不可用'}   "
            f"字体 {st.get('font', '?')}",
            f"收发  ↑{human_size(st.get('sent', 0))}  "
            f"↓{human_size(st.get('recv', 0))}",
        ])

        cy = y
        for lines in cards:
            ch = vp * 2 + len(lines) * lh
            if cy + ch > y + h:
                break
            d.rectangle([x, cy, x + w, cy + ch], fill=C_CARD,
                        outline=C_CARD_LN)
            for i, ln in enumerate(lines):
                col = C_FG if i else C_ACC
                d.text((x + pad, cy + vp + i * lh),
                       ellipsize(d, ln, f_body, w - pad * 2),
                       font=f_body, fill=col)
            cy += ch + int(8 * k)

        # 最近事件
        rest = y + h - cy
        if rest > lh * 2:
            d.text((x + pad, cy), "最近事件", font=f_body, fill=C_WARN)
            cy += lh + 2
            evs = self.app.log_lines[-((rest - lh) // lh):][::-1]
            for ln in evs:
                if cy + lh > y + h:
                    break
                d.text((x + pad, cy), ellipsize(d, ln, f_small, w - pad * 2),
                       font=f_small, fill=C_DIM)
                cy += lh


# ---------------------------------------------------------------------------
# 页面 2：文件浏览
# ---------------------------------------------------------------------------

class BrowserPage(Page):
    name = "browser"
    title = "文件"

    def __init__(self, app):
        super().__init__(app)
        self.cwd = ""                 # 相对卡根的路径，"" = 根
        self.entries = []
        self.list = ListView()
        self.loading = False
        self.error = None
        self._cache = {}

    # ---------- 数据 ----------

    def reload(self, keep_cursor=False):
        """从本机文件系统读当前目录（不依赖 PC，本机自己也能看）。"""
        base = self.app.st.get("base") or ""
        if not base:
            self.error = "未拿到卡根路径"
            self.entries = []
            self.list.set_items([])
            return
        path = os.path.join(base, self.cwd) if self.cwd else base
        if not os.path.isdir(path):
            self.error = f"目录不存在: {self.cwd}"
            return
        try:
            names = os.listdir(path)
        except PermissionError as e:
            self.error = f"无权限: {e}"
            return
        except Exception as e:
            self.error = f"读取失败: {e}"
            return

        entries = []
        for nm in names:
            if nm.startswith("."):
                continue
            full = os.path.join(path, nm)
            try:
                isdir = os.path.isdir(full)
                sz = 0 if isdir else os.path.getsize(full)
            except Exception:
                isdir, sz = False, 0
            entries.append({"name": nm, "dir": isdir, "size": sz})

        # 目录在前，各自按名称排序
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        self.entries = entries
        self.list.set_items(entries, keep_cursor=keep_cursor)
        self.error = None

    def on_enter(self):
        if not self.entries and not self.error:
            self.reload()

    # ---------- 输入 ----------

    def handle(self, evt):
        if evt.kind == "axis":
            if evt.name == "上":
                return self.list.move(-1)
            if evt.name == "下":
                return self.list.move(1)
            return False

        n = evt.name
        if n == "A":
            cur = self.list.current()
            if not cur:
                return False
            if cur["dir"]:
                self.cwd = (self.cwd + "/" + cur["name"]).lstrip("/")
                self.list.set_items([])
                self.reload()
                self.app.toast(f"进入 {cur['name']}")
            else:
                self.app.toast(f"文件: {cur['name']} ({human_size(cur['size'])})")
            return True

        if n == "B":
            if self.cwd:
                self.cwd = os.path.dirname(self.cwd)
                self.list.set_items([])
                self.reload()
                self.app.toast("返回上级")
            else:
                # 已经在卡根：明确告诉用户"到顶了"，而不是静默无反应。
                # 之前没有这个提示，用户会以为"程序卡住 / 上不去"。
                self.app.toast("已在卡根，无法再往上")
            return True

        if n == "Y":
            self.reload(keep_cursor=True)
            self.app.toast("已刷新")
            return True

        if n in ("L1", "R1"):
            self.list.move(-5 if n == "L1" else 5)
            return True

        return False

    # ---------- 绘制 ----------

    def draw(self, d, x, y, w, h, fonts):
        f = fonts.body
        fs = fonts.small
        k = self.app.k
        pad = max(6, int(10 * k))
        lh = self.app.line_h(f)

        # 路径条
        if self.cwd:
            path_txt = "/" + self.cwd
            hint = f"卡根{path_txt}"
            col = C_ACC
        else:
            # 卡根：显式标出，避免用户以为"还能再往上"
            path_txt = ""
            hint = "卡根 /  （已是顶层，B 无法再上）"
            col = C_OK
        d.text((x + pad, y), ellipsize(d, hint, fs, w - pad * 2),
               font=fs, fill=col)
        ty = y + self.app.line_h(fs) + 2

        if self.error:
            d.text((x + pad, ty + lh), ellipsize(d, self.error, f, w - pad * 2),
                   font=f, fill=C_ERR)
            return

        if not self.entries:
            d.text((x + pad, ty + lh), "（空目录）", font=f, fill=C_DIM)
            return

        avail = y + h - ty
        row_h = lh + max(3, int(5 * k))
        rows = max(1, int(avail // row_h))
        self.list.ensure_visible(rows)

        # 滚动位置提示
        total = len(self.entries)
        start = self.list.scroll
        first = ty
        for i in range(start, min(total, start + rows)):
            e = self.entries[i]
            ry = first + (i - start) * row_h
            sel = (i == self.list.cursor)
            if sel:
                d.rectangle([x, ry - 1, x + w, ry + row_h - 2],
                            fill=C_CARD_SEL, outline=C_ACC)
            icon = "▣" if e["dir"] else "  "
            name = e["name"]
            if e["dir"]:
                name += "/"
            nm_w = w - pad * 2 - self.app.tw(d, icon + " ", fs) - \
                self.app.tw(d, " 999.9MB ", fs)
            label = f"{icon} " + ellipsize(d, name, f, max(20, nm_w))
            d.text((x + pad, ry), label, font=f,
                   fill=C_DIR if e["dir"] else C_FG)
            if not e["dir"]:
                sz = human_size(e["size"])
                sw = self.app.tw(d, sz, fs)
                d.text((x + w - sw - pad, ry + 1), sz, font=fs, fill=C_DIM)

        # 右侧滚动条
        if total > rows:
            bar_x = x + w - max(2, int(3 * k))
            track_h = avail
            th = max(12, int(track_h * rows / total))
            tp = int(track_h * start / max(1, total))
            d.rectangle([bar_x, ty, bar_x + max(2, int(3 * k)), ty + track_h],
                        fill=(30, 34, 44))
            d.rectangle([bar_x, ty + tp, bar_x + max(2, int(3 * k)),
                         ty + tp + th], fill=C_ACC)


# ---------------------------------------------------------------------------
# 页面 3：网络页
# ---------------------------------------------------------------------------

class NetPage(Page):
    name = "net"
    title = "网络"

    def __init__(self, app):
        super().__init__(app)
        self.rows = []            # 本机扫描到的局域网设备（仅展示）
        self.scanning = False
        self.scan_t = 0.0

    def scan(self):
        self.scanning = True
        self.scan_t = time.time()

    def handle(self, evt):
        if evt.name in ("A", "Y"):
            self.scan()
            self.app.toast("正在扫描局域网…")
            return True
        if evt.name == "X":
            # 切换可写状态（与 SELECT 等价，这里给个更顺手的入口）
            self.app.toggle_writable()
            return True
        return False

    def draw(self, d, x, y, w, h, fonts):
        st = self.st
        f = fonts.body
        fs = fonts.small
        k = self.app.k
        pad = max(6, int(10 * k))
        lh = self.app.line_h(f)
        cy = y

        net = self.app.net
        state = net.state if net else "未启动"
        lines = [
            f"本机 IP  {st.get('ip', '?')}",
            f"TCP 端口 {st.get('port')}   UDP {st.get('udp_port')}",
            f"状态 {state}   广播应答 {net.udp_seen if net else 0} 次",
            f"权限 {'可写（已解锁）' if st.get('writable') else '只读（需解锁）'}",
        ]
        ch = pad * 2 + len(lines) * lh
        d.rectangle([x, cy, x + w, cy + ch], fill=C_CARD, outline=C_CARD_LN)
        for i, ln in enumerate(lines):
            col = C_OK if i == 0 else (
                C_WARN if (i == 3 and not st.get("writable")) else C_FG)
            d.text((x + pad, cy + pad + i * lh),
                   ellipsize(d, ln, f, w - pad * 2), font=f, fill=col)
        cy += ch + int(8 * k)

        # 提示 PC 端操作方式
        d.text((x + pad, cy), "PC 端如何连上", font=f, fill=C_WARN)
        cy += lh
        tips = [
            f"1. 与掌机连同一 WiFi",
            f"2. 运行 PC 端 PocketTransfer.exe",
            f"3. 会自动发现本机（{st.get('ip', '?')}）",
            f"   或手动填 IP + 端口 {st.get('port')}",
        ]
        for t in tips:
            if cy + lh > y + h:
                break
            d.text((x + pad, cy), ellipsize(d, t, fs, w - pad * 2),
                   font=fs, fill=C_DIM)
            cy += self.app.line_h(fs)


# ---------------------------------------------------------------------------
# 页面 4：运行日志
# ---------------------------------------------------------------------------

class LogPage(Page):
    name = "log"
    title = "日志"

    def __init__(self, app):
        super().__init__(app)
        self.scroll = 0

    def handle(self, evt):
        if evt.kind == "axis":
            if evt.name == "上":
                self.scroll += 1
                return True
            if evt.name == "下":
                self.scroll = max(0, self.scroll - 1)
                return True
        if evt.name == "A":
            self.app.open_log_file()
            return True
        return False

    def draw(self, d, x, y, w, h, fonts):
        f = fonts.small
        k = self.app.k
        pad = max(6, int(10 * k))
        lh = self.app.line_h(f)
        rows = max(1, int((h) // lh))
        lines = self.app.log_lines
        end = max(0, len(lines) - self.scroll)
        start = max(0, end - rows)
        shown = lines[start:end]
        cy = y
        for ln in shown:
            col = C_DIM
            if "错误" in ln or "FAIL" in ln or "异常" in ln or "rc=" in ln:
                col = C_WARN
            if "FATAL" in ln or "die" in ln:
                col = C_ERR
            d.text((x + pad, cy), ellipsize(d, ln, f, w - pad * 2),
                   font=f, fill=col)
            cy += lh
        if not lines:
            d.text((x + pad, y), "（暂无日志）", font=f, fill=C_DIM)
        # 底部计数
        d.text((x + pad, y + h - lh), f"共 {len(lines)} 行   滚动 {self.scroll}",
               font=f, fill=C_ACC)


# ---------------------------------------------------------------------------
# 页面 5：输入诊断页（实机校准用）
# ---------------------------------------------------------------------------

class DiagPage(Page):
    """
    实时显示「原始输入事件 → 程序识别结果」。

    为什么需要它（重要）：
        摇杆/方向键的真实轴码在文档和真机之间可能对不上 ——
        我们只有"拨摇杆有震动但方向乱"这种间接反馈，无法定位具体是
        哪个 code 映射错了。把原始事件直接画出来，用户拨一次就能看到
        "我往右拨 → ABS_RX 值 +3700 → 识别为 下"，一眼定位。

    本页只读输入、不做任何设备写操作，退出后不影响任何状态。
    """

    name = "diag"
    title = "输入诊断"

    # 标定步骤：(提示语, 该步预期物理操作)。
    # 进入标定模式后逐步引导，每完成一步自动记录"这一步拨出的轴码+极值"，
    # 走完 8 步就能拿到一张确定的「物理方向 → 轴码/符号」对照表。
    CALIB_STEPS = [
        ("左摇杆  向上", "LU"),
        ("左摇杆  向下", "LD"),
        ("左摇杆  向左", "LL"),
        ("左摇杆  向右", "LR"),
        ("右摇杆  向上", "RU"),
        ("右摇杆  向下", "RD"),
        ("右摇杆  向左", "RL"),
        ("右摇杆  向右", "RR"),
    ]

    def __init__(self, app):
        super().__init__(app)
        self.rows = []          # [(时间, 描述, kind), ...]
        self.max_rows = 200
        # 标定模式状态
        self.calib = False
        self.calib_i = 0            # 当前第几步
        self.calib_result = {}      # 步骤 key -> (code, value, 方向名)
        self.calib_seen = {}        # 当前步观测到的 code -> 极值

    # ---------- 标定 ----------

    def start_calib(self):
        self.calib = True
        self.calib_i = 0
        self.calib_result = {}
        self.calib_seen = {}
        self.rows = []
        self.app.toast("标定开始：请按屏幕提示拨摇杆")

    def _calib_note(self, evt):
        """标定模式下记录轴事件（不受死区影响，记录原始极值）。"""
        if evt.kind != "raw" or evt.etype != 3:
            return
        code, val = evt.code, evt.value
        if code not in self.calib_seen:
            self.calib_seen[code] = [val, val]
        else:
            self.calib_seen[code][0] = min(self.calib_seen[code][0], val)
            self.calib_seen[code][1] = max(self.calib_seen[code][1], val)

    def _calib_advance(self):
        """进入下一步；记录这一步的结论。"""
        if self.calib_seen:
            # 取绝对值最大的那个 code 作为本步主结果
            best = max(self.calib_seen.items(),
                       key=lambda kv: max(abs(kv[1][0]), abs(kv[1][1])))
            code, (lo, hi) = best
            peak = hi if abs(hi) >= abs(lo) else lo
            key = self.CALIB_STEPS[self.calib_i][1]
            self.calib_result[key] = (code, peak)
        self.calib_seen = {}
        self.calib_i += 1
        if self.calib_i >= len(self.CALIB_STEPS):
            self.calib = False
            self.app.toast("标定完成！结果已显示")

    def calib_text(self):
        """把标定结果整理成可直接抄进 AXIS_ROLE 的文本。"""
        lines = []
        for (_label, key) in self.CALIB_STEPS:
            r = self.calib_result.get(key)
            if r:
                lines.append(f"{key}: code={r[0]} peak={r[1]:+d}")
            else:
                lines.append(f"{key}: (无事件)")
        return lines

    def note(self, evt):
        """
        记录事件用于展示。

        "raw" 来自 main.poll() 的原始事件流（**未经任何过滤**），
        这是排查输入问题的唯一可信来源：
          - 显示真实 etype（1=KEY / 3=ABS / 其它）
          - 显示真实 value（摇杆是 ±3700 而不是 +1）
          - 连"没被识别"的事件也能看到
        """
        # 标定模式：只关心轴事件，且用原始值（不过死区）
        if self.calib:
            if evt.kind == "raw" and evt.etype == 3:
                self._calib_note(evt)
                # 判定"一次完整拨动"的结束：值回到接近 0
                if abs(evt.value) <= 2 and self.calib_seen:
                    self._calib_advance()
            # 标定期间不刷普通流水，避免干扰
            return

        ETYPE_NAME = {0: "SYN", 1: "KEY", 2: "REL", 3: "ABS", 4: "MSC",
                      5: "SW", 17: "LED", 18: "SND", 21: "FF"}
        if evt.kind == "raw":
            tn = ETYPE_NAME.get(evt.etype, str(evt.etype))
            if evt.etype == 3:
                ax = AXIS_ROLE_HINT.get(evt.code, str(evt.code))
                desc = (f"{tn} {ax}({evt.code}) value={evt.value:+6d}")
            elif evt.etype == 1:
                desc = (f"{tn} code={evt.code:<3} value={evt.value:+d}")
            else:
                desc = (f"{tn} code={evt.code:<3} value={evt.value:+d}")
        elif evt.kind == "axis":
            desc = (f"→ 识别为 {evt.name}  (code={evt.code} "
                    f"val={evt.value:+d})")
        else:
            desc = (f"→ 按键 {evt.name}  (code={evt.code})")
        self.rows.append((evt.stamp, desc, evt.kind))
        if len(self.rows) > self.max_rows:
            del self.rows[:len(self.rows) - self.max_rows]

    def handle(self, evt):
        # 标定模式下：X 跳过当前步，B/START 退出标定
        if self.calib:
            if evt.kind == "key" and evt.name == "X":
                self._calib_advance()
                self.app.toast("已跳过本步")
                return True
            if evt.kind == "key" and evt.name in ("B", "START", "MENUF"):
                self.calib = False
                self.app.toast("标定已取消")
                return True
            return False

        # 本页不拦截任何键 —— 让所有事件都能被记录并正常冒泡。
        # Y = 清空，A = 开始标定（引导式）
        if evt.name == "Y" and evt.kind == "key":
            self.rows = []
            self.app.toast("诊断记录已清空")
            return True
        if evt.name == "A" and evt.kind == "key":
            self.start_calib()
            return True
        return False

    def draw(self, d, x, y, w, h, fonts):
        f = fonts.small
        k = self.app.k
        pad = max(6, int(10 * k))
        lh = self.app.line_h(f)

        # ================= 标定模式 =================
        if self.calib:
            i = self.calib_i
            if i < len(self.CALIB_STEPS):
                label = self.CALIB_STEPS[i][0]
                d.text((x + pad, y),
                       f"【标定 {i + 1}/8】 {label}",
                       font=f, fill=C_WARN)
                ty = y + lh + max(2, int(4 * k))
                d.text((x + pad, ty),
                       "把摇杆往该方向推到底，然后松手",
                       font=f, fill=C_FG)
                ty += lh
                d.text((x + pad, ty),
                       "（松手后自动进入下一步；X 跳过，B 取消）",
                       font=f, fill=C_DIM)
                ty += lh + max(4, int(6 * k))

                # 实时显示本步观测到的极值
                if self.calib_seen:
                    parts = []
                    for c in sorted(self.calib_seen.keys()):
                        lo, hi = self.calib_seen[c]
                        parts.append(f"{AXIS_ROLE_HINT.get(c, c)}({c})"
                                     f"[{lo:+d},{hi:+d}]")
                    d.text((x + pad, ty), "本步观测: " + ellipsize(
                        d, "  ".join(parts), f, w - pad * 2),
                           font=f, fill=C_OK)
                else:
                    d.text((x + pad, ty), "本步观测: （等待拨动…）",
                           font=f, fill=C_DIM)

                # 已完成步骤
                ty += lh * 2
                for (_lbl, key) in self.CALIB_STEPS[:i]:
                    r = self.calib_result.get(key)
                    txt = (f"{key}  code={r[0]}  peak={r[1]:+d}" if r
                           else f"{key}  (无事件)")
                    d.text((x + pad, ty), txt, font=f, fill=C_OK)
                    ty += lh
            return

        # ================= 普通模式 =================
        d.text((x + pad, y), "原始事件实时显示（未过滤）  A 标定  Y 清空",
               font=f, fill=C_ACC)
        ty = y + lh + max(2, int(4 * k))

        # ---- 实时原始轴值（3 秒内有效）----
        inp = getattr(self.app, "inp", None)
        raw = getattr(inp, "last_raw", None) if inp else None
        if raw:
            now = time.time()
            parts = []
            for c in sorted(raw.keys()):
                v, ts = raw[c]
                if now - ts > 3.0:
                    continue
                role = AXIS_ROLE_HINT.get(c, "?")
                parts.append(f"{role}({c})={v:+d}")
            if parts:
                d.text((x + pad, ty), "轴值: " + ellipsize(
                    d, "  ".join(parts), f, w - pad * 2),
                       font=f, fill=C_OK)
                ty += lh

        # ---- 标定结果（如果有） ----
        if self.calib_result:
            d.text((x + pad, ty), "上次标定结果：", font=f, fill=C_WARN)
            ty += lh
            for (_lbl, key) in self.CALIB_STEPS:
                r = self.calib_result.get(key)
                txt = (f"  {key}: code={r[0]} peak={r[1]:+d}" if r
                       else f"  {key}: (无事件)")
                d.text((x + pad, ty), txt, font=f, fill=C_OK)
                ty += lh
            ty += max(2, int(4 * k))

        # ---- 事件流水 ----
        rows = max(1, int((y + h - ty - lh) // lh))
        shown = self.rows[-rows:]
        cy = ty
        for row in shown:
            ts, desc, kind = row
            if kind == "raw":
                col = C_WARN
            elif kind == "axis":
                col = C_OK
            else:
                col = C_FG
            d.text((x + pad, cy), ellipsize(d, f"{ts} {desc}", f, w - pad * 2),
                   font=f, fill=col)
            cy += lh

        if not self.rows and not self.calib_result:
            d.text((x + pad, ty), "（暂无事件，请拨摇杆 / 按方向键）",
                   font=f, fill=C_DIM)

        d.text((x + pad, y + h - lh),
               "按 A 开始引导式标定（8 步，自动记录轴码）",
               font=f, fill=C_ACC)


# ---------------------------------------------------------------------------
# 模态确认框（掌机掌握决定权）
# ---------------------------------------------------------------------------

class Confirm:
    """
    由网络线程请求、主线程渲染的模态框。

    ★★ 交互定稿（2026-10-05 用户实测反馈）：
        用户原话 ——
        「左边按钮 拒绝（B），右边按钮 允许（A），我看你写的操作逻辑应该是，
          默认焦点在拒绝按钮上，得手动把焦点移到允许按钮上，再按 A 键，
          才是允许，直接按 A 键，相当于选择了拒绝，实际上实现的是拒绝功能。
          这个我觉得得改改，这里改成不需要选择焦点，直接按 B 是拒绝，
          按 A 是允许。」

    **这是一个真 bug**：旧版 `_handle_confirm` 里 `A/START` 键是
    "确认当前焦点"，而默认焦点在「拒绝」上 —— 于是用户按 A 想允许，
    结果执行的是拒绝。语义与直觉完全相反，在"写文件"这种
    不可逆操作上尤其危险。

    改成**直接动作键**：
        A           → 允许（无条件）
        B           → 拒绝（无条件）
        左/右/摇杆  → 只移动高亮（纯视觉，不影响按 A/B 的结果）
        START/MENUF → 拒绝（把它们当"取消/退出"，语义上等于拒绝）

    所以 `picked` 现在**只是高亮的展示位置**，不再决定 A 键的结果。
    """

    def __init__(self, action, target, name, size, ev, box):
        self.action = action
        self.target = target
        self.name = name
        self.size = size
        self.ev = ev
        self.box = box
        # ★ 默认高亮「允许」。
        #   理由：弹这个框说明用户（在 PC 上）刚刚明确表达了"我要做这件事"
        #   的意图，掌机这侧是二次确认。默认高亮允许更顺；
        #   又因为 A/B 已经是直接动作键，即使高亮位置和用户预期不符，
        #   也不会导致按错。
        self.picked = 1        # 0=高亮拒绝 1=高亮允许（仅视觉）
        self.deadline = time.time() + 60

    @property
    def remaining(self):
        return max(0, int(self.deadline - time.time()))

    def title(self):
        if self.action == "put":
            return "PC 请求写入文件"
        if self.action == "mkdir":
            return "PC 请求新建目录"
        if self.action == "delete":
            return "PC 请求删除文件"
        return "PC 请求操作"

    def lines(self):
        out = [f"名称 {self.name}"]
        if self.action == "put":
            out.append(f"大小 {human_size(self.size)}")
        if self.action == "delete":
            # 删除不可逆，把这一点明确写出来，别让用户随手按了 A
            out.append("★ 此操作不可恢复")
        out.append(f"位置 …/{self.target.split(os.sep)[-3:] and '/'.join(self.target.split(os.sep)[-3:])}")
        return out

    def answer(self, ok):
        self.box["ok"] = bool(ok)
        try:
            self.ev.set()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 应用（页面栈 + 主循环）
# ---------------------------------------------------------------------------

class App:
    """
    把绘制、输入、页面栈、模态框串起来。

    主循环在 main.py，每帧调用：
        app.pump()          排空网络事件队列
        app.handle(evt)     分发输入
        app.render()        画一帧（返回 PIL Image）
    """

    def __init__(self, f, fonts, st, net=None):
        self.f = f
        self.fonts = fonts
        self.st = st
        self.net = net

        self.k = max(0.5, min(4.0, f.width / 640.0))
        self.fb_w = f.width
        self.fb_h = f.height
        self.fbo = (getattr(f, "hoffset", 16), getattr(f, "goffset", 8),
                    getattr(f, "boffset", 0))

        self.stack = []
        self.pages = {
            "home": HomePage(self),
            "status": StatusPage(self),
            "browser": BrowserPage(self),
            "net": NetPage(self),
            "log": LogPage(self),
            "diag": DiagPage(self),
        }
        # ★ 开机落在首页 —— 它是仪表盘，用户一进来就能看到"连没连上"
        #   和当前页的操作说明。
        self.stack.append(self.pages["home"])

        # 当前生效的轴方向（code → 方向名/None），诊断页展示用
        self.axis_state = {}

        self.log_lines = []
        self.toasts = []          # (text, expire_ts)
        self.confirm = None
        self.quit = False
        self.dirty = True

        self._tw_cache = {}

    # ---------- 工具 ----------

    def tw(self, d, text, font):
        """缓存的文字宽度（PIL 每帧重复测量很贵）。"""
        key = (text, id(font))
        v = self._tw_cache.get(key)
        if v is None:
            v = d.textlength(text, font=font)
            if len(self._tw_cache) > 4000:
                self._tw_cache.clear()
            self._tw_cache[key] = v
        return v

    def line_h(self, font):
        try:
            a, dd = font.getmetrics()
            return a + dd + max(2, int(4 * self.k))
        except Exception:
            return max(16, int(22 * self.k))

    def toast(self, msg):
        self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(self.log_lines) > 500:
            del self.log_lines[:100]
        self.toasts.append((msg, time.time() + 2.5))
        self.dirty = True

    def log_only(self, msg):
        self.log_lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(self.log_lines) > 500:
            del self.log_lines[:100]

    def open_log_file(self):
        try:
            import boot
            boot.log("---- 用户在运行日志页请求 dump ----")
            for ln in self.log_lines[-40:]:
                boot.log(f"  UI| {ln}")
            self.toast("已写入 boot.log")
        except Exception as e:
            self.toast(f"写日志失败 {e}")

    def toggle_writable(self):
        """
        切换「允许 PC 写入」开关。

        ★ 2026-10-05 用户反馈 #1：默认改成**可写**。
          所以这个键的语义从"解锁"变成了"临时上锁"：
            RW（默认） → 按一下 → RO → 再按一下 → RW
          它不再是写入的必经步骤，而是用户想临时挡住写入时用的刹车。

        注意：无论 RW 还是 RO，每次真正写入前掌机仍会弹确认框 —— 
        这是产品的核心承诺，不随这个开关改变。
        """
        if not self.net:
            self.toast("网络未启动")
            return
        new = not self.st.get("writable", False)
        self.net.set_writable(new)
        self.st["writable"] = new
        if new:
            self.toast("已允许 PC 写入（每次写入仍会弹窗确认）")
        else:
            self.toast("已临时锁定：PC 无法写入")
        self.dirty = True

    def push(self, page):
        self.stack[-1].on_exit() if self.stack else None
        self.stack.append(page)
        page.on_enter()
        self.dirty = True

    def pop(self):
        if len(self.stack) <= 1:
            return False
        self.stack.pop().on_exit()
        self.stack[-1].on_enter()
        self.dirty = True
        return True

    # ---------- 网络事件 ----------

    def pump(self):
        if not self.net:
            return
        got = 0
        while got < 200:
            try:
                ev = self.net.events.get_nowait()
            except Exception:
                break
            got += 1
            self._on_net_event(ev)
        if got:
            self.dirty = True

    def _on_net_event(self, ev):
        kind = ev.get("kind")
        if kind == "discovered":
            self.toast(f"PC 已发现本机（来自 {ev.get('peer')}）")
        elif kind == "connected":
            self.toast(f"PC 已连接：{ev.get('peer')}")
            self.st["peer"] = ev.get("peer")
        elif kind == "disconnected":
            self.toast("PC 已断开")
            self.st["peer"] = None
        elif kind == "confirm":
            self.confirm = Confirm(ev["action"], ev["target"], ev["name"],
                                   ev["size"], ev["ev"], ev["box"])
            self.log_only(f"待确认: {self.confirm.title()} {self.confirm.name}")
        elif kind == "xfer_start":
            d = ev.get("direction")
            self.st["mode"] = (MODE_XFER_OUT if d == "out" else MODE_XFER_IN)
            self.st["xfer_name"] = ev.get("name")
            self.st["xfer_size"] = ev.get("size", 0)
            self.st["xfer_got"] = 0
            self.toast(f"{'发送' if d == 'out' else '接收'} "
                       f"{ev.get('name')} ({human_size(ev.get('size', 0))})")
        elif kind == "xfer_done":
            self.st["mode"] = MODE_BROWSE
            self.st["sorted"] = 0
            n, s = ev.get("name"), ev.get("size", 0)
            if ev.get("direction") == "out":
                self.st["sent"] = self.st.get("sent", 0) + s
                self.toast(f"发送完成 {n} ({human_size(s)})")
            else:
                self.st["recv"] = self.st.get("recv", 0) + s
                self.toast(f"接收完成 {n} ({human_size(s)})")
                # 若正在浏览该目录，自动刷新
                bp = self.pages["browser"]
                if self.stack and self.stack[-1] is bp:
                    bp.reload(keep_cursor=True)
        elif kind == "xfer_abort":
            self.st["mode"] = MODE_BROWSE
            self.toast(f"传输中断 {ev.get('name')}")
        elif kind == "write_denied":
            self.toast(f"已拒绝 PC 写入（{ev.get('why')}）")
        elif kind == "mkdir_done":
            self.toast(f"已新建目录 {os.path.basename(ev.get('path', ''))}")
            bp = self.pages["browser"]
            bp.reload(keep_cursor=True)
        elif kind == "net_error":
            self.toast(f"网络异常: {ev.get('msg')}")
        elif kind == "toast":
            self.toast(ev.get("msg", ""))

    # ---------- 输入 ----------

    def handle(self, evt):
        # 先把原始事件喂给诊断页（无论当前在哪一页都记）。
        # 诊断页只做展示，不改变行为 —— 这样出问题时能直接看到真相。
        try:
            self.pages["diag"].note(evt)
            if evt.kind == "axis":
                self.axis_state[evt.code] = evt.name
        except Exception:
            pass

        # "raw" 是诊断专用事件，不是操作指令 —— 记完就走，不参与分发。
        if evt.kind == "raw":
            return False

        # 模态确认框优先
        if self.confirm:
            self._handle_confirm(evt)
            return True

        # 全局键
        n = evt.name
        if evt.kind == "key":
            if n in ("START", "MENUF"):
                # 在子页面：先返回；在根页面：退出
                if len(self.stack) > 1:
                    self.pop()
                    return True
                self.quit = True
                return True
            if n == "SELECT":
                self.toggle_writable()
                return True
            # ---- L2 / R2 切页 ----
            #
            # 【2026-10-05 实机反馈修正】
            # 上一版 L2=下一个、R2=上一个，用户实测「顺序和手感相反」。
            # 掌机横握时 R2 在右手食指、L2 在左手食指，标签栏是从左到右
            # 排列（状态→文件→网络→日志），直觉是：
            #   R2（右手）= 往右走 = 下一个标签
            #   L2（左手）= 往左走 = 上一个标签
            # 这里按用户体感调换。两个分支除此之外完全对称。
            if n in ("L2", "R2"):
                order = PAGE_ORDER
                i = order.index(self.stack[0].name)
                step = 1 if n == "R2" else -1
                tgt = order[(i + step) % len(order)]
                # 回到根页面（清空子页面栈），再切标签
                while len(self.stack) > 1:
                    self.stack.pop().on_exit()
                self.stack[0] = self.pages[tgt]
                self.stack[0].on_enter()
                self.dirty = True
                return True

        # 交给当前页面
        page = self.stack[-1]
        if page.handle(evt):
            self.dirty = True
            return True
        return False

    def _handle_confirm(self, evt):
        """
        模态确认框的按键处理。

        ★★ 关键：A / B 是**直接动作键**，不看焦点位置。
            A → 允许
            B → 拒绝
            左/右/摇杆 → 只移动高亮（纯视觉）
        """
        c = self.confirm
        n = evt.name

        # 移动高亮：只改视觉，不影响按 A/B 的结果
        if evt.kind == "axis":
            if evt.name in ("左", "右"):
                c.picked = 1 - c.picked
                self.dirty = True
            return

        if n == "A":
            # 无条件允许
            c.answer(True)
            self.log_only(f"确认框 → 允许（{c.action} {c.name}）")
            self.toast("已允许")
            self.confirm = None
            self.dirty = True
        elif n in ("B", "MENUF", "START"):
            # 无条件拒绝。START/MENUF 当"取消"用，语义上等于拒绝。
            c.answer(False)
            self.log_only(f"确认框 → 拒绝（{c.action} {c.name}）")
            self.toast("已拒绝")
            self.confirm = None
            self.dirty = True
        elif n in ("左", "右"):
            c.picked = 1 - c.picked
            self.dirty = True

    # ---------- 绘制 ----------

    def render(self):
        W, H = self.f.width, self.f.height
        k = self.k
        img = Image.new("RGB", (W, H), C_BG)
        d = ImageDraw.Draw(img)
        fonts = self.fonts

        # 尺寸
        pad = max(6, int(10 * k))
        f_small = fonts.small
        f_body = fonts.body
        ti = self.app_ti(d, fonts)

        bar_h = ti["bar_h"]
        foot_h = ti["foot_h"]
        tab_h = ti["tab_h"]

        # ---- 标题条 ----
        d.rectangle([0, 0, W, bar_h], fill=C_BAR)
        title = self.stack[0].title if len(self.stack) == 1 else \
            self.stack[-1].title
        if len(self.stack) > 1:
            title = f"← {title}"
        ty = max(0, (bar_h - ti["head_h"]) // 2)
        d.text((pad, ty), title, font=fonts.head, fill=(255, 255, 255))

        # 右上：模式 + 权限
        mode = MODE_LABEL.get(self.st.get("mode", MODE_BROWSE), "?")
        perm = "RW" if self.st.get("writable") else "RO"
        tag = f"{mode} · {perm}"
        tw = self.tw(d, tag, f_small)
        d.text((W - tw - pad, ty + max(0, (ti["head_h"] - ti["small_h"]) // 2)),
               tag, font=f_small,
               fill=(255, 236, 200) if self.st.get("writable")
               else (215, 232, 250))

        # ---- 内容区 ----
        cy = bar_h
        # 标签栏（仅根页面）
        if len(self.stack) == 1:
            cy += self._draw_tabs(d, 0, cy, W, tab_h, fonts)

        content_y = cy
        content_h = H - content_y - foot_h
        if self.confirm:
            content_h = max(40, content_h - int(96 * k))

        page = self.stack[-1]
        try:
            page.draw(d, pad, content_y + max(2, int(4 * k)),
                      W - pad * 2, content_h - max(4, int(8 * k)), fonts)
        except Exception as e:
            d.text((pad, content_y + 8), f"页面绘制异常: {e}",
                   font=f_small, fill=C_ERR)

        # ---- 模态确认框 ----
        if self.confirm:
            self._draw_confirm(d, W, H, fonts, foot_h)

        # ---- 提示条 ----
        self._draw_toasts(d, W, H - foot_h, fonts)

        # ---- 底栏 ----
        foot_y = H - foot_h
        d.rectangle([0, foot_y, W, H], fill=C_BAR_D)
        hint = self._footer_hint()
        fps = f"{self.st.get('fps', 0):.0f} FPS"
        fw = self.tw(d, fps, f_small)
        ftx = foot_y + max(0, (foot_h - ti["small_h"]) // 2)
        d.text((pad, ftx), ellipsize(d, hint, f_small,
                                     W - fw - pad * 3), font=f_small,
               fill=(255, 255, 255))
        d.text((W - fw - pad, ftx), fps, font=f_small, fill=(198, 224, 255))

        return img

    def app_ti(self, d, fonts):
        """测量各类尺寸，避免每帧重复算。"""
        k = self.k
        def h_of(font):
            try:
                a, b = font.getmetrics()
                return a + b
            except Exception:
                return int(20 * k)
        small_h = h_of(fonts.small)
        head_h = h_of(fonts.head)
        bar_h = max(int(34 * k), head_h + int(16 * k))
        foot_h = max(int(28 * k), small_h + int(10 * k))
        tab_h = 0
        if len(self.stack) == 1:
            tab_h = max(int(28 * k), h_of(fonts.body) + int(6 * k))
        return {"small_h": small_h, "head_h": head_h, "body_h": h_of(fonts.body),
                "bar_h": bar_h, "foot_h": foot_h, "tab_h": tab_h}

    def _draw_tabs(self, d, x, y, w, h, fonts):
        order = PAGE_ORDER
        labels = PAGE_LABEL
        cur = self.stack[0].name
        n = len(order)
        bw = w // n
        d.rectangle([0, y, w, y + h], fill=(20, 24, 32))
        for i, key in enumerate(order):
            bx = x + i * bw
            sel = (key == cur)
            if sel:
                d.rectangle([bx + 2, y + 2, bx + bw - 2, y + h - 1],
                            fill=C_BAR)
            tw = self.tw(d, labels[key], fonts.body)
            ty = y + max(0, (h - self.app_ti(d, fonts)["body_h"]) // 2)
            d.text((bx + (bw - tw) // 2, ty), labels[key], font=fonts.body,
                   fill=(255, 255, 255) if sel else C_DIM)
        return h

    def _draw_confirm(self, d, W, H, fonts, foot_h):
        c = self.confirm
        k = self.k
        pad = max(6, int(10 * k))
        f = fonts.body
        fs = fonts.small
        lh = self.line_h(f)

        lines = c.lines()
        bw = min(W - pad * 2, int(460 * k))
        bh = pad * 3 + (len(lines) + 3) * lh
        bx = (W - bw) // 2
        by = H - foot_h - bh - int(12 * k)

        d.rectangle([bx, by, bx + bw, by + bh], fill=(40, 22, 22),
                    outline=C_ERR, width=max(2, int(2 * k)))
        d.rectangle([bx, by, bx + bw, by + int(28 * k)], fill=C_ERR)
        d.text((bx + pad, by + max(2, int(5 * k))), c.title(),
               font=f, fill=(255, 255, 255))

        cy = by + int(34 * k)
        for ln in lines:
            d.text((bx + pad, cy), ellipsize(d, ln, fs, bw - pad * 2),
                   font=fs, fill=C_FG)
            cy += self.line_h(fs)
        d.text((bx + pad, cy), f"剩余 {c.remaining} 秒（超时视为拒绝）",
               font=fs, fill=C_WARN)
        cy += self.line_h(fs) + int(4 * k)

        # 两个按钮
        #
        # ★ 绘制要点（配合"A/B 直接动作"的交互）：
        #   旧版用 `sel = (i == c.picked)` 画一个"选中框"，视觉上暗示
        #   "先选中再按 A 确认" —— 这正是用户误操作的根源。
        #   现在把按键字母**画大、画进按钮里**（"按 B 拒绝" / "按 A 允许"），
        #   并且不再强调"当前焦点"，让用户一眼看到"直接按哪个键"。
        #   c.picked 只用来做一个轻量的高亮，不改变按键语义。
        bwid = (bw - pad * 3) // 2
        for i, (label, col) in enumerate((("按 B 拒绝", (90, 40, 40)),
                                          ("按 A 允许", (30, 90, 60)))):
            x0 = bx + pad + i * (bwid + pad)
            y0 = cy
            hl = (i == c.picked)
            d.rectangle([x0, y0, x0 + bwid, y0 + lh + int(8 * k)],
                        fill=col,
                        outline=C_ACC if hl else C_CARD_LN,
                        width=max(2, int(2 * k)) if hl else 1)
            tw = self.tw(d, label, f)
            d.text((x0 + (bwid - tw) // 2, y0 + int(4 * k)), label,
                   font=f, fill=(255, 255, 255))

        # 再补一行明确的按键提示 —— 让"不用选焦点"这件事写在脸上
        cy += lh + int(8 * k) + int(6 * k)
        hint = "按 A 允许 / 按 B 拒绝（无需先移动选择）"
        d.text((bx + pad, cy), ellipsize(d, hint, fs, bw - pad * 2),
               font=fs, fill=C_DIM)

    def _draw_toasts(self, d, W, y, fonts):
        now = time.time()
        self.toasts = [(t, e) for (t, e) in self.toasts if e > now]
        if not self.toasts:
            return
        k = self.k
        pad = max(6, int(10 * k))
        f = fonts.small
        lh = self.line_h(f)
        h = len(self.toasts) * lh + pad
        top = y - h - int(4 * k)
        d.rectangle([pad, top, W - pad, top + h], fill=(28, 34, 46),
                    outline=C_ACC)
        cy = top + pad // 2
        for (t, _e) in self.toasts[-3:]:
            d.text((pad * 2, cy), ellipsize(d, t, f, W - pad * 3),
                   font=f, fill=C_FG)
            cy += lh

    def _footer_hint(self):
        """
        底栏那行提示。

        ★ 现在从 FOOTER_HINT 表里读，不再各处硬编码 ——
          这样"底栏提示"和"首页按键说明"是同一份事实的两个视图。
          确认框存在时优先显示确认框的操作（那时其他键都不生效）。
        """
        if self.confirm:
            return "A 允许   B 拒绝"
        page = self.stack[-1].name
        return FOOTER_HINT.get(page, FOOTER_HINT["home"])
