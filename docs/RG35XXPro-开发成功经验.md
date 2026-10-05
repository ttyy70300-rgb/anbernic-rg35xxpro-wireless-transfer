# RG35XX Pro 第三方应用开发 · 成功经验手册

> 版本：v1.0
> 日期：2026-10-05
> 状态：**首战告捷，经验已验证** —— PocketTransfer 在真机上稳定运行 29.9 FPS，
>       进入 / 退出 / 再进入均正常
> 用途：**下次给这台掌机开发任何新程序，先读本文档。**
>       这里每一条都是从真机血案里换来的，不是推测。

---

## 〇、先记住这一条（其它都可以后看）

> ### 在这台机器上画图，**只有一条路：SDL2 的 `SDL_WINDOW_FULLSCREEN_DESKTOP`**。
> ### 裸写 `/dev/fb0` 一定会被别的图层盖住，你什么都看不见。

这不是"推荐做法"，是**物理约束**。原因见第二章。

---

## 一、结论速查表（TL;DR）

| 事项 | 结论 | 置信度 |
|---|---|---|
| **显示** | **必须走 SDL2**，`FULLSCREEN_DESKTOP` + Renderer + Texture | ⭐ 真机验证 |
| **不要做什么** | 不要裸 mmap `/dev/fb0` 当唯一显示手段 | ⭐ 真机验证 |
| **语言** | **Python 3.10**（固件自带），与原厂 5 个应用一致 | ⭐ 真机验证 |
| **图形库** | **Pillow 9.0.1** 画 → 转 SDL Surface → Texture → 上屏 | ⭐ 真机验证 |
| **窗口尺寸** | 输出恒为 **640×480**（面板物理分辨率，1:1 无缩放） | ⭐ 真机验证 |
| **不要碰 `sdl2.ext`** | 会加载坏掉的 SDL2_image，污染 SDL 状态 → 段错误 | ⭐ 真机验证 |
| **键盘输入** | 读 `/dev/input/event1`，按键在 `EV_KEY` | ⭐ 真机验证 |
| **摇杆/方向** | 在 `EV_ABS`（`ABS_RX`/`ABS_RY`），**不在 EV_KEY** | ⭐ 真机验证 |
| **ABS 负值** | 补码表示（`-3700` 读出来是 `4294963596`），必须转有符号 | ⭐ 真机验证 |
| **震动** | 写 `/sys/class/power_supply/axp2202-battery/moto`（1 开 / 0 关） | ⭐ 真机验证 |
| **字体** | `/mnt/vendor/bin/default.ttf`（原厂自带，可中文） | ⭐ 真机验证 |
| **系统 locale** | `zh_CN.UTF-8` —— 渲染中文**零配置** | ⭐ 真机验证 |
| **机型识别** | `/mnt/vendor/oem/board.ini` → `RG35xxPRO` | ⭐ 真机验证 |
| **固件版本** | `/mnt/vendor/oem/version.ini` → `20260522`（官方固件，非魔改） | ⭐ 真机验证 |
| **部署** | `Roms/APPS/<名>.sh` + 兄弟目录负载 + `APPS/Imgs/<名>.png` | ⭐ 真机验证 |
| **菜单占用屏幕时** | SDL 建窗会失败（**正常现象**），应检测并安静退出，不要报错 | ⭐ 真机验证 |
| **开发调试通道** | SSH（`root`/`root`），但**只推文件，不动系统** | 用户硬边界 |

> **完整实测值（含分区布局 / 挂载 / 时区 / SDL2 双版本）见 [`docs/固件信息核实.md`](固件信息核实.md)。**

---

## 二、核心原则 0：先搞清楚屏幕是"单画布"还是"多图层合成器"

**这是整个项目最关键的一次认知升级。搞错这一条，后面所有努力都白费。**

### 2.1 两种显示模型

