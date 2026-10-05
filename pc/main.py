#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer —— Windows PC 端主程序
=====================================

一个窗口：**左边是本机文件，右边是掌机文件**，中间两个箭头按钮。
箭头方向即"文件往哪边去"，不用学，看一眼就会用。

【为什么本机在左、掌机在右】
    这是最符合直觉的排布：你坐在电脑前面，本机就是你手边的机器。
    中间按钮写的是「复制进掌机 →」→ 点一下文件就往右边（掌机）去，
    按钮箭头方向和文件流向一致。

【启动行为（用户反馈第二轮定稿）】
    打开程序就自动开始扫描局域网，不需要点任何按钮。
    启动先立即扫 1 次，之后每 7 秒扫 1 次，直到程序启动满 5 分钟为止；
    还没连上就停手，只留一行日志提示手动重试 —— 不会没完没了地打扰用户。

【掌机 IP 从哪来（用户反馈第六轮 #2）】
    绝不硬编码某个 IP。顺序是：命令行 --host > 上次连成功记下的 > 留空。
    留空时输入框里是一段灰色占位提示，引导用户点「扫描」或手填。

【文件列表（用户反馈第五轮）】
    左右两个框都是四列：名称 / 后缀 / 大小 / 修改时间。
    列宽在 App.COL_*_W 里统一定义。

【本机栏默认目录（用户反馈第六轮 #3）】
    默认打开**桌面**，不是 C 盘根也不是用户主目录 ——
    对普通人来说桌面才是放东西的地方。

【线程模型】
    tkinter 单线程。所有网络/磁盘操作放后台线程，
    通过 queue.Queue + root.after(60ms) 轮询回主线程刷 UI。
    绝不在后台线程里碰 tk 控件 —— 会随机崩溃。

【空闲保活（用户反馈第六轮 #1）】
    后台每 4 秒发一次心跳，而且心跳用 GET_DEVICE_INFO 而不是 PING：
    一次往返**同时**做到"保活"和"把掌机最新的 writable 同步回来"。
    这样用户在掌机上按 SELECT 切读写，PC 面板最多 4 秒就跟着变。

用法：
    python pc/main.py
    python pc/main.py --host 192.168.3.25     # 跳过自动发现
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client as clipy         # noqa: E402
import protocol as P           # noqa: E402
from protocol import Nack      # noqa: E402

APP = "PocketTransfer"
VER = "0.3"

# ---------------------------------------------------------------------------
# ★ 配置持久化（用户反馈 #2：记住上次连上的掌机 IP）
# ---------------------------------------------------------------------------
# 【为什么不能硬编码一个 IP】
#   开发这台机器在局域网上恰好是 192.168.3.25，但**分发给别人就不是了**。
#   写死一个 IP 会让别人一打开就看到"连不上"，而且不知道要改哪里。
#
# 【策略】
#   · 首次启动：输入框留**空**，提示语告诉他可以点「扫描」自动发现
#   · 连接成功一次 → 记住这个 IP，下次启动自动填上并尝试连接
#   · 之后在别的 IP 上又连成功 → 用新的覆盖（永远记住"最近一次成功的"）
#
# 【存哪】%APPDATA%\PocketTransfer\config.json
#   Windows 惯例位置，不污染程序目录（exe 可能是只读的、也可能在 U 盘上）。
#   读写失败一律静默降级 —— 配置坏了不该让程序起不来。
CONFIG_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"), APP)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def load_config():
    """读配置。任何异常都返回 {}（配置坏了不该让程序起不来）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_config(d):
    """写配置。失败静默 —— 记不住上次 IP 不影响正常使用。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)      # 原子替换，避免写一半断电留残file
        return True
    except Exception:
        return False


def remember_host(ip):
    """把"最近一次连接成功"的掌机 IP 记下来。"""
    if not ip:
        return
    cfg = load_config()
    if cfg.get("last_host") == ip:
        return                            # 没变就不写盘
    cfg["last_host"] = ip
    save_config(cfg)


def default_local_dir():
    """
    本机栏的默认目录。

    ★ 用户反馈 #3：默认给桌面。用 `~`（就是 C:\\Users\\xxx）普通人会懵，
      桌面才是大家真正放东西的地方。
    找不到桌面（极少见）就退回用户主目录。
    """
    for sub in ("Desktop", "桌面"):
        p = os.path.join(os.path.expanduser("~"), sub)
        if os.path.isdir(p):
            return p
    return os.path.expanduser("~")


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------

BG = "#1e2229"
BG2 = "#262b34"
FG = "#e6e9ef"
DIM = "#8b93a3"
ACC = "#4da3ff"
OK = "#4cd07d"
WARN = "#ffb454"
ERR = "#ff6b6b"
SEL = "#33517a"
ATTN = "#3a2f14"          # 需要用户去掌机操作时的醒目底色


def list_drives():
    """
    枚举本机可用盘符，返回 [("C:", "C:\\"), ...]。

    ★ 为什么要有这个：
        用户想把文件放到 D 盘/U 盘/移动硬盘，光靠"选目录"对话框
        要先点很多层。给一个下拉框直接切盘符，一步到位。

    实现上优先用 Windows API（GetLogicalDrives），拿不到就退回
    枚举 A: 到 Z: 的存在性探测。非 Windows（开发机）返回 [("/", "/")]。
    """
    if os.name != "nt":
        return [("/", "/")]
    drives = []
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bitmask & (1 << i):
                letter = chr(ord("A") + i)
                root = f"{letter}:\\"
                drives.append((f"{letter}:", root))
    except Exception:
        drives = []
    if not drives:
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if os.path.exists(root):
                drives.append((f"{letter}:", root))
    # 加个"快速位置"：桌面 / 下载 / 文档 —— 平常用得最多
    return drives


def quick_places():
    """常用目录，拼在下拉框里方便一键跳转。"""
    home = os.path.expanduser("~")
    out = []
    for label, sub in (("桌面", "Desktop"), ("下载", "Downloads"),
                       ("文档", "Documents"), ("图片", "Pictures"),
                       ("视频", "Videos"), ("音乐", "Music")):
        p = os.path.join(home, sub)
        if os.path.isdir(p):
            out.append((label, p))
    return out


def file_ext(name, isdir=False):
    """
    取文件后缀（不含点），用于列表的「后缀」列。

    ★ 用户反馈 #5 新增这一列。规则：
        · 目录     → 显示 "DIR"（一眼区分目录和文件）
        · 无后缀   → 显示 "—"（不是空白，空白会让人以为列错位了）
        · 多后缀   → 只取最后一段（a.tar.gz → "gz"）
        · 统一小写（.JPG / .jpg 看起来一致，便于扫读）
        · 太长时截断到 8 字符，避免撑破列宽
    """
    if isdir:
        return "DIR"
    base = os.path.basename(name)
    # 纯 dotfile（.gitignore / .bashrc）整体算"无后缀"，
    # 因为"gitignore"不是后缀，是文件名本身。
    if not base or base.startswith(".") and base.count(".") == 1:
        return "—"
    if "." not in base:
        return "—"
    ext = base.rsplit(".", 1)[-1]
    if not ext or ext == base:
        return "—"
    ext = ext.lower()
    return ext[:8] if len(ext) > 8 else ext


