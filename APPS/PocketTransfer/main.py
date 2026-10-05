#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer 掌机端主程序
=========================

阶段 0  自检 —— 证明「屏幕能画、按键能读、马达能震」
阶段 1  协议骨架 —— TCP 服务端跑起来，PC 能连上并取到设备信息
阶段 2  **局域网文件传输** —— UDP 广播发现 / 文件浏览 / 确认机制 /
        双向传输 / 新目录，全部落到真实的卡上

设计约束（上一版闪退的教训，逐条对应）：
  1. 绝不调用 FBIOPUT_VSCREENINFO / 绝不改显示模式。
     实测驱动会忽略写回的值但 GET 却返回新值，导致 mmap 长度与
     实际 stride 不符 → 写越界 → SIGBUS 闪退。
     现在只读参数、按读到的参数作画。
  2. 绝不用 exec 覆盖原厂 stdout。
     日志一律通过 boot.log() 追加到文件。
  3. 任何异常都经 boot.die() 把中文错误画到屏幕上再退出，
     不再出现"点进去闪退、屏上什么都没".
  4. 退出时必须释放显示，让 dmenu 能正常回来。
     网络线程也要干净退出（daemon 线程 + 显式 stop）。
  5. 不碰 /tmp/.next、不 kill 任何进程、不写 sysfs 配置。
  6. 【阶段 2 新增】写操作必须由用户在掌机上物理确认。
     PC 只能发起请求，落地与否由掌机决定 —— 这是产品核心承诺。

用法（由 launcher.sh 调用）：
    python3 main.py
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ---------------------------------------------------------------------------
# 第一步：日志与诊断层
#
# boot 只用标准库，必须最先 import，这样后面任何失败都能被记录并上屏。
# ---------------------------------------------------------------------------
import boot  # noqa: E402

boot.checkpoint("=" * 56)
boot.checkpoint(f"启动 {sys.argv[0]}")
boot.checkpoint(f"Python {sys.version.split()[0]}  "
                f"pid={os.getpid()}  cwd={os.getcwd()}")
boot.checkpoint(f"BASE_PATH={os.environ.get('BASE_PATH', '(未设置)')}")
boot.checkpoint(f"PYSDL2_DLL_PATH={os.environ.get('PYSDL2_DLL_PATH', '(未设置)')}")

# ---------------------------------------------------------------------------
# 第二步：依赖检查
#
# 逐项 import 并单独给出中文错误。上一版直接 import PIL，一旦缺失就静默闪退。
# ---------------------------------------------------------------------------
try:
    import mmap
except Exception as e:
    boot.die("Python 环境异常（mmap 缺失）", e,
             [f"Python {sys.version.split()[0]}"],
             ascii_title="PYTHON ENV BROKEN",
             ascii_extra=["mmap module missing.",
                          "Firmware python build is incomplete."])
    raise

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as e:
    boot.die("图形库 Pillow 加载失败", e, [
        "本机固件应自带 Pillow 9.0.1。",
        "请确认固件版本，或联系开发者。",
    ],
        ascii_title="PILLOW LOAD FAILED",
        ascii_extra=["Pillow (PIL) is required but missing.",
                     "Stock firmware should ship Pillow 9.0.1.",
                     "Check firmware version."])
    raise

boot.checkpoint("Pillow 已加载")

# ui.py（页面框架）也依赖 PIL，放在这里一起检查。
# 它缺失时同样要能上屏说清楚，而不是静默闪退。
try:
    import ui as uimod
except Exception as e:
    boot.die("界面模块 ui.py 加载失败", e, [
        f"应用目录: {HERE}",
        "请确认 ui.py 与本文件在同一目录。",
    ],
        ascii_title="UI MODULE MISSING",
        ascii_extra=[f"App dir: {HERE}",
                     "ui.py must sit next to main.py."])
    raise

boot.checkpoint("ui 模块已加载")

# 网络层。任何失败都不应让程序起不来 —— 没有网络时至少还能本地浏览。
try:
    import net as netmod
except Exception as e:
    netmod = None
    boot.checkpoint(f"网络模块 net.py 加载失败（将无网络功能）: "
                    f"{type(e).__name__}: {e}")

try:
    import fb as fbmod
except Exception as e:
    boot.die("显示模块 fb.py 加载失败", e, [
        f"应用目录: {HERE}",
        "请确认 fb.py 与本文件在同一目录。",
    ],
        ascii_title="FB MODULE MISSING",
        ascii_extra=[f"App dir: {HERE}",
                     "fb.py must sit next to main.py."])
    raise

boot.checkpoint("fb 模块已加载")

# ---------------------------------------------------------------------------
# 第三步：常量
# ---------------------------------------------------------------------------

APP_NAME = "PocketTransfer"
APP_VER = "1.4"