```
【模型 A：单 framebuffer】—— 老式设备、多数教程假定
   你的程序 ──写像素──> /dev/fb0 ──> 屏幕
   你写什么，屏幕就显示什么。简单直接。

【模型 B：多图层合成器（DispEngine）】—— 本机属于这种 ★
   你的程序 ──写像素──> /dev/fb0        ┐
   别人的程序 ──写像素──> /dev/disp 图层 ┘──> 硬件合成 ──> 屏幕
                        ↑ 图层的 z-order 决定谁在上面
   你写在底层，别人写在顶层 → 你写的东西被完全盖住。
```

### 2.2 本机的铁证

RG35XX Pro（Allwinner H700）用的是 **Allwinner DispEngine**，设备节点 `/dev/disp`。

**证据 1** —— 原厂菜单 `dmenu.bin` 静态链接，字符串里直接有：

```
/dev/disp
/dev/ion
CdcIonAlloc err, ret %d
CdcIonGetFd map other dev's handle err
CdcIonMmap failed: %s
DisplayEngine Frequency: %ld Hz
```

它通过 `open("/dev/disp")` + `ioctl(DISP_LAYER_SET_CONFIG2)` + `ioctl(DISP_HWC_COMMIT)`
建立**硬件图层**，像素数据放在 `/dev/ion` 分配的显存里（**不在 `/dev/fb0` 内**）。

**证据 2** —— 把 `/dev/fb0` 整块 7MB 显存逐页扫一遍，**找不到 dmenu 画的"加载中"画面**。
（工具：`tools/probe-fb-whole.py`）

**证据 3** —— `dmesg` 里有 280+ 条 `disp_mgr_protect_reg_for_rcq`，说明 disp 驱动在持续管理图层。

**证据 4** —— 原厂 5 个 Python 应用**全部**用
`SDL_WINDOW_FULLSCREEN_DESKTOP`。SDL2 会 `open("/dev/disp")` 建**自己的新图层**并置于最顶层，
退出时销毁。所以它们天然盖过 dmenu 的残留图层。

### 2.3 dmenu 退出时**不做清理**（关键陷阱）

`dmenu.bin` 退出时**不调用** `DISP_BLANK` 释放图层 → **图层残留**，
持续把最后一帧（"加载中"）扫描输出到屏幕。

于是出现这个极其迷惑的现象：

> **你的程序在正常跑（写回自检全过、帧率稳定 29.9 FPS），
> 但屏幕上一直显示"加载中" —— 那是 dmenu 的残留图层，不是你的画面。**

我们在这个问题上卡了很久。程序"看起来卡住了"，实际上**运行得好好的**，
只是画的画在被别人盖住的底层。

### 2.4 识别信号（拿到一台陌生设备时怎么快速判断）

按顺序检查，任一条命中就高度怀疑是多图层合成器：

```bash
ls -l /dev/disp /dev/ion 2>/dev/null          # 存在 → 强烈怀疑
cat /proc/mounts | grep -i disp
dmesg | grep -i disp | head                   # 有 disp_mgr_* 日志 → 确认
strings <原厂程序> | grep -E '/dev/disp|/dev/ion|DISP_LAYER'   # 确认
```

**行动清单**：
1. **先试 SDL2**（最省事，SDL 帮你封装了 disp 的所有 ioctl）
2. 不要花时间优化裸 framebuffer 的写入性能 —— 方向错了
3. 如果必须裸写 fb0，得先自己 `DISP_BLANK` 清掉别人的图层 —— **但本用户明确禁止改动系统**，所以不可行

---

## 三、SDL2 通路：完整可用的最小实现

### 3.1 环境要求

```bash
export PYSDL2_DLL_PATH="/usr/lib"      # ★ 必须！原厂 5 个 .sh 都有这一行
export LD_LIBRARY_PATH="/usr/lib:/mnt/vendor/lib:${LD_LIBRARY_PATH:-}"
```

缺 `PYSDL2_DLL_PATH` 会导致 `import sdl2` 后找不到 libSDL2 → 秒退。

环境事实：

