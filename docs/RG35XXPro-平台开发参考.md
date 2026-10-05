# RG35XX Pro 平台开发参考（应用 / 游戏通用）

> **这份文档是什么**：从本项目 12 篇文档里提炼出的一站式平台参考，
> 专门服务"在这台掌机上开发**下一个应用或游戏**"的场景。
> 只收录**实机验证过的事实**；少数未验证项会显式标注「待验证」。
> 设备事实与本文冲突时，以 [`固件信息核实.md`](固件信息核实.md)（事实基准）为准。

---

## 一、机器基本信息

### 1.1 硬件规格

| 项 | 参数 |
|---|---|
| 设备 | Anbernic RG35XX Pro（`/mnt/vendor/oem/board.ini` → `RG35xxPRO`）|
| SoC | Allwinner H700（sun50i，4× Cortex-A53 @ 1.512GHz，aarch64）|
| GPU | Mali-G31 MP2（mali_kbase.ko；**无 Vulkan**，Mesa 支持有限）|
| 内存 | 1 GB LPDDR4 |
| 屏幕 | 3.5" IPS，**640×480**，4:3，60Hz，**无触摸** |
| 音频 | 单声道扬声器 + 3.5mm 耳机孔，**无麦克风**；音量键 EV_KEY 114/115 |
| 存储 | 双 TF 卡槽，无内部存储。TF1（系统卡）`/mnt/mmc`，TF2（游戏卡）`/mnt/sdcard` |
| 震动 | 有马达（sysfs 接口见第四节）|
| 电池 | 3200 mAh（`/sys/class/power_supply/axp2202-battery/`）|
| 网络 | WiFi 5 (802.11ac)，Bluetooth 4.2 |
| 按键 | A/B/X/Y、L1/R1、L2/R2、SELECT/START/MENU、音量±、**双摇杆 + 十字键** |

### 1.2 固件与系统

| 项 | 值 |
|---|---|
| 固件 | **Anbernic 官方 `20260522`**（2026-05-22 构建，`/mnt/vendor/oem/version.ini`）|
| 系统 | **Ubuntu 22.04 LTS (Jammy)**，glibc 2.35，systemd 249 |
| 内核 | Linux **4.9.170** `#32 SMP PREEMPT`（Linaro GCC 5.3.1 构建）|
| locale | **`zh_CN.UTF-8`** —— 中文渲染零配置 |
| 时区 | **`Etc/UTC`** —— 显示"本地时间"要自己做换算 |
| fstab | 空（挂载由厂商脚本完成，**别指望改 fstab**）|
| hostname | `ANBERNIC` |
| SSH | 原厂自带：设置 → Modify System Tools → Temporary SSH Server，`root`/`root` |
| Samba | 原厂自带：Temporary SAMBA Server |

### 1.3 自带软件栈（开发直接白嫖，零依赖分发）

| 组件 | 版本 | 备注 |
|---|---|---|
| **Python** | **3.10.12**（`/usr/bin/python3`）| ★ 原厂 5 个应用全是 Python，直接推 .py 就能跑 |
| Pillow | 9.0.1 | 绘图主力（字体/抗锯齿/中文）|
| pysdl2 | 0.9.17 | 显示通路 |
| **libSDL2（pysdl2 实际加载）** | **2.28.5** | `/usr/lib/aarch64-linux-gnu/libSDL2-2.0.so.0.2800.5` |
| libSDL2（原厂 dmenu 静态链接） | 2.0.12 | `/usr/lib/libSDL2-2.0.so.0.12.0` |
| gcc | 11.4.0 aarch64 | 有，但 Python 路线用不上 |
| 中文字体 | `/mnt/vendor/bin/default.ttf`（9.6 MB 完整字体）| **无需子集化、无需自带字体** |
| dpkg/apt | 1.21.1 / 可用（清华源）| 1284 个包是官方固件正常状态 |

> ⚠️ **机内有两个版本的 SDL2**。Python 程序加载的是 **2.28.5**；
> 排查渲染问题先确认用的是哪一份。C 程序动态链接时 `-L` 路径要选对。

### 1.4 分区与挂载

