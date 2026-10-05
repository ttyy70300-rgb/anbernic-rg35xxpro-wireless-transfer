#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PocketTransfer 掌机端 SDL2 显示后端
====================================

【为什么必须用 SDL2 而不是裸 framebuffer —— 核心结论】
    这台机器（RG35XX Pro / Allwinner H700）的显示子系统是
    **DispEngine 多图层合成器**（device: /dev/disp）。

    实测证据（2026-10-05）：
      - 原厂菜单 dmenu.bin 通过 open("/dev/disp") +
        ioctl(DISP_LAYER_SET_CONFIG2) + ioctl(DISP_HWC_COMMIT)
        建立**一个硬件图层**，像素数据放在 /dev/ion 分配的显存里。
      - 这个图层的数据**不在 /dev/fb0 内**。把 fb0 全部 7MB 逐页扫一遍，
        都找不到 dmenu 画的"加载中"画面。
      - /dev/fb0 对应的是 disp 的**底层图层**。
      - dmenu.bin 退出时不做 DISP_BLANK 清理，图层残留，
        持续把最后一帧扫描输出到屏幕。
      → 于是：直接在 fb0 上作画，画面被上层残留图层**完全盖住**。
        程序跑得再好、显存写得再对，用户也**什么都看不见**。

    原厂 5 个 Python 应用（时钟/图片浏览器/魔改系统工具/魔改系统设置/
    瑞士小刀抓取器）全部走 SDL2：
        SDL_CreateWindow(..., SDL_WINDOW_FULLSCREEN_DESKTOP | SDL_WINDOW_SHOWN)
        SDL_CreateRenderer(w, -1, SDL_RENDERER_ACCELERATED)
    SDL2 的 FULLSCREEN_DESKTOP 会 open /dev/disp 创建**自己的新图层**
    并置于最顶层，退出时销毁。所以它天然盖过 dmenu 残留图层。

    → 结论：**在这个平台上，只有走 /dev/disp（SDL2 帮你封装好了）
      才能显示出画面。裸 fb0 必然被盖。**

【为什么不用 SDL 的 texture 直通】
    我们已经有整套基于 PIL 的绘制代码（字体、抗锯齿、中文字形都靠它）。
    重写成 SDL_gfx 代价太大且中文支持差。
    所以走：PIL 画 RGBA → 转 SDL_Surface → 转 Texture → renderer 上屏。
    与原厂 clock/graphic.py 的做法完全一致。

【依赖】
    pysdl2（本机 0.9.17）+ 系统 libSDL2-2.0.so.0
    运行前必须 export PYSDL2_DLL_PATH=/usr/lib（原厂 5 个应用都这么设）
    launcher.sh 已经设好了。

【接口契约】
    本类对外暴露的 API 与 fb.Framebuffer **完全相同**：
        open() / info() / px() / fill() / blit(img) / close()
        属性 width / height / bpp / line_px / probe_note / smem_len
    这样 main.py 的 UI 代码一行都不用改。