| 组件 | 版本/路径 |
|---|---|
| Python | 3.10.12（`/usr/bin/python3`） |
| pysdl2 | 0.9.17（`/usr/lib/python3/dist-packages/sdl2/`） |
| Pillow | 9.0.1 |
| libSDL2（**pysdl2 实际加载**） | `/usr/lib/aarch64-linux-gnu/libSDL2-2.0.so.0.2800.5` → **SDL 2.28.5** |
| libSDL2（原厂 dmenu 静态链接用） | `/usr/lib/libSDL2-2.0.so.0.12.0` → SDL 2.0.12 |
| 视频驱动 | `SDL_GetNumVideoDrivers()` → `b'mali'` + `b'dummy'`（只有 2 个） |

> ⚠️ **这台机器里有两个不同版本的 SDL2！** 实测 `SDL_GetVersion()` 返回 **2.28.5**
> （`ctypes.util.find_library('SDL2')` → `libSDL2-2.0.so.0` → 解析到 aarch64-linux-gnu 那份）。
> 原厂 `dmenu.bin` 静态链接的是旧的 2.0.12。**两者相差 12 年上游演进**，
> 排查渲染问题时务必先确认用的是哪一份。详见 `docs/固件信息核实.md` §4.2。

### 3.2 创建窗口（照抄，已验证）

```python
import sdl2

sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO)

# 关键：FULLSCREEN_DESKTOP 会走 /dev/disp 建顶层图层
flags = sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP | sdl2.SDL_WINDOW_SHOWN
title = b"PocketTransfer"              # ★ 必须由变量持有（见 3.5 悬空指针）
window = sdl2.SDL_CreateWindow(
    title,
    sdl2.SDL_WINDOWPOS_UNDEFINED,
    sdl2.SDL_WINDOWPOS_UNDEFINED,
    0, 0,                              # 全屏模式下尺寸被忽略
    flags,
)

# 渲染器：先硬件加速，失败回退软件
renderer = sdl2.SDL_CreateRenderer(window, -1, sdl2.SDL_RENDERER_ACCELERATED)
if not renderer:
    renderer = sdl2.SDL_CreateRenderer(window, -1, sdl2.SDL_RENDERER_SOFTWARE)

sdl2.SDL_SetHint(sdl2.SDL_HINT_RENDER_SCALE_QUALITY, b"0")   # 关插值，要像素级清晰
```

实测结果：`SDL_CreateWindow(FULLSCREEN_DESKTOP) ok`，
`SDL_CreateRenderer` **ACCELERATED 直接成功**（渲染器=HW），输出尺寸 **640×480**。

### 3.3 上屏：PIL Image → Texture

```python
def blit(self, img):
    rgba = img.convert("RGBA")
    key = rgba.tobytes()

    buf = key                          # ★★★ 必须持有，见 3.5
    surface = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
        buf, self.width, self.height,
        32, self.width * 4,
        sdl2.SDL_PIXELFORMAT_RGBA32,
    )
    try:
        if self._texture:
            sdl2.SDL_DestroyTexture(self._texture)
        self._texture = sdl2.SDL_CreateTextureFromSurface(self.renderer, surface)
    finally:
        sdl2.SDL_FreeSurface(surface)
        del buf                        # texture 已拷贝数据，此时可释放

    sdl2.SDL_RenderClear(self.renderer)
    sdl2.SDL_RenderCopy(self.renderer, self._texture, None, None)
    sdl2.SDL_RenderPresent(self.renderer)
```

**性能实测**：**29.9 FPS 稳定**（30 FPS 上限节流），CPU 占用可接受。

### 3.4 为什么不用 SDL 直绘 / SDL_gfx

我们已经有整套 Pillow 绘制代码（字体、抗锯齿、中文字形）。重写成 SDL_gfx 代价大且中文支持差。
**PIL 画 → 转 Surface → Texture** 这条桥接路与原厂 `clock/graphic.py` 的做法完全一致，是最省事的。