| 分区 | 挂载点 | 大小 | 文件系统 | 用途 |
|---|---|---|---|---|
| mmcblk0p1 | `/mnt/mmc` | 44 GB | **vfat** | TF1：ROM/APPS（应用、游戏都放这）|
| mmcblk0p2~4 | — | — | raw | uboot / env / boot |
| mmcblk0p5 | `/` | 7 GB | ext4 | 系统根 |
| mmcblk0p6 | `/mnt/vendor` | 4 GB | ext4 | 厂商分区（字体、dmenu、资源）|
| mmcblk0p7 | `/mnt/data` | 2.5 GB | ext4 | 用户数据 |
| TF2 | `/mnt/sdcard` | — | — | 游戏卡 |

> ⚠️ **`/mnt/mmc` 是 vfat**（`iocharset=iso8859-1` + `utf8`）：
> 无符号链接、无权限位、不区分大小写。放 Linux 专属的东西（软链等）会失败。

---

## 二、显示子系统（写渲染代码之前必须读）

### 2.1 唯一重要的事实

> **这台机器不是"单画布 framebuffer"，而是 Allwinner DispEngine
> 多图层合成器（`/dev/disp` + `/dev/ion`）。`/dev/fb0` 只是最底层图层。**

后果：**裸写 fb0 的程序会"完美运行"（自检全过、29.9 FPS 稳定），
但用户屏幕上什么也看不见** —— 被上层残留图层完全盖住。
本项目最大的弯路就是这条（详见开发成功经验第九章"死路 2"）。

**结论：显示只有一条路 —— SDL2，`SDL_WINDOW_FULLSCREEN_DESKTOP`
（它内部 open `/dev/disp` 建顶层图层）。** 原厂 dmenu 和我们都是这么干的。

### 2.2 环境变量（.sh 里必须 export）

```bash
export PYSDL2_DLL_PATH="/usr/lib"        # ★ 缺了 import sdl2 直接秒退
export LD_LIBRARY_PATH="/usr/lib:/mnt/vendor/lib:${LD_LIBRARY_PATH:-}"
```

### 2.3 上屏管线（已验证，29.9 FPS 稳定）

```
PIL Image 绘制（中文/抗锯齿/圆角随便画）
  → img.convert("RGBA").tobytes()
  → SDL_CreateRGBSurfaceWithFormatFrom(buf, ...)
  → SDL_CreateTextureFromSurface / UpdateTexture
  → SDL_RenderCopy → RenderPresent
```

- 建窗：`SDL_Init(VIDEO)` → `SDL_CreateWindow(..., 0, 0, FULLSCREEN_DESKTOP | SHOWN)`
  → Renderer 先 `ACCELERATED`（实测直接成功）失败再 `SOFTWARE`
- 输出尺寸恒为 **640×480**（面板物理分辨率 1:1，无缩放）
- `SDL_SetHint(SDL_HINT_RENDER_SCALE_QUALITY, b"0")` 关插值，像素风必备
- 视频驱动只有 `b'mali'` + `b'dummy'` 两个

### 2.4 三个段错误级必踩坑（全是真机验证过的）

| # | 坑 | 症状 | 解法 |
|---|---|---|---|
| 1 | `SDL_CreateRGBSurfaceWithFormatFrom` 收临时对象 | SIGSEGV(rc=139)，**无 Python traceback** | `buf = img.tobytes()` 先赋局部变量再传入（C 函数不拷贝数据）|
| 2 | `import sdl2.ext` | 加载坏的 SDL2_image（缺 `SDL_roundf`）污染 SDL 状态 → 后续渲染段错误 | **只 `import sdl2`，永远别碰 `sdl2.ext`** |
| 3 | 首次渲染前没 pump 事件 | 全屏+硬件加速下访问未初始化内部状态 | 渲染前循环 3 次：`SDL_PollEvent` 清空 + `SDL_PumpEvents()` |

另有一个悬空指针变种：窗口 **title 必须用变量持有**（`title = b"..."` 传变量，
不能传内联字面量进 `SDL_CreateWindow` 后让变量消失）。

### 2.5 菜单占用屏幕时，SDL 建窗失败是**正常的**

原厂 dmenu 在前台时，你的程序 `SDL_CreateWindow` 会失败。
正确处理：检测（如连续 3 次尝试失败）→ **安静退出**（不报错不弹窗）——
用户从菜单点进来时 dmenu 正在收尾，稍后再进就成功了。
GUI 程序**必须在真实启动路径（掌机菜单）上测试**，SSH 里测不算数
（SSH 无 disp 上下文，EGL/SDL 初始化必然失败——我们据此写过错误结论，后来全部推翻）。

### 2.6 休眠与帧率