"""
import os

# 屏幕逻辑尺寸。SDL 的 FULLSCREEN_DESKTOP 会自动用真实面板分辨率，
# 我们把 PIL 画布定成 640x480（与面板物理分辨率一致，1:1 无缩放）。
DEFAULT_W = 640
DEFAULT_H = 480


class SDLDisplayError(Exception):
    pass


def _import_sdl2():
    """
    延迟导入 pysdl2。

    延迟的原因：
      - 在 PC 上跑离线测试（tools/selftest.py）时没有 SDL2，
        不能在 import 阶段就炸掉整个模块。
      - 让调用方能在 try/except 里优雅降级到 fb.py 后端。

    ⚠️ 千万不要 import sdl2.ext！
        实测：`import sdl2.ext` 会连带加载 SDL2_image / SDL2_ttf /
        SDL2_mixer 等扩展库。本机上 SDL2_image 是坏的：

            DLLWarning: OSError('/lib/aarch64-linux-gnu/
                libSDL2_image-2.0.so.0: undefined symbol: SDL_roundf')

        这些失败会污染 SDL 的内部状态，导致后续渲染调用段错误
        （rc=139 / SIGSEGV，且段错误打不出 traceback）。
        控制实验：不 import sdl2.ext 的最小程序（probe-sdl.py）一切正常；
        import 了它的版本在窗口创建后的渲染阶段崩溃。
        本模块只需要核心 sdl2，不需要任何 ext 里的辅助功能。
    """
    try:
        import sdl2
    except ImportError as e:
        raise SDLDisplayError(
            f"pysdl2 不可用: {e}。"
            f"请确认已安装 python3-sdl2 且 PYSDL2_DLL_PATH 指向 libSDL2 所在目录"
            f"（原厂设置为 /usr/lib）")
    return sdl2


class SDLDisplay:
    """
    与 fb.Framebuffer 接口兼容的 SDL2 显示后端。

    内部把 PIL Image 转成 SDL 纹理上屏。SDL 的 FULLSCREEN_DESKTOP
    会接管 /dev/disp 创建一个顶层硬件图层 —— 这是能显示出来的唯一途径。
    """

    def __init__(self, width=DEFAULT_W, height=DEFAULT_H, title="PocketTransfer"):
        self.want_w = width
        self.want_h = height
        self.title = title

        self._sdl2 = None
        self.window = None
        self.renderer = None
        self._texture = None
        self._last_key = None

        # --- 与 fb.Framebuffer 对齐的属性 ---
        self.width = width
        self.height = height
        self.virtual_w = width
        self.virtual_h = height
        self.bpp = 32                     # SDL 纹理永远是 RGBA8888
        self.line_px = width
        self.hoffset = 16
        self.goffset = 8
        self.boffset = 0
        self.toffset = 24
        self.smem_len = 0
        self.smem_start = 0
        self.probe_note = []
        self.opened = False

    # ---------- 打开 ----------

    def open(self):
        sdl2 = _import_sdl2()
        self._sdl2 = sdl2
        # 细粒度阶段日志。SDL 层一旦段错误，进程直接没了，
        # traceback 也打不出来 —— 只能靠"最后一条日志"定位。
        self._stage = []

        def stage(msg):
            self._stage.append(msg)
            self.probe_note.append(msg)
            try:
                import boot
                boot.log(f"  [SDL阶段] {msg}")
            except Exception:
                pass

        # SDL_Init：只需要 VIDEO 子系统。
        # 不要初始化 AUDIO —— 本应用不发声，还会拖慢启动。
        stage("1 SDL_Init 前置")
        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO) != 0:
            err = sdl2.SDL_GetError()
            raise SDLDisplayError(f"SDL_Init 失败: {err}")
        stage("2 SDL_Init(VIDEO) ok")

        # 关键：SDL_WINDOW_FULLSCREEN_DESKTOP（无边框全屏）
        # 会走 /dev/disp 创建顶层硬件图层 —— 这正是原厂应用的做法。
        flags = sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP | sdl2.SDL_WINDOW_SHOWN
        # 标题必须是 bytes，且**由 self 持有**（见 blit 里同类的悬空指针教训）。
        # SDL_CreateWindow 按文档会拷贝标题，但持有引用零成本，不给它留坑。
        self._title_b = self.title.encode("utf-8")
        stage("3 SDL_CreateWindow 前置")
        self.window = sdl2.SDL_CreateWindow(
            self._title_b,
            sdl2.SDL_WINDOWPOS_UNDEFINED,
            sdl2.SDL_WINDOWPOS_UNDEFINED,
            0, 0,          # 全屏模式下尺寸参数被忽略
            flags,
        )
        if not self.window:
            err = sdl2.SDL_GetError()
            sdl2.SDL_Quit()
            raise SDLDisplayError(f"SDL_CreateWindow 失败: {err}")
        stage("4 SDL_CreateWindow(FULLSCREEN_DESKTOP) ok")

        # 渲染器：先试硬件加速，失败回退软件渲染。
        # 原厂 clock/graphic.py 就是这么写的，说明这机器上两种都可能出现。
        stage("5 SDL_CreateRenderer 前置")
        self.renderer = sdl2.SDL_CreateRenderer(
            self.window, -1, sdl2.SDL_RENDERER_ACCELERATED)
        if not self.renderer:
            self.probe_note.append(
                f"ACCELERATED 渲染器失败({sdl2.SDL_GetError()})，回退 SOFTWARE")
            self.renderer = sdl2.SDL_CreateRenderer(
                self.window, -1, sdl2.SDL_RENDERER_SOFTWARE)
        if not self.renderer:
            err = sdl2.SDL_GetError()
            sdl2.SDL_DestroyWindow(self.window)
            sdl2.SDL_Quit()
            raise SDLDisplayError(f"SDL_CreateRenderer 失败: {err}")
        stage("6 SDL_CreateRenderer ok")

        # 关掉线性插值 —— 我们要像素级清晰，不要模糊
        sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"0")

        # 读回真实输出尺寸（FULLSCREEN_DESKTOP 下通常等于面板分辨率）
        dw = sdl2.c_int(0)
        dh = sdl2.c_int(0)
        try:
            sdl2.SDL_GetRendererOutputSize(self.renderer, dw, dh)
            if dw.value > 0 and dh.value > 0:
                self.width = dw.value
                self.height = dh.value
        except Exception:
            pass
        self.virtual_w = self.width
        self.virtual_h = self.height
        self.line_px = self.width
        stage(f"7 输出尺寸 {self.width}x{self.height}")

        # ---- 先建立事件循环，再做任何渲染 ----
        #
        # SDL 的硬性要求：在窗口真正"进入事件循环"之前反复渲染输出，
        # 某些后端（尤其全屏 + 硬件加速）会访问尚未初始化的内部状态。
        # 这里先 pump 一轮事件，把窗口推入正常状态。
        ev = sdl2.SDL_Event()
        for _ in range(3):
            while sdl2.SDL_PollEvent(ev):
                pass
            sdl2.SDL_PumpEvents()
        stage("8 首轮事件泵完成")

        # 清一次黑，避免看到上一帧残留
        sdl2.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 255)
        stage("9 SetRenderDrawColor ok")
        sdl2.SDL_RenderClear(self.renderer)
        stage("10 RenderClear ok")
        sdl2.SDL_RenderPresent(self.renderer)
        stage("11 RenderPresent ok")

        self.opened = True
        stage("12 open() 完成")
        return self

    def info(self):
        return (f"SDL2 {self.width}x{self.height} bpp=32 "
                f"fullscreen_desktop 渲染器={'HW' if self.renderer else '-'}")

    # ---------- 绘制 ----------
    #
    # px() 仅供 sanity_write_test 之类的探针使用。
    # SDL 后端下它只更新一张内部 PIL 缓冲，不直接上屏 ——
    # 真正的上屏在 blit()。这样可以保持接口兼容而不破坏 SDL 的纹理模型。

    def px(self, x, y, rgb):
        # 记录到一张懒创建的缓冲，blit 时会一起送上去。
        if not hasattr(self, "_pxbuf"):
            try:
                from PIL import Image
                self._pxbuf = Image.new("RGB", (self.width, self.height),
                                        (0, 0, 0))
                self._pxdraw = None
            except Exception:
                self._pxbuf = None
        if getattr(self, "_pxbuf", None) is None:
            return
        if 0 <= x < self.width and 0 <= y < self.height:
            self._pxbuf.putpixel((x, y), (rgb >> 16 & 0xFF,
                                          rgb >> 8 & 0xFF,
                                          rgb & 0xFF))

    def fill(self, rgb):
        try:
            from PIL import Image
            img = Image.new("RGB", (self.width, self.height),
                            (rgb >> 16 & 0xFF, rgb >> 8 & 0xFF, rgb & 0xFF))
            self.blit(img, force=True)
        except Exception:
            pass

    def blit(self, img, force=False):
        """
        把 PIL Image 上屏。

        带变化检测：与上一帧完全相同则跳过（除非 force），
        配合 UI 的"只在必要时重绘"策略省电。
        """
        sdl2 = self._sdl2
        if sdl2 is None:
            raise SDLDisplayError("SDL 未初始化，先调用 open()")

        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))

        rgba = img.convert("RGBA")
        key = rgba.tobytes()
        if not force and key == self._last_key:
            return False
        self._last_key = key

        # ⚠️⚠️ 致命坑（曾经导致 SIGSEGV，rc=139，闪退且日志断在半路）：
        #
        #   SDL_CreateRGBSurfaceWithFormatFrom() **不拷贝数据**，
        #   它只是把传入的 buffer **包装成指针**。
        #
        #   如果直接写 SDL_CreateRGBSurfaceWithFormatFrom(rgba.tobytes(), ...)，
        #   这个 bytes 是**临时对象**，没有变量持有它 ——
        #   CPython 在调用返回后立刻就能回收它，
        #   而 SDL 仍拿着**悬空指针**去读像素 → SIGSEGV。
        #
        #   表现：程序在 blit 处段错误，boot.log 恰好断在
        #   "输出尺寸 640x480" 之后（下一行本该是上屏自检的结果）。
        #
        #   修复：用局部变量 buf 显式持有，直到 texture 建好为止。
        #   （probe-sdl.py 之所以没事，就是因为它用了 buf 变量。）
        buf = key  # ← 必须持有！Python 的引用计数是这里的唯一保障
        surface = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
            buf,
            self.width,
            self.height,
            32,
            self.width * 4,
            sdl2.SDL_PIXELFORMAT_RGBA32,
        )
        if not surface:
            raise SDLDisplayError(
                f"SDL_CreateRGBSurfaceWithFormatFrom 失败: {sdl2.SDL_GetError()}")
        try:
            # 每次都重建 texture。虽然可以复用并 SDL_UpdateTexture，
            # 但 PIL 的 tobytes() 每次都是新 buffer，复用需要保证
            # 尺寸不变并处理 pitch —— 重建更简单可靠，实测 30 FPS 完全够。
            #
            # 注意：SDL_CreateTextureFromSurface 会把像素**拷贝**进纹理，
            # 所以 texture 建好后 buf 就可以安全释放了。
            # 若走软件渲染器，SDL 仍可能引用 surface 数据 —— 所以
            # 这里用"建完再放"而不是提前 del。
            if self._texture:
                sdl2.SDL_DestroyTexture(self._texture)
                self._texture = None
            # 首帧打印阶段，便于定位段错误（后续帧不再刷日志）
            self._blit_n = getattr(self, "_blit_n", 0) + 1
            if self._blit_n <= 3:
                try:
                    import boot
                    boot.log(f"  [SDL阶段] blit#{self._blit_n} "
                             f"surface ok {self.width}x{self.height} "
                             f"{len(buf)}B")
                except Exception:
                    pass
            self._texture = sdl2.SDL_CreateTextureFromSurface(
                self.renderer, surface)
            if self._blit_n <= 3:
                try:
                    import boot
                    boot.log(f"  [SDL阶段] blit#{self._blit_n} texture "
                             f"{'ok' if self._texture else 'NULL'}")
                except Exception:
                    pass
        finally:
            sdl2.SDL_FreeSurface(surface)
            # 到这里 texture 已持有自己的像素副本，buf 可以放开了
            del buf

        if not self._texture:
            raise SDLDisplayError(
                f"SDL_CreateTextureFromSurface 失败: {sdl2.SDL_GetError()}")

        sdl2.SDL_RenderClear(self.renderer)
        sdl2.SDL_RenderCopy(self.renderer, self._texture, None, None)
        sdl2.SDL_RenderPresent(self.renderer)
        return True

    def pump_events(self):
        """
        处理 SDL 事件队列。

        SDL 要求事件队列被定期抽干，否则系统可能认为应用无响应。
        本应用自己的输入走 evdev（见 main.py 的 Input 类），
        这里只做最小必要的 pump。
        """
        sdl2 = self._sdl2
        if sdl2 is None:
            return
        ev = sdl2.SDL_Event()
        while sdl2.SDL_PollEvent(ev):
            pass

    # ---------- 关闭 ----------

    def close(self):
        sdl2 = self._sdl2
        if sdl2 is None:
            return
        # 退出前清黑，避免残留画面闪一下
        try:
            sdl2.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 255)
            sdl2.SDL_RenderClear(self.renderer)
            sdl2.SDL_RenderPresent(self.renderer)
        except Exception:
            pass
        for obj, destroy in (
                (self._texture, sdl2.SDL_DestroyTexture),
                (self.renderer, sdl2.SDL_DestroyRenderer),
                (self.window, sdl2.SDL_DestroyWindow)):
            try:
                if obj:
                    destroy(obj)
            except Exception:
                pass
        self._texture = None
        self.renderer = None
        self.window = None
        try:
            sdl2.SDL_QuitSubSystem(sdl2.SDL_INIT_VIDEO)
        except Exception:
            pass
        self.opened = False

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()