### 3.5 ⚠️ 三个必踩的坑（都是段错误级，都会让你调试半天）

#### 坑 1：`SDL_CreateRGBSurfaceWithFormatFrom` 收到临时对象 → SIGSEGV

```python
# ✗ 错误：rgba.tobytes() 是临时对象，函数返回后 CPython 立刻回收
surface = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(
    rgba.tobytes(), ...)               # ← 悬空指针！

# ✓ 正确：用局部变量托住
buf = rgba.tobytes()
surface = sdl2.SDL_CreateRGBSurfaceWithFormatFrom(buf, ...)
```

**为什么**：这个 C 函数**不拷贝数据**，只把 buffer 包成指针。
Python 的引用计数是这里的唯一保障 —— 没有变量持有，对象立刻被回收，SDL 拿着悬空指针读像素。

**症状**：`rc=139`（SIGSEGV），**打不出任何 Python traceback**，
日志恰好断在 `[SDL阶段] 7 输出尺寸 640x480` 之后（下一行本该是 blit 的结果）。

#### 坑 2：`import sdl2.ext` 会污染 SDL 状态

```
DLLWarning: OSError('/lib/aarch64-linux-gnu/libSDL2_image-2.0.so.0:
                    undefined symbol: SDL_roundf')
```

`import sdl2.ext` 会连带加载 SDL2_image / SDL2_ttf / SDL2_mixer。
**本机上 SDL2_image 是坏的**（缺 `SDL_roundf` 符号），加载失败会污染 SDL 内部状态，
导致后续渲染调用段错误。

**只 `import sdl2`，绝不 `import sdl2.ext`。**

#### 坑 3：首次渲染前必须先 pump 事件

SDL 的硬性要求：在窗口真正"进入事件循环"之前反复渲染输出，
某些后端（尤其全屏 + 硬件加速）会访问尚未初始化的内部状态。

```python
ev = sdl2.SDL_Event()
for _ in range(3):
    while sdl2.SDL_PollEvent(ev):
        pass
    sdl2.SDL_PumpEvents()
# 然后才 RenderClear / RenderPresent
```

### 3.6 细粒度阶段日志（SDL 段错误的唯一调试手段）

**SDL 层段错误时进程直接消失，traceback 打不出来。**
唯一可靠的定位手段是：**每一步都 `write + fsync` 落盘，看最后一条日志停在哪。**

```python
def stage(msg):
    with open(LOG, "a") as f:
        f.write(f"  [SDL阶段] {msg}\n")
        f.flush()
        os.fsync(f.fileno())           # ★ 必须 fsync，否则崩溃时缓冲区里的日志会丢
```

实测成功路径的 12 个阶段：

```
 1 SDL_Init 前置
 2 SDL_Init(VIDEO) ok
 3 SDL_CreateWindow 前置
 4 SDL_CreateWindow(FULLSCREEN_DESKTOP) ok
 5 SDL_CreateRenderer 前置
 6 SDL_CreateRenderer ok
 7 输出尺寸 640x480          ← 老版本的崩溃就断在这行之后
 8 首轮事件泵完成
 9 SetRenderDrawColor ok
10 RenderClear ok
11 RenderPresent ok
12 open() 完成
```

---

## 四、菜单占用屏幕时，SDL 建窗失败是**正常的**

### 4.1 现象

如果在 dmenu 还在运行时启动你的程序：

```
SDL_CreateWindow NULL err=b"mali-fbdev: Can't create EGL window surface"
```

**这不是 bug，是正常现象** —— dmenu 正持有 `/dev/disp` 图层（此时 fb 是 `1280×1024 16bpp` 菜单模式），
SDL 抢不到。

### 4.2 正确处理：检测 + 安静退出

**不要在菜单占用时 `die()` 弹错误屏** —— 用户会看到一个无意义的错误，以为程序坏了。

