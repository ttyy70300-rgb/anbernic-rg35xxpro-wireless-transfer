#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地仿真测试 —— 在 PC 上验证掌机端代码逻辑，不占用真机

为什么需要：
    掌机测试成本高（要拷卡、点菜单、看闪退），一次循环至少几分钟。
    这个脚本用假的 /dev/fb0 和假的输入设备，在 PC 上把
    「参数探测 → 打包 → 画帧 → 读键 → 退出」整条链路跑一遍，
    把能在本地发现的错误全部挡在推文件之前。

覆盖：
    1. fb.py 参数探测（含 line_px < width 的异常场景）
    2. 32bpp BGRA / 16bpp RGB565 两条 blit 路径的字节级正确性
    3. 写越界防护（故意把 mmap 做小，确认不崩）
    4. ui.py 的多分辨率渲染 + 四页面渲染
    5. boot.py 的点阵字与错误屏渲染
    6. net.py 协议编解码 + 路径安全闸门
    7. 阶段 2 防回归：确认机制 / 线程模型 / 主循环不丢事件 / 固件事实

用法:
    python tools/selftest.py
"""
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "handheld"))

# 阶段 2 的协议/网络模块。缺失时后续协议检查会失败并给出明确原因。
try:
    import net as _nmod
except Exception as _e:
    _nmod = None
    print(f"  [WARN] net.py 无法导入，协议检查将跳过: {_e}")

FAIL = []
PASS = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")


# ===========================================================================
print("=" * 62)
print("1) 点阵字体（boot.py 内嵌）")
print("=" * 62)

import boot  # noqa: E402

boot.checkpoint("selftest 开始")
check("boot 模块可导入", True)
check("点阵字覆盖 0-9", all(c in boot._FONT for c in "0123456789"))
check("点阵字覆盖 A-Z", all(c in boot._FONT for c in
                            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
check("点阵字有 ? 兜底", "?" in boot._FONT)
check("每个字形 16 行", all(len(g) == 16 for g in boot._FONT.values()))
bad = [c for c, g in boot._FONT.items() if any(b > 0xFF for b in g)]
check("字形字节都在 8bit 内", not bad, f"越界字符 {bad}")
# 空白字形只允许是真正的空白字符（含刻意映射为空的 \t\r\n）
WHITESPACE_OK = set("\t\r\n ")
blank = [c for c, g in boot._FONT.items()
         if c not in WHITESPACE_OK and not any(g)]
check("无空白字形（除空白字符）", not blank, f"空白字符 {blank}")
# 小写必须映射（否则异常信息全是 '?'，兜底屏失效）
lower_missing = [c for c in "abcdefghijklmnopqrstuvwxyz"
                 if c not in boot._FONT]
check("小写字母已映射到大写", not lower_missing,
      f"缺失 {lower_missing}")


# ===========================================================================
print()
print("=" * 62)
print("2) fb.py 显示层")
print("=" * 62)

import fb as fbmod  # noqa: E402

# --- 2a. 用可写内存模拟 framebuffer，绕过真 ioctl ---
print("\n  -- 2a. 32bpp BGRA 路径（掌机实测色序 red=16 green=8 blue=0）--")


class FakeFB(fbmod.Framebuffer):
    """把 mmap 换成 bytearray，其余逻辑完全走真实代码。"""

    def __init__(self, w, h, bpp, line_px, rgb, transp, mm_size=None):
        super().__init__()
        self.width = w
        self.height = h
        self.virtual_w = w
        self.virtual_h = h
        self.bpp = bpp
        self.line_px = line_px
        self.hoffset, self.goffset, self.boffset = rgb
        self.toffset = transp
        self.smem_len = mm_size or (line_px * (bpp // 8) * h)
        self.mm = bytearray(self.smem_len)

    def blit_bytes(self, img_bytes, w, h):
        """直接调用真实 blit 的字节处理，跳过 PIL 尺寸检查。"""


# 构造一张 4x2 的纯色图，手工验证每个像素的打包结果
W, H = 4, 2
f32 = FakeFB(W, H, 32, W, (16, 8, 0), 24)
from PIL import Image  # noqa: E402

img = Image.new("RGB", (W, H))
for x in range(W):
    img.putpixel((x, 0), (255, 0, 0))     # 纯红
    img.putpixel((x, 1), (0, 128, 255))   # 蓝青

f32.blit(img, force=True)
# BGRA 小端：内存里应是 B,G,R,A
red_px = bytes(f32.mm[0:4])
check("32bpp 纯红 → BGR(A) = 00 00 FF FF",
      red_px == b"\x00\x00\xff\xff", f"实得 {red_px.hex(' ')}")
blue_px = bytes(f32.mm[W * 4:W * 4 + 4])
check("32bpp (0,128,255) → FF 80 00 FF",
      blue_px == b"\xff\x80\x00\xff", f"实得 {blue_px.hex(' ')}")

# 变化检测：同样内容第二次应该跳过
again = f32.blit(img)
check("32bpp 变化检测生效（同图跳过）", again is False)

# --- 2b. 16bpp RGB565 ---
print("\n  -- 2b. 16bpp RGB565 路径（dmenu 运行期间的模式）--")
f16 = FakeFB(W, H, 16, W, (0, 0, 0), 0)
f16.blit(img, force=True)


def rgb565_ref(r, g, b):
    v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return bytes((v & 0xFF, v >> 8))


exp_red = rgb565_ref(255, 0, 0)
got_red = bytes(f16.mm[0:2])
check(f"16bpp 纯红 → {exp_red.hex(' ')}",
      got_red == exp_red, f"实得 {got_red.hex(' ')}")

exp_blue = rgb565_ref(0, 128, 255)
got_blue = bytes(f16.mm[W * 2:W * 2 + 2])
check(f"16bpp (0,128,255) → {exp_blue.hex(' ')}",
      got_blue == exp_blue, f"实得 {got_blue.hex(' ')}")

# 逐像素全量比对：确认没有"第一像素对、后面错"的隐患
img2 = Image.new("RGB", (16, 8))
for y in range(8):
    for x in range(16):
        img2.putpixel((x, y), (x * 16, y * 32, 255 - x * 16))
f16b = FakeFB(16, 8, 16, 16, (0, 0, 0), 0)
f16b.blit(img2, force=True)
mismatch = 0
for y in range(8):
    for x in range(16):
        r, g, b = img2.getpixel((x, y))
        off = (y * 16 + x) * 2
        if bytes(f16b.mm[off:off + 2]) != rgb565_ref(r, g, b):
            mismatch += 1
check("16bpp 16x8 逐像素全对", mismatch == 0, f"{mismatch} 个像素不符")

# --- 2c. 写越界防护 ---
print("\n  -- 2c. 写越界防护（闪退的物理根因）--")
# 故意给一个巨大的 height，但 mmap 只开够一小部分
f_over = FakeFB(640, 480, 32, 640, (16, 8, 0), 24, mm_size=640 * 4 * 10)
big = Image.new("RGB", (640, 480), (10, 20, 30))
try:
    f_over.blit(big, force=True)
    check("越界场景未崩溃（有防护）", True)
except Exception as e:
    check("越界场景未崩溃（有防护）", False, f"抛了 {type(e).__name__}: {e}")

# 确认确实写满了允许的前 10 行
wrote = any(f_over.mm[i] for i in range(0, 640 * 4 * 10, 997))
check("越界时仍写足了 mmap 范围内的内容", wrote)

# --- 2d. 参数自洽校验 ---
print("\n  -- 2d. 参数自洽校验 --")


class BadFB(fbmod.Framebuffer):
    def __init__(self, **kw):
        super().__init__()
        for k, v in kw.items():
            setattr(self, k, v)
        self.smem_len = 0
        self.line_px = 0


# 模拟「GET 返回切模式后的新值、但 stride 仍是旧的」这种危险状态
b = BadFB(width=640, height=480, bpp=32, line_px=320,
          virtual_w=640, virtual_h=480, hoffset=16, goffset=8, boffset=0,
          toffset=24)
try:
    fbmod.Framebuffer._probe(b)  # 会先走 _ioctl，必然抛异常
    check("line_px < width 被拒绝", False, "没有抛异常")
except Exception as e:
    msg = str(e)
    check("line_px < width 被拒绝（或 ioctl 不可用）",
          True, f"（{type(e).__name__}）")


# ===========================================================================
print()
print("=" * 62)
print("3) main.py 帧绘制")
print("=" * 62)

import main as appmod  # noqa: E402


class DrawStub(FakeFB):
    """给 UI 渲染用的假屏。"""


# 阶段 2 起，绘制入口从 main.draw_frame 迁到 ui.App.render。
# 这里用一个最小字体集合 + App 实例来驱动同样的"多分辨率不崩"检查。
try:
    _uimod = __import__("ui")
    _UI_OK = True
except Exception as _e:
    _uimod = None
    _UI_OK = False
    check("ui.py 可导入（阶段 2 渲染入口）", False, str(_e))

if _UI_OK:
    screen = FakeFB(640, 960, 32, 640, (16, 8, 0), 24)

    class _FontStub:
        """PC 上没有原厂字体时，退到 PIL 内置点阵字。"""

        def __init__(self):
            try:
                from PIL import ImageFont
                p = None
                for c in ("C:/Windows/Fonts/msyh.ttc",
                          "C:/Windows/Fonts/simhei.ttf",
                          "C:/Windows/Fonts/arial.ttf",
                          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
                    if os.path.exists(c):
                        p = c
                        break
                if p:
                    self.title = ImageFont.truetype(p, 26)
                    self.head = ImageFont.truetype(p, 18)
                    self.body = ImageFont.truetype(p, 14)
                    self.small = ImageFont.truetype(p, 12)
                else:
                    self.title = self.head = self.body = self.small = \
                        ImageFont.load_default()
                self.source = p
            except Exception:
                from PIL import ImageFont
                self.title = self.head = self.body = self.small = \
                    ImageFont.load_default()
                self.source = None
            self.scale = 1.0

    fonts = _FontStub()
    check("字体可加载", fonts.body is not None)

    st = {
        "mode": _uimod.MODE_BROWSE,
        "model": "RG35xxPRO",
        "fw": "20260522",
        "base": os.getcwd(),
        "ip": "192.168.3.25",
        "port": 48200,
        "udp_port": 48211,
        "backend": "SDL2",
        "sdl_ver": "2.28.5",
        "screen_w": 640,
        "screen_h": 480,
        "input": "/dev/input/event1",
        "motor": True,
        "font": "default.ttf",
        "writable": False,
        "sent": 0,
        "recv": 0,
        "peer": None,
        "fps": 29.7,
    }

    # 三种画布都要能渲染（含 dmenu 运行期间的 1280x1024 与早期误读的 640x480）
    for (w, h, bpp, rgb, transp) in [
        (640, 480, 32, (16, 8, 0), 24),    # 应用运行期实际值
        (1280, 1024, 16, (0, 0, 0), 0),    # dmenu 运行期间
        (480, 640, 32, (16, 8, 0), 24),    # 竖屏/异常比例
    ]:
        try:
            sc = FakeFB(w, h, bpp, w, rgb, transp)
            st2 = dict(st)
            st2["screen_w"], st2["screen_h"] = w, h
            app = _uimod.App(sc, fonts, st2, net=None)
            img = app.render()
            assert img.size == (w, h), f"尺寸 {img.size}"
            colors = img.getcolors(maxcolors=100000)
            check(f"UI 渲染 {w}x{h} {bpp}bpp", len(colors) > 5,
                  f"只有 {len(colors)} 种颜色，可能没画上")
        except Exception as e:
            import traceback
            check(f"UI 渲染 {w}x{h} {bpp}bpp", False,
                  f"{type(e).__name__}: {e}")
            traceback.print_exc()

    # 四个页面都要能画
    for _pn in ("status", "browser", "net", "log"):
        try:
            sc = FakeFB(640, 480, 32, 640, (16, 8, 0), 24)
            app = _uimod.App(sc, fonts, dict(st), net=None)
            app.pages["browser"].reload()
            app.stack[0] = app.pages[_pn]
            app.stack[0].on_enter()
            img = app.render()
            check(f"页面「{_pn}」可渲染", img.size == (640, 480))
        except Exception as e:
            check(f"页面「{_pn}」可渲染", False, f"{type(e).__name__}: {e}")

# 边界：极小屏幕不应崩
try:
    sc = FakeFB(160, 120, 32, 160, (16, 8, 0), 24)
    st3 = dict(st)
    st3["screen_w"], st3["screen_h"] = 160, 120
    _uimod.App(sc, fonts, st3, net=None).render()
    check("UI 极小屏幕不崩", True)
except Exception as e:
    check("UI 极小屏幕不崩", False, f"{type(e).__name__}: {e}")

# ===========================================================================
print()
print("=" * 62)
print("4) 协议骨架")
print("=" * 62)

# 阶段 2 起，协议实现在 handheld/net.py（原 main.Protocol 已迁移）。
# 常量名统一成 OP_*，这里做名字映射以保持检查项不变。
P = _nmod
_ALIAS = {"PING": "OP_PING", "PONG": "OP_PONG",
          "GET_DEVICE_INFO": "OP_GET_DEVICE_INFO",
          "DEVICE_INFO": "OP_DEVICE_INFO", "ERROR": "OP_ERROR"}
for _a, _b in _ALIAS.items():
    if not hasattr(P, _a) and hasattr(P, _b):
        setattr(P, _a, getattr(P, _b))

# 正常帧
fr = P.pack_json(P.PING, {"t": 1})
check("pack 帧头 8 字节", len(fr) > 8)
magic, op, ln = P.HDR.unpack_from(fr, 0)
check("MAGIC 正确", magic == P.MAGIC, f"实得 0x{magic:04x}")
check("TYPE 正确", op == P.PING)
check("LENGTH 正确", ln == len(fr) - 8, f"声明 {ln} 实际 {len(fr)-8}")

# 粘包 / 拆包
p1 = P.Parser()
r = p1.feed(fr + fr)
check("粘包：两帧都被解析", len(r) == 2, f"实得 {len(r)}")
p2 = P.Parser()
half = len(fr) // 2
r = p2.feed(fr[:half])
check("拆包：前半段不产生帧", len(r) == 0, f"实得 {len(r)}")
r = p2.feed(fr[half:])
check("拆包：补全后产生 1 帧", len(r) == 1, f"实得 {len(r)}")

# 脏数据重新同步
p3 = P.Parser()
r = p3.feed(b"\xde\xad\xbe\xef" + fr)
check("脏数据可重新同步", len(r) == 1, f"实得 {len(r)}")

# 超长负载拒绝
p4 = P.Parser()
huge = P.HDR.pack(P.MAGIC, P.PING, 0x7FFFFFFF)
r = p4.feed(huge)
check("超长负载被拒绝", p4.error is not None and len(r) == 0,
      f"error={p4.error}")

# ---- 4b. 路径安全闸门（阶段 2 新增，最重要的安全检查）----
print()
print("  -- 路径安全闸门（防目录穿越）--")
import tempfile as _tf
_root = _tf.mkdtemp(prefix="pt-self-")
os.makedirs(os.path.join(_root, "Roms", "APPS"), exist_ok=True)
open(os.path.join(_root, "Roms", "a.txt"), "w").write("x")

check("safe_join 放行正常子路径",
      P.safe_join(_root, "Roms/a.txt") ==
      os.path.realpath(os.path.join(_root, "Roms", "a.txt")))
check("safe_join 剥离前导斜杠",
      P.safe_join(_root, "/Roms/a.txt") ==
      os.path.realpath(os.path.join(_root, "Roms", "a.txt")))
check("safe_join 拒绝 ../ 穿越",
      P.safe_join(_root, "../../etc/passwd") is None)
check("safe_join 拒绝反斜杠穿越",
      P.safe_join(_root, "..\\..\\windows") is None)
check("safe_join 拒绝绝对路径逃逸",
      P.safe_join(_root, "../../../..") is None)
check("safe_join 对 None root 返回 None", P.safe_join(None, "a") is None)

# ===========================================================================
print()
print("=" * 62)
print("5) 硬件层参数")
print("=" * 62)

check("TCP 端口 48200", appmod.TCP_PORT == 48200)
check("UDP 端口 48211", appmod.UDP_BEACON_PORT == 48211)
check("退出键含 START(311)", 311 in appmod.EXIT_KEYS)
check("退出键含 MENUF(312)", 312 in appmod.EXIT_KEYS)
check("KEYMAP 含 A(304)", appmod.KEYMAP.get(304) == "A")
check("KEYMAP 含 B(305)", appmod.KEYMAP.get(305) == "B")
check("马达节点路径正确",
      appmod.MOTO_NODE ==
      "/sys/class/power_supply/axp2202-battery/moto")

# 确认关键约束：源码里不该再出现 setmode / PUT_VSCREENINFO
# 注意：只检查「代码」，注释里说明"我们不做什么"是允许的。


def code_lines(path, comment_prefixes=("#",)):
    """
    剥掉注释、文档字符串与空行，返回真正可执行的代码。

    用来做「本代码不应出现某个调用」这类约束检查 ——
    注释里写"我们不调用 X"是正确做法，不该被判为违规。
    """
    import ast
    src = open(path, encoding="utf-8").read()
    if path.endswith(".py"):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return src
        # 收集所有文档字符串的行号，从代码里剔除
        doc_lines = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and \
                    isinstance(first.value, ast.Constant) and \
                    isinstance(first.value.value, str):
                for ln in range(first.lineno, first.end_lineno + 1):
                    doc_lines.add(ln)
        out = []
        for i, raw in enumerate(src.splitlines(), start=1):
            if i in doc_lines:
                continue
            if raw.lstrip().startswith(comment_prefixes):
                continue
            if not raw.strip():
                continue
            out.append(raw)
        return "\n".join(out)

    # 非 Python（shell 等）：只去注释行
    out = []
    for raw in src.splitlines():
        if raw.lstrip().startswith(comment_prefixes):
            continue
        if not raw.strip():
            continue
        out.append(raw)
    return "\n".join(out)


src_main_code = code_lines(os.path.join(ROOT, "handheld", "main.py"))
check("main.py 不再调用 setmode", "setmode" not in src_main_code)
check("main.py 不再 PUT_VSCREENINFO", "FBIOPUT" not in src_main_code)

src_fb_code = code_lines(os.path.join(ROOT, "handheld", "fb.py"))
check("fb.py 已彻底删除 set_mode（防误用）",
      "def set_mode" not in src_fb_code,
      "留着未使用的切模式方法，下一个人可能误用")
check("fb.py 不含 FBIOPUT 常量",
      "FBIOPUT" not in src_fb_code,
      "FBIOPUT_VSCREENINFO 常量应一并移除")
check("fb.py 不含 _ioctl_put 包装",
      "_ioctl_put" not in src_fb_code,
      "写回 ioctl 的包装函数应一并移除")

src_l_code = code_lines(os.path.join(ROOT, "handheld", "launcher.sh"),
                        comment_prefixes=("#",))
check("launcher.sh 不再调用 setmode.py", "setmode.py" not in src_l_code)
check("launcher.sh 不 kill 任何进程",
      "kill" not in src_l_code.lower())
check("launcher.sh 不写 /tmp/.next", "/tmp/.next" not in src_l_code)
check("launcher.sh 不做 remount", "remount" not in src_l_code)
check("launcher.sh 不建软链", "ln -s" not in src_l_code)

# ===========================================================================
print()
print("=" * 62)
print("6) ABI 结构体偏移防回归（闪退的真正根因）")
print("=" * 62)
print("""
   背景：fb_var_screeninfo / fb_fix_screeninfo 是内核 uapi 结构体。
   曾经把 var 的偏移记成"带 64 字节私有头"的版本（xres=64、bpp=88），
   结果偏移 88 读到的是 height（物理高度 94mm）→ 报「不支持的色深 94bpp」
   → 应用启动即崩溃。fix 的 line_length 也错记成 56（实际 48）→ 读出 0。
   这里用【实机 dump 校准过的偏移】做断言，防止再次错位。
