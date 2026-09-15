#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
注销接口测试 + 自动登录验证（一次性自包含脚本）。

为什么单独写成一个脚本：一旦注销成功，本机就会断网，写这个脚本的 AI 也会掉线，
所以整个过程必须在本地独立跑完，并把每一步写进日志，等网络恢复后再看结果。

执行顺序:
  1. 记录当前认证状态
  2. 依次尝试各个注销接口, 每试一个就检查是否已下线
  3. 确认下线后, 立刻执行一次自动登录
  4. 如果自动登录失败, 再等最多 3 分钟让计划任务兜底
  5. 全过程写入 logs/logout_test.log

    python logout_test.py
"""

from __future__ import annotations

# 让 tools/ 下的脚本能导入仓库根目录的模块
import pathlib as _pl
import sys as _sys
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import logging
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import campus_login as c

LOG_FILE = c.LOG_DIR / "logout_test.log"

log = logging.getLogger("logout_test")


def setup() -> None:
    c.LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.setLevel(logging.INFO)


def request(url: str, data: bytes | None = None, timeout: int = 10) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "User-Agent": c.USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            return resp.status, resp.read(2000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(500).decode("utf-8", "ignore")
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"


def logout_candidates(cfg: dict) -> list[tuple[str, str, bytes | None]]:
    portal = cfg["portal_url"].rstrip("/")
    return [
        (
            "Dr.COM 门户注销(ACSetting)",
            f"{portal}:801/eportal/?c=ACSetting&a=Logout&ver=1.0",
            b"url=drappall",
        ),
        (
            "Dr.COM 门户注销(portal/logout)",
            f"{portal}:801/eportal/portal/logout",
            b"",
        ),
        (
            "统一认证注销(wifiLogin/isLogout)",
            "https://c.nua.edu.cn/cas/wifiLogin/isLogout",
            b"",
        ),
        (
            "统一认证注销(cas/logout)",
            "https://c.nua.edu.cn/cas/logout",
            b"",
        ),
    ]


def main() -> int:
    setup()
    cfg = c.load_config()
    account, password = c.load_secret()

    log.info("=" * 60)
    log.info("注销测试开始")

    state, detail = c.evaluate_state(cfg)
    log.info("注销前状态: %s - %s", state, detail)
    if state != c.STATE_ONLINE:
        log.warning("当前不在线, 无需注销; 直接尝试登录验证")
    else:
        logged_out = False
        for name, url, data in logout_candidates(cfg):
            log.info("尝试注销接口: %s", name)
            log.info("  POST/GET %s", url)
            status, body = request(url, data)
            log.info("  返回: HTTP %s %s", status, body.replace("\n", " ")[:300])
            time.sleep(3)
            if not c.is_online(cfg, quiet=True):
                log.info("  -> 已下线! 生效的接口是: %s", name)
                logged_out = True
                break
            log.info("  -> 仍然在线, 试下一个")

        if not logged_out:
            log.warning("所有注销接口都没能把会话清掉, 保持在线")
            log.info("结论: 注销接口不可用, 需要用别的方式制造未认证状态")
            return 1

        st, dt = c.evaluate_state(cfg)
        log.info("注销后状态: %s - %s", st, dt)

    # 立刻执行一次自动登录
    log.info("-" * 60)
    log.info("开始验证自动登录")
    ok = c.do_login(cfg, account, password)
    log.info("自动登录返回: %s", ok)

    if not ok:
        log.info("自动登录未成功, 等计划任务兜底(最多 3 分钟)")
        for _ in range(18):
            time.sleep(10)
            if c.is_online(cfg, quiet=True):
                ok = True
                log.info("计划任务已把网络恢复")
                break

    final_state, final_detail = c.evaluate_state(cfg)
    log.info("最终状态: %s - %s", final_state, final_detail)
    log.info("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