```python
def _dmenu_running():
    """遍历 /proc/*/cmdline 找原厂菜单进程。"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except Exception:
            continue
        if any(k in cmd for k in ("dmenu.bin", "muos1.bin", "muos2.bin")):
            return True, cmd
    return False, None


def bring_up_display():
    try:
        import sdl_display
        f = sdl_display.SDLDisplay(); f.open()
        return f
    except Exception:
        pass
    busy, who = _dmenu_running()
    if busy:
        log(f"检测到原厂菜单正在运行（{who}），屏幕被它占用。不是错误 —— 安静退出。")
        return None                    # ← 关键：返回 None，不 die
    # 兜底：裸 framebuffer
    ...
```

`main()` 里：

```python
f = bring_up_display()
if f is None:
    log("菜单占用屏幕，本次不启动 UI，已安静退出")
    return 0
```

### 4.3 正常启动时序（不用管）

从菜单点进应用时，`dmenu.bin` **已经退出了**（它把命令写进 `/tmp/.next` 后退出，
`dmenu_ln` 再执行 `sh /tmp/.next`）。所以正常路径下 SDL 能顺利拿到 disp。

---

## 五、输入：按键在 EV_KEY，**摇杆/方向在 EV_ABS**

### 5.1 设备节点

| 节点 | 内容 |
|---|---|
| `event0` | 仅 `KEY 116 (POWER)` |
| **`event1`** | **主力**：所有按键 + 摇杆 |
| `event2` | 另一套映射，平时不用管 |

结构：24 字节 `struct llHHI`（`time(8) + type(2) + code(2) + value(4)`）——照抄原厂 `input.py`。

### 5.2 `event1` 能力（实测 `EVIOCGBIT`）

**`EV_KEY`**：

| code | 名称 | code | 名称 |
|---|---|---|---|
| 304 | A | 310 | SELECT |
| 305 | B | 311 | START |
| 306 | Y | 312 | MENUF |
| 307 | X | 314 | L2 |
| 308 | L1 | 315 | R2 |
| 309 | R1 | 114 / 115 | 音量 + / - |

**`EV_ABS`**（★ 方向在这里！）：

| code | 含义 |
|---|---|
| 2 | ABS_Z |
| 3 | **ABS_RX**（摇杆水平） |
| 4 | **ABS_RY**（摇杆垂直） |
| 5 | ABS_RZ |
| 16 | ABS_HAT0X |
| 17 | ABS_HAT0Y |

### 5.3 两个必须处理的细节

**① 负值是补码**：

```python
v = value - (1 << 32) if value >= (1 << 31) else value
# 实测 4294963596 → -3700
```

值域约 **±3700**，中心 0。

**② 摇杆会持续发同值事件，必须去重**：

```python
STICK_DEADZONE = 800

def axis_direction(axis_code, value):
    if value > STICK_DEADZONE:   sign = 1
    elif value < -STICK_DEADZONE: sign = -1
    else:
        return None
    if axis_code in (3, 16):  return "右" if sign > 0 else "左"
    if axis_code in (4, 17):  return "下" if sign > 0 else "上"
    return None

# 主循环里记录上一次方向，只有变化时才产生事件
prev = self._axis_state.get(code)
if name != prev:
    self._axis_state[code] = name
    if name is not None:
        emit(("axis", name, (code, v)))
```

### 5.4 血泪教训

**不要靠"猜"来映射按键。** 我们一开始按常见约定把 `code 16/17` 当方向键（那是 ABS 轴号，
不是按键），又把 ABS 事件整个丢掉 —— 结果用户反馈"拨摇杆上下左右没反应"。

**诊断工具**：`tools/probe-input.py` —— 只读枚举 evdev 能力（`EVIOCGBIT`）+ 监听 N 秒
原样打印每个事件（type/code/value + 人话解读）。**新设备上手第一步就跑它。**

---

## 六、外设接口