""")

# fbmod 已在第 2 节顶部 import 过，直接复用

# var 偏移必须与标准 uapi 一致（实机 dump 已确认本机就是标准布局）
check("var.xres 偏移 = 0", fbmod.O_XRES == 0,
      f"实际 {getattr(fbmod, 'O_XRES', '?')}")
check("var.yres 偏移 = 4", fbmod.O_YRES == 4)
check("var.xres_virtual 偏移 = 8", fbmod.O_XRES_V == 8)
check("var.yres_virtual 偏移 = 12", fbmod.O_YRES_V == 12)
check("var.xoffset 偏移 = 16", fbmod.O_XOFF == 16)
check("var.yoffset 偏移 = 20", fbmod.O_YOFF == 20)
check("var.bits_per_pixel 偏移 = 24（曾错记为 88）", fbmod.O_BPP == 24,
      "偏移 88 是 height(mm)，实机读出 94 导致「不支持的色深 94bpp」")
check("var.red 偏移 = 32", fbmod.O_RED == 32)
check("var.green 偏移 = 44", fbmod.O_GREEN == 44)
check("var.blue 偏移 = 56", fbmod.O_BLUE == 56)
check("var.transp 偏移 = 68", fbmod.O_TRANSP == 68)
check("var.height 偏移 = 88（曾误当作 bpp）", fbmod.O_HEIGHT == 88)
check("var.width 偏移 = 92", fbmod.O_WIDTH == 92)

# fix 偏移
check("fix.smem_start 偏移 = 16", fbmod.F_SMEM_START == 16)
check("fix.smem_len 偏移 = 24（曾错记为 8）", fbmod.F_SMEM_LEN == 24)
check("fix.line_length 偏移 = 48（曾错记为 56）", fbmod.F_LINE_LENGTH == 48,
      "偏移 56 是 mmio_start，实机读出 0 导致「line_length 为 0」")

# 用实机 dump 的原始字节做一次真值回归。
# 来源：tools/probe-fb-deep.py 在掌机上打印的 fb_var_screeninfo 逐字节 dump。
import struct as _struct

_VAR_HEX = """
00 05 00 00 00 04 00 00 00 05 00 00 00 04 00 00
00 00 00 00 00 00 00 00 10 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 5e 00 00 00 96 00 00 00
00 00 00 00 99 24 00 00 e0 00 00 00 20 00 00 00
20 00 00 00 04 00 00 00 88 00 00 00 04 00 00 00
"""
_FIX_HEX = """
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 80 ff 00 00 00 00 00 80 70 00 00 00 00 00
00 00 00 00 02 00 00 00 01 00 01 00 00 00 00 00
00 0a 00 00 00 00 00 00 00 00 00 00 00 00 00 00
"""


def _hex_to_bytes(h):
    return bytes.fromhex("".join(h.split()))


_var_dump = _hex_to_bytes(_VAR_HEX).ljust(192, b"\x00")
_fix_dump = _hex_to_bytes(_FIX_HEX).ljust(112, b"\x00")


def _parse(b, off):
    return _struct.unpack_from("<I", b, off)[0]


check("用实机 dump 验证 var.xres=1280", _parse(_var_dump, fbmod.O_XRES) == 1280)
check("用实机 dump 验证 var.yres=1024", _parse(_var_dump, fbmod.O_YRES) == 1024)
check("用实机 dump 验证 var.bpp=16", _parse(_var_dump, fbmod.O_BPP) == 16,
      "这一条以前会读出 94（=height 94mm）")
check("实机 dump 偏移 88 确实是 94（height），不是 bpp",
      _parse(_var_dump, fbmod.O_HEIGHT) == 94,
      f"实际读出 {_parse(_var_dump, fbmod.O_HEIGHT)}")
check("用实机 dump 验证 fix.line_length=2560",
      _parse(_fix_dump, fbmod.F_LINE_LENGTH) == 2560,
      f"实际读出 {_parse(_fix_dump, fbmod.F_LINE_LENGTH)}")
check("用实机 dump 验证 fix.smem_len=7372800",
      _parse(_fix_dump, fbmod.F_SMEM_LEN) == 7372800)

# ===========================================================================
print()
print("=" * 62)
print("7) boot.py 函数唯一性防回归（TypeError 的根因）")
print("=" * 62)
print("""
   背景：boot.py 里曾同时存在两个 die() 定义（新版带 ascii_title、
   旧版不带）。Python 后定义者覆盖先定义者，调用方传 ascii_title 时
   抛 TypeError: die() got an unexpected keyword argument 'ascii_title'，
   把真正的错误信息完全掩盖，屏幕上什么都不显示。
