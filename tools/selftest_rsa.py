#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSA 加密实现比对测试。

用学校登录页自己的 security.js（RSAUtils.encryptedString）加密几个测试密码，
和 campus_http.py 里的纯 Python 实现逐字符比对。完全确定、不涉及真实账号。

    python selftest_rsa.py
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import campus_http

TEST_PASSWORDS = [
    "abc123",
    "2026000000",                      # 学号当密码的情况（占位符）
    "P@ssw0rd!2026#",
    "a" * 125,                          # 正好一块（126 字节含一个补零）
    "b" * 126,                          # 满一块
    "c" * 200,                          # 跨两块
]


def main() -> int:
    from playwright.sync_api import sync_playwright

    campus_http.setup_logging()
    url = (
        f"{campus_http.DEFAULT_CONFIG['cas_login_url']}?service="
        + urllib_quote(campus_http.DEFAULT_CONFIG["service"])
    )

    passed = 0
    with sync_playwright() as playwright:
        ctx = playwright.chromium.launch_persistent_context(
            user_data_dir=str(campus_http.APP_DIR / "browser-profile"),
            channel="msedge", headless=True, viewport={"width": 1200, "height": 800},
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_function("() => typeof RSAUtils !== 'undefined'", timeout=15000)
            modulus = campus_http.MODULUS_HEX.replace("\n", "").replace(" ", "")

            for pwd in TEST_PASSWORDS:
                theirs = page.evaluate(
                    """(args) => {
                        RSAUtils.setMaxDigits(131);
                        var key = RSAUtils.getKeyPair("010001", '', args.modulus);
                        return RSAUtils.encryptedString(key, args.pwd);
                    }""",
                    {"modulus": modulus, "pwd": pwd},
                )
                mine = campus_http.rsa_encrypt(pwd)
                ok = theirs == mine
                label = pwd if len(pwd) <= 20 else f"{pwd[:12]}...({len(pwd)} 字节)"
                print(f"[{'通过' if ok else '失败'}] {label}")
                if not ok:
                    print(f"    学校: {theirs[:80]}...")
                    print(f"    我们: {mine[:80]}...")
                passed += 1 if ok else 0
        finally:
            ctx.close()

    print(f"\n{passed}/{len(TEST_PASSWORDS)} 项通过")
    return 0 if passed == len(TEST_PASSWORDS) else 1


def urllib_quote(value: str) -> str:
    import urllib.parse
    return urllib.parse.quote(value, safe="")


if __name__ == "__main__":
    raise SystemExit(main())