MOTO_NODE = "/sys/class/power_supply/axp2202-battery/moto"
INPUT_CANDIDATES = ["/dev/input/event1", "/dev/input/event0"]
BOARD_INI = "/mnt/vendor/oem/board.ini"
FONT_CANDIDATES = [
    # 掌机原厂字体（真机上走这个）
    "/mnt/vendor/bin/default.ttf",
    # Linux 常见后备
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    # Windows 中文字体（仅用于 PC 端预览，真机上不存在，不影响运行）
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

TCP_PORT = 48200
UDP_BEACON_PORT = 48211

# 按键映射。来自原厂 Roms/APPS/clock/input.py + 本轮实机实测
# （tools/probe-input.py 枚举 EVIOCGBIT + 监听 30 秒，2026-10-05）。
#
# 实测结果（event1 = ANBERNIC-keys，主力设备）：
#   EV_KEY : 304A 305B 306Y 307X 308L1 309R1 310SELECT 311START 312MENUF
#            314L2 315R2 114V+ 115V-
#   EV_ABS : 2(ABS_Z) 3(ABS_RX) 4(ABS_RY) 5(ABS_RZ)
#            16(ABS_HAT0X) 17(ABS_HAT0Y)
# event0 只有 116(POWER)；event2 是另一套映射，平时不用管。
KEYMAP = {
    304: "A", 305: "B", 306: "Y", 307: "X",
    308: "L1", 309: "R1", 314: "L2", 315: "R2",
    310: "SELECT", 311: "START", 312: "MENUF",
    114: "V+", 115: "V-",
    116: "POWER",
}

# 摇杆/十字方向 —— 走 EV_ABS，不是 EV_KEY！
#
# 原厂 input.py 把 16/17 标成 "DX"/"DY" 并当 KEY 读，那是个巧合：
# 它用 read(24) 读 KEY 结构，ABS 事件的 value 落在同一位置，
# 拨摇杆时 value != 0 就被当成"按下"了。
#
# 正确做法是识别 EV_ABS 事件，并按轴值判方向。
# 实测值域约 ±3700（有符号），中心 0。负值在 evdev 里以补码出现
# （如 4294963596 = -3700），读取时必须做有符号转换。
#
#   ABS_RX (3) = 水平轴：负=左  正=右
#   ABS_RY (4) = 垂直轴：负=上  正=下（若实测相反，只改这里）
#   ABS_HAT0X (16) / ABS_HAT0Y (17) = 十字帽（备用，本次未触发）
ABS_AXES = {
    3: "摇杆X", 4: "摇杆Y",
    2: "摇杆Z", 5: "摇杆RZ",
    16: "十字X", 17: "十字Y",
}

# 死区：绝对值小于它视为"回中"，不产生方向事件
#
# ⚠️ 不同轴的死区必须**分开设**：
#   - 模拟摇杆（ABS_X/Y/Z/RX/RY/RZ）：值域约 ±3800，死区 800 左右
#   - 十字键 HAT（ABS_HAT0X/HAT0Y）：值是 **±1** 的开关量！
#     若共用 800 死区，±1 会被当成"回中"全部丢掉 —— 这就是
#     "十字方向键完全没反应"的根因（2026-10-05 真机验证）。
STICK_DEADZONE = 800
HAT_DEADZONE = 0        # HAT 是数字量，任何非 0 都是有效方向

# ---------------------------------------------------------------------------
# ★ 轴映射表（真机实测标定，2026-10-05）
# ---------------------------------------------------------------------------
#
# 【本机实测事实】(RG35XX Pro / Anbernic H700 / 固件 20260522)
#
#   十字键 (D-Pad)：
#       ABS_HAT0X(16)  -1=左  +1=右      （数字式，值 ±1）
#       ABS_HAT0Y(17)  -1=上  +1=下      （数字式，值 ±1）
#
#   模拟摇杆（★左摇杆=轴2/3，右摇杆=轴4/5，成对分配）：
#       ABS_Z (2)  左摇杆 H   -4096=左  +4096=右
#       ABS_RX(3)  左摇杆 V   -4092=上  +4071=下
#       ABS_RY(4)  右摇杆 H   -4096=左  +3309=右
#       ABS_RZ(5)  右摇杆 V   -3675=上  +4096=下
#
#   ★ 2026-10-05 引导式标定实测（照片读数）：
#       LU code=3 peak=-4092    LD code=3 peak=+4071
#       LL code=2 peak=-3792    LR code=2 peak=+4096
#       RU code=5 peak=-3675    RD code=5 peak=+4096
#       RL code=4 peak=-4096    RR code=4 peak=+3309
#
#   ⚠️ 曾误判为"2/5 垂直、3/4 水平"，实测证明是 "2/3 左杆、4/5 右杆"。
#      规律：轴码 2/3 一对 = 左摇杆，4/5 一对 = 右摇杆；
#            每对里 code 小的 = 水平(H)，code 大的 = 垂直(V)。
#
# 【表格式】code -> (role, sign, deadzone)
#   role : "H" 水平导航 / "V" 垂直导航
#   sign : +1 表示"正值 = 正向（右/下）"，-1 表示取反
#   deadzone : 该轴专属死区（HAT 传 0）
#
# ⚠️ 本机轴语义与 evdev 标准不一致，必须以真机诊断页实测为准。
#    若导航方向不对，只改这张表即可。
AXIS_ROLE = {
    # ---- 十字键：数字量，值 ±1 ----
    16: ("H", +1, HAT_DEADZONE),   # ABS_HAT0X  -1=左 +1=右
    17: ("V", +1, HAT_DEADZONE),   # ABS_HAT0Y  -1=上 +1=下

    # ---- 左摇杆：轴 2/3，值域 ±4096 ----
    2:  ("H", +1, STICK_DEADZONE),  # ABS_Z   左杆水平 -4096=左 +4096=右
    3:  ("V", +1, STICK_DEADZONE),  # ABS_RX  左杆垂直 -4092=上 +4071=下

    # ---- 右摇杆：轴 4/5，值域 ±4096 ----
    4:  ("H", +1, STICK_DEADZONE),  # ABS_RY  右杆水平 -4096=左 +3309=右
    5:  ("V", +1, STICK_DEADZONE),  # ABS_RZ  右杆垂直 -3675=上 +4096=下
}


def axis_direction(axis_code, value):
    """
    把一次 EV_ABS 事件翻译成方向名。返回 None 表示回中/未知。

    【为什么不能按 code 段硬判】
        本机轴码语义与 evdev 标准不一致（见 AXIS_ROLE 注释）。
        必须按"角色 + 符号 + 专属死区"查表。

    【为什么死区要分轴】
        HAT（十字键）值是 ±1，摇杆是 ±3800。
        共用一个大死区会把十字键全部丢掉。
    """
    role = AXIS_ROLE.get(axis_code)
    if role is None:
        return None
    kind, sign_positive, dz = role

    if value > dz:
        s = 1
    elif value < -dz:
        s = -1
    else:
        return None
    s *= sign_positive

    if kind == "H":
        return "右" if s > 0 else "左"
    return "下" if s > 0 else "上"

# 退出键：START / MENUF，与产品设计一致
EXIT_KEYS = (311, 312)

# 配色（深色主题，浅字，对比度足够）
C_BG = (16, 18, 24)
C_BAR = (0, 114, 187)
C_BAR_D = (0, 68, 112)
C_CARD = (26, 30, 40)
C_CARD_LN = (56, 64, 80)
C_FG = (236, 239, 245)
C_DIM = (128, 136, 152)
C_ACC = (0, 214, 255)
C_OK = (60, 210, 130)
C_WARN = (255, 200, 60)
C_ERR = (235, 90, 90)

TARGET_FPS = 30


# ---------------------------------------------------------------------------
# 第四步：硬件访问
# ---------------------------------------------------------------------------


class Input:
    """
    裸读输入设备。/dev/input/event1 是 ANBERNIC-keys（实测主力设备）。

    同时处理两类事件：
      - EV_KEY（type=1）：A/B/Y/X/L1/R1/L2/R2/SELECT/START/MENUF/V+/V-
      - EV_ABS（type=3）：摇杆轴（ABS_RX/ABS_RY 等），**方向键走这里**

    历史 bug：上一版只认 EV_KEY，于是"拨摇杆上下左右没反应"。
    原厂 input.py 也把 ABS 当 KEY 读（巧合能工作），但那是不对的写法。
    """

    def __init__(self, paths=None):
        self.paths = paths or INPUT_CANDIDATES
        self.path = None
        self.f = None
        self.tried = []
        # 轴去重状态：code → 当前方向名（None=回中）
        self._axis_state = {}
        # 上一次上报过的"活跃方向集合"
        self._active = set()
        # 诊断用：code → (最新原始值, 时间戳)
        self.last_raw = {}
        # 诊断用：最近收到的原始事件 (ts, etype, code, value)
        self.recent = []

    def open(self):
        for p in self.paths:
            if not os.path.exists(p):
                self.tried.append(f"{p} 不存在")
                continue
            try:
                # 非阻塞：用 os.open + fcntl 设 O_NONBLOCK，
                # 避免 read 在没事件时把主循环卡住
                import fcntl
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
                self.f = fd
                self.path = p
                try:
                    fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
                except Exception:
                    pass
                return True
            except Exception as e:
                self.tried.append(f"{p} 打开失败: {e}")
        return False

    def poll(self, budget=64):
        """
        读走当前所有事件。

        返回 [(kind, name, value), ...]，kind ∈ {"key", "axis"}：
          - ("key",  "A",  +1/-1)    按键按下/抬起
          - ("axis", "左", +1/-1)    摇杆方向变化（已过死区）

        【去重策略 —— 2026-10-05 修正】
        旧版按 **code** 去重（每个轴各自记一份状态）。这在"一根物理摇杆
        只产出一个轴"时没问题，但本机轴语义不标准，多轴可能同时变化，
        按 code 存状态会互相覆盖，导致方向事件丢或重复。

        现在按 **方向名** 去重：
          - 每轮记录每个轴解析出的方向（code → 方向名/None）
          - 当前"活跃方向集合" = 所有轴当前方向去掉 None
          - 只有活跃集合里**新增**的方向才上报
          - 摇杆保持推着不动时集合不变 → 不重复上报 ✓
        """
        out = []
        if self.f is None:
            return out
        import struct
        # code → 最后一次解析出的方向（None 表示该轴回中）
        state = self._axis_state

        for _ in range(budget):
            try:
                raw = os.read(self.f, 24)
            except BlockingIOError:
                break
            except OSError as e:
                # EAGAIN 是正常的"没数据"，其它按错误处理但不再抛
                if getattr(e, "errno", None) in (11, 35):
                    break
                boot.log(f"读输入出错: {e}")
                break
            if not raw or len(raw) < 24:
                break
            try:
                # ⚠️⚠️ 必须用 qqHHi，不能用 llHHI（本机真机实测踩过）
                #
                # 64 位 Linux 的 struct input_event 布局：
                #     offset  0  tv_sec   signed long (8 字节)
                #     offset  8  tv_usec  signed long (8 字节)
                #     offset 16  type     u16       (2)
                #     offset 18  code     u16       (2)
                #     offset 20  value    s32       (4)
                #                         共 24 字节
                #
                # Python 的 struct **默认不做对齐**（无 padding）：
                #     "llHHI" = 4+4+2+2+4 = 16 字节  ← 短了 8 字节！
                #     "qqHHi" = 8+8+2+2+4 = 24 字节  ← 正确
                # （Python 里 'l' 恒为 4 字节，与 C 的 long 无关）
                #
                # 用 llHHI 解 24 字节数据的后果：整体错位 8 字节，
                # type/code/value 全是垃圾 —— 按键和轴两个分支都不匹配，
                # 事件被**静默全部丢弃**，表现为"方向键完全没反应"。
                # 这正是 2026-10-05 用户实测发现的根因。
                _s, _us, etype, code, value = struct.unpack("qqHHi", raw)
            except Exception:
                continue

            # ---- EV_KEY：按键 ----
            if etype == 1 and value != 0:
                out.append(("key", KEYMAP.get(code, f"key{code}"),
                            (code, 1 if value == 1 else -1)))
                # 诊断用：原样记一条（带真实 value）
                self.recent.append((time.time(), etype, code, value))

            # ---- EV_ABS：摇杆轴 ----
            elif etype == 3:
                # 用 qqHHi 解出的 value 已经是 s32 有符号，无需补码转换。
                # （旧版用 llHHI 读成无符号，才需要 (1<<32) 修正）
                v = value
                state[code] = axis_direction(code, v)
                # 额外记一份"原始值 + 时间"，给诊断页显示实时轴状态。
                # 诊断页需要看到**未过死区**的原始值才能判断轴是否接对。
                self.last_raw[code] = (v, time.time())
                self.recent.append((time.time(), etype, code, value))
            else:
                # 其它类型（MSC/REL/未知）也记录，便于发现"漏网"事件
                if etype != 0:
                    self.recent.append((time.time(), etype, code, value))
        # 清理 3 秒前的原始值，避免诊断页显示陈旧数据
        now = time.time()
        for c in list(self.last_raw.keys()):
            if now - self.last_raw[c][1] > 3.0:
                del self.last_raw[c]
        # recent 只留最近 120 条
        if len(self.recent) > 120:
            del self.recent[:len(self.recent) - 120]

        # 本轮结束，算出"当前活跃方向"，与上一次比较后上报新增项
        active = {d for d in state.values() if d}
        prev = self._active
        self._active = active
        for d in ("上", "下", "左", "右"):
            if d in active and d not in prev:
                out.append(("axis", d, (0, +1)))
        return out

    def close(self):
        if self.f is not None:
            try:
                os.close(self.f)
            except Exception:
                pass
            self.f = None


class Motor:
    """震动马达：写 1 开 / 0 关。"""

    def __init__(self, node=MOTO_NODE):
        self.node = node
        self.available = os.path.exists(node)
        self.last_error = None
        if not self.available:
            self.last_error = f"{node} 不存在"

    def on(self):
        return self._write("1")

    def off(self):
        return self._write("0")

    def _write(self, v):
        try:
            with open(self.node, "w") as f:
                f.write(v)
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    def pulse(self, ms=180):
        """同步震动（会阻塞 ms 毫秒）。用于按键反馈，可接受。"""
        if not self.available:
            return False
        if not self.on():
            return False
        try:
            time.sleep(max(0.01, ms / 1000.0))
        finally:
            self.off()
        return True


def board_model():
    try:
        with open(BOARD_INI, "r", encoding="utf-8", errors="replace") as f:
            first = f.read().splitlines()[0].strip()
            return first or "unknown"
    except Exception:
        return "unknown"


def local_ip():
    """取本机在局域网里的 IP。用 UDP connect 技巧，不会真的发包。"""
    import socket
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def base_path():
    """卡根由启动脚本注入。没拿到就退回探测。"""
    p = os.environ.get("BASE_PATH", "").strip()
    if p and os.path.isdir(p):
        return p
    for cand in ("/mnt/mmc", "/mnt/sdcard"):
        if os.path.isdir(cand):
            return cand
    return ""


def other_slots(cur):
    """仅用于展示"另一张卡在不在"。不参与任何决策。"""
    out = []
    for slot, p in (("TF1", "/mnt/mmc"), ("TF2", "/mnt/sdcard")):
        if p == cur:
            continue
        try:
            mounted = os.path.ismount(p)
        except Exception:
            mounted = False
        if mounted:
            out.append(f"{slot} 已挂载")
    return out


# ---------------------------------------------------------------------------
# 第五步：UI 绘制
# ---------------------------------------------------------------------------


class Fonts:
    """
    字体集合。字号按【基准宽 640】定义，再乘缩放系数 k 适配实际画布。

    为什么需要缩放：
        同一台设备上 framebuffer 可能是 640x480，也可能是 1280x1024
        （驱动 modes 里两种都有）。若字号固定，1280 宽下字会小得看不清。
        统一按 k = 实际宽 / 640 缩放，两种画布下观感一致。
    """

    def __init__(self, scale=1.0, path=None):
        self.source = None
        # 传给 _load 的缩放，同时也是外部查询用的字段
        self.scale = max(0.5, min(4.0, float(scale)))
        if path:
            # 指定了字体就用它（用于 PC 预览固定风格）
            FONT_CANDIDATES.insert(0, path)
        self.title = self._load(28)
        self.head = self._load(19)
        self.body = self._load(15)
        self.small = self._load(13)

    def _load(self, base_size):
        size = max(8, int(round(base_size * self.scale)))
        for p in FONT_CANDIDATES:
            if not os.path.exists(p):
                continue
            try:
                f = ImageFont.truetype(p, size)
                self.source = p
                return f
            except Exception as e:
                boot.log(f"字体 {p} 加载失败: {e}")
        boot.log("所有字体候选都失败，退回 Pillow 内置点阵字")
        return ImageFont.load_default()


def ui_scale(f):
    """UI 缩放系数：以 640 宽为基准。"""
    return max(0.5, min(4.0, f.width / 640.0))


# ---------------------------------------------------------------------------
# 第六步：设备信息（给网络层与 UI 用）
# ---------------------------------------------------------------------------

def build_device_info():
    """
    组装 DEVICE_INFO 响应。字段名与 PC 端契约一致。

    这是一个工厂函数而非静态 dict —— 因为 IP / 可写状态 / 统计
    都是会变的，每次请求都要现算。
    """
    st = _RUNTIME["st"]
    return {
        "app": "PocketTransfer",
        "ver": APP_VER,
        "model": st.get("model", "unknown"),
        "fw": st.get("fw", "unknown"),
        "ip": st.get("ip", "0.0.0.0"),
        "port": TCP_PORT,
        "udp_port": UDP_BEACON_PORT,
        "base": st.get("base", ""),
        "screen": f"{st.get('screen_w', 0)}x{st.get('screen_h', 0)}",
        "backend": st.get("backend", "?"),
        "sdl": st.get("sdl_ver", "?"),
        "host": "ANBERNIC",
        # 平台事实（PC 端可用来判断兼容性）
        "os": "Ubuntu 22.04 LTS",
        "kernel": "4.9.170",
        "python": sys.version.split()[0],
    }


# 网络线程与主线程共享的运行时状态。
# 用模块级 dict 而不是全局变量，便于在函数间传递且不污染命名空间。
_RUNTIME = {"st": {}, "net": None}


def fw_version():
    """读厂商固件版本号。读不到就返回 unknown（不影响功能）。"""
    try:
        with open("/mnt/vendor/oem/version.ini", "r",
                  encoding="utf-8", errors="replace") as f:
            v = f.read().strip()
            return v or "unknown"
    except Exception:
        return "unknown"


def sdl_version_string():
    """取运行时的 SDL2 版本。用运行时自报而不是 strings —— 本机有两份 libSDL2。"""
    try:
        import sdl2
        v = sdl2.SDL_version()
        sdl2.SDL_GetVersion(v)
        return f"{v.major}.{v.minor}.{v.patch}"
    except Exception:
        return "?"


def parse_events(raw_events, inp):
    """
    把 Input.poll() 的原始结果转成 ui.Evt 列表。

    axis 事件里额外带上**原始轴码**（从 inp.last_raw 反查），
    因为诊断页要显示"哪个 code 变成了哪个方向"。

    另外把**原始事件流**（inp.recent 里自上次之后的新增部分）也一并
    转成 Evt 交给诊断页 —— 诊断页需要看到未经任何过滤的真相，
    包括 value 的真实数值、以及"根本没被识别"的事件。
    """
    out = []
    raw_axes = getattr(inp, "_axis_state", {}) if inp is not None else {}

    for item in raw_events:
        try:
            kind, name, payload = item
        except Exception:
            continue
        if kind == "axis":
            # 找出当前贡献该方向的轴码与真实值（可能不止一个，取第一个）
            code, val = 0, +1
            lr = getattr(inp, "last_raw", {}) if inp is not None else {}
            for c, d in raw_axes.items():
                if d == name and isinstance(c, int):
                    code = c
                    break
            if code in lr:
                val = lr[code][0]
            out.append(uimod.Evt("axis", name, code, val))
        else:
            code, val = payload
            out.append(uimod.Evt("key", name, code, val))

    # ---- 原始事件流（供诊断页展示，不作为操作指令）----
    if inp is not None:
        recent = getattr(inp, "recent", [])
        prev_n = getattr(parse_events, "_seen", 0)
        new_items = recent[prev_n:] if prev_n <= len(recent) else recent[-20:]
        parse_events._seen = len(recent)
        for (ts, etype, code, value) in new_items:
            e = uimod.Evt("raw", f"raw{etype}", code, value)
            e.stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            e.etype = etype
            out.append(e)

    return out


# ---------------------------------------------------------------------------
# 第七步：主流程
# ---------------------------------------------------------------------------


def _dmenu_running():
    """
    判断原厂菜单 dmenu.bin 是否正在运行。

    为什么需要这个判断（重要）：
        dmenu.bin 运行时会持有 /dev/disp 图层。此时 SDL 创建全屏窗口
        会失败，报：
            mali-fbdev: Can't create EGL window surface
        这是**正常且期望**的行为 —— 菜单正在用屏幕，我们不该去抢。

        如果这时还弹错误屏、还 die，就会**打断用户的菜单**，
        看起来像"程序坏了"，其实只是用户在菜单里点错了时机。

    实测：dmenu.bin 运行时 fb0 是 1280x1024 16bpp（菜单模式），
    dmenu 退出后变 640x480 32bpp（应用模式）。
    但**不要**用 fb 模式判断 —— 那不如直接看进程可靠。
    """
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as fh:
                    cmd = fh.read().decode("utf-8", "replace")
            except Exception:
                continue
            if "dmenu.bin" in cmd or "muos1.bin" in cmd or "muos2.bin" in cmd:
                return True, cmd.strip("\x00").replace("\x00", " ")
    except Exception:
        pass
    return False, ""


def bring_up_display():
    """
    打开显示。

    【为什么必须优先 SDL2 —— 本平台的核心结论】
        这台机器（RG35XX Pro / Allwinner H700）的显示子系统是
        DispEngine 多图层合成器（/dev/disp），不是单一 framebuffer。

        原厂菜单 dmenu.bin 通过 /dev/disp 建了一个**硬件图层**，
        像素数据放在 /dev/ion 显存里（**不在 /dev/fb0 内**），
        退出时不做 DISP_BLANK 清理 → 图层残留，持续输出"加载中"。

        /dev/fb0 是 disp 的**底层图层** → 裸写 fb0 会被残留图层
        **完全盖住**，画得再对用户也看不见。

        原厂 5 个 Python 应用全部走 SDL2 的 FULLSCREEN_DESKTOP，
        SDL 会 open /dev/disp 创建自己的顶层图层，所以能正常显示。

    → 因此：**优先 SDL2。fb0 只作兜底**（万一 pysdl2 环境被破坏）。

    两条路都失败才 die —— 且这时 die 自己也会尽力用 SDL 画错误屏。
    """
    # ---- 第一优先：SDL2 ----
    sdl_err = None
    try:
        import sdl_display
        f = sdl_display.SDLDisplay()
        f.open()
        boot.checkpoint(f"显示就绪(SDL2): {f.info()}")
        for n in f.probe_note:
            boot.checkpoint(f"  sdl探测 | {n}")
        return f
    except Exception as e:
        sdl_err = e
        boot.checkpoint(f"SDL2 显示不可用，回退 framebuffer: "
                        f"{type(e).__name__}: {e}")

    # ---- 特殊情形：dmenu 正在跑 ----
    # 此时 SDL 建窗口会因 EGL 失败而返回 NULL（菜单占着 disp 图层）。
    # 这是**正常的**：用户还在菜单里，屏幕就应该显示菜单。
    # 绝不能弹错误屏或 die —— 那会打断菜单，看起来像程序坏了。
    busy, who = _dmenu_running()
    if busy:
        boot.checkpoint(f"检测到原厂菜单正在运行（{who}），"
                        f"屏幕被它占用。这不是错误 —— 安静退出。")
        return None

    # ---- 兜底：裸 framebuffer ----
    f = fbmod.Framebuffer()
    try:
        f.open()
    except Exception as e:
        boot.die("显示初始化失败（SDL2 与 fb 均不可用）", e, [
            "SDL2 失败原因见上一条日志。",
            "未能打开 /dev/fb0。",
            "请确认机型为 RG35XX Pro 且系统正常。",
            f"SDL2 错误: {sdl_err}",
        ],
            ascii_title="DISPLAY INIT FAILED",
            ascii_extra=["Both SDL2 and /dev/fb0 unusable.",
                         f"SDL2: {type(sdl_err).__name__ if sdl_err else '-'}",
                         "Check this is an RG35XX Pro."])
        raise
    boot.checkpoint(f"显示就绪(fb 兜底): {f.info()}")
    for n in f.probe_note:
        boot.checkpoint(f"  fb探测 | {n}")

    # 合理性二次校验（fb.py 已经验过，这里再兜一层，
    # 因为 stride 不对会直接写越界闪退）
    need = f.line_px * (f.bpp // 8) * f.virtual_h
    if need <= 0 or need > 64 * 1024 * 1024:
        boot.die("显示参数异常", None, [
            f"{f.width}x{f.height} bpp={f.bpp} line_px={f.line_px}",
            f"虚拟高 {f.virtual_h}，算出映射 {need} 字节。",
        ],
            ascii_title="BAD SCREEN PARAMETERS",
            ascii_extra=[f"{f.width}x{f.height} bpp={f.bpp} "
                         f"line_px={f.line_px}",
                         f"virtual_h={f.virtual_h} -> {need} bytes.",
                         "Refusing to draw (would write out of bounds)."])
    return f


# 兼容旧名字
bring_up_framebuffer = bring_up_display


def sanity_write_test(f):
    """
    写回读自检：往显示表面里写几个像素再原样读回来比对。

    为什么需要：
        裸 framebuffer 路线下，写越界会触发 SIGBUS，进程被内核瞬间杀死，
        屏幕上不留任何信息。这一步用"小块写入 + 立即读回"提前暴露问题 ——
        即便真的越界，也发生在启动阶段、日志已落盘。

    SDL 路线下没有 mmap（像素先到 PIL 缓冲再送纹理），
    自检退化为「画一张测试图 → blit → 确认没抛异常」，
    同样能证明显示通道是通的。

    只动我们自己要画的地方，不碰任何系统状态。
    """
    # ---- SDL 路线：没有 mm，改成端到端上屏测试 ----
    if not hasattr(f, "mm") or f.mm is None:
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (f.width, f.height), (0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rectangle([0, 0, f.width - 1, f.height - 1],
                        outline=(0, 214, 255), width=2)
            d.line([0, 0, f.width - 1, f.height - 1], fill=(255, 96, 0), width=1)
            ok = f.blit(img, force=True)
            boot.checkpoint(f"上屏自检(SDL): {f.width}x{f.height} "
                            f"blit 返回 {ok}，通道正常")
            return True
        except Exception as e:
            boot.checkpoint(f"上屏自检(SDL)异常（不阻断启动）: "
                            f"{type(e).__name__}: {e}")
            return False

    # ---- fb 路线：写→读回比对 ----
    try:
        mv = memoryview(f.mm)
        limit = len(mv)
        wo = f.bpp // 8
        tested = 0
        bad = 0
        pts = [(0, 0),
               (f.width - 1, 0),
               (f.width // 2, f.height // 2),
               (0, f.height - 1),
               (f.width - 1, f.height - 1),
               (f.width // 2, f.height - 1)]
        for (x, y) in pts:
            off = (y * f.line_px + x) * wo
            if off + wo > limit:
                bad += 1
                continue
            before = bytes(mv[off:off + wo])
            f.px(x, y, 0xA5A5A5 if f.bpp == 32 else 0xA5A5A5)
            after = bytes(mv[off:off + wo])
            mv[off:off + wo] = before
            tested += 1
            if after == before and before != b"\xa5" * wo:
                bad += 1
        boot.checkpoint(f"写回自检: 采样 {tested} 点，异常 {bad} 点，"
                        f"映射 {limit} 字节，行宽 {f.line_px}px，"
                        f"最后一行偏移 {(f.height - 1) * f.line_px * wo}")
        return bad == 0
    except Exception as e:
        boot.checkpoint(f"写回自检异常（不阻断启动）: "
                        f"{type(e).__name__}: {e}")
        return False


def _start_network(st):
    """
    启动后台网络线程。失败不影响界面（只影响联网功能）。
    """
    if netmod is None:
        st["net_note"] = "网络模块未加载"
        return None
    try:
        nt = netmod.NetThread(
            device_info_fn=build_device_info,
            base_path_fn=lambda: st.get("base") or "",
            log=boot.log,
        )
        nt.start()
        # 把当前状态同步给网络线程
        nt.send_command({"act": "root", "path": st.get("base") or ""})
        # ★★ 默认「可写」（2026-10-05 用户实测反馈 #1）
        #
        #   上一版默认只读，用户必须先按 SELECT 解锁才能写入。
        #   用户实测后明确要求去掉这道门槛：往里写文件不该还要先解锁。
        #
        #   注意：这**不是**把安全机制拆掉了 —— 真正的把关仍在原地：
        #     · 路径必须落在卡根内（safe_join + realpath 校验）
        #     · 每次写入前，掌机屏幕仍会弹确认框，用户按 A 才落地
        #   只是取消了"还要额外按一次解锁"这个多余步骤。
        #
        #   SELECT 键仍然有效：按一下变只读（RO），再按变可写（RW），
        #   用户想临时锁住时随时可用。
        nt.send_command({"act": "writable", "on": True})
        st["net_note"] = "已启动"
        boot.checkpoint(f"网络线程已启动：TCP:{TCP_PORT} "
                        f"UDP:{UDP_BEACON_PORT} 根={st.get('base')}")
        return nt
    except Exception as e:
        st["net_note"] = f"启动失败: {e}"
        boot.checkpoint(f"网络线程启动失败: {type(e).__name__}: {e}")
        return None


def main():
    # ---- 显示（优先 SDL2，失败回退 fb0）----
    f = bring_up_display()

    # f 为 None 表示"原厂菜单正在占用屏幕"——
    # 这是正常情形（用户还在菜单里），安静退出即可，不要报错。
    if f is None:
        boot.checkpoint("菜单占用屏幕，本次不启动 UI，已安静退出")
        return 0

    # 上屏自检：提前暴露"写越界/写不进"这类会直接 SIGBUS 的问题，
    # 让故障发生在有日志的启动阶段，而不是屏幕上不留痕迹的闪退。
    sanity_write_test(f)

    # ---- 硬件 ----
    inp = Input()
    input_ok = inp.open()
    if not input_ok:
        boot.log(f"输入设备全部不可用: {inp.tried}")
    motor = Motor()
    boot.checkpoint(f"输入 {inp.path or '不可用'} / 马达 "
                    f"{'可用' if motor.available else '不可用'}")

    fonts = Fonts(scale=ui_scale(f))
    boot.checkpoint(f"字体来源: {fonts.source or '(内置)'} "
                    f"缩放 {fonts.scale:.2f}（画布 {f.width}x{f.height}）")

    # ---- 全局状态 ----
    st = {
        "model": board_model(),
        "fw": fw_version(),
        "base": base_path(),
        "ip": local_ip(),
        "port": TCP_PORT,
        "udp_port": UDP_BEACON_PORT,
        "input": inp.path or "-",
        "motor": motor.available,
        "font": os.path.basename(fonts.source) if fonts.source else "builtin",
        "screen_w": f.width,
        "screen_h": f.height,
        "backend": ("SDL2" if f.__class__.__name__ == "SDLDisplay" else "fb0"),
        "sdl_ver": sdl_version_string(),
        # ★ 默认可写（用户反馈 #1：不再要求先按 SELECT 解锁）
        "writable": True,
        "mode": uimod.MODE_BROWSE,
        "sent": 0,
        "recv": 0,
        "fps": 0.0,
        "peer": None,
    }
    st["other"] = other_slots(st["base"])
    if not st["base"]:
        boot.checkpoint("警告: 未拿到卡根，功能将受限")

    _RUNTIME["st"] = st
    boot.checkpoint(f"设备信息 {st}")

    # ---- 网络 ----
    net = _start_network(st)
    _RUNTIME["net"] = net

    # ---- 界面 ----
    app = uimod.App(f, fonts, st, net=net)
    # 把输入设备挂到 app 上：诊断页需要读"实时原始轴值"，
    # 这是排查轴映射错误的关键信息。
    app.inp = inp
    app.toast("已启动，等待 PC 连接")
    if not st["base"]:
        app.toast("警告: 未拿到卡根路径")

    # 开场震动：让用户立刻确认"程序真的跑起来了"
    motor.pulse(220)

    # ---- 主循环 ----
    frames = 0
    fps_frames = 0
    last_fps_t = time.time()
    frame_interval = 1.0 / TARGET_FPS
    next_frame = time.time()
    idle_s = min(frame_interval, 0.005)

    boot.checkpoint("进入主循环")

    try:
        while not app.quit:
            # ---- 网络事件（每帧都排空，保证 UI 及时反映）----
            app.pump()

            # ---- 节流 ----
            now = time.time()
            if now < next_frame:
                time.sleep(min(idle_s, next_frame - now))
                continue
            next_frame = now + frame_interval

            # ---- 输入 ----
            #
            # 关键修复：旧版在这里 `continue`，把这一帧的事件全丢了。
            # 现在改成"处理完再决定要不要跳过渲染"，
            # 保证任何按键/方向都不会因为恰好落在节流窗口内而丢失。
            events = parse_events(inp.poll(), inp)
            handled_no_nav = False
            for evt in events:
                # 全局震动反馈
                if evt.kind == "key":
                    motor.pulse(90)
                elif evt.kind == "axis":
                    motor.pulse(40)
                if app.handle(evt):
                    handled_no_nav = True

            # ---- 绘制 ----
            #
            # 只在"内容变了"时重绘。UI 是静态的，满速重绘纯属浪费电。
            # 但每秒至少强制画一帧更新 FPS 数字。
            if app.dirty or (now - last_fps_t >= 1.0):
                img = app.render()
                try:
                    f.blit(img)
                except Exception as e:
                    boot.log(f"blit 失败: {e}")
                    boot.die("画面刷新失败", e, ["显示驱动可能异常"],
                             ascii_title="DRAW FAILED",
                             ascii_extra=["Framebuffer write error.",
                                          "Display driver may be unstable."])
                    break
                app.dirty = False

            # SDL 事件泵：不抽干事件队列，SDL 会认为应用无响应。
            # 本应用自己的输入走 evdev，这里只做最小必要的 pump。
            try:
                if hasattr(f, "pump_events"):
                    f.pump_events()
            except Exception:
                pass

            frames += 1
            fps_frames += 1
            if now - last_fps_t >= 1.0:
                st["fps"] = fps_frames / max(1e-6, now - last_fps_t)
                fps_frames = 0
                last_fps_t = now
                app.dirty = True

            # ---- 同步可写状态给网络线程 ----
            if net:
                net.set_writable(st.get("writable", False))

        boot.checkpoint(f"主循环结束，共 {frames} 帧")

    finally:
        # ---- 收尾：无论怎么退出都要释放显示与网络 ----
        boot.checkpoint("开始收尾")
        try:
            if net:
                net.stop()
                # ★ 这里的 join 曾经炸过 'Event' object is not callable，
                #   根因是 NetThread 用 self._stop 遮蔽了 Thread._stop()。
                #   已在 net.py 里改名 _stop_evt 修掉（详见那段注释）。
                #   这里再兜一层：join 失败也绝不阻断退出流程。
                try:
                    net.join(timeout=2.0)
                except Exception as je:
                    boot.checkpoint(f"net.join 异常（不阻断退出）: {je}")
                boot.checkpoint("网络线程已停止")
        except Exception as e:
            boot.checkpoint(f"停止网络线程异常: {e}")
        try:
            inp.close()
        except Exception:
            pass
        try:
            f.fill(0x000000)
        except Exception:
            pass
        try:
            f.close()
        except Exception:
            pass
        boot.checkpoint("显示已释放，控制权交还 dmenu")

    motor.pulse(360)  # 退出提示
    return 0


if __name__ == "__main__":
    try:
        rc = main()
        boot.checkpoint(f"正常退出 rc={rc}")
        sys.exit(rc)
    except KeyboardInterrupt:
        boot.checkpoint("被用户中断")
        sys.exit(130)
    except SystemExit:
        raise
    except BaseException as e:
        # 最后的兜底：连 main 都没兜住的错误，也要画到屏幕上
        boot.die("程序异常终止", e,
                 ascii_title="UNEXPECTED CRASH",
                 ascii_extra=["See boot.log for full traceback."])
        sys.exit(1)