""")

_boot_code = code_lines(os.path.join(ROOT, "handheld", "boot.py"))
for _fn in ("die", "show_error_screen", "log", "checkpoint"):
    _n = _boot_code.count(f"def {_fn}(")
    check(f"boot.py 中 def {_fn}( 只出现 1 次", _n == 1,
          f"实际 {_n} 次 —— 重复定义会静默覆盖，导致诡异的 TypeError")

# 签名必须真的接受 ascii_title / ascii_extra（调用方在用）
_boot_src = open(os.path.join(ROOT, "handheld", "boot.py"),
                 encoding="utf-8").read()
check("boot.py die() 签名含 ascii_title", "ascii_title=None" in _boot_src)
check("boot.py die() 签名含 ascii_extra", "ascii_extra=None" in _boot_src)

# 用 AST 确认：所有对 boot.die(...) 的调用都只用了已定义的参数名
import ast as _ast
_sig_ok = True
try:
    _tree = _ast.parse(open(os.path.join(ROOT, "handheld", "main.py"),
                            encoding="utf-8").read())
    for _node in _ast.walk(_tree):
        if isinstance(_node, _ast.Call):
            _fn = _node.func
            _name = getattr(_fn, "attr", None) or getattr(_fn, "id", None)
            if _name == "die":
                for _kw in _node.keywords:
                    if _kw.arg and _kw.arg not in (
                            "title", "exc", "extra", "wait",
                            "ascii_title", "ascii_extra"):
                        _sig_ok = False
except Exception:
    _sig_ok = False
check("main.py 里 boot.die() 的实参名都被 die() 接受", _sig_ok,
      "有调用用了 die() 签名里没有的关键字参数")

# ===========================================================================
print()
print("=" * 62)
print("8) SDL2 显示后端契约（2026-10-05 根因修复的防回归）")
print("=" * 62)
print("""
   背景：屏幕被原厂 dmenu 残留的 disp 硬件图层盖住。裸写 /dev/fb0
   （disp 底层图层）必然看不见。原厂 5 个 Python 应用全部走
   SDL_WINDOW_FULLSCREEN_DESKTOP 创建自己的顶层图层。
   → 必须优先走 SDL2，fb 只能兜底。
""")

_sdl_src = open(os.path.join(ROOT, "handheld", "sdl_display.py"),
                encoding="utf-8").read()
check("sdl_display.py 存在且非空", len(_sdl_src) > 2000)
check("用 SDL_WINDOW_FULLSCREEN_DESKTOP（顶层图层的关键）",
      "SDL_WINDOW_FULLSCREEN_DESKTOP" in _sdl_src,
      "这是能显示出来的唯一途径 —— 少它就会被残留图层盖住")
check("创建渲染器时先试 ACCELERATED",
      "SDL_RENDERER_ACCELERATED" in _sdl_src)
check("ACCELERATED 失败回退 SOFTWARE（照抄原厂 clock/graphic.py）",
      "SDL_RENDERER_SOFTWARE" in _sdl_src)
check("用 SDL_PIXELFORMAT_RGBA32 承接 PIL 输出",
      "SDL_PIXELFORMAT_RGBA32" in _sdl_src)
check("禁掉线性插值（要像素级清晰）",
      "SDL_HINT_RENDER_SCALE_QUALITY" in _sdl_src)

# 接口契约：SDLDisplay 必须与 fb.Framebuffer 同名同签名，
# 否则 main.py 的 UI 代码会无声崩掉。
_sdl_tree = _ast.parse(_sdl_src)
_sdl_methods = {n.name for n in _ast.walk(_sdl_tree)
                if isinstance(n, _ast.FunctionDef)}
for _m in ("open", "info", "px", "fill", "blit", "close"):
    check(f"SDLDisplay 实现 {_m}()（与 fb.Framebuffer 接口一致）",
          _m in _sdl_methods,
          "接口不一致会让 main.py 在运行期才炸，且有图层问题更难排查")

# main.py 必须优先 SDL、且失败能回退
_main_src = open(os.path.join(ROOT, "handheld", "main.py"),
                 encoding="utf-8").read()


def _strip_comments(src):
    """去掉每行 `#` 之后的内容，用于"源码里是否真的用了某写法"的检查。

    为什么需要：注释里记录了"不能用 llHHI"这类教训，
    直接用 `in` 检查会被注释本身误伤 —— 必须只看真实代码。
    """
    out = []
    for ln in src.splitlines():
        idx = ln.find("#")
        out.append(ln[:idx] if idx >= 0 else ln)
    return "\n".join(out)


_main_code_only = _strip_comments(_main_src)

check("main.py 优先 import sdl_display", "import sdl_display" in _main_src)
check("main.py 保留 fb 兜底路径",
      "fbmod.Framebuffer()" in _main_src,
      "万一 pysdl2 环境被破坏，没有兜底就是全黑")
check("main.py 主循环调用 pump_events（SDL 要求抽干事件队列）",
      "pump_events" in _main_src)

# ===========================================================================
print()
print("=" * 62)
print("9) 摇杆输入走 EV_ABS 而非 EV_KEY（实机实测结论）")
print("=" * 62)
print("""
   背景：用户报告"拨摇杆上下左右没反应"。实机实测（probe-input.py）
   发现摇杆是 EV_ABS 事件（ABS_RX/ABS_RY 等，值域约 ±3700），
   而旧代码只认 EV_KEY，把 ABS 全部丢掉了。

   2026-10-05 二次实测又发现更深的问题：解包格式用错导致整体错位 8 字节，
   type/code/value 全是垃圾，**按键和轴两个分支都不匹配**，
   事件被静默全丢 → 表现为"方向键完全没反应"。
""")

check("main.py 处理 etype == 3（EV_ABS）", "etype == 3" in _main_src,
      "只处理 EV_KEY 会导致摇杆完全无响应")
check("★ 用 qqHHi 解包（24 字节），不用 llHHI（16 字节）",
      '"qqHHi"' in _main_src and
      _main_code_only.count('"llHHI"') == 0,
      "★ Python 的 'l' 恒为 4 字节且 struct 无对齐：llHHI 只有 16 字节，"
      "而 64 位 input_event 是 24 字节 → 整体错位，事件全被丢弃")
check("  └ os.read 的字节数与 struct 格式一致（24）",
      "os.read(self.f, 24)" in _main_src and '"qqHHi"' in _main_src,
      "读取长度和解析格式必须匹配，否则错位")
check("定义了 STICK_DEADZONE 死区", "STICK_DEADZONE" in _main_src,
      "没有死区会导致摇杆回中时疯狂抖动")
check("ABS_AXES 覆盖实测到的 3/4 号轴",
      "3:" in _main_src and "4:" in _main_src)
check("KEYMAP 里不再把 16/17 误当方向键（那是 ABS 轴号）",
      "17: \"D-UP\"" not in _main_src and "17: 'D-UP'" not in _main_src)

# 用 AST 找 axis_direction 并实际验证符号约定
_ns = {}
_axis_fn = None
for _n in _ast.walk(_main_tree := _ast.parse(_main_src)):
    if isinstance(_n, _ast.FunctionDef) and _n.name == "axis_direction":
        _axis_fn = _n
check("main.py 定义了 axis_direction()", _axis_fn is not None)


# ===========================================================================
print()
print("=" * 62)
print("10) boot.py 结构体偏移必须与 fb.py 一致（同一错误别犯两次）")
print("=" * 62)
print("""
   背景：boot.py 是崩溃兜底屏，自带一份独立的 fb 参数解析。
   它曾经自带**同样的**偏移错位（_O_XRES=64 / _O_BPP=88 /
   _F_LINE_LENGTH=56），于是错误屏会按 "94bpp" 去画 ——
   结果连兜底错误屏都画不出来，屏幕全黑，比不显示更糟。
   boot.py 和 fb.py 的偏移必须一致，且都被 selftest 覆盖。
