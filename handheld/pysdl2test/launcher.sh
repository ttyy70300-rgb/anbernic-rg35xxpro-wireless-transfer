#!/bin/bash
# PocketTransfer 阶段 0-C/0-D 启动脚本
# 结构完全对齐原厂 Roms/APPS/*.sh：
#   - 脚本放 APPS 根目录，Python 负载放兄弟子目录
#   - export PYSDL2_DLL_PATH="/usr/lib"
#   - 日志重定向到子目录 log.txt
#
# 负载目录名自动取本脚本去掉 .sh 的名字，
# 所以本文件改名为 X.sh 时，负载目录必须是 APPS/X/。
# 这样部署工具无需改写脚本内容。

appname="$(basename "$0" .sh)"
progdir="$(cd "$(dirname "$0")" || exit; pwd)/$appname"

export PYSDL2_DLL_PATH="/usr/lib"

program="python3 ${progdir}/main.py"
log_file="${progdir}/log.txt"

$program > "$log_file" 2>&1
