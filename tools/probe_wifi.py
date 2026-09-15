#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
探测无线网段（校园 WiFi）走的是哪套登录页。

做法：把请求绑定到无线网卡的 IP 上再访问门户，门户就会按"无线客户端"来响应。
这样在**不影响有线连接**的前提下就能看到无线那套页面。

    python probe_wifi.py                # 自动找名含 WLAN 的网卡
    python probe_wifi.py --iface "WLAN 3"
    python probe_wifi.py --bind 10.54.1.100      # 手动指定无线网卡的地址
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import argparse
import http.client
import re
import subprocess
import sys

import campus_http as c


def _ps(command: str) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command],
            capture_output=True, timeout=20,
        )
        return out.stdout.decode("utf-8", "ignore").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(查询失败: {exc})"


def find_ip(iface: str) -> str | None:
    out = _ps(
        "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |"
        f" Where-Object {{ $_.InterfaceAlias -like '*{iface}*' -and $_.IPAddress -notlike '169.254.*' }} |"
        " Select-Object -First 1 -ExpandProperty IPAddress"
    )
    return out or None


def fetch(url_host: str, path: str, bind_ip: str, timeout: int = 8) -> tuple[int, str]:
    conn = http.client.HTTPConnection(url_host, 80, timeout=timeout, source_address=(bind_ip, 0))
    try:
        conn.request("GET", path, headers={"User-Agent": c.USER_AGENT, "Host": url_host})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("gbk", "ignore")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def post(url_host: str, path: str, bind_ip: str, data: str = "", timeout: int = 8) -> tuple[int, str]:
    conn = http.client.HTTPConnection(url_host, 80, timeout=timeout, source_address=(bind_ip, 0))
    try:
        conn.request(
            "POST", path, body=data.encode("utf-8"),
            headers={"User-Agent": c.USER_AGENT, "Host": url_host,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Content-Length": str(len(data))},
        )
        resp = conn.getresponse()
        return resp.status, resp.read().decode("gbk", "ignore")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default="WLAN", help="网卡名称关键字，默认 WLAN")
    parser.add_argument("--bind", help="直接指定绑定的源 IP")
    args = parser.parse_args()

    c.setup_logging()
    bind_ip = args.bind or find_ip(args.iface)
    if not bind_ip:
        print(f"没能找到匹配 '{args.iface}' 的网卡地址，请用 --bind 手动指定")
        return 1

    host = "10.255.255.2"
    print(f"绑定源地址: {bind_ip}")
    print(f"从该地址访问门户 {host} ...")
    code, body = fetch(host, "/", bind_ip)
    print(f"  HTTP {code}，页面 {len(body)} 字节")
    if code != 200:
        print(f"  内容: {body[:200]}")
        return 1

    ip = c.client_ip_from_portal(body)
    flow = "统一身份认证（有拼图滑块）" if c.needs_cas(ip) else "Dr.COM 门户登录（无线，无滑块）"
    print(f"  门户看到的客户端地址: {ip}")
    print(f"  应该走: {flow}")

    conf = c.parse_portal_config(body)
    for key in sorted(conf):
        print(f"  门户配置 {key} = {conf[key]}")

    print()
    if "DDDDD" in body:
        print("提示: 页面里出现了 DDDDD 字段，说明确实是 Dr.COM 门户自己的登录表单。")

    # 从同一个地址访问外网，判断这个网段当前是否已经认证
    print()
    print("查询门户记录的该网段在线状态 ...")
    code2, body2 = post(host, "/drcom/chkstatus", bind_ip)
    marker = [k for k in ("uid", "online", "ss5", "vlanid", "myv6ip") if k in body2]
    if code2 == 200 and body2.strip():
        print(f"  → HTTP {code2}，返回 {len(body2)} 字节，关键字 {marker}")
        print(f"     内容片段: {body2.strip()[:300]}")
    else:
        print(f"  → HTTP {code2}，内容: {body2[:200]}")

    print("提示: 这个探测是只读的，没有提交任何账号密码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
