#!/bin/bash
# fbtest.sh — 阶段 0-B 测试用启动脚本
#
# 部署：拷到 TF 卡的 Roms/APPS/ 下（和正式 launcher 一样的位置规则）
# 启动：Apps Center → APPS → 点开

progdir="$(cd "$(dirname "$0")"; pwd)"
basedir="$(cd "$progdir/../.."; pwd)"

cd "$progdir/PocketTransfer" || exit 1

mkdir -p logs
exec >logs/stdout.log 2>&1

export BASE_PATH="$basedir"
chmod +x ./fbtest 2>/dev/null

./fbtest

exit 0