| 外设 | 接口 | 用法 |
|---|---|---|
| **震动马达** | `/sys/class/power_supply/axp2202-battery/moto` | 写 `1` 开，`0` 关 |
| **字体** | `/mnt/vendor/bin/default.ttf` | 原厂自带，支持中文（Pillow `ImageFont.truetype`） |
| **机型** | `/mnt/vendor/oem/board.ini` | 读到 `RG35xxPRO`（映射值 9） |
| **电池** | `/sys/class/power_supply/axp2202-battery/` | `capacity` 等 |

---

## 七、部署契约（必须照抄）

### 7.1 目录结构

```
Roms/APPS/
├── <AppName>.sh              # ★ 启动脚本，必须在 APPS 根目录
├── <AppName>/                # 负载目录（启动脚本的兄弟目录）
│   ├── main.py
│   ├── ...
│   └── boot.log              # 运行时日志（脚本自动追加）
└── Imgs/
    └── <AppName>.png         # ★ 菜单图标，是 APPS/Imgs/ 不是 Roms/Imgs/
```

> ⚠️ **写成 `Roms/APPS/<AppName>/launcher.sh` = 菜单里根本不出现。**
> 原厂启动器只列出 `Roms/APPS/*.sh`，**目录不列为应用**。

### 7.2 启动脚本模板（已验证可用）

```bash
#!/bin/bash
set -u
progdir="$(cd "$(dirname "$0")" && pwd)"
basedir="$(cd "$progdir/../.." && pwd)"    # Roms/APPS -> 卡根
APPDIR="$progdir/<AppName>"

export BASE_PATH="$basedir"                 # 卡根（程序据此认定管哪张卡）
export PYSDL2_DLL_PATH="/usr/lib"           # ★ 必须
export LD_LIBRARY_PATH="/usr/lib:/mnt/vendor/lib:${LD_LIBRARY_PATH:-}"

# 日志：追加 + 轮转（不要用 exec > 截断，会丢历史）
LOG="$APPDIR/run.log"
[ -f "$LOG" ] && [ $(stat -c%s "$LOG") -gt 262144 ] && mv "$LOG" "$LOG.1"

# 自检：失败时明确报错，不静默退出
[ -d "$APPDIR" ]         || { echo "缺应用目录"; exit 1; }
[ -f "$APPDIR/main.py" ] || { echo "缺 main.py";   exit 1; }

PY="$(command -v python3)" || { echo "无 python3"; exit 1; }

"$PY" "$APPDIR/main.py" >> "$LOG" 2>&1
exit 0
```

### 7.3 启动契约（理解它才能正确退出）

```
loadapp.sh(222)                dmenu_ln                      你的应用
     │                             │                             │
     ├── while [ -f $RunBin ] ────>│                             │
     │                             ├── dmenu.bin（前台，占屏）   │
     │                             │       │                     │
     │                             │   用户选中 → 写 /tmp/.next  │
     │                             │       │ dmenu 退出          │
     │                             │       └── sh /tmp/.next ───>│ 启动
     │                             │                             │
     │                             │<──── 应用退出 ──────────────┘
     │                             └── 循环重开 dmenu（菜单自然回来）
```

**结论**：
1. **应用退出后 dmenu 自动重启，菜单自然回来** —— 不需要你做任何清理
2. dmenu 的关闭信号是 **SIGUSR1**（`/etc/init.d/launcher.sh:45`），SIGINT 无效
3. **绝不能 kill dmenu** —— 来不及初始化会黑屏（我们踩过，把用户掌机搞黑屏了）

---

## 八、操作纪律（用户硬边界，绝不可越）

用户明确规定，**开发期间只允许通过 SSH 往掌机推文件，不改动系统其他任何内容**：

| 禁止 | 原因 |
|---|---|
| ❌ kill 任何进程（尤其 dmenu / dmenu_ln / loadapp） | 会把掌机搞黑屏 |
| ❌ 写 `/tmp/.next` | 那是 dmenu 的私有协议 |
| ❌ 切换显示模式（`FBIOPUT_VSCREENINFO`） | 会 SIGBUS，见第九章 |
| ❌ remount 文件系统 | 用户自己修卡 |
| ❌ 建软链、写 sysfs | 改动系统状态 |
| ❌ 在掌机上编译 | 没必要，且风险高 |

