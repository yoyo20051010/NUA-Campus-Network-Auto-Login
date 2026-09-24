#!/bin/sh
# 校园网自动登录（C++ 版）—— OpenWrt / 路由器安装脚本
#
# 用法（先把整个目录传到路由器，然后在路由器上执行）:
#   scp -r openwrt-cpp/openwrt root@192.168.1.1:/tmp/
#   ssh root@192.168.1.1
#   cd /tmp/openwrt && sh install.sh
#
# 和 Python 版最大的区别：不需要 python3，装的就是一个静态二进制，
#              常驻内存约 0.5MB（Python 版是 28~48MB）。

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="/etc/campus-net-login"

echo "==> 1/6 检查架构"
MACHINE="$(uname -m)"
if [ "$MACHINE" != "aarch64" ]; then
    echo "    警告：本包是为 aarch64 编译的，当前机器是 $MACHINE，二进制可能跑不起来。"
    echo "    如果确实要装，请自行交叉编译：见 openwrt-cpp/README.md 的“自己编译”。"
    printf "    继续安装？[y/N]: "
    read -r ANSWER || ANSWER=""
    case "$ANSWER" in
        y|Y|yes|YES) ;;
        *) echo "    已取消。"; exit 1 ;;
    esac
else
    echo "    架构 aarch64，匹配。"
fi

echo "==> 2/6 停掉正在跑的旧服务（Python 版也在内）"
if [ -x /etc/init.d/campus-net-login ]; then
    /etc/init.d/campus-net-login stop 2>/dev/null || true
fi
sleep 1

echo "==> 3/6 复制程序到 $TARGET"
mkdir -p "$TARGET/logs"
cp "$HERE/campus-net-login" "$TARGET/campus-net-login"
chmod +x "$TARGET/campus-net-login"

# 已有的 config.json / secret.json / profiles 一律保留 —— 从 Python 版升级时
# 账号密码和线路配置都不用重新填。
if [ ! -f "$TARGET/config.json" ] && [ -f "$HERE/config.json" ]; then
    cp "$HERE/config.json" "$TARGET/config.json"
    echo "    写入默认 config.json"
else
    echo "    已有 config.json，保留不动"
fi

echo "==> 4/6 保存校园网账号密码"
if [ -f "$TARGET/secret.json" ] || [ -d "$TARGET/profiles" ]; then
    echo "    已存在账号信息，跳过（想改密码就删掉 secret.json 再运行本脚本）"
else
    cd "$TARGET"
    ./campus-net-login --set-password
    chmod 600 "$TARGET/secret.json" 2>/dev/null || true

    echo "      1) 学生账号 —— 学校夜间断网时段（默认 00:00-06:00）不尝试认证"
    echo "      2) 教师账号 —— 不受夜间限制，整夜正常检测并自动登录"
    printf "    请选择 [1]: "
    read -r ACCT_TYPE || ACCT_TYPE=""
    case "$ACCT_TYPE" in
        2|t|T|teacher|教师|老师) TARGET_TYPE="teacher" ;;
        *) TARGET_TYPE="student" ;;
    esac
    ./campus-net-login --mode "$TARGET_TYPE" || true
fi

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
echo "  手动登录: $TARGET/campus-net-login --login"
echo "  检测状态: $TARGET/campus-net-login --check"
echo "  看账号类型: $TARGET/campus-net-login --mode"
echo "  切教师模式: $TARGET/campus-net-login --mode teacher"
echo "  估算内存: grep VmRSS /proc/\$(pgrep -f 'campus-net-login --watch')/status"
echo "  停止服务: /etc/init.d/campus-net-login stop"
echo ""
echo "提示: 如果登录没成功，先看日志；里面会把每一步的返回都记下来。"
