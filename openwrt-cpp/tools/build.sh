#!/bin/sh
# 把 C++ 版交叉编译成路由器能跑的静态二进制（aarch64 + musl）。
# 这个脚本是给 Linux / macOS 用的，Windows 请用 build.ps1。
#
# 用法:
#   sh openwrt-cpp/tools/build.sh            # 用 .toolchain 里的 zig
#   ZIG=/usr/local/bin/zig sh openwrt-cpp/tools/build.sh

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$PROJECT/.." && pwd)"
OUT="${OUT_DIR:-$PROJECT/build}"

if [ -n "$ZIG" ]; then
    ZIG_BIN="$ZIG"
elif [ -x "$REPO/.toolchain/zig/zig" ]; then
    ZIG_BIN="$REPO/.toolchain/zig/zig"
else
    ZIG_BIN="$(command -v zig || true)"
fi

if [ -z "$ZIG_BIN" ] || [ ! -x "$ZIG_BIN" ]; then
    echo "找不到 zig。请放到 <仓库>/.toolchain/zig/zig，或用 ZIG=... 指定。" >&2
    exit 1
fi

echo "使用工具链: $ZIG_BIN ($("$ZIG_BIN" version))"

# zig 默认缓存放在用户目录，这里统一放到仓库内，避免污染环境
ZIG_LOCAL_CACHE_DIR="$REPO/.toolchain/zigcache"
ZIG_GLOBAL_CACHE_DIR="$REPO/.toolchain/zigglobal"
export ZIG_LOCAL_CACHE_DIR ZIG_GLOBAL_CACHE_DIR
mkdir -p "$ZIG_LOCAL_CACHE_DIR" "$ZIG_GLOBAL_CACHE_DIR" "$OUT"

echo "== 交叉编译 aarch64-linux-musl =="
"$ZIG_BIN" c++ \
    -target aarch64-linux-musl \
    -std=c++17 -Os -fno-exceptions -fno-rtti \
    -ffunction-sections -fdata-sections \
    -Wall -Wextra -static -pthread \
    -Wl,--gc-sections -Wl,-s \
    -o "$OUT/campus-net-login" \
    "$PROJECT"/src/*.cpp

ls -l "$OUT/campus-net-login"
