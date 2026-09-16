#!/bin/sh
# 校园网自动登录 · macOS 卸载
# 直接【双击】本文件即可

cd "$(dirname "$0")" 2>/dev/null || { echo "无法定位脚本目录"; sleep 5; exit 1; }

pause_and_exit() {
    printf '\n按回车键关闭本窗口... '
    read -r _ 2>/dev/null || true
    printf '\n'
}
trap pause_and_exit EXIT

printf '=====================================================\n'
printf '   校园网自动登录 · macOS 卸载\n'
printf '=====================================================\n\n'

if sh ./uninstall.sh; then
    printf '\n✅ 卸载完成。\n'
else
    printf '\n❌ 卸载过程出错了，把上面的信息截图发出来。\n'
fi
