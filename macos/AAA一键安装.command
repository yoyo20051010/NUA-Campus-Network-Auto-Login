#!/bin/sh
# 校园网自动登录 · macOS 一键安装
# 直接【双击】本文件即可。
# 如果提示"无法打开，来自身份不明的开发者"：在这行上【右键】→【打开】→ 再点【打开】

cd "$(dirname "$0")" 2>/dev/null || { echo "无法定位脚本目录"; sleep 5; exit 1; }

pause_and_exit() {
    printf '\n按回车键关闭本窗口... '
    read -r _ 2>/dev/null || true
    printf '\n'
}
trap pause_and_exit EXIT

printf '=====================================================\n'
printf '   校园网自动登录 · macOS 一键安装\n'
printf '=====================================================\n\n'

PY=""
for c in "$(command -v python3 2>/dev/null)" /usr/bin/python3 /usr/local/bin/python3 \
         /opt/homebrew/bin/python3 /Library/Frameworks/Python.framework/Versions/*/bin/python3
do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        PY="$c"
        break
    fi
done

if [ -z "$PY" ]; then
    printf '❌ 没有找到可用的 Python 3.8 或更高版本。\n\n'
    printf '请二选一：\n\n'
    printf '  ① 安装 Xcode 命令行工具（自带 Python3，约 1~2GB）：\n'
    printf '       打开"终端"App，运行：   xcode-select --install\n\n'
    printf '  ② 从官网安装 Python：\n'
    printf '       https://www.python.org/downloads/\n\n'
    printf '装好之后再双击一次本文件即可。\n'
    exit 1
fi

printf '使用的 Python：%s\n\n' "$("$PY" -V 2>&1)"
printf '接下来会问你要【校园网账号】和【密码】（密码输入时不显示），\n'
printf '然后自动安装开机自启。\n\n'
printf '%s\n\n' '-----------------------------------------------------'

if sh ./install.sh; then
    printf '\n%s\n' '-----------------------------------------------------'
    printf '✅ 安装完成！现在就可以关掉这个窗口了。\n'
else
    printf '\n%s\n' '-----------------------------------------------------'
    printf '❌ 安装过程出错了。\n'
    printf '请把上面的信息截图，发给帮你装的同学。\n'
fi
