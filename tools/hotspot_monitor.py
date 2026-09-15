#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
热点共享测试监控。

每 5 秒记录一次:
  - 校园网认证状态(是否被 AC 踢下线)
  - 移动热点网卡是否存在(192.168.137.x)
  - 通过热点接入的下游设备(MAC/地址)

用来验证: 一台电脑/路由器占用一个认证名额, 下游多台设备能不能正常上网,
以及校园网有没有"防私接"检测把上面这个设备踢掉。

    python hotspot_monitor.py            # 一直跑到 Ctrl+C
    python hotspot_monitor.py --seconds 900
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import argparse
import subprocess
import sys
import time

import campus_login as c

log = c.log


def _ps(command: str) -> str:
    """跑一条 PowerShell 命令并返回输出。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command],
            capture_output=True, timeout=20,
        )
        return out.stdout.decode("utf-8", "ignore").strip()
    except Exception as exc:
        return f"(查询失败: {exc})"


def hotspot_snapshot() -> str:
    """热点网卡 + 下游设备。"""
    cmd = (
        "$a = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |"
        " Where-Object { $_.IPAddress -like '192.168.137.*' };"
        " if (-not $a) { '热点未开启'; exit }"
        " $n = Get-NetNeighbor -InterfaceIndex $a.InterfaceIndex -ErrorAction SilentlyContinue |"
        " Where-Object { $_.State -in 'Reachable','Stale','Permanent' -and $_.IPAddress -ne '192.168.137.255' };"
        " '热点IP=' + $a.IPAddress + ' 下游设备=' + $n.Count +"
        " ($(if ($n) { ' [' + (($n | ForEach-Object { $_.IPAddress + '/' + $_.LinkLayerAddress }) -join ', ') + ']' } else { '' }))"
    )
    return _ps(cmd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=1800, help="监控时长(秒), 默认 30 分钟")
    parser.add_argument("--interval", type=int, default=5, help="采样间隔(秒)")
    args = parser.parse_args()

    c.setup_logging()
    cfg = c.load_config()
    log.info("=" * 60)
    log.info("热点共享测试监控开始, 每 %s 秒采样一次, 共 %s 秒", args.interval, args.seconds)

    deadline = time.time() + args.seconds
    last_state = None
    last_hotspot = None
    while time.time() < deadline:
        state, detail = c.evaluate_state(cfg)
        hotspot = hotspot_snapshot()

        if state != last_state:
            log.info("校园网状态: %s - %s", state, detail)
            last_state = state
        if hotspot != last_hotspot:
            log.info("热点: %s", hotspot)
            last_hotspot = hotspot

        # 一旦掉线, 立刻记一笔(这是"防私接"最关键的证据)
        if state != c.STATE_ONLINE:
            log.warning("检测到校园网掉线: %s (%s)", state, detail)

        time.sleep(args.interval)

    log.info("监控结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