- 系统默认约 1 分钟无操作休眠并断网；**我们的应用在前台运行期间未触发休眠**
  （PocketTransfer 长时间挂机连接不断），机制未深究 —— 游戏若有问题再回来补
- PIL→Texture 管线实测 **29.9 FPS**（30 FPS 节流），2D 游戏/像素风足够
- 性能经验：全屏 32bpp 逐行搬运 ~0.1s（掌机约 0.6~1.0s 级别的是 16bpp 逐通道重排）；
  **必须做"变化检测"** —— 与上一帧逐字节比对，没变化直接跳过重绘
  （UI 类应用只在状态改变时重绘；游戏每帧都变则不适用此优化）

### 2.7 绝对不要做的显示操作

- ❌ `FBIOPUT_VSCREENINFO` 切显示模式 → **SIGBUS**（写回"成功"但驱动不认，
  mmap 长度和实际 stride 对不上，内核直接杀进程，用户看到"点进去黑一下回菜单"）
- ❌ 裸 mmap `/dev/fb0` 当显示手段（被 disp 图层盖住，白写）
- ❌ kill dmenu / dmenu_ln / loadapp（来不及重新初始化会黑屏）

---

## 三、输入系统

### 3.1 设备节点

| 节点 | 内容 |
|---|---|
| `event0` | 只有 POWER 键 |
| **`event1`** | **主力**：全部按键 + 双摇杆 + 十字键 |
| `event2` | 另一套映射，一般不用 |

事件结构 24 字节：`struct.unpack("qqHHi")` → time(8) + type(2) + code(2) + value(4)。

### 3.2 按键（EV_KEY，value 1=按下 0=抬起）

| code | 键 | code | 键 |
|---|---|---|---|
| 304 | A | 310 | SELECT |
| 305 | B | 311 | START |
| 306 | Y | 312 | MENU |
| 307 | X | 314 | L2 |
| 308 | L1 | 315 | R2 |
| 309 | R1 | 114/115 | 音量 −/＋ |

### 3.3 摇杆与十字键（EV_ABS，★ 方向在这里，不在 EV_KEY）

实机标定结论（`handheld/ui.py` 的 `AXIS_ROLE_HINT`）：

| 轴 code | 角色 | 说明 |
|---|---|---|
| 2 / 3 | **左摇杆** 垂直 / 水平 | 值域约 ±3700，中心 0 |
| 4 / 5 | **右摇杆** 垂直 / 水平 | 同上 |
| 16 / 17 | **十字键** HAT0X / HAT0Y | value = −1/0/1 |
| 18 / 19 | HAT1X / HAT1Y | 预留 |

两个必须处理的细节：

```python
# ① 负值是补码：直接读出来 4294963596 其实是 -3700
v = value - (1 << 32) if value >= (1 << 31) else value

# ② 摇杆会持续发同值事件：必须死区 + 去重
STICK_DEADZONE = 800
# 主循环记录上次方向，只有变化时才产生"移动"事件
```

### 3.4 铁律

> **不要靠"猜"来映射按键/摇杆。** 我们按常见约定猜错过（把轴当键、
> 把方向整个丢掉），用户实测"摇杆没反应"。
> 新设备上手第一步：`tools/probe-input.py`（只读枚举 EVIOCGBIT 能力 +
> 实时打印事件的人话解读）。方向极性拿不准时用**引导式标定**
> （PocketTransfer 诊断页按 A 那套：屏幕提示方向 → 用户拨杆 → 自动记录）。

---

## 四、外设与其它接口

| 外设 | 接口 | 用法 |
|---|---|---|
| **震动马达** | `/sys/class/power_supply/axp2202-battery/moto` | 写 `1` 开 / `0` 关（操作反馈好用，别太频繁）|
| **中文字体** | `/mnt/vendor/bin/default.ttf` | Pillow `ImageFont.truetype` 直接用 |
| 电池 | `/sys/class/power_supply/axp2202-battery/capacity` | 读百分比 |
| 机型标识 | `/mnt/vendor/oem/board.ini` | `RG35xxPRO` |
| 音频 | 硬件有扬声器+耳机孔；**软件通路未在项目中使用，待验证** | ⚠️ 已知 `import sdl2.ext` 连带的 SDL2_image 是坏的（缺 `SDL_roundf`）；SDL2_mixer/SDL2_ttf 未测。做游戏音频前先写个最小验证程序单独测 |

---

## 五、应用部署机制（原厂 launcher 契约，必须照抄）

### 5.1 目录结构