def style_setup(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=BG)
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Dim.TLabel", background=BG, foreground=DIM)
    st.configure("Head.TLabel", background=BG, foreground=ACC,
                 font=("Microsoft YaHei UI", 11, "bold"))
    st.configure("TButton", padding=(10, 5))
    st.configure("Accent.TButton", padding=(10, 5))
    st.map("Accent.TButton",
           background=[("!disabled", ACC)],
           foreground=[("!disabled", "#0b1620")])
    st.configure("Treeview",
                 background=BG2, fieldbackground=BG2, foreground=FG,
                 rowheight=24, borderwidth=0)
    st.configure("Treeview.Heading",
                 background="#2f3540", foreground=FG, borderwidth=0)
    st.map("Treeview", background=[("selected", SEL)])
    st.map("Treeview.Heading", background=[("active", "#3a414e")])
    st.configure("TProgressbar", background=ACC, troughcolor=BG2,
                 borderwidth=0)
    st.configure("TCombobox", padding=(4, 2))


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root, host=None):
        self.root = root
        self.cli = None
        self.cfg = load_config()          # ★ 持久化配置（记住上次成功的 IP）

        # ★ 用户反馈 #2：不再硬编码开发机那台 IP。
        #   优先级：命令行 -H  >  配置文件里"上次连成功的"  >  空
        #   空的时候界面上会提示"点扫描自动发现"，而不是给一个假 IP 让人困惑。
        if host:
            self.host = host
        else:
            self.host = self.cfg.get("last_host") or ""
        self.host_from_cfg = bool(not host and self.host)

        self.busy = False
        self.cancel_flag = False
        self.remote_items = []      # 当前掌机目录的 [{name,dir,size,mtime}]
        self.remote_path = ""       # 相对卡根
        # ★ 用户反馈 #3：本机栏默认给桌面（`~` 会让普通人懵）
        self.local_path = default_local_dir()
        self.notice_box = None      # 当前"请去掌机操作"的提示窗（没有则 None）
        self._last_ka = time.time()  # 上次心跳时间

        # ---- 自动扫描状态（用户反馈 #2，第二轮定稿）----
        #   启动 → 立即扫 1 次 → 之后每 7 秒 1 次 → 满 5 分钟停止。
        #   详见 _auto_scan_start 上方的注释。
        self.auto_scan = True        # 自动扫描总开关（连上/超时即关闭）
        self.scan_round = 0          # 本次自动扫描已扫了几次
        self._scan_t0 = 0.0          # 自动扫描起始时刻（时间窗基准）
        self._scan_job = None        # after() 句柄，用于取消挂起的下一拍

        self.q = queue.Queue()      # 后台线程 → 主线程的消息
        self.cli_lock = threading.RLock()   # 保护 self.cli 的读写（避免竞态）

        root.title(f"{APP} {VER}")
        root.geometry("1120x700")
        root.minsize(940, 580)
        style_setup(root)

        self._build()
        self._poll()
        self._heartbeat()

        if host:
            self.root.after(200, lambda: self.do_connect(host))
        else:
            # ★ 用户反馈 #2：打开就默认开始扫描
            #   （立即 1 次 + 每 7 秒 1 次，5 分钟后停）
            self.root.after(300, self._auto_scan_start)

    # ---------------- 界面搭建 ----------------

    def _build(self):
        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill="x")

        ttk.Label(top, text=APP, style="Head.TLabel").pack(side="left")
        ttk.Label(top, text=f"v{VER}", style="Dim.TLabel").pack(
            side="left", padx=(6, 18))

        ttk.Label(top, text="掌机 IP").pack(side="left")
        # ★ 用户反馈 #2：没记住 IP 时留空，用 placeholder 提示怎么填，
        #   而不是塞一个"别人的" IP 让人以为是自己的。
        self.var_host = tk.StringVar(value=self.host or "")
        e = ttk.Entry(top, textvariable=self.var_host, width=16)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _e: self.do_connect(self.var_host.get()))
        self.entry_host = e
        if not self.host:
            self._attach_host_placeholder(e)

        self.btn_scan = ttk.Button(top, text="扫描", command=self.do_scan)
        self.btn_scan.pack(side="left", padx=2)
        self.btn_conn = ttk.Button(top, text="连接", style="Accent.TButton",
                                   command=lambda: self.do_connect(
                                       self.var_host.get()))
        self.btn_conn.pack(side="left", padx=2)

        self.lbl_state = ttk.Label(top, text="未连接", style="Dim.TLabel")
        self.lbl_state.pack(side="left", padx=14)

        # 权限开关提示
        self.lbl_perm = ttk.Label(top, text="", style="Dim.TLabel")
        self.lbl_perm.pack(side="right")

        # ---- 醒目提示条：掌机需要人动手时才出现 ----
        # 默认隐藏（pack_forget 状态由 _show_attn / _hide_attn 控制）
        self.attn_frame = tk.Frame(self.root, bg=ATTN, padx=12, pady=8)

        # ---- 中部：双栏（★ 左=本机，右=掌机）----
        mid = ttk.Frame(self.root, padding=(12, 0, 12, 0))
        mid.pack(fill="both", expand=True)
        self._mid_ref = mid       # 醒目提示条要插在它前面
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=0)
        mid.columnconfigure(2, weight=1)
        mid.rowconfigure(0, weight=1)

        # ===== 左：本机 (Windows) =====
        lf = ttk.Frame(mid)
        lf.grid(row=0, column=0, sticky="nsew")
        lf.rowconfigure(2, weight=1)
        lf.columnconfigure(0, weight=1)

        lh = ttk.Frame(lf)
        lh.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(lh, text="本机 (Windows)", style="Head.TLabel").pack(
            side="left")
        ttk.Button(lh, text="↑ 上级", width=8,
                   command=self.local_up).pack(side="right")
        ttk.Button(lh, text="选目录", width=8,
                   command=self.local_pick).pack(side="right", padx=4)

        # ★ 盘符/常用位置下拉框（问题 2）
        lr = ttk.Frame(lf)
        lr.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(lr, text="位置", style="Dim.TLabel").pack(side="left")
        self.var_drive = tk.StringVar()
        self.cmb_drive = ttk.Combobox(
            lr, textvariable=self.var_drive, width=34, state="readonly")
        self.cmb_drive.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.cmb_drive.bind("<<ComboboxSelected>>", self._on_drive_pick)

        # ★ 四列（用户反馈 #5）：名称 / 后缀 / 大小 / 修改时间
        self.tree_l = self._make_tree(
            lf, columns=("ext", "size", "mtime"),
            headings=("后缀", "大小", "修改时间"),
            widths=(self.COL_EXT_W, self.COL_SIZE_W, self.COL_MTIME_W))
        self.tree_l.grid(row=2, column=0, sticky="nsew")
        self.tree_l.bind("<Double-1>", lambda _e: self.local_enter())

        self.lbl_lpath = ttk.Label(lf, text="", style="Dim.TLabel")
        self.lbl_lpath.grid(row=3, column=0, sticky="w", pady=(4, 0))

        # ===== 中：按钮 =====
        cf = ttk.Frame(mid, padding=14)
        cf.grid(row=0, column=1, sticky="ns")
        ttk.Label(cf, text="", width=12).pack()
        # ★ 文案（2026-10-05 用户反馈 #4）：
        #   用户说"上传下载怎么理解都不太对"，建议改成「复制进掌机/复制进电脑」。
        #   这次从"发送"进一步改成"复制"，语义更准 —— 两边都是真文件，
        #   用户理解成"把文件复制过去"最自然。
        #   箭头方向 = 文件流向：
        #     「← 复制进电脑」在左边栏（本机）方向 → 箭头朝左
        #     「复制进掌机 →」在右边栏（掌机）方向 → 箭头朝右
        self.btn_to_pc = ttk.Button(cf, text="← 复制进电脑", width=13,
                                    style="Accent.TButton",
                                    command=lambda: self.transfer("down"))
        self.btn_to_pc.pack(pady=(60, 8))
        self.btn_to_hh = ttk.Button(cf, text="复制进掌机 →", width=13,
                                    style="Accent.TButton",
                                    command=lambda: self.transfer("up"))
        self.btn_to_hh.pack(pady=8)

        # ★ 用户反馈 #4：按钮要说明"在掌机的当前目录下"新建，而不是笼统"新建目录"
        self.btn_mkdir = ttk.Button(
            cf, text="在掌机当前目录\n新建文件夹", width=13,
            command=self.do_mkdir)
        self.btn_mkdir.pack(pady=(22, 6))

        # ★ 用户反馈 #5：新增删除按钮 —— 默认禁用，选中掌机文件后才可用
        self.btn_del = ttk.Button(
            cf, text="删除选中的\n掌机文件", width=13,
            command=self.do_delete, state="disabled")
        self.btn_del.pack(pady=(6, 8))

        # ===== 右：掌机 (RG35XX Pro) =====
        rf = ttk.Frame(mid)
        rf.grid(row=0, column=2, sticky="nsew")
        rf.rowconfigure(1, weight=1)
        rf.columnconfigure(0, weight=1)

        rh = ttk.Frame(rf)
        rh.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(rh, text="掌机 (RG35XX Pro)", style="Head.TLabel").pack(
            side="left")
        self.btn_up_r = ttk.Button(rh, text="↑ 上级", width=8,
                                   command=self.remote_up)
        self.btn_up_r.pack(side="right")
        self.btn_ref_r = ttk.Button(rh, text="刷新", width=6,
                                    command=self.remote_reload)
        self.btn_ref_r.pack(side="right", padx=4)

        # ★ 四列（用户反馈 #5）：名称 / 后缀 / 大小 / 修改时间
        self.tree_r = self._make_tree(
            rf, columns=("ext", "size", "mtime"),
            headings=("后缀", "大小", "修改时间"),
            widths=(self.COL_EXT_W, self.COL_SIZE_W, self.COL_MTIME_W))
        self.tree_r.grid(row=1, column=0, sticky="nsew")
        self.tree_r.bind("<Double-1>", lambda _e: self.remote_enter())
        # ★ 用户反馈 #5：选中掌机文件时启用「删除」按钮
        self.tree_r.bind("<<TreeviewSelect>>", self._on_remote_select)

        self.lbl_rpath = ttk.Label(rf, text="/", style="Dim.TLabel")
        self.lbl_rpath.grid(row=2, column=0, sticky="w", pady=(4, 0))

        # ---- 底部：进度 + 日志 ----
        bot = ttk.Frame(self.root, padding=(12, 6, 12, 10))
        bot.pack(fill="x")

        pf = ttk.Frame(bot)
        pf.pack(fill="x")
        self.pb = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True)
        self.btn_cancel = ttk.Button(pf, text="取消", width=8,
                                     command=self.do_cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=8)
        self.lbl_prog = ttk.Label(pf, text="", width=28, style="Dim.TLabel")
        self.lbl_prog.pack(side="left")

        self.log = tk.Text(bot, height=6, bg=BG2, fg=DIM, bd=0,
                           font=("Consolas", 9), wrap="none")
        self.log.pack(fill="x", pady=(8, 0))
        self.log.configure(state="disabled")

        self._refresh_drives()
        self._refresh_local()

    # ---------------- 掌机 IP 输入框的占位提示 ----------------

    def _attach_host_placeholder(self, entry):
        """
        给空的「掌机 IP」输入框挂一段灰色占位文字。

        ★ 用户反馈 #2（2026-10-05）：
          不能再默认填 192.168.3.25 —— 那是开发者自己局域网里的地址，
          别人拿去用会一脸懵（"我没填过啊，哪来的 IP？"）。
          但纯空框又不知道要填什么，于是用占位文字说清楚。

        实现上有两个坑，必须绕开：

        ① **占位文字绝不能进 var_host**。
           如果为了"显示提示"而把文字真的写进 StringVar，
           用户直接点「连接」时就会拿着 "点「扫描」自动发现…" 去连，
           报一个莫名其妙的错。所以这里用独立的 Label 浮在输入框上，
           只在 `var_host` 为空时 place 出来，一旦有真值就 place_forget。

        ② **输入法/程序化赋值都要能识破**。
           `var_host.trace_add` 能同时覆盖「手打」和「代码里 set()」两种情况，
           比只绑 <KeyRelease> 稳（粘贴、清除都得照顾到）。
        """
        # 输入框里的实际字体高度不固定，用 textvariable 变化来驱动更可靠
        state = {"on": False}

        def show():
            if not state["on"]:
                ph.place(relx=0, rely=0.5, anchor="w", x=6)
                state["on"] = True

        def hide():
            if state["on"]:
                ph.place_forget()
                state["on"] = False

        def on_change(*_a):
            # 有内容 → 藏提示；空 → 显提示
            if self.var_host.get():
                hide()
            else:
                show()

        def on_focus_in(_e=None):
            # 聚焦时如果还是空的，隐藏提示，避免和光标打架
            if not self.var_host.get():
                hide()

        def on_focus_out(_e=None):
            if not self.var_host.get():
                show()

        # 占位 Label 挂在输入框的父容器上，用 place 叠在输入框位置。
        # 注意：ttk.Entry 的底色由主题决定（这里主题是深色 BG），
        #       所以 Label 也用 BG，视觉上才像"输入框里的一段浅色字"。
        ph = tk.Label(
            entry.master,
            text="点「扫描」自动发现，或填掌机 IP",
            fg=DIM, bg=BG, font=("Microsoft YaHei UI", 9))

        entry.bind("<FocusIn>", on_focus_in, add="+")
        entry.bind("<FocusOut>", on_focus_out, add="+")
        self.var_host.trace_add("write", on_change)

        # 绑定完立刻按当前值同步一次
        on_change()
        self._host_ph = ph           # 留个引用，免得被回收

    # 四个列的默认宽度（像素）
    #
    # ★ 用户反馈 #5：原本是「名称 / 大小 / 修改时间」三列，
    #   要求拆成「名称 / 后缀 / 大小 / 修改时间」四列，
    #   并且每列宽度要"合理些"。
    #
    #   宽度是照着实际内容定的：
    #     名称    —— 要放得下 "📁 Documents" 这类带图标的 20 来个字符
    #     后缀    —— 最多 2~4 个字符（gba / pdf / mp4 / iso …），但要
    #                显得不局促，给 62
    #     大小    —— "1023.5 MB" 这种最长的约 10 字符，80 够
    #     修改时间 —— "2026-10-05 08:44" 是 16 字符，给 132
    #   实测 1120px 宽的窗口下四列加起来刚好铺满，不会横向滚动。
    COL_NAME_W = 250
    COL_EXT_W = 62
    COL_SIZE_W = 80
    COL_MTIME_W = 132

    def _make_tree(self, parent, columns, headings, widths):
        """
        建一个四列文件表：名称 / 后缀 / 大小 / 修改时间。

        列宽的取值见类属性 COL_*_W 的注释 —— 别随手改小，
        否则"修改时间"会被截成 "2026-10-05 08:4…"，很难看。
        """
        t = ttk.Treeview(parent, columns=("name",) + columns,
                         show="headings", selectmode="extended")
        t.heading("name", text="名称")
        t.column("name", width=self.COL_NAME_W, anchor="w",
                 minwidth=120, stretch=True)
        for c, h, w in zip(columns, headings, widths):
            t.heading(c, text=h)
            # 后缀/时间居中，大小右对齐（便于数字纵向比较）
            anchor = "e" if c == "size" else "center"
            # 只有"名称"列跟着窗口拉伸，其余保持固定宽度
            t.column(c, width=w, anchor=anchor, minwidth=w, stretch=False)
        sb = ttk.Scrollbar(parent, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
        return t

    # ---------------- 日志 ----------------

    def say(self, msg, tag=None):
        ts = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ts}] {msg}\n")
        if int(self.log.index("end-1c").split(".")[0]) > 800:
            self.log.delete("1.0", "200.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------------- 后台 → 主线程 ----------------

    def _poll(self):
        """每 60ms 排空后台消息队列。这是唯一的 UI 更新入口。"""
        try:
            while True:
                fn, args = self.q.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    self.say(f"UI 回调异常: {e}")
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def post(self, fn, *args):
        self.q.put((fn, args))

    # ---------------- 连接 ----------------

    # ---------------- 自动扫描（用户反馈 #2，第二轮定稿） ----------------
    #
    # 需求原话（第一轮）：
    #   "pc端打开就默认扫描，连扫3遍后如果还没有连接，就再扫第4、5遍，
    #    3/4/5遍之间每次扫描间隔20秒，扫完5遍就不再主动扫描了。"
    #
    # 需求原话（第二轮修正，以此为准）：
    #   "现在开始的扫描好像还不太合适，改成win11端启动后进行一次扫描，
    #    然后每隔7秒进行一次扫描，直到启动软件5分钟后停止自动扫描。"
    #
    # 定稿策略：
    #   启动 → 立即扫 1 次
    #   之后 → 每 7 秒扫 1 次（固定间隔，不再"前 3 遍连扫"）
    #   截止 → 距启动满 5 分钟时停止，不再主动扫
    #
    # 为什么从"5 遍"改成"5 分钟内每 7 秒"：
    #   固定遍数在"用户还没去掌机点开应用"时显得很急（前几遍白扫），
    #   在"用户已经就位"时又扫得太少。改成按时间窗更自然：
    #   5 分钟给足了"开机 → 点开应用"的时间，7 秒一次也不会太吵。
    AUTO_SCAN_INTERVAL_S = 7.0    # 每次扫描之间的间隔
    AUTO_SCAN_WINDOW_S = 300.0    # 自动扫描的总时间窗（5 分钟）

    def _auto_scan_start(self):
        """启动自动扫描。记下起点，之后按时间窗判断是否该停。"""
        if not self.auto_scan:
            return
        self._scan_t0 = time.time()     # 本轮自动扫描的起始时刻
        self.scan_round = 0
        self.say(f"自动扫描已开启：每 {int(self.AUTO_SCAN_INTERVAL_S)} 秒一次，"
                 f"{int(self.AUTO_SCAN_WINDOW_S // 60)} 分钟后自动停止")
        self._auto_scan_tick()

    def _auto_scan_tick(self):
        """自动扫描的一次"拍"。扫完一轮由 _auto_scan_next 再排下一次。"""
        if not self.auto_scan or self.cli or self.busy:
            return

        # 时间窗到了 → 停（用户反馈 #2 的核心约束）
        elapsed = time.time() - getattr(self, "_scan_t0", time.time())
        if elapsed >= self.AUTO_SCAN_WINDOW_S:
            self._stop_auto_scan()
            self.lbl_state.config(text="未发现设备（已停止自动扫描）")
            self.say(f"已自动扫描 {int(elapsed)} 秒（超过 "
                     f"{int(self.AUTO_SCAN_WINDOW_S // 60)} 分钟），自动扫描结束。\n"
                     "  → 请检查：① 掌机已开机 ② 同一个 WiFi "
                     "③ 掌机上 PocketTransfer 已点开\n"
                     "  → 就绪后点右上角「扫描」或「连接」重试")
            return

        self.scan_round += 1
        n = self.scan_round
        left = int(self.AUTO_SCAN_WINDOW_S - elapsed)
        self.lbl_state.config(text=f"正在扫描局域网…（第 {n} 次）")
        self.say(f"自动扫描 第 {n} 次 …（剩余 {left // 60}:{left % 60:02d}）")

        def work():
            try:
                found = clipy.discover(timeout=2.5)
            except Exception as e:
                self.post(self.say, f"扫描失败: {e}")
                self.post(self._auto_scan_next)
                return
            if found:
                d = found[0]
                self.post(self.say, f"✓ 发现 {d.get('model', '掌机')} "
                                    f"@ {d['ip']}")
                self.post(self.var_host.set, d["ip"])
                # 发现即停自动扫描，转入手动连接流程
                self._stop_auto_scan()
                self.post(self.do_connect, d["ip"])
                return
            self.post(self.say, f"第 {n} 次没扫到掌机")
            self.post(self._auto_scan_next)

        threading.Thread(target=work, daemon=True).start()

    def _auto_scan_next(self):
        """一轮没扫到 → 排下一次（固定 7 秒间隔，到点由 tick 自己收尾）。"""
        if not self.auto_scan or self.cli:
            return
        elapsed = time.time() - getattr(self, "_scan_t0", time.time())
        if elapsed >= self.AUTO_SCAN_WINDOW_S:
            # 交给 tick 去做统一的收尾（保证只有一处"结束"文案）
            self._scan_job = self.root.after(1, self._auto_scan_tick)
            return
        # ★ 固定 7 秒间隔（用户反馈 #2：每隔 7 秒一次）
        gap = self.AUTO_SCAN_INTERVAL_S
        self.lbl_state.config(
            text=f"未发现设备，{int(gap)} 秒后重试")
        self._scan_job = self.root.after(int(gap * 1000),
                                         self._auto_scan_tick)

    def do_scan(self):
        """
        手动扫描（点「扫描」按钮）。

        手动扫描不受自动扫描的 5 分钟时间窗约束 —— 用户主动点，
        说明他就是想现在再试一次，没道理拦着。
        """
        if self.busy:
            return
        self.lbl_state.config(text="正在扫描局域网…")
        self.say("UDP 广播扫描中…")

        def work():
            try:
                found = clipy.discover(timeout=2.5)
            except Exception as e:
                self.post(self.say, f"扫描失败: {e}")
                self.post(self.lbl_state.config, {"text": "扫描失败"})
                return
            if not found:
                self.post(self.say, "没扫到掌机。请确认："
                                    "① 掌机已开机 ② 连的是同一个 WiFi "
                                    "③ 掌机上 PocketTransfer 正在运行")
                self.post(self.lbl_state.config, {"text": "未发现设备"})
                return
            d = found[0]
            self.post(self.say, f"发现 {d.get('model', '掌机')} @ {d['ip']}")
            self.post(self.var_host.set, d["ip"])
            self.post(self.do_connect, d["ip"])

        threading.Thread(target=work, daemon=True).start()

    def do_connect(self, host):
        host = (host or "").strip()
        if not host or self.busy:
            return
        # 连接开始 → 无论如何都不用再自动扫描了
        self._stop_auto_scan()
        self.busy = True
        self._enable(False)
        self.lbl_state.config(text=f"连接 {host} …")
        self.say(f"连接 {host} …")

        def work():
            try:
                c = clipy.Client(host).connect()
                # ★ 订阅"需要人动手"的通知（问题 5）
                c.on_notice = self._on_notice
                info = c.device_info()
            except Exception as e:
                self.post(self.say, f"连接失败: {e}")
                self.post(self.lbl_state.config, {"text": "连接失败"})
                self.post(self._done)
                return
            with self.cli_lock:
                self.cli = c
            self.host = host
            # ★ 用户反馈 #2：记住"最近一次连成功"的 IP，下次启动自动填上。
            #   在别的 IP 上又连成功 → 用新的覆盖。
            #   放后台线程里写盘，不阻塞 UI（写失败也不影响连接）。
            remember_host(host)
            self.cfg["last_host"] = host
            self._last_ka = time.time()
            # ★ 连上后占位提示必须撤掉，否则会盖在真实 IP 上
            if getattr(self, "_host_ph", None) is not None:
                try:
                    self.post(self._host_ph.place_forget)
                except Exception:
                    pass
            self._last_ka = time.time()
            self.post(self.say, f"已连接：{info.get('model', '?')} / "
                                f"固件 {info.get('fw', '?')} / "
                                f"卡根 {info.get('root', '?')}")
            self.post(self.lbl_state.config,
                      {"text": f"已连接 {host}"})
            self.post(self._update_perm, info)
            self.post(self.remote_reload)
            self.post(self._done)

        threading.Thread(target=work, daemon=True).start()

    def _stop_auto_scan(self):
        """关掉自动扫描并取消挂起的下一拍（连上/放弃时调用）。"""
        self.auto_scan = False
        job = self._scan_job
        self._scan_job = None
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def _update_perm(self, info):
        if info.get("writable"):
            self.lbl_perm.config(text="掌机：可写（写入时会弹窗确认）",
                                 foreground=OK)
            # 已可写就别再挂着"请解锁"的提示条了
            self._hide_attn()
        else:
            # ★ 2026-10-05 用户反馈 #1：掌机默认就是可写。
            #   走到这里说明用户**主动**在掌机上按了 SELECT 把它锁住了。
            #   所以文案是"你锁住了"，不是"你还没解锁"。
            self.lbl_perm.config(
                text="掌机：已锁定（掌机上按 SELECT 可解锁）",
                foreground=WARN)

    def _enable(self, on):
        st = "normal" if on else "disabled"
        for b in (self.btn_scan, self.btn_conn, self.btn_to_pc,
                  self.btn_to_hh, self.btn_mkdir, self.btn_ref_r,
                  self.btn_up_r):
            b.config(state=st)
        # ★ 「删除」按钮有额外条件：必须选中了掌机文件（用户反馈 #5）
        #   忙碌时一律禁用；空闲时看有没有选中项。
        if not on:
            self.btn_del.config(state="disabled")
        else:
            self._on_remote_select()

    def _done(self):
        self.busy = False
        self._enable(True)
        self.btn_cancel.config(state="disabled")
        self.pb["value"] = 0

    # ---------------- ★ 醒目提示条（掌机需要人动手时） ----------------

    def _show_attn(self, text, kind="confirm"):
        """
        显示顶部醒目提示条。

        ★ 为什么需要这个（问题 5）：
            上传/新建目录时，掌机会弹确认框并阻塞等用户按 A/B。
            PC 端如果不提示，用户只会看到一个卡住的进度条，
            根本不知道要去掌机上按一下 —— 体验上像"卡死"。
            这里把"请去掌机操作"直接摆到最显眼的位置。
        """
        color = WARN if kind != "locked" else ERR
        icon = "⏳" if kind == "confirm" else "🔒"

        # 先清掉旧的子控件（反复调用不叠加）
        for w in self.attn_frame.winfo_children():
            w.destroy()

        self.attn_label = tk.Label(
            self.attn_frame, text=f"{icon}  {text}", bg=ATTN, fg=color,
            font=("Microsoft YaHei UI", 12, "bold"), anchor="w",
            justify="left")
        self.attn_label.pack(side="left", fill="x", expand=True)

        # 放到底部日志之上、顶部之下 —— 这里用 pack 在 mid 之前插入
        if not self.attn_frame.winfo_ismapped():
            self.attn_frame.pack(fill="x", padx=12, pady=(6, 6),
                                 before=self._mid_ref)

    def _hide_attn(self):
        try:
            self.attn_frame.pack_forget()
        except Exception:
            pass

    def _on_notice(self, kind, data):
        """
        client 层的 on_notice 回调（在**后台线程**里被调用）。
        绝对不能在这里碰 tk 控件 —— 排队回主线程。
        """
        hint = data.get("hint", "")
        name = data.get("name", "")
        if kind == "confirm":
            verb = "新建目录" if data.get("action") == "mkdir" else "写入文件"
            msg = (f"请在掌机上确认：{verb} {name}\n"
                   f"按【A】允许，按【B】拒绝（最长 60 秒）")
            self.post(self._show_attn, msg, "confirm")
            self.post(self.say, f"⏳ 等待掌机确认 {name} …")
        elif kind == "locked":
            self.post(self._show_attn,
                      "掌机已被锁定：请在掌机上按【SELECT】键解锁后再试",
                      "locked")
            self.post(self.say, f"🔒 {hint or '掌机已锁定，需要解锁'}")
        elif kind == "perm":
            # ★ 用户反馈：掌机按 SELECT 切换读写后，PC 必须同步。
            #   在此之前只有"连接时"和"写入后"才刷新权限标签，
            #   于是掌机上按 SELECT，PC 面板纹丝不动。
            #   掌机侧现在会主动推这条通知。
            w = bool(data.get("writable"))
            self.post(self._apply_perm, w)
            self.post(self.say, f"{'🔓' if w else '🔒'} {hint}")
        elif kind == "busy":
            self.post(self.say, "掌机正忙，请稍后重试")

    def _apply_perm(self, writable):
        """按"掌机可写与否"刷新权限标签（主线程）。"""
        self._update_perm({"writable": writable})

    # ---------------- 掌机侧文件浏览 ----------------

    def remote_reload(self):
        with self.cli_lock:
            cli = self.cli
        if not cli:
            return

        def work():
            try:
                d = cli.list_dir(self.remote_path)
            except Nack as e:
                self.post(self.say, f"列目录被拒: {e.msg}")
                return
            except Exception as e:
                self.post(self.say, f"列目录失败: {e}")
                self.post(self._lost)
                return
            self.post(self._fill_remote, d)

        threading.Thread(target=work, daemon=True).start()

    def _fill_remote(self, d):
        self.remote_items = d.get("items", [])
        self.tree_r.delete(*self.tree_r.get_children())
        for it in self.remote_items:
            # ★ 四列：名称 / 后缀 / 大小 / 修改时间（用户反馈 #5）
            self.tree_r.insert(
                "", "end", iid=it["name"],
                values=("▣ " + it["name"] + "/" if it["dir"]
                        else "   " + it["name"],
                        file_ext(it["name"], it["dir"]),
                        "" if it["dir"] else P.human_size(it["size"]),
                        P.fmt_mtime(it.get("mtime"))))
        rel = d.get("path") or ""
        self.remote_path = rel
        self.lbl_rpath.config(text="掌机：/" + rel if rel else "掌机：/")

    def remote_enter(self):
        sel = self.tree_r.selection()
        if not sel:
            return
        name = sel[0]
        it = self._remote_item(name)
        if it and it["dir"]:
            self.remote_path = (self.remote_path + "/" + name).lstrip("/")
            self.remote_reload()

    def remote_up(self):
        if not self.remote_path:
            self.say("已在卡根")
            return
        self.remote_path = os.path.dirname(self.remote_path)
        self.remote_reload()

    def _remote_item(self, name):
        for it in self.remote_items:
            if it["name"] == name:
                return it
        return None

    def _on_remote_select(self, _evt=None):
        """
        掌机文件选中状态变化 → 决定「删除」按钮是否可用。

        ★ 用户反馈 #5：「当有掌机文件被选中时，该按钮可用」。
          注意 `_enable()` 会在忙碌时禁用一组按钮，所以这里要
          尊重忙碌状态 —— 不能在传输过程中把删除按钮点开。
        """
        try:
            has = bool(self.tree_r.selection())
            if self.busy:
                self.btn_del.config(state="disabled")
            else:
                self.btn_del.config(state="normal" if has else "disabled")
        except Exception:
            pass

    def _remote_selected(self):
        out = []
        for n in self.tree_r.selection():
            it = self._remote_item(n)
            if it:
                out.append(it)
        return out

    # ---------------- 本机侧文件浏览 ----------------

    def _refresh_drives(self):
        """
        刷新盘符下拉框。

        ★ 解决"不能从这个大选择框里选不同的硬盘"的问题（问题 2）。
          下拉里同时包含：各盘符 + 常用目录（桌面/下载/文档…），
          用户既能一步切盘，也能一步跳到常用位置。
        """
        items = []           # [(显示文本, 真实路径)]
        for label, root in list_drives():
            items.append((f"💽  {label}", root))
        for label, p in quick_places():
            items.append((f"📁  {label}  ({p})", p))

        self._drive_items = items
        self.cmb_drive["values"] = [t for t, _ in items]
        self._sync_drive_box()

    def _sync_drive_box(self):
        """让下拉框的当前值反映 self.local_path。"""
        try:
            items = getattr(self, "_drive_items", [])
            cur = os.path.abspath(self.local_path)
            best = None
            for text, path in items:
                ap = os.path.abspath(path)
                if cur == ap or cur.startswith(ap.rstrip("\\/") + os.sep):
                    # 取最长匹配（即最具体的那个盘/目录）
                    if best is None or len(ap) > len(os.path.abspath(
                            best[1])):
                        best = (text, path)
            if best:
                self.var_drive.set(best[0])
            else:
                self.var_drive.set(cur)
        except Exception:
            pass

    def _on_drive_pick(self, _evt=None):
        """下拉框选择变化 → 切换本机当前目录。"""
        sel = self.var_drive.get()
        for text, path in getattr(self, "_drive_items", []):
            if text == sel:
                if os.path.isdir(path):
                    self.local_path = path
                    self._refresh_local()
                else:
                    self.say(f"路径不存在：{path}")
                return

    def _refresh_local(self):
        p = self.local_path
        self.tree_l.delete(*self.tree_l.get_children())
        if not os.path.isdir(p):
            self.lbl_lpath.config(text=f"本机：{p}（不可读）")
            return
        try:
            names = os.listdir(p)
        except PermissionError:
            self.say(f"无权限读取 {p}")
            self.lbl_lpath.config(text=f"本机：{p}（无权限）")
            return
        rows = []
        for nm in names:
            if nm.startswith("$"):
                continue
            full = os.path.join(p, nm)
            try:
                isdir = os.path.isdir(full)
                stt = os.stat(full)
                sz = 0 if isdir else stt.st_size
                mt = stt.st_mtime
            except Exception:
                isdir, sz, mt = False, 0, 0
            rows.append((nm, isdir, sz, mt))
        rows.sort(key=lambda r: (not r[1], r[0].lower()))
        for nm, isdir, sz, mt in rows:
            # ★ 四列：名称 / 后缀 / 大小 / 修改时间（用户反馈 #5）
            self.tree_l.insert(
                "", "end", iid=nm,
                values=("▣ " + nm + "/" if isdir else "   " + nm,
                        file_ext(nm, isdir),
                        "" if isdir else P.human_size(sz),
                        P.fmt_mtime(mt)))
        self.lbl_lpath.config(text="本机：" + p)
        self._sync_drive_box()

    def local_enter(self):
        sel = self.tree_l.selection()
        if not sel:
            return
        full = os.path.join(self.local_path, sel[0])
        if os.path.isdir(full):
            self.local_path = full
            self._refresh_local()

    def local_up(self):
        parent = os.path.dirname(self.local_path)
        # ★ Windows 盘根（C:\）再往上会跑出盘符，要拦住
        if parent and os.path.isdir(parent) and \
                len(parent.rstrip("\\/")) >= 2:
            self.local_path = parent
            self._refresh_local()
        elif parent and os.path.isdir(parent):
            self.local_path = parent
            self._refresh_local()

    def local_pick(self):
        d = filedialog.askdirectory(initialdir=self.local_path)
        if d:
            self.local_path = d
            self._refresh_local()

    # ---------------- 传输 ----------------

    def transfer(self, direction):
        with self.cli_lock:
            got_cli = self.cli
        if not got_cli:
            messagebox.showwarning(APP, "还没连接掌机。先点「连接」。")
            return
        if self.busy:
            return

        if direction == "down":
            items = [i for i in self._remote_selected() if not i["dir"]]
            if not items:
                messagebox.showinfo(APP, "请在右边（掌机）选中要复制进电脑的"
                                         "文件。\n（目录暂不支持整目录复制）")
                return
            if len(items) > 1:
                # 多选：逐个来，目标目录取本机当前目录
                dst_dir = self.local_path
            else:
                dst_dir = None
            self._start_transfer("down", items, dst_dir)
        else:
            sel = self.tree_l.selection()
            files = []
            for n in sel:
                full = os.path.join(self.local_path, n)
                if os.path.isfile(full):
                    files.append(full)
            if not files:
                messagebox.showinfo(APP, "请在左边（本机）选中要复制进掌机的"
                                         "文件。\n（目录暂不支持整目录复制）")
                return
            self._start_transfer("up", files, None)

    def _start_transfer(self, direction, items, dst_dir):
        with self.cli_lock:
            cli = self.cli
        self.busy = True
        self.cancel_flag = False
        self._enable(False)
        self.btn_cancel.config(state="normal")
        verb = "复制进电脑" if direction == "down" else "复制进掌机"

        def work():
            n_ok = 0
            total = len(items)
            try:
                for idx, it in enumerate(items, 1):
                    if self.cancel_flag:
                        self.post(self.say, "已取消")
                        break
                    if direction == "down":
                        remote = "/".join(
                            [p for p in (self.remote_path, it["name"]) if p])
                        local = os.path.join(dst_dir or self.local_path,
                                             it["name"])
                        self.post(self.say,
                                  f"[{idx}/{total}] 复制进电脑：{it['name']} "
                                  f"({P.human_size(it['size'])})")
                        cli.download(
                            remote, local,
                            on_progress=self._mk_prog(it["name"]),
                            should_cancel=lambda: self.cancel_flag)
                    else:
                        local = it
                        name = os.path.basename(it)
                        remote = "/".join(
                            [p for p in (self.remote_path, name) if p])
                        self.post(self.say,
                                  f"[{idx}/{total}] 复制进掌机：{name} "
                                  f"（等掌机确认…）")
                        cli.upload(
                            local, remote,
                            on_progress=self._mk_prog(name),
                            should_cancel=lambda: self.cancel_flag)
                    n_ok += 1
                    self.post(self.say, f"  ✓ 完成 {n_ok}/{total}")
            except Nack as e:
                self.post(self.say, f"✗ 掌机拒绝: {e.msg}")
                self.post(messagebox.showwarning, APP, e.msg)
            except InterruptedError:
                self.post(self.say, "已取消")
            except Exception as e:
                self.post(self.say, f"✗ 失败: {type(e).__name__}: {e}")
                self.post(messagebox.showerror, APP, f"{verb}失败：\n{e}")
                self.post(self._lost)
            finally:
                self.post(self._hide_attn)          # ★ 结束后收起提示条
                self.post(self._finish_transfer, direction, n_ok, total)

        threading.Thread(target=work, daemon=True).start()

    def _mk_prog(self, name):
        last = [0.0]

        def cb(got, total):
            now = time.time()
            if now - last[0] < 0.08 and got < total:
                return
            last[0] = now
            pct = (got * 100.0 / total) if total else 0
            self.post(self._set_prog, pct, name, got, total)

        return cb

    def _set_prog(self, pct, name, got, total):
        self.pb["value"] = max(0, min(100, pct))
        self.lbl_prog.config(
            text=f"{name[:14]}  {P.human_size(got)}/{P.human_size(total)}")

    def _finish_transfer(self, direction, n_ok, total):
        self.busy = False
        self._enable(True)
        self.btn_cancel.config(state="disabled")
        self.pb["value"] = 0
        self.lbl_prog.config(text="")
        if n_ok:
            self.say(f"本轮完成 {n_ok}/{total}")
        if direction == "down":
            self._refresh_local()
        else:
            self.remote_reload()
        # 权限可能被用户改过，刷新一下
        self._refresh_info()

    def do_cancel(self):
        self.cancel_flag = True
        self.say("正在取消…（当前数据块传完即停）")

    def do_mkdir(self):
        """
        在**掌机当前目录**下新建文件夹。

        ★ 用户反馈 #4：按钮上要讲清楚这是"在掌机当前目录下新建"，
          所以按钮文案改成了「在掌机当前目录 / 新建文件夹」。
          弹窗标题也带上完整的目标路径，避免用户误以为建在本机。
        """
        with self.cli_lock:
            cli = self.cli
        if not cli:
            messagebox.showwarning(APP, "还没连接掌机")
            return
        if self.busy:
            return

        target_dir = "/" + (self.remote_path or "")
        if target_dir == "/":
            target_dir = "/（卡根）"
        name = simpledialog.askstring(
            APP,
            f"在【掌机】的当前目录下新建文件夹\n\n"
            f"掌机位置：{target_dir}\n\n"
            f"请输入文件夹名称：",
            parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return

        remote = "/".join([p for p in (self.remote_path, name) if p])

        def work():
            try:
                self.post(self.say, f"请求在掌机 {target_dir} 新建 "
                                    f"「{name}」（等掌机确认…）")
                cli.mkdir(remote)
                self.post(self.say, f"✓ 已在掌机新建 {remote}")
                self.post(self.remote_reload)
            except Nack as e:
                self.post(self.say, f"✗ 被拒绝: {e.msg}")
                self.post(messagebox.showwarning, APP, e.msg)
            except Exception as e:
                self.post(self.say, f"✗ 失败: {e}")
                self.post(messagebox.showerror, APP, f"新建目录失败：\n{e}")
            finally:
                self.post(self._hide_attn)

        threading.Thread(target=work, daemon=True).start()

    # ---------------- ★ 删除掌机文件（用户反馈 #5） ----------------

    def do_delete(self):
        """
        删除掌机上选中的文件。

        ★ 用户反馈 #5 要求：
            "需要双端确认，电脑端出一个弹窗确认，掌机端也要有个确认弹窗，
             样式可以和写入确认弹窗一致"

        所以这里有两道独立的确认，缺一不可：
            ① PC 端 messagebox（本函数内）—— 说明要删哪些、在哪
            ② 掌机端模态框（handheld/ui.py 的 Confirm，action="delete"）
               —— 用户在掌机上按 A 才算数

        为什么删目录要单独拦一下：
            删除目录涉及递归，风险面比删单个文件大得多。
            这一版只做文件删除，目录给一个明确的提示，
            不做"看起来能删其实删一半"的半吊子实现。
        """
        with self.cli_lock:
            cli = self.cli
        if not cli:
            messagebox.showwarning(APP, "还没连接掌机")
            return
        if self.busy:
            return

        items = self._remote_selected()
        if not items:
            messagebox.showinfo(APP, "请先在右边（掌机）选中要删除的文件。")
            return

        dirs = [i for i in items if i["dir"]]
        files = [i for i in items if not i["dir"]]

        if dirs and not files:
            # 只选了目录：这一版明确不支持，别让用户以为能删
            names = "、".join(d["name"] for d in dirs[:5])
            messagebox.showinfo(
                APP, f"暂不支持删除掌机上的文件夹。\n\n"
                     f"选中的是：{names}\n\n"
                     f"请先进入该文件夹逐个删除其中的文件。")
            return
        if dirs:
            # 混选：只处理文件，并明确告知目录被跳过了
            skipped = "、".join(d["name"] for d in dirs[:5])
            if not messagebox.askokcancel(
                    APP,
                    f"选中的内容里包含 {len(dirs)} 个文件夹，"
                    f"它们将被【跳过】（暂不支持删目录）：\n"
                    f"{skipped}\n\n"
                    f"继续删除其中的 {len(files)} 个文件吗？",
                    icon="warning"):
                return

        # ---- 第 ① 道确认：PC 端弹窗 ----
        target_dir = "/" + (self.remote_path or "")
        if target_dir == "/":
            target_dir = "/（卡根）"

        if len(files) == 1:
            title = "确认删除掌机上的文件？"
            detail = (f"文件：{files[0]['name']}\n"
                      f"大小：{P.human_size(files[0]['size'])}\n"
                      f"位置：{target_dir}")
        else:
            listed = "\n".join(f"  · {i['name']}" for i in files[:10])
            more = f"\n  …等共 {len(files)} 个" if len(files) > 10 else ""
            title = f"确认删除掌机上的 {len(files)} 个文件？"
            detail = f"文件：\n{listed}{more}\n\n位置：{target_dir}"

        if not messagebox.askokcancel(
                APP,
                f"{title}\n\n{detail}\n\n"
                f"⚠ 删除后无法恢复。\n"
                f"确认后还需要你在【掌机】上再按一次 A 才会真正删除。",
                icon="warning", default="cancel"):
            self.say("已取消删除")
            return

        self.busy = True
        self._enable(False)
        self.btn_cancel.config(state="normal")
        self.lbl_prog.config(text="等待掌机确认…")

        def work():
            n_ok = 0
            total = len(files)
            try:
                for idx, it in enumerate(files, 1):
                    if self.cancel_flag:
                        self.post(self.say, "已取消")
                        break
                    remote = "/".join(
                        [p for p in (self.remote_path, it["name"]) if p])
                    self.post(self.say,
                              f"[{idx}/{total}] 删除 {it['name']}"
                              f"（等掌机确认…）")
                    # ★ 第 ② 道确认在掌机侧：这个方法会阻塞到用户按 A/B
                    cli.delete(remote)
                    n_ok += 1
                    self.post(self.say, f"  ✓ 已删除 {it['name']}")
            except Nack as e:
                self.post(self.say, f"✗ 掌机拒绝: {e.msg}")
                self.post(messagebox.showwarning, APP, e.msg)
            except InterruptedError:
                self.post(self.say, "已取消")
            except Exception as e:
                self.post(self.say, f"✗ 删除失败: {type(e).__name__}: {e}")
                self.post(messagebox.showerror, APP, f"删除失败：\n{e}")
                self.post(self._lost)
            finally:
                self.post(self._hide_attn)
                self.post(self._finish_delete, n_ok, total)

        threading.Thread(target=work, daemon=True).start()

    def _finish_delete(self, n_ok, total):
        self.busy = False
        self._enable(True)
        self.btn_cancel.config(state="disabled")
        self.pb["value"] = 0
        self.lbl_prog.config(text="")
        if n_ok:
            self.say(f"删除完成 {n_ok}/{total}")
            self.remote_reload()
        # 刷新后选中项会清空，删除按钮该回到禁用态
        self._on_remote_select()

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 连接保活 ----------------

    def _heartbeat(self):
        """
        周期性心跳。远小于掌机侧 IDLE_TIMEOUT（10 分钟）。

        ★ 这是"传完一个文件连接就断"的另一半解法：
            掌机侧把空闲超时放宽了，PC 侧再加心跳，
            双保险。用户挂机去干别的，回来还能接着传。

        ★ 2026-10-05：心跳现在顺带把设备信息读回来
            （见 Client.keepalive），所以掌机上按 SELECT 切读写后，
            PC 面板最多 4 秒就会跟着变 —— 用户反馈的
            "再按一下 select 键，win11 这边就没有同步状态" 由此修复。
            4 秒是刻意的：比人按两下 SELECT 的间隔短，又不至于刷太频。
        """
        now = time.time()
        with self.cli_lock:
            cli = self.cli
        if cli and not self.busy and now - self._last_ka > 4:
            self._last_ka = now

            def work():
                if not cli.keepalive():
                    self.post(self.say, "心跳失败：与掌机的连接已断开")
                    self.post(self._lost)
                    return
                # ★ 顺带同步权限（心跳返回的就是最新设备信息）
                info = getattr(cli, "info", None)
                if info:
                    self.post(self._update_perm, info)

            threading.Thread(target=work, daemon=True).start()
        self.root.after(2000, self._heartbeat)

    def _refresh_info(self):
        with self.cli_lock:
            cli = self.cli
        if not cli:
            return

        def work():
            try:
                info = cli.device_info()
                self.post(self._update_perm, info)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _lost(self):
        self.say("与掌机的连接已断开")
        with self.cli_lock:
            old = self.cli
            self.cli = None
        if old:
            try:
                old.close()
            except Exception:
                pass
        self.lbl_state.config(text="已断开（点「扫描」或「连接」重连）")
        self.lbl_perm.config(text="")
        self._hide_attn()


def _base_dir():
    """
    取"程序所在目录"。

    打包成 exe 后 `__file__` 指向临时解压目录（_MEIPASS），
    不能拿它当程序位置。必须用 sys.executable 所在目录。
    这决定了"打开文件对话框的默认位置"等是否合理。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource(name):
    """取随程序分发的资源（打包后在 _MEIPASS 里）。"""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", _base_dir()), name)
    return os.path.join(_base_dir(), name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None)
    args = ap.parse_args()

    root = tk.Tk()

    # 窗口图标（有就设，没有不报错）
    for cand in (_resource("icon.ico"), _resource("assets/icon.png")):
        if os.path.exists(cand):
            try:
                if cand.endswith(".ico"):
                    root.iconbitmap(cand)
                else:
                    _ico = tk.PhotoImage(file=cand)
                    root.iconphoto(True, _ico)
                    root._ico_ref = _ico     # ★ 必须持引用，否则被回收
                break
            except Exception:
                pass

    App(root, host=args.host)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # exe 模式下没有控制台，崩溃会"静默消失"。
        # 把异常弹成对话框，至少让用户看见发生了什么。
        import traceback
        tb = traceback.format_exc()
        try:
            import tkinter.messagebox as mb
            mb.showerror(APP, f"程序异常退出：\n\n{tb[-1200:]}")
        except Exception:
            pass
        try:
            log = os.path.join(_base_dir(), "PocketTransfer-error.log")
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                f.write(tb)
        except Exception:
            pass
        sys.exit(1)
