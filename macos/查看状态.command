#!/bin/sh
# 校园网自动登录 · 查看当前状态
# 直接【双击】本文件即可（第一次可能要右键 → 打开）

cd "$(dirname "$0")" 2>/dev/null || { echo "无法定位脚本目录"; sleep 5; exit 1; }

pause_and_exit() {
    printf '\n按回车键关闭本窗口... '
    read -r _ 2>/dev/null || true
    printf '\n'
}
trap pause_and_exit EXIT

PY=""
for c in "$(command -v python3 2>/dev/null)" /usr/bin/python3 /usr/local/bin/python3 \
         /opt/homebrew/bin/python3 /Library/Frameworks/Python.framework/Versions/*/bin/python3
do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done

if [ -z "$PY" ]; then
    printf '❌ 没有找到 Python 3.8+。\n'
    printf '   请先安装 Xcode 命令行工具：  xcode-select --install\n'
    exit 1
fi

"$PY" ./campus_mac.py --status
