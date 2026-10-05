# PocketTransfer

掌机 ↔ Win11 无线文件管家。给 Anbernic RG35XX Pro 做的图形化文件传输工具，替代 SSH / Samba / WinSCP。

## 当前状态

**★ 掌机端显示 / 输入 / 部署链路已全部打通，真机稳定运行（2026-10-05）**

- 从菜单点进 → 界面正常显示（SDL2 通路，640×480，**29.9 FPS 稳定**）
- 按键 / 摇杆输入正常
- 震动正常
- **退出后再进入也正常**

下一阶段：**阶段 2 — UDP 自动发现**（前置：先解决休眠断网）。

## 如果你是接手者，按这个顺序读

| 顺序 | 文档 | 为什么读 |
|---|---|---|
| **0️⃣** | **[docs/固件信息核实.md](docs/固件信息核实.md)** | **★★★ 事实基准。** 设备 / 固件 / SDL2 / 分区 / locale 的实测值。**与其它文档冲突时以本文为准** |
| **1️⃣** | **[docs/RG35XXPro-开发成功经验.md](docs/RG35XXPro-开发成功经验.md)** | **★★ 先读这个。** 真机踩坑总结：显示模型、SDL2 通路、输入、部署契约、三条死路 |
| **2️⃣** | [docs/交接文档.md](docs/交接文档.md) | 项目全貌：环境事实、技术选型、需求清单、协议、阶段规划、**操作纪律** |
| 3️⃣ | [docs/开发规划.md](docs/开发规划.md) | 完整技术规划，12 章：架构、协议、UI、代码结构 |
| 4️⃣ | [docs/技术栈决策v1.3.md](docs/技术栈决策v1.3.md) | 为什么用 Python。**末尾附录含已作废结论的更正** |
| 5️⃣ | [docs/v1.4闪退修复记录.md](docs/v1.4闪退修复记录.md) | 三次闪退的根因分析（很有教育意义） |
| — | [docs/构建与依赖.md](docs/构建与依赖.md) | 构建产物结构、依赖、部署说明 |
| — | [docs/工具链安装指南.md](docs/工具链安装指南.md) | ⚠️ 已降级为备用（Python 路线下不需要交叉编译） |

## 核心技术结论（浓缩版）

| 事项 | 结论 |
|---|---|
| **显示** | **必须 SDL2 `SDL_WINDOW_FULLSCREEN_DESKTOP`**。裸写 `/dev/fb0` 会被 disp 图层盖住 —— 这台机器是 **DispEngine 多图层合成器**，不是单 framebuffer |
| **语言** | Python 3.10（固件自带，原厂 5 个应用都是 Python） |
| **绘图** | Pillow 9.0.1 画 → RGBA bytes → SDL Surface → Texture |
| **输入** | `/dev/input/event1`；按键在 `EV_KEY`，**摇杆在 `EV_ABS`**（补码 + 死区 + 去重） |
| **震动** | 写 `/sys/class/power_supply/axp2202-battery/moto` |
| **部署** | `Roms/APPS/*.sh` + 兄弟目录 + `APPS/Imgs/*.png`；脚本必须 export `PYSDL2_DLL_PATH=/usr/lib` |

三条**必踩的坑**（都会 SIGSEGV）：
1. `SDL_CreateRGBSurfaceWithFormatFrom` 首参不能是临时表达式（悬空指针）
2. 不能 `import sdl2.ext`（会加载坏掉的 SDL2_image）
3. 首次渲染前必须先 pump 事件

## 核心特性

- **掌机掌握最终决定权** —— 任何写操作都需掌机侧物理确认，PC 端无权绕过
- **自动发现** —— UDP 广播，同 WiFi 下零配置
- **默认锁定** —— 每次启动都是只读态，START 键解锁，解锁后仍逐次确认
- **轻量** —— 复用固件自带 SDL2，不打包任何运行时

## 环境事实摘要

**目标设备**：Anbernic RG35XX Pro（Allwinner H700 / 1.5GHz 四核 A53 / 1GB LPDDR4 / 640×480 IPS）
**掌机固件**：**Anbernic 官方固件 `20260522`**（2026-05-22 构建，官方客服提供），**Ubuntu 22.04 LTS (Jammy)**，内核 Linux 4.9.170 `#32`（编译于 2026-05-12）
**自带栈**：Python 3.10.12 / Pillow 9.0.1 / pysdl2 0.9.17 / gcc 11.4.0
**SDL2**：**双版本** —— pysdl2 实际加载 **2.28.5**（`/usr/lib/aarch64-linux-gnu/libSDL2-2.0.so.0.2800.5`）；原厂 dmenu 静态链接 2.0.12（`/usr/lib/libSDL2-2.0.so.0.12.0`）
**系统 locale**：`zh_CN.UTF-8`（渲染中文零配置）

> ⚠️ 网上多数 H700 机型教程假定 Buildroot（无 Python / 无编译器）。
> **本机是 Ubuntu 22.04，有完整 Python 桌面栈。** 设备事实一律以本机实测为准。
> 完整实测值见 [docs/固件信息核实.md](docs/固件信息核实.md)。

**开发电脑**：Windows 11（非管理员会话，WSL2 被拦截）
→ 用 Python + paramiko 做部署工具，**不需要交叉编译**。

## 开发标准流程

```bash
python tools/selftest.py            # 1. 本地静态自检（116 项）
python tools/deploy-only.py --dry   # 2. 看要传什么
python tools/deploy-only.py         # 3. 部署（只推文件，不启动）
                                    # 4. 校验 MD5
                                    # 5. 用户在掌机上手动点菜单启动
                                    # 6. 出问题读 boot.log
```

## 操作纪律（用户硬边界）

**只允许通过 SSH 往掌机推文件，不能对系统其他内容做任何改动。**

❌ 不 kill 进程（尤其 dmenu）· ❌ 不写 `/tmp/.next` · ❌ 不切显示模式
❌ 不 remount · ❌ 不建软链 · ❌ 不写 sysfs

**应用一律由用户在掌机上手动点击菜单启动。**
