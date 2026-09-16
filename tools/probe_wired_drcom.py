#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
探测：有线网段能不能直接走 Dr.COM 表单（绕开统一身份认证）。

背景：门户是按客户端 IP 把有线用户「引导」到统一认证页的，但 Dr.COM 自己的
登录接口不一定拒绝有线 IP。如果成立，就可以完全绕开统一认证（以及它的
拼图滑块 / 人脸识别）。

本脚本用**不存在的假账号**发请求，只观察服务器是否按"登录请求"处理，
不会真的登录，也不影响任何真实账号。

    python tools/probe_wired_drcom.py            # 走默认路由（有线）
    python tools/probe_wired_drcom.py --bind 10.54.1.100   # 指定源地址（无线）
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import argparse
import http.client
import re
import sys
import urllib.parse

import campus_http as c

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOST = "10.255.255.2"
PORT = 801
FAKE_USER = "zzz000000000"
FAKE_PASS = "not-a-real-password"


def request(method: str, path: str, data: dict | None, bind_ip: str | None,
            ajax: bool = True) -> tuple[int, str]:
    conn = http.client.HTTPConnection(
        HOST, PORT, timeout=8,
        **({"source_address": (bind_ip, 0)} if bind_ip else {}),
    )
    headers = {
        "User-Agent": c.USER_AGENT,
        "Host": HOST,                      # 注意：不带端口
        "X-Requested-With": "XMLHttpRequest" if ajax else "XMLHttpRequest",
    }
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(body))
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("gbk", "ignore")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", help="绑定源地址（留空则走默认路由，通常是有线）")
    args = parser.parse_args()

    where = f"绑定源地址 {args.bind}" if args.bind else "走默认路由（有线网卡）"
    print(f"探测目标: {HOST}:{PORT}   {where}")
    print(f"使用假账号 {FAKE_USER}（不会真的登录）\n")

    common = {
        "R1": "", "R2": "", "R3": "", "R6": "0", "para": "", "v6ip": "",
        "terminal_type": "1", "lang": "zh-cn",
    }

    trials = [
        ("① 旧版接口 ACSetting + 短连接 POST（v1.1 已验证可用的写法）",
         "POST", "/eportal/?c=ACSetting&a=Login&ver=1.0",
         dict(common, DDDDD=FAKE_USER, upass=FAKE_PASS, **{"0MKKey": "123456"}, url="drappall")),
        ("② 新版接口 portal/login（GET + user_account 形式）",
         "GET",
         "/eportal/portal/login?callback=dr1003&login_method=1"
         "&user_account=" + urllib.parse.quote(f",0,{FAKE_USER}") +
         "&user_password=" + urllib.parse.quote(FAKE_PASS) +
         "&jsVersion=4.1.3&terminal_type=1&lang=zh-cn",
         None),
    ]

    for name, method, path, data in trials:
        code, body = request(method, path, data, args.bind)
        text = re.sub(r"\s+", " ", body).strip()[:220]
        print(name)
        print(f"   {method} {path[:70]}")
        print(f"   → HTTP {code}: {text}")
        # 判断关键点：AC 是否"按登录请求处理"了
        if "Login succeed" in body:
            verdict = "[?] 居然登录成功（不该发生，假账号）"
        elif "Wired" in body or "不允许" in body or "拒绝" in body:
            verdict = "[X] 被拒绝：有线网段不允许走这套接口"
        elif "已在线" in body:
            verdict = "[OK] 被当作登录请求处理（本机已在线所以拒绝）—— 有线确实能走 Dr.COM"
        elif "Dr.COMWebLoginID_2" in body or "msga=" in body:
            verdict = "[OK] 被当作登录请求处理 —— 有线确实能走 Dr.COM"
        elif "无法获取用户认证账号" in body:
            verdict = "[!] 接口认得，但参数名不对"
        else:
            verdict = "[!] 返回了非登录结果（可能是后台页面）"
        print(f"   判断: {verdict}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
