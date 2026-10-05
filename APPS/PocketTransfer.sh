#!/bin/bash
# PocketTransfer 掌机端启动脚本
# ================================
# 部署位置：<卡根>/Roms/APPS/PocketTransfer.sh
# 负载位置：<卡根>/Roms/APPS/PocketTransfer/
#
# 原厂机制（从 /mnt/vendor/ctrl/dmenu_ln 源码确认）：
#   dmenu_ln 前台跑 dmenu.bin；用户选中应用后 dmenu.bin 把命令写进
#   /tmp/.next 然后自己退出；dmenu_ln 执行 sh /tmp/.next；
#   我们的进程结束后 dmenu_ln 的 while 循环会自动重启 dmenu.bin。
#   → 所以本脚本【不需要】做任何清理或重启动作。
#
# ⚠ 本脚本绝不修改任何系统状态：
#   不 kill 进程 / 不写 /tmp/.next / 不切显示模式 / 不建软链 / 不写 sysfs。
#
# 日志：原厂把应用的 stdout / stderr 丢进 /dev/null，所以必须自己留一份。
#       没有日志的话，崩溃在现场不会留下任何痕迹。

set -u

# ---- 路径：$0 可能是 /tmp/.next，因此全部用绝对路径自算 ----
progdir="$(cd "$(dirname "$0")" && pwd)"
# Roms/APPS -> 卡根（/mnt/mmc 或 /mnt/sdcard）
basedir="$(cd "$progdir/../.." && pwd)"
APPDIR="$progdir/PocketTransfer"

# ---- 日志（追加模式，保留历史；同时防止无限增长）----
LOGFILE="$APPDIR/launcher.log"
if [ -f "$LOGFILE" ]; then
    # 超过 256KB 就轮转一次
    size=$(wc -c < "$LOGFILE" 2>/dev/null || echo 0)
    if [ "$size" -gt 262144 ] 2>/dev/null; then
        mv -f "$LOGFILE" "$LOGFILE.old" 2>/dev/null
    fi
fi

log() {
    echo "[$(date '+%H:%M:%S')] $*" >> "$LOGFILE" 2>/dev/null
    echo "[$(date '+%H:%M:%S')] $*"
}

log "----------------------------------------"
log "PocketTransfer launcher 启动"
log "progdir=$progdir"
log "basedir=$basedir"
log "APPDIR=$APPDIR"

# ---- 环境 ----
# PYSDL2_DLL_PATH 是原厂 5 个应用都设置的一行，保持一致。
# 本版本没用 pysdl2，但留着无害，且为后续留后路。
export BASE_PATH="$basedir"
export PYSDL2_DLL_PATH="/usr/lib"
export LD_LIBRARY_PATH="/usr/lib:/mnt/vendor/lib:${LD_LIBRARY_PATH:-}"
export TERM=linux

# ---- 自检：任何一项不过都要在屏幕上说清楚，不能静默退出 ----
fail() {
    log "FAIL: $1"
    # 尽力把错误写到控制台（用户从菜单进入时多半看不到，但 SSH 能看到）
    echo "PocketTransfer 启动失败: $1" > /dev/console 2>/dev/null
    exit 1
}

[ -d "$APPDIR" ] || fail "找不到应用目录 $APPDIR（请确认发布包已完整拷贝）"

PY=""
for cand in /usr/bin/python3 /usr/local/bin/python3 /bin/python3; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
done
[ -n "$PY" ] || fail "系统未找到 python3"

log "python=$PY  version=$("$PY" -V 2>&1)"

[ -f "$APPDIR/main.py" ] || fail "缺少 $APPDIR/main.py"

# ---- 运行 ----
# 注意：这里刻意不用 exec > 重定向到固定文件，
# 因为 boot.py 自己会写 boot.log，两者分工不同。
log "启动 main.py ..."
"$PY" "$APPDIR/main.py" >> "$LOGFILE" 2>&1
rc=$?
log "main.py 退出，rc=$rc"

if [ "$rc" -ne 0 ]; then
    log "非零退出码，详见 $APPDIR/boot.log"
fi

# ---- 交还控制权给 dmenu（它会自动重启，我们什么都不用做）----
exit 0