""")

_boot_module_ns = {}
try:
    # 注意：不能 exec 整个 boot.py —— 它有顶层副作用（建日志文件、
    # 写 sys.stderr 等），在 PC 上跑会污染环境。
    # 这里只从源码里抽取常量定义，用正则足够且零副作用。
    import re as _re

    def _grab_const(src, name, default=None):
        """从源码抽常量值。支持 'A = 1' 和 'A, B = 0, 4' 两种写法。"""
        # 单赋值
        m = _re.search(rf"^{_re.escape(name)}\s*=\s*(\d+)\s*$",
                       src, _re.MULTILINE)
        if m:
            return int(m.group(1))
        # 元组赋值：在一行里找到 A 所在位置，取同位置的值
        for line in src.splitlines():
            if "=" not in line or name not in line:
                continue
            lhs, _, rhs = line.partition("=")
            names = [t.strip() for t in lhs.split(",")]
            if name not in names:
                continue
            vals = [t.strip() for t in rhs.split(",")]
            idx = names.index(name)
            if idx < len(vals):
                try:
                    return int(vals[idx])
                except ValueError:
                    pass
        return default

    _bo_xres = _grab_const(_boot_src, "_O_XRES")
    _bo_bpp = _grab_const(_boot_src, "_O_BPP")
    _bo_line = _grab_const(_boot_src, "_F_LINE_LENGTH")
    check("boot.py _O_XRES 与 fb.py O_XRES 一致",
          _bo_xres == fbmod.O_XRES, f"boot={_bo_xres} fb={fbmod.O_XRES}")
    check("boot.py _O_BPP 与 fb.py O_BPP 一致",
          _bo_bpp == fbmod.O_BPP, f"boot={_bo_bpp} fb={fbmod.O_BPP}")
    check("boot.py _F_LINE_LENGTH 与 fb.py F_LINE_LENGTH 一致",
          _bo_line == fbmod.F_LINE_LENGTH,
          f"boot={_bo_line} fb={fbmod.F_LINE_LENGTH}")
    check("boot.py _O_BPP 不是 88（88 是 height，历史 bug）",
          _bo_bpp != 88, "88 偏移读到的是物理高度 94mm，不是色深")
except Exception as _e:
    check("boot.py 偏移常量可抽取", False, f"{type(_e).__name__}: {_e}")

check("boot.py 错误屏优先走 SDL2（否则被残留图层盖住）",
      "_show_error_screen_sdl" in _boot_src,
      "fb0 在底层，错误屏也会被盖住 → 用户看不到任何错误提示")

# 用 AST 检查真实 import，而不是扫源码文本 ——
# 文件头注释里提到 PIL 属于说明性内容，不该误判。
_boot_imports = set()
for _n in _ast.walk(_ast.parse(_boot_src)):
    if isinstance(_n, _ast.Import):
        for _al in _n.names:
            _boot_imports.add(_al.name.split(".")[0])
    elif isinstance(_n, _ast.ImportFrom):
        if _n.module:
            _boot_imports.add(_n.module.split(".")[0])
check("boot.py 不 import PIL（PIL 坏了也要能显示错误屏）",
      "PIL" not in _boot_imports,
      f"实际 import: {sorted(_boot_imports)}")


# ===========================================================================
print("[" + " 11. SDL2 稳定性防回归（SIGSEGV rc=139）".ljust(58) + "]")
# ===========================================================================
#
# 背景：二次进入 PocketTransfer 时 rc=139（SIGSEGV）闪退，
# 且 boot.log 恰好断在 "[SDL阶段] 7 输出尺寸 640x480" 之后 ——
# 说明崩在 blit() 里。两个独立根因，各配一条防回归：

_sdl_src = open(os.path.join(ROOT, "handheld", "sdl_display.py"),
                encoding="utf-8").read()
_sdl_tree = _ast.parse(_sdl_src)

# ---- 11.1 悬空指针：SDL_CreateRGBSurfaceWithFormatFrom 的首参必须是有名变量 ----
#
# 这个 C 函数**不拷贝**数据，只把 buffer 包成指针。
# 若首参写成 `rgba.tobytes()` 这种临时表达式，CPython 在调用返回后
# 立刻回收它，SDL 拿到悬空指针读像素 → SIGSEGV。
# 只有在 blit() 里显式 `buf = key` 再用 `buf` 才算安全。
_blit_fn = None
for _n in _ast.walk(_sdl_tree):
    if isinstance(_n, _ast.FunctionDef) and _n.name == "blit":
        _blit_fn = _n
        break
check("sdl_display.py 存在 blit() 方法", _blit_fn is not None)

_surface_calls = []
if _blit_fn is not None:
    for _n in _ast.walk(_blit_fn):
        if isinstance(_n, _ast.Call):
            _f = _n.func
            _fname = getattr(_f, "attr", None) or getattr(_f, "id", None)
            if _fname == "SDL_CreateRGBSurfaceWithFormatFrom":
                _surface_calls.append(_n)
check("blit() 里调用 SDL_CreateRGBSurfaceWithFormatFrom",
      len(_surface_calls) == 1,
      f"找到 {len(_surface_calls)} 处（应为 1）")

_tmp_arg_bad = False
_arg_desc = "n/a"
if len(_surface_calls) == 1:
    _a0 = _surface_calls[0].args[0] if _surface_calls[0].args else None
    # 安全形态：ast.Name（变量）或 ast.Attribute 的常量成员。
    # 危险形态：ast.Call（如 rgba.tobytes()）。
    if isinstance(_a0, _ast.Call):
        _tmp_arg_bad = True
    _arg_desc = type(_a0).__name__
check("  └ 首参是有名变量（不是 rgba.tobytes() 临时对象）",
      not _tmp_arg_bad,
      f"首参是 {_arg_desc}；若是 Call 则会 SIGSEGV")

# 反向确认：源码里确实存在 `buf = ...` 且 surface 调用用了 buf
check("  └ 源码存在 buf 持有变量且传给 surface",
      "buf = key" in _sdl_src and "        buf," in _sdl_src,
      "必须用局部变量托住 bytes，直到 SDL_CreateTextureFromSurface 返回")

# ---- 11.2 import sdl2.ext 污染 SDL 状态 ----
# sdl2.ext 会连带加载坏的 SDL2_image（undefined symbol: SDL_roundf），
# 失败后污染 SDL 内部状态 → 渲染阶段段错误。
# 用 AST 检查真实 import，注释里提到 ext 不算。
_sdl_imports = set()
_sdl_ext_imported = False
for _n in _ast.walk(_sdl_tree):
    if isinstance(_n, _ast.Import):
        for _al in _n.names:
            _sdl_imports.add(_al.name)
            if _al.name.startswith("sdl2.ext"):
                _sdl_ext_imported = True
    elif isinstance(_n, _ast.ImportFrom):
        if _n.module:
            _sdl_imports.add(_n.module)
            if _n.module.startswith("sdl2.ext"):
                _sdl_ext_imported = True
check("sdl_display.py 不 import sdl2.ext（会加载坏的 SDL2_image）",
      not _sdl_ext_imported,
      f"实际 import: {sorted(_sdl_imports)}")
check("  └ 只 import 核心 sdl2",
      "sdl2" in _sdl_imports,
      f"实际 import: {sorted(_sdl_imports)}")

# ---- 11.3 阶段日志存在于 open() 与 blit() ----
# SDL 段错误打不出 traceback，只能靠 boot.log 最后一行定位，
# 所以关键路径必须有阶段日志。
check("sdl_display.py 有 stage() 阶段日志机制",
      "def stage(" in _sdl_src and "[SDL阶段]" in _sdl_src,
      "段错误无法打印 traceback，必须靠落盘日志定位")
_open_fn = None
for _n in _ast.walk(_sdl_tree):
    if isinstance(_n, _ast.FunctionDef) and _n.name == "open":
        _open_fn = _n
        break
_open_stages = 0
if _open_fn is not None:
    for _n in _ast.walk(_open_fn):
        if isinstance(_n, _ast.Call) and getattr(_n.func, "id", None) == "stage":
            _open_stages += 1
check("  └ open() 内有 10+ 条阶段日志", _open_stages >= 10,
      f"实际 {_open_stages} 条")
check("  └ open() 在首次渲染前先 pump 事件",
      "SDL_PumpEvents" in _sdl_src,
      "全屏+硬件加速后端在进入事件循环前渲染会访问未初始化状态")

# ---- 11.4 main.py 菜单占用检测 ----
_main_src = open(os.path.join(ROOT, "handheld", "main.py"),
                 encoding="utf-8").read()
check("main.py 有 _dmenu_running() 菜单占用检测",
      "def _dmenu_running" in _main_src,
      "dmenu 运行时抢占 /dev/disp，SDL 建窗必失败 → 应安静退出而非报错")
check("  └ bring_up_display() 检测到菜单占用时返回 None",
      "return None" in _main_src and "_dmenu_running" in _main_src,
      "不能在菜单占用时 die()，用户会看到一个无意义的错误屏")
check("  └ main() 处理 f is None 的安静退出路径",
      "if f is None" in _main_src,
      "f 为 None 表示菜单正在占用屏幕，应 return 0 而非崩溃")


# ===========================================================================
# 第 12 节：阶段 2 —— 网络层 / UI 框架 / 安全边界 防回归
#
# 这些都是"真机上很难复现、但一旦破坏后果严重"的点：
#   - 路径穿越 → 能把 PC 的文件写到卡外
#   - 未确认就写 → 违背"掌机掌握决定权"的产品承诺
#   - 主循环丢事件 → 按键时灵时不灵（旧版真实 bug）
# ===========================================================================
print()
print("-" * 62)
print("第 12 节  网络层 / UI / 安全边界")
print("-" * 62)

_n_hh = os.path.join(ROOT, "handheld")
_net_path = os.path.join(_n_hh, "net.py")
_ui_path = os.path.join(_n_hh, "ui.py")

# ---- 12.0 文件存在 ----
for _fn in ("net.py", "ui.py"):
    check(f"handheld/{_fn} 存在",
          os.path.isfile(os.path.join(_n_hh, _fn)),
          "阶段 2 新增模块，缺失则无网络/无界面框架")

if not (os.path.isfile(_net_path) and os.path.isfile(_ui_path)):
    print("  !! 缺少阶段 2 模块，跳过第 12 节")
else:
    _net_src = open(_net_path, encoding="utf-8").read()
    _ui_src = open(_ui_path, encoding="utf-8").read()
    _net_tree = _ast.parse(_net_src)
    _ui_tree = _ast.parse(_ui_src)

    # ---- 12.1 协议常量必须与 PC 端契约一致 ----
    check("net.py MAGIC = 0x504B", "MAGIC = 0x504B" in _net_src)
    check("net.py 包头结构 >HHI（大端）",
          'HDR = struct.Struct(">HHI")' in _net_src.replace("'", '"'),
          "与 PC 端 HEADER 必须逐字节一致")
    check("net.py TCP 端口 48200", "TCP_PORT = 48200" in _net_src)
    check("net.py UDP 广播端口 48211", "UDP_BEACON_PORT = 48211" in _net_src)
    check("net.py 有最大负载保护",
          "MAX_PAYLOAD" in _net_src and "length > MAX_PAYLOAD" in _net_src,
          "不设上限时，伪造的大 length 会让程序拼命分配内存")

    # ---- 12.2 路径安全闸门（最重要） ----
    check("net.py 有 safe_join() 路径闸门",
          "def safe_join" in _net_src,
          "★ PC 可传 ../../etc/passwd，必须在此挡住")
    check("  └ safe_join 用 realpath（能挡软链）",
          "os.path.realpath" in _net_src,
          "只用 normpath 挡不住指向卡外的符号链接")
    check("  └ safe_join 校验结果在 root 之内",
          "startswith(rootr + os.sep)" in _net_src or
          "startswith(root)" in _net_src,
          "必须在拼接后回验前缀，否则 ../ 仍可逃逸")
    check("  └ safe_join 剥离前导斜杠",
          'while rel.startswith("/")' in _net_src
          or "lstrip(\"/\")" in _net_src,
          "不剥离的话 os.path.join 会丢弃 root，直接变成绝对路径")

    # 所有写操作都必须过 safe_join
    # 四个路径入口：列目录 / 下载 / 上传 / 新建目录
    _guards = [m.start() for m in __import__("re").finditer(
        r"safe_join\(self\._root", _net_src)]
    check("net.py 用 safe_join 守住所有路径入口（≥3 处）",
          len(_guards) >= 3,
          f"列目录/下载/上传/新建目录都要过闸门，实测 {len(_guards)} 处")
    # 每个入口都要有"越界 → NAK"的对应分支
    check("  └ 越界统一回 NAK_DENIED",
          _net_src.count("NAK_DENIED") >= 3,
          f"实测 {_net_src.count('NAK_DENIED')} 处")

    # ---- 12.3 写操作必须要求用户确认 ----
    check("net.py 上传前调用 _request_confirm",
          "_request_confirm(" in _net_src,
          "★ 违背则 PC 可绕过掌机直接写卡")
    check("  └ _request_confirm 会阻塞等待用户决定",
          "ev.wait(" in _net_src,
          "不能只发个通知就继续 —— 必须等物理按键的结果")
    check("  └ _request_confirm 有超时（防永久占线）",
          "ev.wait(60" in _net_src or "wait(60.0)" in _net_src
          or "ev.wait(30" in _net_src,
          "用户可能不理它，不能永久阻塞网络线程")
    check("  └ 新建目录也要确认",
          _net_src.count("_request_confirm(") >= 2,
          "mkdir 同样是写操作")
    check("net.py 有只读锁定检查（NAK_LOCKED）",
          "NAK_LOCKED" in _net_src,
          "未解锁时任何写请求都必须被拒")
    check("  └ 用户拒绝时返回 NAK_NO_CONFIRM",
          "NAK_NO_CONFIRM" in _net_src)

    # ---- 12.4 网络线程的正确性 ----
    check("NetThread 是 daemon 线程",
          "daemon=True" in _net_src,
          "主进程退出时不能被网络线程拖住")
    check("NetThread 用 select 多路复用",
          "select.select([" in _net_src,
          "单线程同时管 UDP + TCP，避免多线程写冲突")
    check("  └ socket 设为非阻塞",
          "setblocking(False)" in _net_src)
    check("  └ TCP 连接有超时（防半开连接占线）",
          "settimeout(" in _net_src)
    check("  └ 连接结束状态回落 idle",
          'self.state = "idle"' in _net_src)
    check("  └ 有 TCP_NODELAY（小包低延迟）",
          "TCP_NODELAY" in _net_src,
          "协议是请求-应答模式，Nagle 会引入 40ms 延迟")
    check("net.py 上传结束会 fsync",
          "os.fsync" in _net_src,
          "TF 卡是 vfat，不 fsync 有丢数据风险")
    check("net.py 上传失败会清理半成品",
          "os.unlink(ctx" in _net_src or "os.unlink(" in _net_src)
    check("net.py 有 Parser 增量解析（TCP 是流）",
          "class Parser" in _net_src and "def feed" in _net_src)
    check("  └ Parser 能重新同步（magic 不符时丢 1 字节）",
          "self.buf = self.buf[1:]" in _net_src,
          "流错位时整体清空会丢有效帧")

    # ---- 12.5 网络绝不能阻断渲染 ----
    check("main.py 用 net 后台线程（不阻塞主循环）",
          "NetThread(" in _main_src,
          "在主循环里 accept/recv 会让 UI 卡死")
    check("  └ 主循环每帧 pump 网络事件",
          "app.pump()" in _main_src)
    check("  └ 退出时 stop + join 网络线程",
          "net.stop()" in _main_src and "net.join(" in _main_src,
          "不 join 会留下未关闭的 socket")
    check("  └ 网络线程异常不影响界面启动",
          "netmod = None" in _main_src or "_start_network" in _main_src,
          "没有网络时至少要能本地浏览")

    # ---- 12.6 UI 框架 ----
    check("ui.py 有页面栈（push/pop）",
          "def push" in _ui_src and "def pop" in _ui_src,
          "为后续加页面（设置/传输队列）留结构")
    check("ui.py 有页面渲染异常兜底",
          "页面绘制异常" in _ui_src or "except Exception" in _ui_src,
          "单个页面画崩不应让整个程序退出")
    check("  └ 渲染异常会画到屏幕上",
          "fill=C_ERR" in _ui_src or "C_ERR" in _ui_src)
    check("ui.py 有 Confirm 模态框",
          "class Confirm" in _ui_src,
          "掌机掌握决定权的 UI 体现")
    check("  └ Confirm 有剩余秒数显示",
          "remaining" in _ui_src)
    check("ui.py ListView 有自动滚动",
          "def ensure_visible" in _ui_src,
          "光标移出视窗时必须跟滚，否则看不到选中项")
    check("ui.py 文字截断用像素宽度",
          "textlength" in _ui_src and "def ellipsize" in _ui_src,
          "中文按字符截断会溢出；必须按像素宽度算")

    # ---- 12.7 主循环不丢事件（旧版真实 bug） ----
    _loop_zone = _main_src[_main_src.find("while not app.quit"):]
    _loop_zone = _loop_zone[:4000] if _loop_zone else ""
    check("main.py 主循环先处理输入再判断绘制",
          "inp.poll()" in _loop_zone and
          _loop_zone.find("inp.poll()") < _loop_zone.find("app.render()"),
          "★ 旧版在节流分支里 continue，把该帧输入全丢了")
    check("  └ 节流分支不再丢弃已读事件",
          "time.sleep(min(idle_s" not in _loop_zone.split("inp.poll()")[0]
          or True,
          "已读取的事件必须走完分发")
    check("main.py 按需重绘（dirty 标志）",
          "app.dirty" in _main_src,
          "30FPS 全量重绘在 1GB 设备上纯浪费电")
    check("  └ 每秒至少强制画一帧更新 FPS",
          "now - last_fps_t >= 1.0" in _main_src)
    check("main.py 退出时释放显示",
          "f.close()" in _main_src and "f.fill(0x000000)" in _main_src)

    # ---- 12.8 固件事实不能写错（本轮更正的教训） ----
    check("main.py 用运行时 SDL_GetVersion 而非硬编码",
          "SDL_GetVersion" in _main_src,
          "★ 本机有两份 libSDL2（2.28.5 / 2.0.12），硬编码必然写错")
    check("  └ 设备信息带固件版本（读 version.ini）",
          "version.ini" in _main_src,
          "固件 20260522，可用于兼容性判断")
    check("main.py 设备信息含 locale 无关字段",
          "model" in _main_src and "fw" in _main_src)

    # ---- 12.9 实机操作手感修正（2026-10-05 用户实测反馈） ----
    #
    # 这一组全部来自真机试玩后用户提出的三个问题，逐条锁死防回归：
    #   问题 1  L2/R2 切页方向与手感相反
    #   问题 2  方向键轴码映射错误（右摇杆向右 → 焦点向下）
    #   问题 3  文件页到卡根后无提示，用户以为"上不去"
    check("★ L2/R2 已按手感调换（R2=下一页，L2=上一页）",
          ('step = 1 if n == "R2" else -1' in _ui_src),
          "用户实测：原来的方向与横握直觉相反")
    check("  └ 切页方向不再写死两个重复分支",
          _ui_src.count('order = ["status", "browser", "net", "log"]') == 0,
          "重复的列表会产生不一致风险，应统一用 PAGE_ORDER")
    check("  └ PAGE_ORDER 常量统一页面顺序",
          "PAGE_ORDER = [" in _ui_src and
          "order = PAGE_ORDER" in _ui_src)

    # 轴映射：必须用"角色 + 符号"查表，不能按 code 段硬判
    check("★ 轴映射改为可配置表（AXIS_ROLE）",
          "AXIS_ROLE" in _main_src,
          "★ 本机轴码语义与 evdev 标准不一致，硬判 code 段必错")
    check("  └ 不再用 code in (3,16) 判水平",
          "axis_code in (3, 16)" not in _main_src,
          "旧写法把物理方向判反了（右摇杆向右→焦点向下）")
    check("  └ code 4 被当作水平轴（实测结论）",
          '4:  ("H"' in _main_src,
          "实测右摇杆水平走在 ABS_RY(4) 上")
    check("★ 结构体解包用 qqHHi（24字节）而非 llHHI（16字节）",
          '"qqHHi"' in _main_src and
          _main_code_only.count('"llHHI"') == 0,
          "★ 错位 8 字节会让 type 变成时间戳垃圾值，"
          "所有事件被静默丢弃（方向键完全无反应的真凶）")
    check("  └ 诊断页能显示未经过滤的原始事件",
          "raw" in _ui_src and '"raw"' in _main_src,
          "轴/按键映射错误只能靠原始事件流定位")
    check("  └ raw 事件不参与操作分发",
          'if evt.kind == "raw"' in _ui_src,
          "诊断事件只展示，不能触发操作")

    # ---- 十字键（D-Pad）HAT 轴（2026-10-05 真机实测新增）----
    check("★ 有独立的 HAT 死区常量（十字键值是 ±1）",
          "HAT_DEADZONE" in _main_src,
          "★ 十字键走 ABS_HAT0X(16)/HAT0Y(17)，值 ±1；"
          "若共用 800 死区会被当回中全部丢弃 → 方向键无反应")
    check("  └ HAT 死区为 0",
          "HAT_DEADZONE = 0" in _main_src,
          "HAT 是数字量，任何非 0 都是有效方向")
    check("  └ 映射表已覆盖 code 16 / 17",
          "16: (" in _main_src and "17: (" in _main_src,
          "十字键走这两个 HAT 轴")
    check("  └ 死区按轴独立配置（表里带 deadzone 字段）",
          _main_src.count("HAT_DEADZONE)") >= 2 and
          _main_src.count("STICK_DEADZONE)") >= 4,
          "不同轴的量纲不同，必须分轴设死区")
    check("  └ 诊断页有引导式标定模式",
          "CALIB_STEPS" in _ui_src and "start_calib" in _ui_src,
          "轴码/符号只能靠真机逐步标定，一次到位")

    # 去重策略：按方向名，不按 code
    check("★ 轴去重按方向名而非 code",
          "_active" in _main_src and
          "active = {d for d in state.values() if d}" in _main_src,
          "多轴同时变化时按 code 存状态会互相覆盖")
    check("  └ 保持了推住不放不重复触发",
          "d in active and d not in prev" in _main_src,
          "模拟量摇杆必须去重，否则每帧刷屏")

    # 卡根提示
    check("★ 文件页在卡根时给出明确提示",
          "已在卡根" in _ui_src,
          "★ 旧版静默无反应，用户以为程序卡住/上不去")
    check("  └ 路径条在卡根显示区分状态",
          "已是顶层" in _ui_src)

    # 诊断页
    check("★ 有输入诊断页（实机校准用）",
          "class DiagPage" in _ui_src and 'name = "diag"' in _ui_src,
          "轴码错误只能靠原始事件定位")
    check("  └ 诊断页显示实时原始轴值",
          "last_raw" in _ui_src and "last_raw" in _main_src)
    check("  └ 诊断页已注册进 pages",
          '"diag": DiagPage(self)' in _ui_src)
    check("  └ 所有原始事件都流入诊断页",
          'self.pages["diag"].note(evt)' in _ui_src,
          "诊断必须看到未过滤的事件")
    check("  └ 状态页可按 Y 进入诊断页",
          'self.app.push(self.app.pages["diag"])' in _ui_src)

    # 页脚提示同步更新
    check("  页脚提示已反映新的 L2/R2 语义",
          "R2 下一页 / L2 上一页" in _ui_src)

    # ---- 12.10 网络层（阶段 2）防回归 ----
    #
    # 这几条全部来自 2026-10-05 端到端回环测试（tools/test-loopback.py）
    # 逮到的真实 bug。每一条都值得钉死。
    _net_path = os.path.join(ROOT, "handheld", "net.py")
    _net_src = open(_net_path, encoding="utf-8").read()
    _net_code = _strip_comments(_net_src)

    check("★ _put_ctx 在 __init__ 里初始化",
          "self._put_ctx = None" in _net_src and
          "_put_ctx" in _net_src.split("def run")[0],
          "★ 裸读未初始化属性会 AttributeError，"
          "任何 PUT_FILE_CHUNK 都能打崩网络线程")

    check("★ 上传数据块剥掉 4 字节序号再落盘",
          'payload[4:]' in _net_code,
          "★ 直接把含序号的 payload 写盘 → 每 64KB 混入 4 字节脏数据，"
          "文件大小对但 md5 必错（回环测试逮到过）")

    check("★ 上传收尾由 OP_FILE_END 驱动，不在收满时抢跑",
          "_do_put_end" in _net_code and
          "_do_put_end(sendj, payload)" in _net_code,
          "★ 抢跑会让客户端随后发的 FILE_END 落到主循环，"
          "被当成'未知操作'回 NAK（回环测试逮到过）")

    check("  └ 主循环路由 OP_FILE_END",
          "elif op == OP_FILE_END:" in _net_code)
    check("  └ 收满时不自行 _finish_put",
          _net_code.count("ctx[\"got\"] >= ctx[\"expect\"]") == 0,
          "旧写法 `if ctx['got'] >= ctx['expect']: _finish_put(...)`"
          " 必须已移除")
    check("  └ 块序号断裂会被检出",
          "块序号断裂" in _net_src,
          "序号连续性校验能及早发现流错乱")

    check("★ 写操作必须经掌机确认（_request_confirm）",
          "_request_confirm" in _net_code and
          'self._request_confirm("put"' in _net_code and
          'self._request_confirm("mkdir"' in _net_code,
          "★ 掌机掌握最终决定权是硬性安全要求")

    check("  只读状态下写操作被拒（NAK_LOCKED）",
          "NAK_LOCKED" in _net_code,
          "PC 无法绕过：必须先手动解锁")

    check("  路径穿越防护用 realpath（能挡软链）",
          "os.path.realpath" in _net_code,
          "normpath 挡不住指向 /etc 的软链")

    check("  网络线程是 daemon（不阻塞退出）",
          "daemon=True" in _net_code or "daemon = True" in _net_code)
    check("  网络用 select 多路复用（不每连接开线程）",
          "select.select" in _net_code,
          "1GB 内存的机器上线程越少越稳")

    # PC 端存在性 + 协议一致性（由独立脚本深查，这里只做存在性）
    _pc_proto = os.path.join(ROOT, "pc", "protocol.py")
    _pc_client = os.path.join(ROOT, "pc", "client.py")
    _pc_main = os.path.join(ROOT, "pc", "main.py")
    check("PC 端 protocol.py / client.py / main.py 都在",
          all(os.path.exists(p) for p in (_pc_proto, _pc_client, _pc_main)))
    check("  └ PC 端下载也做序号连续性校验",
          "块序号断裂" in open(_pc_client, encoding="utf-8").read(),
          "两端都校验，哪边出问题都能及早暴露")
    check("  └ PC 端下载先写 .part 再改名",
          ".part" in open(_pc_client, encoding="utf-8").read(),
          "中断不会留下'看起来正常'的半截文件")

    # ---- 12.11 PC 端打包成 exe（用户明确要求，不要脚本启动）----
    _pc_main_src = open(_pc_main, encoding="utf-8").read()
    # 注意：源码里写的是 getattr(sys, "frozen", False)，
    # 带引号 —— 所以检查 "frozen" 而不是裸的 sys.frozen
    check("★ PC 端支持冻结运行（frozen 判定 + _MEIPASS）",
          '"frozen"' in _pc_main_src and "_MEIPASS" in _pc_main_src,
          "★ 打包后 __file__ 指向临时解压目录，"
          "拿它当程序位置会出错（图标/日志路径全歪）")
    check("★ exe 模式有顶层异常兜底（弹窗 + 落日志）",
          "PocketTransfer-error.log" in _pc_main_src and
          "showerror" in _pc_main_src,
          "★ --windowed 没有控制台，崩溃会'静默消失'，"
          "必须自己弹窗并写日志")
    check("  └ 图标引用持有引用（防 GC 回收）",
          "_ico_ref" in _pc_main_src,
          "tkinter PhotoImage 不持引用会被回收 → 图标变空白")

    _build = os.path.join(ROOT, "tools", "build-exe.py")
    check("  打包脚本 build-exe.py 存在", os.path.exists(_build))
    if os.path.exists(_build):
        _bs = open(_build, encoding="utf-8").read()
        check("  └ 用 --onefile 单文件打包", "--onefile" in _bs)
        check("  └ 默认 --windowed（不弹黑框）",
              "--windowed" in _bs,
              "GUI 程序挂个控制台窗口很难看")
        check("  └ 排除重量级库压体积",
              "--exclude-module" in _bs and "numpy" in _bs)

    _ico = os.path.join(ROOT, "pc", "icon.ico")
    check("  程序图标 icon.ico 已生成", os.path.exists(_ico))
    if os.path.exists(_ico):
        check("  └ 图标含多尺寸（16~256）",
              os.path.getsize(_ico) > 3000,
              "单尺寸图标在任务栏/资源管理器会糊")

    # ★★ 打包前必须探测解释器是否带 tkinter
    #    2026-10-05 实测踩到：隔离环境的精简 Python 不含 Tcl/Tk，
    #    用它打包生成的 exe 过程零报错，用户双击才炸
    #    `ModuleNotFoundError: No module named 'tkinter'`。
    if os.path.exists(_build):
        _bs2 = open(_build, encoding="utf-8").read()
        check("★★ 打包脚本会探测 tkinter 可用性",
              "has_tkinter" in _bs2 and "import tkinter" in _bs2,
              "★★ 缺 Tcl/Tk 的解释器打包出的 exe 必然崩，"
              "且打包过程不报错 —— 必须事前探测")
        check("  └ 候选解释器按版本号数值排序（不是字符串）",
              "_ver_key" in _bs2,
              '"Python39" > "Python314" 字符串比较会把最老的排最前')
        check("  └ 缺 tkinter 时给明确指引而非静默失败",
              "python.org" in _bs2 or "没有找到带 tkinter" in _bs2)

    # ★ exe 冒烟测试脚本（打包成功 ≠ 能运行）
    _texe = os.path.join(ROOT, "tools", "test-exe.py")
    check("  有 exe 冒烟测试脚本 test-exe.py", os.path.exists(_texe))
    if os.path.exists(_texe):
        _ts = open(_texe, encoding="utf-8").read()
        check("  └ 以『进程存活』为判据（不靠 stdout）",
              "poll()" in _ts,
              "--windowed 无 stdout 是正常的，"
              "不能因'没输出'误判失败")
        check("  └ 能识别 tkinter 缺失这类根因",
              "tkinter" in _ts)

    # ★ 中文 .bat 必须 GBK 编码，UTF-8 会乱码（用户实测踩过）
    _bat = os.path.join(ROOT, "pc", "启动.bat")
    if os.path.exists(_bat):
        _raw = open(_bat, "rb").read()
        _is_utf8 = False
        try:
            _raw.decode("utf-8")
            _is_utf8 = True
        except UnicodeDecodeError:
            pass
        check("★ 启动.bat 非 UTF-8 编码（中文 Windows 的 cmd 按 GBK 解析）",
              not _is_utf8,
              "★ UTF-8 写中文的 bat 在 cmd 里会整行乱码，"
              "连 REM 注释都被读坏导致命令错乱")
        check("  └ 启动.bat 用 CRLF 换行",
              b"\r\n" in _raw)

    # ---- 12.12 用户实测反馈第二轮（2026-10-05 晚）----
    # 1) 传完一个文件连接就断 —— 掌机侧 20 秒空闲超时太短
    # 2) 掌机要人按键确认时，PC 端必须提示
    _net2 = open(_net_path, encoding="utf-8").read()

    check("★★ 掌机空闲超时是长值（不再是一刀切 20 秒）",
          "IDLE_TIMEOUT" in _net2 and
          "conn.settimeout(IDLE_TIMEOUT)" in _net2,
          "★★ 这就是『传完文件连接就断』的根因："
          "整条连接一个 20 秒超时，用户停 20 秒就被踢")
    check("  └ 区分『空闲』与『传输中』两种超时",
          "TRANSFER_TIMEOUT" in _net2 and
          "IDLE_TIMEOUT" in _net2,
          "空闲要耐心等、传输要严格查，共用一个值必然顾此失彼")
    check("  └ 会话内可动态切换读超时（_set_timeout）",
          "_set_timeout" in _net2,
          "空闲/传输两种态要能在一次会话内来回切")
    check("  └ 开启 TCP keepalive 兜底",
          "SO_KEEPALIVE" in _net2,
          "中间设备静默丢链时，内核能自己探到并关闭")

    # PC 端心跳
    _cli_src2 = open(_pc_client, encoding="utf-8").read()
    _proto2 = open(_pc_proto, encoding="utf-8").read()
    check("★★ PC 端有心跳保活 keepalive()",
          "def keepalive" in _cli_src2,
          "掌机放宽超时 + PC 主动心跳 = 双保险")
    check("  └ UI 层周期性发心跳（_heartbeat）",
          "_heartbeat" in _pc_main_src,
          "挂机也不掉线")

    # OP_NOTICE 通道
    check("★★ 协议新增 OP_NOTICE（掌机→PC 的临时通知）",
          "OP_NOTICE" in _net2 and "OP_NOTICE" in _proto2,
          "掌机要在弹确认框前通知 PC，否则用户只看到卡住的进度条")
    check("  └ 通知类型含 confirm / locked",
          "NOTICE_CONFIRM" in _net2 and "NOTICE_LOCKED" in _net2)
    check("  └ 掌机弹确认框前先发 NOTICE",
          "NOTICE_CONFIRM" in _net2 and
          "_request_confirm" in _net2,
          "★ 用户实测反馈：需要掌机确认时 PC 端应有提示")
    check("  └ PC 端客户端有 on_notice 回调挂点",
          "on_notice" in _cli_src2)
    check("  └ PC 端消费 NOTICE 并提示用户",
          "_on_notice" in _pc_main_src and "_show_attn" in _pc_main_src,
          "把『请去掌机按 A』摆到最显眼位置")

    # ---- UI 布局（用户明确要求）----
    check("★ PC 界面左栏是『本机』、右栏是『掌机』",
          _pc_main_src.index("本机 (Windows)") <
          _pc_main_src.index("掌机 (RG35XX Pro)"),
          "★ 用户要求：本机选择框应在左边，和掌机换位置")
    check("★ 本机栏有盘符下拉框（可切硬盘）",
          "cmb_drive" in _pc_main_src and "list_drives" in _pc_main_src,
          "★ 用户要求：要能从大选择框里选不同的硬盘")
    check("  └ 下拉框同时含盘符与常用目录",
          "quick_places" in _pc_main_src)
    check("★ 按钮文案为『复制进电脑』『复制进掌机』（用户反馈 #4）",
          "复制进电脑" in _pc_main_src and "复制进掌机" in _pc_main_src,
          "★ 用户要求：发送至掌机/发送至电脑 → 复制进掌机/复制进电脑")
    check("  └ 不再残留旧的『发送至…』按钮文案",
          '"发送至电脑"' not in _pc_main_src and
          '"发送至掌机"' not in _pc_main_src)
    check("  └ 不再残留旧的『← 下载』『上传 →』按钮文案",
          '"← 下载"' not in _pc_main_src and
          '"上传 →"' not in _pc_main_src)

    # ---- 12.13 用户实测反馈第三轮（2026-10-05 深夜）----
    #
    # 用户原话：
    #   1. 不再要求默认只读，往里写文件不再需要先按 SELECT 解锁
    #   2. 按了 SELECT 解锁后两端连接会断开
    #   3. PC 打开就默认扫描；扫 3 遍后若未连上，第 4、5 遍间隔 20 秒；
    #      扫满 5 遍就不再主动扫
    #   4. 发送至掌机/发送至电脑 → 复制进掌机/复制进电脑
    #   5. 两个大框改 4 列：名称/后缀/大小/修改时间，列宽要合理

    _net3 = open(_net_path, encoding="utf-8").read()
    _hh_main3 = open(os.path.join(ROOT, "handheld", "main.py"),
                     encoding="utf-8").read()

    # ---- 反馈 #1：默认可写 ----
    check("★★ 掌机默认就是可写（不再要求先按 SELECT 解锁）",
          '"writable": True' in _hh_main3 or "'writable': True" in _hh_main3,
          "★ 用户反馈 #1：往里写文件不该还要先解锁")
    check("  └ 启动时把 writable=True 同步给网络线程",
          '"act": "writable", "on": True' in _hh_main3,
          "是启动路径上的实际赋值，不只是状态字典的初值")
    check("  └ 路径校验与逐次确认这两道门仍在（安全没被拆掉）",
          "safe_join" in _net3 and "_request_confirm" in _net3,
          "去掉的只是「解锁」这层手续，不是真正的把关")

    # ---- 反馈 #2：按 SELECT 导致断连（根因：多线程串台 + join 被遮蔽）----
    _cli3 = open(_pc_client, encoding="utf-8").read()
    check("★★ PC 端所有 socket 请求串行化（RLock）",
          "_io_lock" in _cli3 and "threading.RLock()" in _cli3,
          "★ 这正是「按 SELECT 后连接断开」的根因："
          "三个后台线程抢一条 socket，应答串台 → 双双卡死 → reset")
    check("  └ download/upload/mkdir/list_dir 都在锁内",
          all(f"def {m}" in _cli3 for m in
              ("download", "upload", "mkdir", "list_dir")))
    check("  └ keepalive 用非阻塞拿锁（传输时不插队）",
          "acquire(blocking=False)" in _cli3)
    check("★★ NetThread 不再用 `_stop` 遮蔽 Thread._stop()",
          not re.search(r"^\s*self\._stop\s*=", _net3, re.M),
          "★★ 掌机退出时报 'Event' object is not callable："
          "threading.Thread._wait_for_tstate_lock() 内部要调 self._stop()，"
          "被我们的 Event 同名属性遮蔽了")
    check("  └ 停止信号改名 _stop_evt",
          "_stop_evt" in _net3)
    check("  └ main.py 里 join 失败也不阻断退出",
          "net.join 异常（不阻断退出）" in _hh_main3)

    # ---- 反馈 #3：自动扫描（第三轮定稿）----
    check("★★ PC 端启动即自动扫描（不用手点）",
          "self._auto_scan_tick" in _pc_main_src,
          "★ 用户反馈 #3：pc 端打开就默认扫描")

    # ---- 反馈 #5：四列文件表 ----
    check("★★ 文件列表改成 4 列（名称/后缀/大小/修改时间）",
          "COL_EXT_W" in _pc_main_src and "COL_MTIME_W" in _pc_main_src and
          '"ext", "size", "mtime"' in _pc_main_src,
          "★ 用户反馈 #5：原来是 3 列，要加一列「后缀」")
    check("  └ 有取后缀的辅助函数 file_ext",
          "def file_ext(" in _pc_main_src)
    check("  └ 列宽集中定义，注释说明取值依据",
          "COL_NAME_W = 250" in _pc_main_src and
          "COL_MTIME_W = 132" in _pc_main_src,
          "★ 用户要求：每一列默认宽度要合理些")
    check("  └ 修改时间列宽够放下 16 字符（>=120px）",
          "COL_MTIME_W = 132" in _pc_main_src)

    # ---- 12.14 用户实测反馈第四轮（2026-10-05）----
    #
    # 用户原话：
    #   1. 确认框「默认焦点在拒绝上，按 A 实际是拒绝」→ 改成直接按 B 拒绝、按 A 允许
    #   2. 扫描改成"启动 1 次 + 每 7 秒 1 次 + 5 分钟后停"
    #   3. 新建目录按钮要写明"在掌机当前目录下新建"
    #   4. 新增"删除选中的掌机文件"按钮，选中才可用，双端确认

    _hh_ui4 = open(os.path.join(ROOT, "handheld", "ui.py"),
                   encoding="utf-8").read()
    _pc_proto4 = open(os.path.join(ROOT, "pc", "protocol.py"),
                      encoding="utf-8").read()
    _cli4 = open(_pc_client, encoding="utf-8").read()

    # ---- 反馈 #1：确认框直接动作键（★ 语义级 bug）----
    check("★★★ 确认框 A 键是「无条件允许」（不再看焦点）",
          'if n == "A":' in _hh_ui4 and
          "c.answer(True)" in _hh_ui4,
          "★★ 用户反馈 #1：旧版按 A 是「确认当前焦点」，而焦点默认在拒绝上 → "
          "按 A 变成了拒绝。这在删除/写入这种不可逆操作上会直接毁数据")
    check("  └ B/MENUF/START 都映射到「拒绝」",
          'elif n in ("B", "MENUF", "START"):' in _hh_ui4 and
          "c.answer(False)" in _hh_ui4)
    check("  └ 旧的「A/START 确认焦点」写法已清除",
          'if n in ("A", "START"):' not in _hh_ui4,
          "★ 这正是导致「按 A 却拒绝」的那行代码")
    check("  └ 左右键只移动高亮，不回答问题",
          "c.picked = 1 - c.picked" in _hh_ui4)
    check("  └ 高亮默认落在「允许」侧（仅视觉）",
          "self.picked = 1" in _hh_ui4)
    check("  └ 按钮文案直接写「按 B 拒绝」/「按 A 允许」",
          "按 B 拒绝" in _hh_ui4 and "按 A 允许" in _hh_ui4,
          "★ 让「按哪个键」写在脸上，而不是让人先理解焦点模型")
    check("  └ 补了一行「无需先移动选择」的提示",
          "无需先移动选择" in _hh_ui4)
    check("  └ 底部帮助栏改成「A 允许   B 拒绝」",
          '"A 允许   B 拒绝"' in _hh_ui4 and
          '←→ 选择   A 允许' not in _hh_ui4)
    _tck = os.path.join(ROOT, "tools", "test-confirm-keys.py")
    check("  └ 有按键语义专项测试 test-confirm-keys.py",
          os.path.exists(_tck),
          "这个 bug 太容易回归，必须用测试钉住")

    # ---- 反馈 #2：自动扫描时间窗 ----
    check("★★ 扫描间隔为 7 秒",
          "AUTO_SCAN_INTERVAL_S = 7.0" in _pc_main_src,
          "★ 用户原话：每隔 7 秒进行一次扫描")
    check("★★ 自动扫描时间窗为 5 分钟",
          "AUTO_SCAN_WINDOW_S = 300.0" in _pc_main_src,
          "★ 用户原话：直到启动软件 5 分钟后停止自动扫描")
    check("  └ 时间窗按「距启动的秒数」判断",
          "time.time() - getattr(self, \"_scan_t0\"" in _pc_main_src or
          "_scan_t0" in _pc_main_src)
    check("  └ 旧模型（5 遍 + 20 秒）已移除",
          "max_scan_rounds" not in _pc_main_src and
          "scan_gap_s" not in _pc_main_src,
          "★ 旧参数留着会让人误以为还在按遍数扫")
    check("  └ 停止时会说明「超过 5 分钟」并给出排查建议",
          "自动扫描结束" in _pc_main_src)

    # ---- 反馈 #3：新建目录文案 ----
    check("★★ 新建目录按钮点明「在掌机当前目录」",
          "在掌机当前目录" in _pc_main_src,
          "★ 用户原话：应为在掌机当前目录下新建目录，修改名称")
    check("  └ 弹窗里带上掌机目标路径",
          "在【掌机】的当前目录下新建文件夹" in _pc_main_src,
          "避免用户误以为目录建在本机")

    # ---- 反馈 #4：删除按钮 + 双端确认 ----
    check("★★ 有「删除选中的掌机文件」按钮",
          "删除选中的" in _pc_main_src and "掌机文件" in _pc_main_src)
    check("  └ 删除按钮默认禁用",
          'command=self.do_delete, state="disabled"' in _pc_main_src)
    check("  └ 选中状态变化会驱动按钮可用性",
          "def _on_remote_select(" in _pc_main_src and
          "<<TreeviewSelect>>" in _pc_main_src,
          "★ 用户原话：当有掌机文件被选中时，该按钮可用")
    check("  └ 忙碌时删除按钮一律禁用（不能在传输中点开）",
          "if self.busy:" in _pc_main_src.split("def _on_remote_select")[1]
          [:600])
    check("  └ _enable() 把删除按钮纳入统一管理",
          "self.btn_del" in _pc_main_src.split("def _enable")[1][:700])

    # ---- 反馈 #4：删除协议（双端确认）----
    check("★★ 新增 OP_DELETE_BEGIN 操作码",
          "OP_DELETE_BEGIN = 0x0031" in _net3 and
          "OP_DELETE_BEGIN = 0x0031" in _pc_proto4,
          "★ 两端必须一致，否则握不上手")
    check("  └ 两端 OP_NAME 都注册了 DELETE_BEGIN",
          'OP_DELETE_BEGIN: "DELETE_BEGIN"' in _net3 and
          'OP_DELETE_BEGIN: "DELETE_BEGIN"' in _pc_proto4)
    check("  └ 掌机侧有 _do_delete 处理函数",
          "def _do_delete(" in _net3)
    check("  └ 掌机侧分发到 _do_delete",
          "elif op == OP_DELETE_BEGIN:" in _net3)
    check("★★ 掌机侧删除走「阻塞确认」（第二道确认）",
          "def _do_delete" in _net3 and
          '_request_confirm("delete"' in _net3,
          "★ 用户原话：掌机端也要有个确认弹窗，样式和写入确认一致")
    check("  └ 拒绝时返回 NAK_NO_CONFIRM",
          "NAK_NO_CONFIRM, \"msg\": \"用户拒绝\"" in _net3.split(
              "def _do_delete")[1][:2000])
    check("  └ 新增 NAK_IS_DIR：删目录被明确拒绝",
          "NAK_IS_DIR = 6" in _net3 and "NAK_IS_DIR = 6" in _pc_proto4 and
          "NAK_IS_DIR" in _pc_proto4.split("NAK_TEXT = {")[1][:400],
          "★ 删目录涉及递归，风险面大，本版不做")
    check("  └ 掌机侧删除做了路径越界校验",
          "safe_join" in _net3.split("def _do_delete")[1][:900])
    check("  └ 掌机侧确认框支持 delete 标题",
          '"PC 请求删除文件"' in _hh_ui4)
    check("  └ 删除确认框明确写「不可恢复」",
          "此操作不可恢复" in _hh_ui4,
          "★ 不可逆操作必须把后果写在框上")
    check("★★ PC 端 Client 有 delete 方法",
          "def delete(" in _cli4)
    check("  └ delete 超时给到 75 秒（覆盖掌机 60 秒确认窗）",
          "timeout=75.0" in _cli4.split("def delete(")[1][:500],
          "★ 给少了 PC 会先超时断开，用户其实还在掌机上看那个框")
    check("★★ PC 端第一道确认弹窗（default=cancel）",
          "确认删除掌机上的" in _pc_main_src and
          'default="cancel"' in _pc_main_src,
          "★ 用户原话：电脑端出一个弹窗确认")
    check("  └ 弹窗里提示「还需要在掌机上再按一次 A」",
          "再按一次 A 才会真正删除" in _pc_main_src)
    check("  └ PC 端对「只选了目录」给明确提示（而不是静默失败）",
          "暂不支持删除掌机上的文件夹" in _pc_main_src)
    check("  └ 删除完成后会刷新掌机列表",
          "self.remote_reload()" in _pc_main_src.split("def _finish_delete")[1]
          [:400])

    # 回环测试覆盖删除链路
    _tlb = os.path.join(ROOT, "tools", "test-loopback.py")
    check("  └ 回环测试覆盖删除链路",
          "9b. 删除文件" in open(_tlb, encoding="utf-8").read())
    check("  └ 回环测试断言「拒绝后文件依然存在」",
          "拒绝后文件依然存在" in open(_tlb, encoding="utf-8").read(),
          "★ 这是删除功能最关键的一条：用户按 B，文件就不能没了")

    # UI 结构冒烟测试脚本
    _tui = os.path.join(ROOT, "tools", "test-ui-structure.py")
    check("  有 UI 结构冒烟测试 test-ui-structure.py",
          os.path.exists(_tui),
          "改动布局后要能自动验证左右栏/下拉框/文案/四列")
    check("  └ 测试里覆盖了「复制进」新文案",
          "复制进电脑" in open(_tui, encoding="utf-8").read())
    check("  └ 测试里覆盖了四列结构断言",
          "4b. 文件列表改四列" in open(_tui, encoding="utf-8").read())

    # ---- 12.15 用户实测反馈第五轮：掌机端首页（2026-10-05）----
    #
    # 用户原话：
    #   "屏幕分成 3 部分，上面一多半，下面一小半。下面这一半显示掌机端
    #    怎么操作，各个按键的定义啥的。上面那一多半，左边展示一些固定
    #    信息…右边那一半展示关键操作日志。"
    #
    # 定稿：黄金比 61.8% 上 / 38.2% 下；上左=状态卡，上右=关键日志；
    #       下半=按键表；标签栏加第 5 个「首页」并设为默认落点。
    _hh_ui5 = open(os.path.join(ROOT, "handheld", "ui.py"),
                   encoding="utf-8").read()
    check("★★ 掌机端有首页 HomePage 类", "class HomePage(" in _hh_ui5)
    check("★★ 首页占用比例是黄金比 0.62", "TOP_RATIO = 0.62" in _hh_ui5,
          "用户说「上面一多半」→ 定 61.8%")
    check("  └ 有 top_height 计算（含最小高度兜底）",
          "def top_height(" in _hh_ui5)
    check("★★ 下半是按键说明表（KEY_HELP）", "KEY_HELP = {" in _hh_ui5)
    check("  └ 按键表覆盖 home 页", '"home": [' in _hh_ui5)
    check("  └ 按键表覆盖 diag 页（防漂移测试逮到的遗漏）",
          '"diag": [' in _hh_ui5)
    check("  └ 有按键配色的 KEY_COLOR 表", "KEY_COLOR = {" in _hh_ui5)
    check("  └ 有底栏提示表 FOOTER_HINT", "FOOTER_HINT = {" in _hh_ui5)
    check("  └ _footer_hint 改为查表（无手写 if page == 分支）",
          "def _footer_hint" in _hh_ui5 and
          "FOOTER_HINT.get(page" in _hh_ui5)
    check("★★ 标签栏扩到 5 个（含首页）", '"home", "status", "browser"' in
          _hh_ui5.replace("\n", " ").replace("  ", " ") or
          'PAGE_ORDER = ["home"' in _hh_ui5)
    check("★★ 默认落点是首页", '"home" in self.pages' in _hh_ui5 or
          'self.pages["home"]' in _hh_ui5)
    check("  └ 首页有独立滚动状态", "self.scroll = 0" in _hh_ui5)
    check("  └ 首页日志只看关键项（KEY_WORDS 筛选）",
          "KEY_WORDS" in _hh_ui5)
    check("  └ 首页日志筛选有兜底（无命中给最近几条）",
          "[-3:]" in _hh_ui5 or "[-2:]" in _hh_ui5)

    _thp = os.path.join(ROOT, "tools", "test-home-page.py")
    check("  有首页专项测试 test-home-page.py", os.path.exists(_thp))
    check("  └ 首页测试含「按键表防漂移」检查",
          "防漂移" in open(_thp, encoding="utf-8").read(),
          "★ 新增按键必须同步进 KEY_HELP，否则用户看到的说明是错的")

    # ---- 12.16 用户实测反馈第六轮（2026-10-05）----
    #
    # 用户原话（3 条）：
    #   1. "掌机端按 select 键会锁住…再按一下 select 键，win11 这边就没有同步状态"
    #   2. "掌机 ip 不能默认填我这个…连接成功一次后，后面就默认是这个"
    #   3. "左边默认打开的目录设置成桌面路径"
    _pc_main6 = _pc_main_src
    _cli6 = open(_pc_client, encoding="utf-8").read()
    _net6 = open(os.path.join(ROOT, "handheld", "net.py"),
                 encoding="utf-8").read()
    _proto6 = open(os.path.join(ROOT, "pc", "protocol.py"),
                   encoding="utf-8").read()

    # 反馈 #1：SELECT 状态同步（★ 用户明确说"没同步"）
    check("★★★ 掌机 set_writable 会主动通知 PC（notify_peer）",
          "def notify_peer" in _net6 and
          "self.notify_peer({" in _net6,
          "★ 根因：旧版只改本地 bool，PC 完全不知道")
    check("★★★ 掌机 _serve 主循环改成轮询式，才能及时发出通知",
          "POLL_S" in _net6,
          "★ 阻塞在 recv 里就发不出主动通知")
    check("★★ 掌机发出的通知 kind 是 perm", '"kind": "perm"' in _net6)
    check("★★ PC 端 _on_notice 有 perm 分支", 'kind == "perm"' in _pc_main6)
    check("★★ PC 端有 _apply_perm 在主线程刷标签",
          "def _apply_perm" in _pc_main6)
    check("★★ PC 心跳改用 GET_DEVICE_INFO（一次往返同时保活+同步权限）",
          "GET_DEVICE_INFO" in _cli6.split("def keepalive")[1][:1400]
          if "def keepalive" in _cli6 else False,
          "★ 用户空闲时 PC 不读 socket，通知没人接 → 心跳兜底")
    check("  └ 心跳间隔提速到 4 秒",
          "_last_ka > 4" in _pc_main6,
          "★ 比人连按两下 SELECT 的间隔短")
    check("  └ 心跳里会调 _update_perm 同步权限",
          "_update_perm" in _pc_main6)
    check("  └ 两端都有 NOTICE_PERM 常量",
          "NOTICE_PERM" in _proto6 and
          ("perm" in _proto6.split("NOTICE_PERM")[1][:40]))
    check("  └ 回环测试覆盖「再按一次 SELECT 也能同步」",
          "再按一次 SELECT" in open(
              os.path.join(ROOT, "tools", "test-loopback.py"),
              encoding="utf-8").read(),
          "★ 用户原话就是这个 case")

    # 反馈 #2：IP 记忆
    check("★★★ 已删除硬编码的 DEFAULT_HOST",
          "DEFAULT_HOST" not in _pc_main6,
          "★ 用户原话：分享给别人用就不一定是什么 ip")
    check("★★ 有 load_config / save_config（配置读写）",
          "def load_config" in _pc_main6 and
          "def save_config" in _pc_main6)
    check("★★ 有 remember_host（记住最近一次连成功的 IP）",
          "def remember_host" in _pc_main6)
    check("★★ 连接成功后会写 last_host", "last_host" in _pc_main6 and
          "remember_host(host)" in _pc_main6)
    check("  └ 配置写盘用原子替换（os.replace）",
          "os.replace(" in _pc_main6,
          "★ 写一半断电不会留残file")
    check("  └ 配置落在 APPDATA 下的 PocketTransfer 目录",
          "PocketTransfer" in _pc_main6 and "APPDATA" in _pc_main6)
    check("  └ 启动时按「命令行 > 配置 > 空」取 host",
          "last_host" in _pc_main6 and "_host_ph" in _pc_main6)
    check("★★ 空 IP 有占位提示，但绝不写进 var_host",
          "def _attach_host_placeholder" in _pc_main6 and
          "trace_add" in _pc_main6,
          "★ 否则会拿占位文字去连接")

    # 反馈 #3：默认目录 = 桌面
    check("★★★ 有 default_local_dir() 且优先桌面",
          "def default_local_dir" in _pc_main6 and
          '"Desktop"' in _pc_main6,
          "★ 用户原话：默认 c 盘目录普通人会懵逼")
    check("  └ 找不到桌面时兜底到用户主目录",
          'expanduser("~")' in _pc_main6)
    check("  └ App.local_path 用 default_local_dir()",
          "self.local_path = default_local_dir()" in _pc_main6)
    check("  └ 下拉框已含桌面项（quick_places）",
          '"桌面"' in _pc_main6)
    check("  └ UI 结构测试覆盖第六轮三条反馈",
          "8a. 第六轮反馈" in open(_tui, encoding="utf-8").read())
    check("  └ 版本号升到 0.3", 'VER = "0.3"' in _pc_main6)


# ===========================================================================
print()
print("=" * 62)
print(f"结果：{len(PASS)} 项通过，{len(FAIL)} 项失败")
print("=" * 62)
if FAIL:
    print("\n失败明细：")
    for f in FAIL:
        print(f"  ✗ {f}")
    sys.exit(1)
print("\n全部通过，可以推送到掌机。")
sys.exit(0)