```
<卡根>/Roms/APPS/
├── <AppName>.sh              # ★ 启动脚本必须在 APPS 根目录
├── <AppName>/                # 负载目录（启动脚本的兄弟目录）
│   ├── main.py
│   └── boot.log              # 运行日志（脚本追加 + 轮转）
└── Imgs/
    └── <AppName>.png         # ★ 菜单图标（是 APPS/Imgs/，不是 Roms/Imgs/）
```

> ⚠️ **launcher 只扫描 `Roms/APPS/` 下的 `*.sh`，目录本身不列为应用**。
> 写成 `Roms/APPS/<名>/launcher.sh` = 菜单里根本不出现。
> 图标规格 128×128 PNG。仓库里的 [`APPS/`](../APPS/) 就是一个完整示例。

### 5.2 启动脚本模板（已验证）

```bash
#!/bin/bash
set -u
progdir="$(cd "$(dirname "$0")" && pwd)"
basedir="$(cd "$progdir/../.." && pwd)"      # Roms/APPS → 卡根
APPDIR="$progdir/<AppName>"

export BASE_PATH="$basedir"                  # 卡根（程序据此认定管哪张卡）
export PYSDL2_DLL_PATH="/usr/lib"            # ★ 必须
export LD_LIBRARY_PATH="/usr/lib:/mnt/vendor/lib:${LD_LIBRARY_PATH:-}"

# 日志：追加 + 超过 256KB 轮转（不要 exec > 截断，会丢历史）
LOG="$APPDIR/boot.log"
[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 262144 ] && mv -f "$LOG" "$LOG.old"

# 自检失败要留痕，不静默
[ -f "$APPDIR/main.py" ] || { echo "缺 main.py" > /dev/console 2>/dev/null; exit 1; }

PY="$(command -v python3)"
"$PY" "$APPDIR/main.py" >> "$LOG" 2>&1
exit 0
```

> ⚠️ `.sh` 必须是 **LF 换行**（Windows 上编辑过变 CRLF 掌机就跑不动了）。
> 本仓库用 `.gitattributes` 锁了 `*.sh eol=lf`，新仓库照做。

### 5.3 启动/退出契约

```
systemd → /etc/init.d/launcher.sh → loadapp.sh → dmenu_ln
   dmenu.bin 前台占屏 → 用户选中 → 命令写入 /tmp/.next → dmenu 退出
   → dmenu_ln 执行 sh /tmp/.next（你的应用）
   → 应用退出 → while 循环自动重启 dmenu（菜单自然回来）
```

1. **应用退出后菜单自动回来，不需要你做任何清理**
2. dmenu 的关闭信号是 **SIGUSR1**（SIGINT 无效）
3. **绝不能 kill dmenu**（黑屏事故我们出过一次）
4. 原厂把应用 stdout/stderr 丢进 /dev/null → **自己写日志**，否则崩溃无痕迹
5. `$0` 可能是 `/tmp/.next`，脚本里路径全部用 `dirname "$0"` 自算绝对路径

---

## 六、开发纪律（本项目硬边界，建议沿用）

| 禁止 | 原因 |
|---|---|
| ❌ kill 任何进程 | 黑屏事故 |
| ❌ 写 `/tmp/.next` | dmenu 私有协议 |
| ❌ 切显示模式（FBIOPUT_*） | SIGBUS |
| ❌ remount / 建软链 / 写 sysfs（震动口除外）| 改动系统状态 |
| ❌ 在掌机上编译 | 没必要且风险高 |
| ❌ SSH 里跑 GUI 测试下结论 | 无 disp 上下文，结论必然错误 |

**部署 = 只推文件 + 用户手动点菜单启动。** 自动化到此为止。

---

## 七、网络与联机

- WiFi 5 / 蓝牙 4.2；休眠会断网（应用前台运行期间未触发休眠，见 2.6）
- SSH / Samba 都是**临时服务**，菜单里手动开
- 想做"PC ↔ 掌机"类应用：直接复用 PocketTransfer 的模型 ——
  **UDP 广播自动发现（掌机应答机型/IP/端口）+ TCP 自定义协议（包头
  `MAGIC(2B)+OP(2B)+LEN(4B)` 大端）+ 掌机侧确认门**。
  完整协议与实现见 [`阶段2联调手册.md`](阶段2联调手册.md) 三、四节
- TF 卡是 vfat：没有权限位/软链/大小写敏感，文件时间用 mtime

---

## 八、给游戏开发的补充清单

