#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
探测 Android 自动化工具能用的"最简登录请求"。

手机上的自动化工具（Tasker / MacroDroid / 快捷指令）发请求的能力有限，
所以这里测清楚：哪种写法**不需要自定义请求头**、**一个 GET 就能完成**。

用不存在的假账号，只观察服务器是否按登录请求处理（不会真的登录）。

    python tools/probe_android_payload.py
    python tools/probe_android_payload.py --bind 10.54.1.100
"""

from __future__ import annotations

import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import argparse
import http.client
import re
import urllib.parse

import campus_http as c

try:
    _sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOST = "10.255.255.2"
PORT = 801
FAKE_USER = "zzz000000000"
FAKE_PASS = "not-a-real-password"


def send(method, path, data, bind_ip, with_ajax_header, host_with_port=False):
    conn = http.client.HTTPConnection(
        HOST, PORT, timeout=8,
        **({"source_address": (bind_ip, 0)} if bind_ip else {}),
    )
    headers = {"User-Agent": c.USER_AGENT,
               "Host": f"{HOST}:{PORT}" if host_with_port else HOST}
    if with_ajax_header:
        headers["X-Requested-With"] = "XMLHttpRequest"
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


def verdict(body: str) -> str:
    t = re.sub(r"\s+", " ", body)
    if "Login succeed" in body or '"result":1' in body:
        return "[OK] 登录成功（假账号不该发生）"
    if "已在线" in body:
        return "[OK] 被当作登录请求处理（本机已在线）→ 这种写法可用"
    if "msga=" in body and "Dr.COMWebLoginID_2" in body:
        return "[OK] 被当作登录请求处理 → 这种写法可用"
    if '"result":0' in body and "msg" in body:
        return "[OK] 被当作登录请求处理 → 这种写法可用"
    if "无法获取用户认证账号" in body:
        return "[!] 参数不对，服务器没认出账号"
    if "<!DOCTYPE html>" in body and "EPortal" in body:
        return "[!] 返回后台页面 → 这种写法不可用"
    return "[?] 需要人工判断"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", help="绑定源地址（不填=默认路由）")
    args = parser.parse_args()

    modern_path = (
        "/eportal/portal/login?callback=dr1003&login_method=1"
        "&user_account=" + urllib.parse.quote(f",0,{FAKE_USER}")
        + "&user_password=" + urllib.parse.quote(FAKE_PASS)
        + "&jsVersion=4.1.3&terminal_type=1&lang=zh-cn"
    )
    legacy_query = (
        "/eportal/?c=ACSetting&a=Login&ver=1.0"
        "&DDDDD=" + FAKE_USER + "&upass=" + urllib.parse.quote(FAKE_PASS)
        + "&0MKKey=123456&url=drappall&terminal_type=1&lang=zh-cn"
    )
    legacy_body = {"DDDDD": FAKE_USER, "upass": FAKE_PASS, "0MKKey": "123456",
                   "R6": "0", "url": "drappall", "terminal_type": "1", "lang": "zh-cn"}

    trials = [
        ("A. 新版接口 GET + 带 AJAX 头", "GET", modern_path, None, True, False),
        ("B. 新版接口 GET + 不带任何自定义头 ← 手机工具最容易实现的写法",
         "GET", modern_path, None, False, False),
        ("C. 新版接口 GET + Host 带端口（模拟普通浏览器）",
         "GET", modern_path, None, False, True),
        ("D. 旧版接口 GET（全部参数放 URL）", "GET", legacy_query, None, False, False),
        ("E. 旧版接口 POST（电脑版现在用的写法，作对照）",
         "POST", "/eportal/?c=ACSetting&a=Login&ver=1.0", legacy_body, True, False),
    ]

    print(f"探测 {HOST}:{PORT}，使用假账号 {FAKE_USER}\n")
    for name, method, path, data, ajax, hostport in trials:
        code, body = send(method, path, data, args.bind, ajax, hostport)
        print(name)
        print(f"   {method} {path[:78]}")
        print(f"   → HTTP {code}: {re.sub(r'\\s+', ' ', body).strip()[:150]}")
        print(f"   {verdict(body)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