**应用一律由用户在掌机上手动点击菜单启动。** 自动化脚本只负责推文件。

---

## 九、反面教材：我们走过的三条死路

### 死路 1：`FBIOPUT_VSCREENINFO` 强切显示模式 → SIGBUS

上一版启动时调 `setmode.py` 把显示切成 640×480 32bpp。实测：

| 步骤 | 结果 |
|---|---|
| `FBIOPUT_VSCREENINFO` 返回 | 0（**成功**） |
| `FBIOPUT_VSCREENINFO` 后 `FBIOGET_VSCREENINFO` 读回 | **返回新值 640×480 32bpp** |
| `cat /sys/class/graphics/fb0/virtual_size` | **仍是旧值 1280,1024** |

→ **写回被接受了，但驱动实际没换模式。** 显示时序由 disp 驱动独立控制，
改 `fb_var_screeninfo` 只是改了软件视角。

**后果链**：

```
main.py 读到 640×480
   → mmap 长度 = 640×4×480 = 1,228,800 字节
   → 但驱动实际仍是 1280×1024，stride = 2560（远大于 640×4）
   → 按 line_px 步进写第 y 行，目标偏移超出映射范围
   → SIGBUS → 进程被内核立刻杀死
   → 用户看到：点进去 → 黑一下 → 回到菜单
```

**教训**：**永不切换显示模式。** 按运行时读到的参数作画就好。

### 死路 2：裸写 `/dev/fb0`（本项目最大的弯路）

花了大量时间优化 fb0 直写性能，程序跑得完美（29.9 FPS、写回自检 0 异常），
但**用户屏幕上一直是"加载中"**。原因见第二章 —— 被 dmenu 残留图层完全盖住。

**教训**：**先确认显示模型，再写一行渲染代码。**

### 死路 3：`v1.3` 文档的错误结论（源自探测方法不当）

`技术栈决策v1.3.md` 曾写下：

> "EGL/GPU 路线彻底不通" / "任何依赖 EGL 的方案（SDL2、pygame…）都会失败" /
> "唯一可靠路径是直接操作 `/dev/fb0`"

**这个结论是错的**，错在探测方法：

| 当时的做法 | 为什么得出错误结论 |
|---|---|
| 在 **SSH 会话里**跑 SDL 测试 | SSH 无 TTY/无 disp 上下文，EGL 初始化必然失败 |
| 用 `ldd dmenu.bin` 看依赖 | dmenu 是**静态链接**的，`ldd` 返回"不是动态可执行文件"，什么也看不到 → 误判它走 SDL1.2 直涂 fb |
| 没检查 `/dev/disp`、`/dev/ion` | 漏掉了多图层合成器这个决定性事实 |

**正确做法**：`strings dmenu.bin | grep -E '/dev/disp|/dev/ion'`，
以及**从掌机菜单里**（不是 SSH 里）启动 SDL 程序实测。

**教训**：
1. **GUI 程序必须在真实启动路径上测**，SSH 里测不算数
2. `ldd` 对静态程序无效，要看 `strings`
3. 文档结论被推翻时，**要显式标注作废并写清原因**，不要让后人再踩

---

## 十、调试工具清单（可直接复用）

| 工具 | 用途 |
|---|---|
| `tools/probe-input.py` | 只读枚举 evdev 能力 + 实时打印事件（**新设备第一步**） |
| `tools/probe-fb-whole.py` | 只读扫整块显存，判断"屏幕内容在不在 fb0 里"（**判断显示模型的关键**） |
| `tools/probe-fb-deep.py` | 逐字节 dump `fb_var/fix_screeninfo`，校准结构体偏移 |
| `tools/probe-sdl.py` | SDL2 通路最小验证（独立进程，5 秒自退，画彩色横条） |
| `tools/selftest.py` | 静态自检（**当前 116 项**），推文件前必跑 |
| `tools/deploy-only.py` | **只推文件 + 装启动脚本，不启动任何东西** |