| 话题 | 现状与建议 |
|---|---|
| 分辨率/比例 | 640×480、4:3、无触摸 → 像素风 2D 最舒服；UI 按物理分辨率 1:1 排 |
| 帧率 | PIL→Texture 管线 29.9 FPS 实测。要求更高可试 SDL_Renderer 直接画几何/纹理（跳过 PIL），**上限未实测**，先写 5 秒最小 benchmark 再决定 |
| 输入 | 双摇杆 + 十字键 + 8 键，映射表见第三节；**先跑 probe-input.py 再写代码** |
| 震动 | sysfs 一个字节，按键反馈/受击反馈即写即用 |
| 音频 | **待验证**（见第四节 ⚠）。建议顺序：① 测 `import sdl2` 后 SDL2_mixer 是否可加载 ② 不行则试 pygame（自带 mixer）③ 再不行 aplay/alsa 直写。**未实测前不要把音频写进核心循环** |
| 存档 | 应用自己目录下建 `saves/`（vfat 无权限位，注意同时写的问题不大但要防并发损坏：先写 .tmp 再 rename）|
| 退出 | 务必走"菜单能自然回来"的正常退出；**"进入 → 退出 → 再进入"必须测**（我们在这里翻过车：退出路径残留状态导致第二次启动闪退）|
| 多任务 | 一次只有一个应用在前台；dmenu 是菜单不是常驻对手 |
| 汉化 | locale zh_CN.UTF-8 + 原厂 TTF，中文字体零成本 |

---

## 九、可复用工具与标准流程

| 工具 | 用途 |
|---|---|
| `tools/probe-input.py` | 枚举 evdev 能力 + 实时打印事件（**新项目第一步**）|
| `tools/probe-fb-whole.py` | 扫显存判断"屏幕内容在不在 fb0"（判显示模型）|
| `tools/probe-sdl.py` | SDL2 通路 5 秒最小验证（彩色横条自退）|
| `tools/probe-firmware-origin.py` | 28 组只读查询，判定固件来源/版本 |
| `tools/push-handheld.py` | SSH 推文件 + MD5 校验（`--dry-run` 只对比）|
| `tools/selftest.py` | 全项目静态自检，推文件前必跑 |

标准部署流程：本地自检 → `--dry-run` 看差异 → 推送 → MD5 校验 →
**用户在掌机上手动点菜单启动**。

---

## 十、新项目启动检查清单（照着做）

```
□ 1. 确认显示模型：ls /dev/disp /dev/ion（本机已确认 = 合成器 → 走 SDL2）
□ 2. 跑 probe-input.py，抓全按键/摇杆轴码（不要猜）
□ 3. 写最小 SDL2 验证程序（色块 + 阶段日志），确认建窗成功
□ 4. 从掌机菜单（不是 SSH）启动，确认肉眼可见
□ 5. 按 5.1/5.2 建部署结构（.sh 放 APPS 根目录、图标放 APPS/Imgs/）
□ 6. 实现：PIL 画 → buf 持有 → Texture；不 import sdl2.ext；先 pump
□ 7. 测"进入 → 退出 → 再进入"
□ 8. 菜单占用检测（建窗失败 → 安静退出）
□ 9. 做游戏的再加：音频最小验证（见八）、帧率 benchmark
```

---

## 十一、文档索引

| 文档 | 内容 |
|---|---|
| [`固件信息核实.md`](固件信息核实.md) | **事实基准**：固件来源铁证链、完整探测数据 |
| [`RG35XXPro-开发成功经验.md`](RG35XXPro-开发成功经验.md) | 显示/输入选型全过程、三条死路复盘 |
| [`阶段2联调手册.md`](阶段2联调手册.md) | 网络协议、安全模型、部署流程、六轮实测迭代 |
| [`构建与依赖.md`](构建与依赖.md) | 部署结构与依赖表 |
| [`v1.4闪退修复记录.md`](v1.4闪退修复记录.md) | 段错误级调试全过程（阶段日志法）|
| [`交接文档.md`](交接文档.md) | 项目全貌与事实更正表 |
| [`掌机端首页设计方案.md`](掌机端首页设计方案.md) | UI 版面设计方法（黄金比/按键表）|

---

*整理自 2026-10-05 的 PocketTransfer 项目开发实录。所有"⭐ 实机验证"结论
都在官方固件 20260522 的 RG35XX Pro 上测得；换固件版本后请重跑探测工具复核。*
