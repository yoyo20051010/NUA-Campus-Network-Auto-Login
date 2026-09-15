#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
探测：登录提交后出现的"请完成安全验证"页面到底是什么。

用一个**不存在的假账号**提交（不会影响任何真实账号、也不会动校园网会话），
把返回的页面 HTML 完整保存下来，用于分析滑块验证的触发条件和后续流程。

    python probe_verify.py
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import pathlib
import time

import campus_login as c

SERVICE = "https%3A%2F%2Fc.nua.edu.cn%2Fcas%2FwifiLogin%2FinnerLogin.jsp"
URL = f"https://c.nua.edu.cn/cas/login?service={SERVICE}"
OUT_HTML = c.LOG_DIR / "verify-page.html"
OUT_SHOT = c.SHOT_DIR / "verify-page.png"


def main() -> int:
    from playwright.sync_api import sync_playwright

    c.setup_logging()
    with sync_playwright() as playwright:
        ctx = c._open_browser(playwright, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(30000)
            page.on("dialog", lambda d: (c.log.info("弹窗: %s", d.message), d.dismiss()))

            c.log.info("打开 %s", URL)
            page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)

            c.log.info("提交前 .slidingverification 的 class = %s",
                       page.eval_on_selector(".slidingverification", "e => e.className"))

            # 用假账号提交
            page.fill("input#username", "zzz000000000")
            page.fill("#passwordShow", "not-a-real-password")
            c._tick_checkbox(page, "#agreement-box-pccheckbox1", "用户协议")
            page.click("#passbutton")
            c.log.info("已提交, 等待 8 秒观察服务器返回的页面 ...")
            page.wait_for_timeout(8000)

            try:
                cls = page.eval_on_selector(".slidingverification", "e => e.className")
            except Exception:
                cls = "(页面上没有 .slidingverification 元素)"
            visible = page.eval_on_selector(
                ".slidingverification",
                "e => { const s = getComputedStyle(e); return s.display !== 'none' && e.getBoundingClientRect().height > 0; }",
            ) if page.query_selector(".slidingverification") else False

            c.log.info("提交后 .slidingverification 的 class = %s", cls)
            c.log.info("提交后是否可见 = %s", visible)
            c.log.info("当前 URL = %s", page.url)

            html = page.content()
            OUT_HTML.write_text(html, encoding="utf-8")
            c.SHOT_DIR.mkdir(exist_ok=True)
            page.screenshot(path=str(OUT_SHOT), full_page=True)
            c.log.info("HTML 已保存: %s (%d 字节)", OUT_HTML, len(html))
            c.log.info("截图已保存: %s", OUT_SHOT)

            # 看看页面里有没有处理滑块的脚本
            hits = [kw for kw in ("slidingverification", "checkHuman", "verify", "captcha") if kw in html]
            c.log.info("页面 HTML 中包含的关键字: %s", hits or "无")
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
