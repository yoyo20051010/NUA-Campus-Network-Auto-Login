#!/bin/sh
# 卸载 macOS 版的开机自启(以及可选地删除钥匙串里的密码)

set -e
LABEL="com.campusnet.autologin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="$(cd "$(dirname "$0")" && pwd)"

launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
launchctl unload "$PLIST" >/dev/null 2>&1 || true
if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "已删除自启配置: $PLIST"
else
    echo "没有找到自启配置: $PLIST"
fi

ACCOUNT="$(python3 - "$HERE/config.json" <<'PY'
import json, pathlib, sys
try:
    print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")).get("account", ""))
except Exception:
    print("")
PY
)"
if [ -n "$ACCOUNT" ]; then
    printf "是否同时删除钥匙串里保存的密码? [y/N] "
    read -r ANS
    case "$ANS" in
        y|Y)
            security delete-generic-password -s campus-net-login -a "$ACCOUNT" >/dev/null 2>&1 && \
                echo "已删除钥匙串条目 campus-net-login / $ACCOUNT" || echo "钥匙串里没找到该条目"
            ;;
        *) echo "保留密码" ;;
    esac
fi
echo "卸载完成。程序文件仍在 $HERE, 想彻底删除直接删掉整个目录即可。"
