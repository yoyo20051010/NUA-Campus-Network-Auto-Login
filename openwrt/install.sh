#!/bin/sh
# 校园网自动登录 —— OpenWrt / 路由器安装脚本
#
# 用法（先把整个目录传到路由器，然后在路由器上执行）:
#   scp -r openwrt root@192.168.1.1:/tmp/
#   ssh root@192.168.1.1
#   cd /tmp/openwrt && sh install.sh

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="/etc/campus-net-login"

echo "==> 1/5 检查 Python 环境"
if ! command -v python3 >/dev/null 2>&1; then
    echo "    没有找到 python3，尝试自动安装（需要路由器能联网）..."
    opkg update || true
    opkg install python3-light python3-urllib python3-openssl \
        || opkg install python3 \
        || { echo "    安装失败，请手动执行: opkg update && opkg install python3"; exit 1; }
fi
python3 -c "import ssl, urllib.request; print('    Python 与 HTTPS 支持正常')" \
    || { echo "    缺少 ssl 模块，请安装 python3-openssl"; exit 1; }

echo "==> 2/5 复制程序到 $TARGET"
mkdir -p "$TARGET/logs"
cp "$HERE/campus_http.py" "$TARGET/campus_http.py"
if [ -f "$HERE/config.json" ]; then
    cp "$HERE/config.json" "$TARGET/config.json"
fi
chmod +x "$TARGET/campus_http.py"

echo "==> 3/5 保存校园网账号密码"
if [ -f "$TARGET/secret.json" ]; then
    echo "    已存在 secret.json，跳过（想改密码就删掉它再运行本脚本）"
else
    python3 "$TARGET/campus_http.py" --set-password
    chmod 600 "$TARGET/secret.json"
fi

echo "==> 4/5 安装后台服务"
cp "$HERE/campus-net-login.init" /etc/init.d/campus-net-login
chmod +x /etc/init.d/campus-net-login

echo "==> 5/5 启用并启动"
/etc/init.d/campus-net-login enable
/etc/init.d/campus-net-login start
sleep 3

echo ""
echo "安装完成。常用命令:"
echo "  查看状态: /etc/init.d/campus-net-login status"
echo "  查看日志: tail -f $TARGET/logs/campus_http.log"
echo "  手动登录: python3 $TARGET/campus_http.py --login"
echo "  检测状态: python3 $TARGET/campus_http.py --check"
echo "  停止服务: /etc/init.d/campus-net-login stop"
echo ""
echo "提示: 如果登录没成功，先看日志；里面会把每一步的返回都记下来。"
