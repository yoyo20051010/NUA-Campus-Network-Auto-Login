#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
探测 Dr.COM 门户登录接口的参数格式（无线网段用）。

用【不存在的假账号】尝试多种参数组合，观察服务器返回的提示怎么变化：
  如果某个组合让服务器从"无法获取用户认证账号"变成"账号不存在/密码错误"，
  说明那个参数名才是对的。全程不涉及真实账号，也没有真实登录。

绑定到无线网卡的地址发出，所以不需要禁用有线网卡。

    python probe_drcom.py
    python probe_drcom.py --bind 10.54.1.100       # 手动指定无线网卡的地址
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import argparse
import http.client
import re

import campus_http as c

HOST = "10.255.255.2"
PORT = 801
FAKE_USER = "zzz000000000"
FAKE_PASS = "not-a-real-password"


def post(path: str, data: dict, bind_ip: str, timeout: int = 8) -> tuple[int, str]:
    body = "&".join(f"{k}={v}" for k, v in data.items())
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout, source_address=(bind_ip, 0))
    try:
        conn.request("POST", path, body=body.encode("utf-8"), headers={
            "User-Agent": c.USER_AGENT,
            "Host": f"{HOST}",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Length": str(len(body.encode("utf-8"))),
        })
        resp = conn.getresponse()
        return resp.status, resp.read().decode("gbk", "ignore")
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=False, help="绑定的源地址（无线网卡地址）")
    parser.add_argument("--iface", default="WLAN")
    args = parser.parse_args()

    import probe_wifi
    bind_ip = args.bind or probe_wifi.find_ip(args.iface)
    if not bind_ip:
        print("找不到无线网卡地址")
        return 1
    print(f"绑定源地址: {bind_ip}")
    print(f"用假账号 {FAKE_USER} 试各种参数格式（不会影响任何真实账号）\n")

    base = {
        "0MKKey": "123456",
        "R1": "", "R2": "", "R3": "", "R6": "0", "para": "", "v6ip": "",
        "terminal_type": "1", "lang": "zh-cn",
    }

    trials = [
        ("① DDDDD + upass（当前实现）",
         "/eportal/portal/login", dict(base, DDDDD=FAKE_USER, upass=FAKE_PASS)),
        ("② DDDDD + upass + 后缀 @njxy",
         "/eportal/portal/login", dict(base, DDDDD=f"{FAKE_USER}@njxy", upass=FAKE_PASS)),
        ("③ user_account + user_password",
         "/eportal/portal/login", dict(base, user_account=FAKE_USER, user_password=FAKE_PASS)),
        ("④ user_account + user_password + 后缀",
         "/eportal/portal/login", dict(base, user_account=f"{FAKE_USER}@njxy", user_password=FAKE_PASS)),
        ("⑤ account + password",
         "/eportal/portal/login", dict(base, account=FAKE_USER, password=FAKE_PASS)),
        ("⑥ DDDDD + upass + wlan_user_ip",
         "/eportal/portal/login",
         dict(base, DDDDD=f"{FAKE_USER}@njxy", upass=FAKE_PASS,
              wlan_user_ip=bind_ip, wlan_user_mac="000000000000", jsVersion="1755483350029")),
        ("⑦ 旧版接口 ACSetting + DDDDD/upass",
         "/eportal/?c=ACSetting&a=Login&ver=1.0",
         dict(base, DDDDD=f"{FAKE_USER}@njxy", upass=FAKE_PASS, url="drappall")),
        ("⑧ /drcom/login 老路径",
         "/drcom/login", dict(base, DDDDD=f"{FAKE_USER}@njxy", upass=FAKE_PASS)),
    ]

    for name, path, data in trials:
        code, body = post(path, data, bind_ip)
        text = body.strip().replace("\n", " ")
        if len(text) > 200:
            text = text[:200] + " ..."
        print(f"{name}")
        print(f"   POST {path}")
        print(f"   → HTTP {code}: {text}")
        print()

    # 第二阶段：固定用认得出账号的 ACSetting 接口，比较不同账号后缀的提示差异
    print("=" * 60)
    print("第二阶段：比较不同账号格式（仍然使用假账号）")
    print("=" * 60)
    suffix_trials = [
        ("不带后缀", FAKE_USER),
        ("@njxy（无线配置里的后缀）", f"{FAKE_USER}@njxy"),
        ("@dx（电信）", f"{FAKE_USER}@dx"),
        ("@lt（联通）", f"{FAKE_USER}@lt"),
        ("@dx.njxy", f"{FAKE_USER}@dx.njxy"),
    ]
    for label, user_value in suffix_trials:
        data = dict(base, DDDDD=user_value, upass=FAKE_PASS, url="drappall")
        code, body = post("/eportal/?c=ACSetting&a=Login&ver=1.0", data, bind_ip)
        match = re.search(r"msga='([^']*)'", body)
        msg = match.group(1) if match else body.strip().replace("\n", " ")[:120]
        print(f"   {label:26} → {msg}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