### 部署标准流程

```bash
# 1. 本地自检
python tools/selftest.py

# 2. 看要传什么
python tools/deploy-only.py --dry

# 3. 部署（不启动）
python tools/deploy-only.py

# 4. 校验 MD5（本地 vs 掌机必须逐字节一致）
#    注意：SFTP 子系统 chroot 到 /mnt/mmc，用 shell 侧 md5sum 更可靠

# 5. 用户在掌机上手动点菜单启动
```

---

## 十一、环境坑（Windows 开发机侧）

| 坑 | 现象 | 解法 |
|---|---|---|
| **Git Bash 路径转换** | `/usr/bin/python3` 被转成 `C:/.../PortableGit/usr/bin/python3` | 命令前加 `MSYS_NO_PATHCONV=1` |
| **SFTP chroot** | `sftp.put("/tmp/x")` 报 `FileNotFoundError` | SFTP 根被 chroot 到 `/mnt/mmc`；先用 shell 通道 base64 分块写 |
| **paramiko 超时** | `channel.recv()` 无数据时立刻抛 `socket.timeout` | 用 `recv_ready()`/`recv_stderr_ready()`/`exit_status_ready()` 轮询 |
| **Bash 引号地狱** | 复杂远端命令引号被打乱 | 写独立 `.py` 文件走 paramiko（`tools/rr.py` 就是干这个的） |

**本地 Python**：`C:/Users/Guo/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（装了 paramiko）

---

## 十二、新项目启动检查清单（照着做）

给这台掌机开新程序时，按顺序走：

```
□ 1. 确认显示模型
      ls /dev/disp /dev/ion；strings 原厂程序 | grep disp
      → 有 disp = 多图层合成器 → 必须走 SDL2

□ 2. 跑 tools/probe-input.py，抓全按键与摇杆事件码
      （不要猜！ABS 在哪个轴、值域多少、死区多大，全靠它）

□ 3. 写最小 SDL2 验证程序（画色块 + 打日志）
      跑 tools/probe-sdl.py 那套，确认 CreateWindow 成功

□ 4. 从掌机菜单（不是 SSH）启动，确认画面能看见

□ 5. 按 7.1/7.2 建部署结构 + 启动脚本

□ 6. 实现：PIL 画 → blit 到 SDL Texture
      记得 buf 持有、不 import sdl2.ext、先 pump 事件

□ 7. 退出路径测试：进入 → 退出 → 再进入（★ 必须测，我们在这翻过车）

□ 8. 菜单占用检测（_dmenu_running）避免无意义报错
```

---

## 十三、一句话总结

| 维度 | 结论 |
|---|---|
| **显示** | 只有 SDL2 `FULLSCREEN_DESKTOP` 一条路；裸 fb0 必被 disp 图层盖住 |
| **绘图** | PIL 画 → RGBA bytes → SDL Surface → Texture；29.9 FPS 够用 |
| **输入** | 按键在 EV_KEY，摇杆在 EV_ABS（补码、要去重） |
| **部署** | `APPS/*.sh` + 兄弟目录 + `APPS/Imgs/*.png`；脚本必须 export `PYSDL2_DLL_PATH` |
| **纪律** | 只推文件、不动系统、不 kill dmenu、不切显示模式 |
| **方法** | 先探明显示模型再写代码；GUI 必须在真机菜单路径上测；文档结论作废要显式标注 |

**最贵的一课**：*「先搞清楚屏幕是单画布还是多图层合成器」* ——
这个问题没答对之前，所有代码写得再好都是白费。

---

*文档结束。相关文档：`开发规划.md`、`交接文档.md`、`技术栈决策v1.3.md`（部分结论已作废，见本文第九章死路 3）*
