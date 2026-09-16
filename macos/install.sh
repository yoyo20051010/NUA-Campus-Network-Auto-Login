#!/bin/sh
# 校园网自动登录 —— macOS 安装脚本
#
# 用法:
#   cd macos && sh install.sh                # 安装并设置开机自启
#   cd macos && sh install.sh --no-autostart # 只存账号密码, 不装自启
#   cd macos && sh install.sh --gui          # 用 macOS 原生弹窗输账号密码(适合让 AI 代装)
#   cd macos && sh install.sh --interval 30 --interface en0
#
# 不需要 sudo: 自启装在当前用户的 ~/Library/LaunchAgents 下, 密码进登录钥匙串。

set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.campusnet.autologin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY_SCRIPT="$HERE/campus_mac.py"
INTERVAL=""
IFACE=""
AUTOSTART=1
GUI=0

while [ $# -gt 0 ]; do
    case "$1" in
        --interval) INTERVAL="$2"; shift 2 ;;
        --interface) IFACE="$2"; shift 2 ;;
        --no-autostart) AUTOSTART=0; shift ;;
        --gui) GUI=1; shift ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

echo "==> 1/5 检查 Python 环境"
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
    echo "    没有找到 python3。请先安装 Xcode 命令行工具(会自动带 Python3):"
    echo "        xcode-select --install"
    exit 1
fi
"$PYTHON" - <<'PYCHECK'
import ssl, socket, struct, sys
assert sys.version_info >= (3, 8), "需要 Python 3.8+"
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
IP_BOUND_IF = 25
try:
    s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, struct.pack("I", 1))
except OSError as exc:
    print("    [警告] 本机不支持 IP_BOUND_IF(%s), 退化为普通连接" % exc)
else:
    print("    %s, HTTPS/绑定网卡都正常" % sys.version.split()[0])
finally:
    s.close()
PYCHECK

echo "==> 2/5 保存校园网账号"
CURRENT="$("$PYTHON" - "$HERE/config.json" <<'PYCFG'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    print(json.loads(p.read_text(encoding="utf-8")).get("account", ""))
except Exception:
    print("")
PYCFG
)"
if [ "$GUI" -eq 1 ]; then
    ACCOUNT="$(osascript <<ASEOF
try
    set res to display dialog "请输入你的校园网账号(学号):" default answer "$CURRENT" with title "校园网自动登录 · 安装" buttons {"取消", "继续"} default button "继续"
    return text returned of res
on error
    return ""
end try
ASEOF
)"
else
    if [ -n "$CURRENT" ]; then
        printf "    当前账号 %s, 直接回车沿用, 或输入新账号: " "$CURRENT"
    else
        printf "    请输入校园网账号(学号): "
    fi
    read -r ACCOUNT
fi
[ -z "$ACCOUNT" ] && ACCOUNT="$CURRENT"
if [ -z "$ACCOUNT" ]; then
    echo "    账号不能为空"; exit 1
fi
"$PYTHON" - "$HERE/config.json" "$ACCOUNT" <<'PYSET'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
cfg["account"] = sys.argv[2]
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
PYSET

echo "==> 3/5 保存密码到 macOS 钥匙串(输入时不显示)"
if [ "$GUI" -eq 1 ]; then
    security delete-generic-password -s campus-net-login -a "$ACCOUNT" >/dev/null 2>&1 || true
    if ! osascript <<ASEOF
set acct to "$ACCOUNT"
try
    set pw to text returned of (display dialog "请输入校园网密码:" default answer "" with hidden answer with title "校园网自动登录 · 安装" buttons {"取消", "保存"} default button "保存")
on error
    error number -128
end try
do shell script "/usr/bin/security add-generic-password -U -s campus-net-login -a " & quoted form of acct & " -T /usr/bin/security -w " & quoted form of pw
ASEOF
    then
        echo "    已取消或保存失败, 安装中止"; exit 1
    fi
    echo "    已保存到钥匙串条目 campus-net-login / $ACCOUNT"
else
    security delete-generic-password -s campus-net-login -a "$ACCOUNT" >/dev/null 2>&1 || true
    security add-generic-password -U -s campus-net-login -a "$ACCOUNT" \
        -T /usr/bin/security -w
    echo "    已保存到钥匙串条目 campus-net-login / $ACCOUNT"
fi

echo "==> 4/5 生成环境配置"
if [ -n "$INTERVAL" ]; then
    "$PYTHON" - "$HERE/config.json" "$INTERVAL" <<'PYSET2'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
cfg = json.loads(p.read_text(encoding="utf-8"))
cfg["interval"] = int(sys.argv[2])
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
PYSET2
fi
if [ -n "$IFACE" ]; then
    "$PYTHON" - "$HERE/config.json" "$IFACE" <<'PYSET3'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
cfg = json.loads(p.read_text(encoding="utf-8"))
cfg["interface"] = sys.argv[2]
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
PYSET3
fi

if [ "$AUTOSTART" -eq 1 ]; then
    echo "==> 5/5 安装开机自启(LaunchAgent)"
    mkdir -p "$HOME/Library/LaunchAgents" "$HERE/logs" "$HERE/state"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$PY_SCRIPT</string>
        <string>--watch</string>
        <string>--quiet</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$HERE</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$HERE/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$HERE/logs/launchd.err.log</string>
</dict>
</plist>
PLISTEOF
    chmod 644 "$PLIST"

    launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    if ! launchctl bootstrap "gui/$UID" "$PLIST" 2>/dev/null; then
        launchctl unload "$PLIST" >/dev/null 2>&1 || true
        launchctl load -w "$PLIST"
    fi
    launchctl kickstart -k "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    echo "    已安装: $PLIST"
else
    echo "==> 5/5 跳过开机自启(--no-autostart)"
fi

echo ""
echo "==> 体检结果"
"$PYTHON" "$PY_SCRIPT" --diagnose || true
echo ""
echo "安装完成。常用命令:"
echo "  查看状态: $PYTHON $PY_SCRIPT --check"
echo "  立即登录: $PYTHON $PY_SCRIPT --login"
echo "  环境体检: $PYTHON $PY_SCRIPT --diagnose"
echo "  查看日志: tail -f $HERE/logs/campus_mac.log"
echo "  自启状态: launchctl print gui/$UID/$LABEL | head -20"
echo "  卸载自启: sh $HERE/uninstall.sh"
