#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
滑块自动化自测。

学校登录页的滑块在直接访问时是隐藏的, 没法在线验证拖拽逻辑,
所以这里用一份复刻了同样前端逻辑(同样的 DOM 结构 + 同样的 mousedown/mousemove/mouseup 判定)
的本地页面来跑一遍, 确认 campus_login._solve_slider 能真正把滑块拖到位。

    python selftest_slider.py
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import pathlib
import tempfile

from campus_login import _slider_ok, _solve_slider, setup_logging

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>slider</title>
<style>
  body { font-family: sans-serif; padding: 40px; }
  #content { position: relative; width: 400px; height: 160px; background: #cfd8dc; overflow: hidden; }
  #shadow { position: absolute; width: 44px; height: 44px; background: rgba(0,0,0,.25); }
  #slidingbox_tip { position: absolute; left: 0; top: 0; width: 44px; height: 44px; background: #607d8b; }
  .track { position: relative; width: 400px; height: 44px; margin-top: 8px; background: #eceff1; }
  #slidingbox_block { position: absolute; left: 0; top: 0; width: 44px; height: 44px; background: #d32f2f; cursor: pointer; }
  #slidingbox_block_left { position: absolute; left: 0; top: 0; height: 44px; background: #a5d6a7; }
</style></head>
<body>
<div id="slidingbox">
  <div id="content">
    <div id="shadow"></div>
    <div id="slidingbox_tip"></div>
  </div>
  <div class="track">
    <div id="slidingbox_block_left"></div>
    <div id="slidingbox_block"></div>
  </div>
</div>
<script>
/* 逻辑与南艺登录页 login.js 中的实现一致 */
window.__sliderOk = false;
var content = document.querySelector('#content');
var shadow = document.querySelector('#shadow');
var tip = document.querySelector('#slidingbox_tip');
var block = document.querySelector('#slidingbox_block');
var leftblock = document.querySelector('#slidingbox_block_left');
var maxWidth = content.clientWidth - shadow.offsetWidth;
var maxHeight = content.clientHeight - shadow.offsetHeight;
var ranX = Math.round(Math.random() * maxWidth < 40 ? 40 : Math.random() * maxWidth);
var ranY = Math.round(Math.random() * maxHeight);
shadow.style.left = ranX + 'px';
shadow.style.top = ranY + 'px';
tip.style.top = ranY + 'px';
tip.style.backgroundPosition = -ranX + 'px ' + (-ranY) + 'px';

block.onmousedown = function (e) {
  var ev = window.event || e;
  var startX = ev.x;
  document.onmousemove = function (e) {
    var ev = window.event || e;
    var x = ev.x;
    var left = x - startX;
    if (left <= 0) left = 0;
    if (left >= maxWidth) left = maxWidth;
    block.style.left = left + 'px';
    tip.style.left = left + 'px';
    leftblock.style.width = left + 5 + 'px';
  };
};

document.onmouseup = function (e) {
  document.onmousemove = null;
  if (e.target !== block) return;
  if (Math.abs(tip.offsetLeft - shadow.offsetLeft) <= 2) {
    window.__sliderOk = true;
  } else {
    block.style.left = 0;
    leftblock.style.width = 0;
    tip.style.left = 0;
    var x2 = Math.round(Math.random() * maxWidth) < 40 ? 40 : Math.round(Math.random() * maxWidth);
    var y2 = Math.round(Math.random() * maxHeight);
    shadow.style.left = x2 + 'px';
    shadow.style.top = y2 + 'px';
    tip.style.top = y2 + 'px';
    tip.style.backgroundPosition = -x2 + 'px ' + (-y2) + 'px';
  }
};
window.ondragstart = function () { return false; };
</script>
</body></html>
"""


def main() -> int:
    setup_logging()
    from playwright.sync_api import sync_playwright

    page_file = pathlib.Path(tempfile.mkdtemp(prefix="slider-selftest-")) / "slider.html"
    page_file.write_text(HTML, encoding="utf-8")
    url = page_file.as_uri()

    passed = 0
    with sync_playwright() as playwright:
        for attempt in range(1, 4):  # 缺口位置每次随机, 多跑几轮
            with playwright.chromium.launch_persistent_context(
                user_data_dir=str(pathlib.Path(tempfile.mkdtemp(prefix="slider-profile-"))),
                channel="msedge",
                headless=True,
                viewport={"width": 1000, "height": 700},
            ) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url)
                state = page.evaluate(
                    "() => { const s=document.querySelector('#shadow'),"
                    " t=document.querySelector('#slidingbox_tip');"
                    " return {shadow:s.offsetLeft, tip:t.offsetLeft}; }"
                )
                result = _solve_slider(page)
                ok = bool(page.evaluate("() => window.__sliderOk === true")) and _slider_ok(page)
                print(f"第 {attempt} 轮: 缺口={state['shadow']}px 结果={result} 通过={ok}")
                passed += 1 if ok else 0

    print(f"\n{passed}/3 轮通过")
    return 0 if passed == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
