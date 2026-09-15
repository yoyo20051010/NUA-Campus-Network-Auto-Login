#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
登录表单识别与填充自测。

用本地合成的页面模拟两种登录页，验证 campus_login 能不能正确识别并填写：
  1. 统一身份认证页 (cas)   —— 有线网段走这条
  2. Dr.COM 门户原生表单 (drcom) —— 校园无线网段走这条

不联网、不碰学校系统。

    python selftest_forms.py
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import pathlib
import tempfile

from campus_login import _fill_login_form, _submit_form, _wait_for_form_kind, setup_logging

CAS_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<form id="fm1" action="/cas/login" method="post">
  <input id="username" name="username" type="text">
  <input id="passwordShow" type="password">
  <input id="password" name="password" type="hidden">
  <input id="agreement-box-pccheckbox1" type="checkbox">
  <div id="slidingbox"><div id="content"><div id="shadow"></div>
    <div id="slidingbox_tip"></div></div>
    <div><div id="slidingbox_block_left"></div><div id="slidingbox_block"></div></div></div>
  <input id="passbutton" type="button" value="登录" onclick="window.__submitted=true">
</form></body></html>"""

DRCOM_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<form id="f4" name="f4">
  <input name="DDDDD" type="text">
  <input name="upass" type="password">
  <input name="C1" type="checkbox">
  <input name="0MKKey" type="button" value="登录" onclick="window.__submitted=true">
</form>
<script>window.f4 = document.forms[0];</script>
</body></html>"""

DRCOM_CAPTCHA_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<form id="f4" name="f4">
  <input name="DDDDD" type="text">
  <input name="upass" type="password">
  <input name="captcha" type="text" style="display:block">
  <input name="0MKKey" type="button" value="登录" onclick="window.__submitted=true">
</form>
<script>window.f4 = document.forms[0];</script>
</body></html>"""

ACCOUNT = "B000000000"
PASSWORD = "test-password-123"


def _new_ctx(playwright):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(pathlib.Path(tempfile.mkdtemp(prefix="form-selftest-"))),
        channel="msedge",
        headless=True,
        viewport={"width": 1200, "height": 800},
    )


def _value(page, selector: str) -> str:
    return page.eval_on_selector(selector, "e => e.value")


def main() -> int:
    setup_logging()
    from playwright.sync_api import sync_playwright

    passed = 0
    with sync_playwright() as playwright:
        # --- 情况 1: 统一身份认证页 ---
        with _new_ctx(playwright) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_content(CAS_HTML)
            kind = _wait_for_form_kind(page, 5)
            filled = _fill_login_form(page, kind, ACCOUNT, PASSWORD) if kind else False
            agreed = page.eval_on_selector("#agreement-box-pccheckbox1", "e => e.checked")
            _submit_form(page, kind) if kind else None
            ok = (
                kind == "cas"
                and filled
                and _value(page, "#username") == ACCOUNT
                and _value(page, "#passwordShow") == PASSWORD
                and agreed
                and page.evaluate("() => window.__submitted === true")
            )
            print(f"[{'通过' if ok else '失败'}] 统一身份认证页: 识别={kind} 填写={filled} 勾选协议={agreed}")
            passed += 1 if ok else 0

        # --- 情况 2: Dr.COM 门户原生表单 ---
        with _new_ctx(playwright) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_content(DRCOM_HTML)
            kind = _wait_for_form_kind(page, 5)
            filled = _fill_login_form(page, kind, ACCOUNT, PASSWORD) if kind else False
            agreed = page.eval_on_selector("input[name='C1']", "e => e.checked")
            _submit_form(page, kind) if kind else None
            ok = (
                kind == "drcom"
                and filled
                and _value(page, "input[name='DDDDD']") == ACCOUNT
                and _value(page, "input[name='upass']") == PASSWORD
                and agreed
                and page.evaluate("() => window.__submitted === true")
            )
            print(f"[{'通过' if ok else '失败'}] Dr.COM 门户表单: 识别={kind} 填写={filled} 勾选协议={agreed}")
            passed += 1 if ok else 0

        # --- 情况 3: 门户要求图形验证码时应明确报告失败 ---
        with _new_ctx(playwright) as ctx:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_content(DRCOM_CAPTCHA_HTML)
            kind = _wait_for_form_kind(page, 5)
            filled = _fill_login_form(page, kind, ACCOUNT, PASSWORD) if kind else True
            ok = kind == "drcom" and filled is False
            print(f"[{'通过' if ok else '失败'}] 图形验证码场景: 识别={kind} 正确拒绝提交={filled is False}")
            passed += 1 if ok else 0

    print(f"\n{passed}/3 项通过")
    return 0 if passed == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
