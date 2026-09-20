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

echo "==> 1/6 检查 Python 环境"
if ! command -v python3 >/dev/null 2>&1; then
    echo "    没有找到 python3，尝试自动安装（需要路由器能联网）..."
    opkg update || true
    # logging 和 codecs 不在 python3-light 里，但脚本必须用到：
    #   缺 logging -> 一启动就报 No module named 'logging'
    #   缺 codecs  -> 缺 unicodedata，连带 idna 不可用，所有网络请求都会报
    #                 LookupError: unknown encoding: idna
    opkg install python3-light python3-logging python3-codecs python3-urllib python3-openssl \
        || opkg install python3 \
        || { echo "    安装失败，请手动执行: opkg update && opkg install python3"; exit 1; }
fi
python3 -c "import ssl, urllib.request, logging, encodings.idna; print('    Python 与网络/HTTPS 支持正常')" \
    || { echo "    缺少必要模块，请执行: opkg install python3-logging python3-codecs python3-openssl"; exit 1; }

echo "==> 2/6 复制程序到 $TARGET"
mkdir -p "$TARGET/logs"
cp "$HERE/campus_http.py" "$TARGET/campus_http.py"
if [ -f "$HERE/config.json" ]; then
    cp "$HERE/config.json" "$TARGET/config.json"
fi
chmod +x "$TARGET/campus_http.py"

TARGET_TYPE=""
echo "==> 3/6 保存校园网账号密码"
if [ -f "$TARGET/secret.json" ]; then
    echo "    已存在 secret.json，跳过（想改密码就删掉它再运行本脚本）"
else
    python3 "$TARGET/campus_http.py" --set-password
    chmod 600 "$TARGET/secret.json"
fi

echo "==> 4/6 选择账号类型"
echo "      1) 学生账号 —— 学校夜间断网时段（默认 00:00-06:00）不尝试认证"
echo "      2) 教师账号 —— 不受夜间限制，整夜正常检测并自动登录"
printf "    请选择 [1]: "
read -r ACCT_TYPE || ACCT_TYPE=""
case "$ACCT_TYPE" in
    2|t|T|teacher|教师|老师) TARGET_TYPE="teacher" ;;
    *) TARGET_TYPE="student" ;;
esac
python3 "$TARGET/campus_http.py" --mode "$TARGET_TYPE" || true

echo "==> 5/6 安装后台服务"
cp "$HERE/campus-net-login.init" /etc/init.d/campus-net-login
chmod +x /etc/init.d/campus-net-login

echo "==> 6/6 启用并启动"
/etc/init.d/campus-net-login enable
/etc/init.d/campus-net-login start
sleep 3

echo ""
echo "安装完成。常用命令:"
echo "  查看状态: /etc/init.d/campus-net-login status"
echo "  查看日志: tail -f $TARGET/logs/campus_http.log"
echo "  手动登录: python3 $TARGET/campus_http.py --login"
echo "  检测状态: python3 $TARGET/campus_http.py --check"
echo "  看账号类型: python3 $TARGET/campus_http.py --mode"
echo "  切教师模式: python3 $TARGET/campus_http.py --mode teacher"
echo "  停止服务: /etc/init.d/campus-net-login stop"
echo ""
echo "提示: 如果登录没成功，先看日志；里面会把每一步的返回都记下来。"
