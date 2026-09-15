#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京艺术学院 校园网自动登录

认证链路:
    10.255.255.2 (Dr.COM ePortal v4.0)
        -> https://c.nua.edu.cn/cas/wifiLogin/innerLogin.jsp  统一身份认证入口
        -> https://c.nua.edu.cn/cas/login                     账号密码 + 滑块拼图

常用命令:
    python campus_login.py --set-password    设置/更新账号密码 (DPAPI 加密保存)
    python campus_login.py --check           检测当前是否在线
    python campus_login.py --login           执行一次登录 (已在线则跳过)
    python campus_login.py --watch           看门狗: 持续检测, 掉线自动重登
    python campus_login.py --dry-run         演练: 打开登录页, 定位表单与滑块并试解, 不提交

安全闸: 只有在"校园网认证页(10.255.255.2)确实能加载"并且"当前未认证"时才会尝试登录。
        连着家里路由 / 手机热点 / 其它网络时一律跳过, 不会乱试密码。

安全性: 密码使用 Windows DPAPI 加密后存放在 secret.bin, 只能被当前 Windows 用户解密。
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import ctypes.wintypes as wt
import datetime
import getpass
import json
import logging
import os
import pathlib
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request

APP_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
SECRET_FILE = APP_DIR / "secret.bin"
LOG_DIR = APP_DIR / "logs"
SHOT_DIR = APP_DIR / "screenshots"
PROFILE_DIR = APP_DIR / "browser-profile"

DEFAULT_CONFIG = {
    "portal_url": "http://10.255.255.2/",
    "cas_entry_url": "https://c.nua.edu.cn/cas/wifiLogin/innerLogin.jsp",
    "status_url": "https://c.nua.edu.cn/cas/wifiLogin/isLogin",
    "probe_url": "http://www.baidu.com/",
    # 已知的校园网段(有线 10.12.x、无线 10.54.x, 以及门户脚本自己用的判断范围)
    "campus_ip_ranges": [["1.1.1.1", "10.51.255.255"], ["10.52.0.0", "10.63.255.255"],
                         ["10.128.0.1", "10.129.255.255"]],
    # 是否强制要求出口地址在校园网段内。默认关闭: 门户能加载已经说明在校园网,
    # 地址判断只作为提示(校园网可能有多个网段, 写死了会误伤)。
    "require_campus_ip": False,
    "interval": 30,
    "headless": True,
    "login_timeout": 90,
    # 夜间限制时段：这段时间学校不允许学生账号认证，就没必要每分钟去试
    # days 用 Python 的星期编号：0=周一 … 6=周日。默认周一~周五 00:00-06:00
    "quiet_hours": {
        "enabled": True,
        "start": "00:00",
        "end": "06:00",
        "days": [0, 1, 2, 3, 4],
    },
    # 登录失败后的退避秒数：2 分钟、5 分钟、15 分钟、30 分钟（之后一直 30 分钟）
    "failure_backoff": [120, 300, 900, 1800],
}

log = logging.getLogger("campus")

STATE_ONLINE = "online"
STATE_OFFLINE_CAMPUS = "offline_campus"
STATE_NOT_CAMPUS = "not_campus"

STATE_TEXT = {
    STATE_ONLINE: "已认证在线",
    STATE_OFFLINE_CAMPUS: "在校园网且未认证",
    STATE_NOT_CAMPUS: "不在校园网环境",
}

STATE_FILE = LOG_DIR / "last_state.txt"
LOCK_FILE = LOG_DIR / "run.lock"
RETRY_FILE = LOG_DIR / "retry_state.json"
MODE_FILE = LOG_DIR / "last_mode.txt"
LOCK_STALE_SECONDS = 900


def setup_logging(verbose: bool = True) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    log_file = LOG_DIR / "campus_login.log"
    try:
        if log_file.exists() and log_file.stat().st_size > 2 * 1024 * 1024:
            log_file.replace(LOG_DIR / "campus_login.log.1")
    except OSError:
        pass

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    if verbose and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)

    log.setLevel(logging.INFO)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_to_bytes(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def dpapi_protect(data: bytes) -> bytes:
    """用当前 Windows 用户的密钥加密数据。"""
    buf = ctypes.create_string_buffer(data)
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptProtectData 失败")
    try:
        return _blob_to_bytes(blob_out)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    """解密 dpapi_protect 加密的数据。"""
    buf = ctypes.create_string_buffer(data)
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("CryptUnprotectData 失败: 可能换了 Windows 用户或换了机器")
    try:
        return _blob_to_bytes(blob_out)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def save_secret(account: str, password: str) -> None:
    raw = json.dumps({"account": account, "password": password}).encode("utf-8")
    SECRET_FILE.write_bytes(dpapi_protect(raw))


def load_secret() -> tuple[str, str]:
    if not SECRET_FILE.exists():
        raise SystemExit("还没有保存账号密码, 请先运行: python campus_login.py --set-password")
    obj = json.loads(dpapi_unprotect(SECRET_FILE.read_bytes()).decode("utf-8"))
    return obj["account"], obj["password"]


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log.warning("config.json 读取失败, 使用默认配置: %s", exc)
    return cfg


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0 Safari/537.36"


def _post_json(url: str, timeout: int = 6) -> dict | None:
    req = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={"User-Agent": USER_AGENT, "X-Requested-With": "XMLHttpRequest"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def _probe_internet(url: str, timeout: int = 6) -> bool:
    """直接访问一个站点, 判断请求是否被校园网门户劫持。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if "Drcom" in resp.headers.get("Server", ""):
                return False
            body = resp.read(4096).decode("utf-8", "ignore")
            return "Dr.COMWebLogin" not in body and "DrcomServer" not in body
    except Exception:
        return False


def is_online(cfg: dict, quiet: bool = False) -> bool:
    """判断当前是否已经过认证。只读接口, 不会导致注销。"""
    data = _post_json(cfg["status_url"])
    if data is not None and data.get("success"):
        return True
    # 接口说未登录 / 接口不可用(离线时会被门户劫持), 都用真实访问再确认一次:
    # 有线走统一认证、无线走门户原生登录, 两种会话的接口状态可能不同。
    if _probe_internet(cfg["probe_url"]):
        return True
    if data is not None and not quiet:
        log.info("状态接口返回未登录: %s", data)
    return False


def local_source_ip(host: str, port: int = 80, timeout: float = 1.0) -> str | None:
    """本机访问 host 时会用哪个地址做源地址(不实际发包)。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            return sock.getsockname()[0]
    except Exception:
        return None


def _ip_to_int(ip: str) -> int:
    parts = [int(x) for x in ip.split(".")]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def in_campus_range(ip: str, ranges: list) -> bool:
    try:
        value = _ip_to_int(ip)
    except (ValueError, IndexError):
        return False
    for low, high in ranges:
        if _ip_to_int(low) <= value <= _ip_to_int(high):
            return True
    return False


def campus_portal_reachable(cfg: dict, timeout: int = 5) -> tuple[bool, str]:
    """确认 10.255.255.2 上的校园网认证页能真正加载出来。"""
    url = cfg["portal_url"]
    timeout = int(cfg.get("portal_timeout", timeout))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            server = resp.headers.get("Server", "")
            body = resp.read(8192).decode("gbk", "ignore")
    except Exception as exc:
        return False, f"校园网门户 {url} 无法访问: {exc}"

    haystack = f"{server} {body}".lower()
    hits = [m for m in ("drcomserver", "dr.comweblogin", "eportal") if m in haystack]
    if hits:
        return True, f"校园网门户可加载(识别到 {'/'.join(hits)})"
    return False, f"{url} 可以访问, 但返回的不是校园网认证页"


def evaluate_state(cfg: dict) -> tuple[str, str]:
    """
    判断当前处境:
      online         已认证在线
      offline_campus 在校园网且未认证 -> 需要登录
      not_campus     不在校园网环境 -> 什么都不做
    """
    host = urllib.parse.urlsplit(cfg["portal_url"]).hostname or "10.255.255.2"
    ip = local_source_ip(host)
    where = f"出口地址 {ip}" if ip else "未取到出口地址"

    reachable, detail = campus_portal_reachable(cfg)
    if not reachable:
        return STATE_NOT_CAMPUS, f"{detail} ({where})"

    ip_known = bool(ip) and in_campus_range(ip, cfg["campus_ip_ranges"])
    if not ip_known:
        if cfg.get("require_campus_ip", False):
            return STATE_NOT_CAMPUS, f"门户可访问但 {where} 不属于已知校园网段"
        log.debug("门户可访问, 但 %s 不在已知校园网段内(仅提示)", where)

    if is_online(cfg, quiet=True):
        return STATE_ONLINE, f"已在线 ({where})"
    note = "" if ip_known else " [出口地址不在已知校园网段]"
    return STATE_OFFLINE_CAMPUS, f"在校园网且未认证 ({where}){note}"


def note_state(state: str, detail: str, always: bool = False) -> bool:
    """状态变化时才写日志, 避免计划任务每分钟刷屏。返回是否发生变化。"""
    previous = None
    try:
        previous = STATE_FILE.read_text(encoding="utf-8").split(" ", 1)[0] or None
    except OSError:
        previous = None
    try:
        STATE_FILE.parent.mkdir(exist_ok=True)
        STATE_FILE.write_text(f"{state} {detail}", encoding="utf-8")
    except OSError:
        pass

    changed = previous != state
    if changed or always:
        log.info("状态: %s - %s", STATE_TEXT.get(state, state), detail)
    return changed


# --------------------------------------------------------------------------- #
# 夜间免打扰 + 失败退避（和纯 HTTP 版行为一致）
# --------------------------------------------------------------------------- #
def _hhmm_to_minutes(text: str) -> int | None:
    try:
        hh, mm = text.split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def in_quiet_hours(cfg: dict, now: datetime.datetime | None = None) -> bool:
    """当前是否处在学校禁止认证的时段（默认周一~周五 00:00-06:00）。"""
    quiet = cfg.get("quiet_hours") or {}
    if not quiet.get("enabled"):
        return False
    now = now or datetime.datetime.now()

    days = quiet.get("days")
    if days is not None and now.weekday() not in days:
        return False

    start = _hhmm_to_minutes(quiet.get("start", "00:00"))
    end = _hhmm_to_minutes(quiet.get("end", "06:00"))
    if start is None or end is None:
        return False

    current = now.hour * 60 + now.minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def note_mode(mode: str, message: str) -> bool:
    """模式（正常/夜间静默/退避）变化时才写一行日志。"""
    previous = None
    try:
        previous = MODE_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        previous = None
    try:
        MODE_FILE.parent.mkdir(exist_ok=True)
        MODE_FILE.write_text(mode, encoding="utf-8")
    except OSError:
        pass
    if previous != mode:
        log.info(message)
        return True
    return False


def load_retry() -> dict:
    try:
        return json.loads(RETRY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_retry(data: dict) -> None:
    try:
        RETRY_FILE.parent.mkdir(exist_ok=True)
        RETRY_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def clear_retry() -> None:
    try:
        RETRY_FILE.unlink()
    except OSError:
        pass


def backoff_seconds(cfg: dict, failures: int) -> int:
    table = cfg.get("failure_backoff") or [120]
    idx = min(max(failures, 1), len(table)) - 1
    return int(table[idx])


def _pid_alive(pid: int) -> bool:
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return False


def acquire_lock() -> bool:
    """防止计划任务和手动运行同时启动浏览器(会抢同一个配置目录)。"""
    try:
        if LOCK_FILE.exists():
            age = time.time() - LOCK_FILE.stat().st_mtime
            try:
                pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = 0
            if pid and age < LOCK_STALE_SECONDS and _pid_alive(pid):
                return False
        LOCK_FILE.parent.mkdir(exist_ok=True)
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(release_lock)
        return True
    except OSError:
        return True


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


LOGIN_FORM_SELECTOR = "input#username"


def _open_browser(playwright, headless: bool):
    """优先复用系统已装的 Edge, 失败再退回 Playwright 自带 Chromium。"""
    last_err: Exception | None = None
    for kwargs in ({"channel": "msedge"}, {}):
        try:
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                viewport={"width": 1366, "height": 900},
                args=["--no-first-run", "--no-default-browser-check"],
                **kwargs,
            )
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"无法启动浏览器: {last_err}")


def _slider_state(page) -> dict | None:
    try:
        return page.evaluate(
            """() => {
                const s = document.querySelector('#shadow');
                const t = document.querySelector('#slidingbox_tip');
                if (!s || !t) return null;
                return {delta: s.offsetLeft - t.offsetLeft};
            }"""
        )
    except Exception:
        return None


def _slider_ok(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                    const s = document.querySelector('#shadow');
                    const t = document.querySelector('#slidingbox_tip');
                    if (!s || !t) return false;
                    return Math.abs(t.offsetLeft - s.offsetLeft) <= 2;
                }"""
            )
        )
    except Exception:
        return False


def _slider_draggable(page) -> bool:
    """滑块是否真的展开、可见、并且鼠标点得到(没被别的元素盖住)。"""
    try:
        return bool(
            page.evaluate(
                """() => {
                    const b = document.querySelector('#slidingbox_block');
                    if (!b) return false;
                    const r = b.getBoundingClientRect();
                    if (r.width < 5 || r.height < 5) return false;
                    const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                    return !!el && (el === b || b.contains(el));
                }"""
            )
        )
    except Exception:
        return False


def _solve_slider(page) -> str:
    """解开滑块拼图, 返回 dragged / forced / absent / failed。"""
    if not page.query_selector("#slidingbox"):
        return "absent"

    state = _slider_state(page)
    if not state:
        return "failed"
    delta = float(state["delta"])

    block = page.query_selector("#slidingbox_block")
    box = block.bounding_box() if block else None
    if box and abs(delta) >= 1 and _slider_draggable(page):
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.mouse.down()
        steps = 20
        for i in range(1, steps + 1):
            page.mouse.move(cx + delta * i / steps, cy)
            time.sleep(0.015)
        page.mouse.up()
        if _slider_ok(page):
            return "dragged"
        log.info("模拟拖拽未对齐, 改用直接归位")
    else:
        log.info("滑块当前未展开或被遮挡, 使用直接归位")

    # 兜底: 该页面只在前端比较滑块与缺口位置, 直接把位置对齐
    try:
        page.evaluate(
            """() => {
                const s = document.querySelector('#shadow');
                const t = document.querySelector('#slidingbox_tip');
                const b = document.querySelector('#slidingbox_block');
                const l = document.querySelector('#slidingbox_block_left');
                if (!s || !t) return false;
                const x = s.offsetLeft;
                t.style.left = x + 'px';
                if (b) {
                    b.style.left = x + 'px';
                    b.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                }
                if (l) l.style.width = (x + 5) + 'px';
                return true;
            }"""
        )
    except Exception as exc:
        log.warning("滑块直接归位失败: %s", exc)
        return "failed"
    return "forced" if _slider_ok(page) else "failed"


def _shot(page, tag: str) -> pathlib.Path | None:
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        path = SHOT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.png"
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception:
        return None


def _dump_page(page, tag: str) -> pathlib.Path | None:
    """把当前页面 HTML 存下来, 用于分析服务器动态渲染出来的页面。"""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.html"
        path.write_text(page.content(), encoding="utf-8")
        return path
    except Exception:
        return None


def _slider_visible(page) -> bool:
    """服务器在需要安全验证时会把滑块面板显示出来。"""
    try:
        return bool(
            page.evaluate(
                """() => {
                    const v = document.querySelector('.slidingverification');
                    if (!v) return false;
                    const st = getComputedStyle(v);
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    return v.getBoundingClientRect().height > 0;
                }"""
            )
        )
    except Exception:
        return False


def _wait_online(cfg: dict, timeout: float, page=None, tag: str = "login-failed") -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if is_online(cfg, quiet=True):
            log.info("登录成功, 网络已恢复")
            return True
        time.sleep(3)
    if page is not None:
        log.error("登录未成功, 截图: %s, 当前页面: %s", _shot(page, tag), page.url)
    return False


def _cas_login_flow(page, cfg: dict, account: str, password: str, timeout: int) -> bool:
    """
    统一身份认证的完整流程。

    关键: 滑块**不是**登录前验证。提交账号密码之后, 服务器才会返回
    "请完成安全验证"页面并要求拖动拼图, 所以必须在这里分两步做。
    """
    deadline = time.time() + timeout
    captured = False

    _shot(page, "before-submit")
    _submit_form(page, "cas")
    log.info("已提交账号密码, 等待服务器响应")

    for round_no in range(1, 9):
        if is_online(cfg, quiet=True):
            log.info("登录成功, 网络已恢复")
            return True
        if time.time() > deadline:
            break

        if _slider_visible(page):
            if not captured:
                dump = _dump_page(page, "cas-verify")
                captured = True
                log.info("服务器要求安全验证, 页面已存档: %s", dump)

            result = _solve_slider(page)
            log.info("第 %s 轮: 滑块处理 = %s", round_no, result)
            if result == "failed":
                log.warning("滑块未通过, 停止本轮")
                break

            time.sleep(2)
            if is_online(cfg, quiet=True):
                log.info("登录成功, 网络已恢复")
                return True

            # 验证通过后通常需要再提交一次, 让服务器带着验证结果放行
            if page.query_selector("input#username"):
                _fill_login_form(page, "cas", account, password)
            if page.query_selector("#passbutton"):
                _submit_form(page, "cas")
                log.info("第 %s 轮: 安全验证完成, 已重新提交", round_no)
            else:
                # 服务器可能把登录表单整个换掉了, 重新走一遍登录页
                log.info("第 %s 轮: 页面上没有登录按钮, 重新打开登录页再试", round_no)
                try:
                    page.goto(cfg["portal_url"], wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                    if _wait_for_form_kind(page, 20) == "cas":
                        _fill_login_form(page, "cas", account, password)
                        _submit_form(page, "cas")
                        log.info("第 %s 轮: 已重新提交", round_no)
                except Exception as exc:
                    log.warning("重新打开登录页失败: %s", exc)
        time.sleep(3)

    if is_online(cfg, quiet=True):
        log.info("登录成功, 网络已恢复")
        return True
    log.error("登录未成功, 截图: %s, 当前页面: %s", _shot(page, "login-failed"), page.url)
    return False


def _wait_for_form_kind(page, timeout_s: float) -> str | None:
    """
    等待登录表单出现, 返回表单类型:
      cas   统一身份认证页 (有线网段走这条)
      drcom Dr.COM 门户原生表单 (校园无线网段走这条)
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _element_visible(page, "input#username"):
            return "cas"
        if _element_visible(page, "input[name='DDDDD']"):
            return "drcom"
        time.sleep(0.5)
    # 兜底：元素存在但被藏在隐藏容器里（某些版式会这样），仍然值得试一试
    if page.query_selector("input#username"):
        log.warning("统一认证表单存在但当前不可见，仍尝试操作")
        return "cas"
    if page.query_selector("input[name='DDDDD']"):
        log.warning("门户登录表单存在但当前不可见，仍尝试操作")
        return "drcom"
    return None


def _element_visible(page, selector: str) -> bool:
    """元素存在且真的能看见(能填)。只判断存在会导致对隐藏表单 fill 超时。"""
    try:
        return bool(
            page.evaluate(
                """(sel) => {
                    const e = document.querySelector(sel);
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const st = getComputedStyle(e);
                    return r.width > 0 && r.height > 0
                        && st.display !== 'none' && st.visibility !== 'hidden';
                }""",
                selector,
            )
        )
    except Exception:
        return False


def _wait_for_form(page, timeout_s: float) -> bool:
    return _wait_for_form_kind(page, timeout_s) is not None


def _tick_checkbox(page, selector: str, label: str) -> None:
    try:
        page.check(selector, timeout=2000)
        return
    except Exception:
        pass
    try:
        page.evaluate(
            "(sel) => { const c = document.querySelector(sel);"
            " if (c) { c.checked = true; c.dispatchEvent(new Event('change', {bubbles: true})); } }",
            selector,
        )
    except Exception:
        log.warning("%s 勾选失败, 继续尝试提交", label)


def _fill_login_form(page, kind: str, account: str, password: str) -> bool:
    if kind == "cas":
        # 密码框是 #passwordShow, 提交时由页面自己用 RSA 加密
        page.fill("input#username", account)
        page.fill("#passwordShow", password)
        _tick_checkbox(page, "#agreement-box-pccheckbox1", "用户协议")
        return True

    # Dr.COM 门户原生表单: 字段名是 DDDDD / upass
    page.fill("input[name='DDDDD']", account)
    page.fill("input[name='upass']", password)
    _tick_checkbox(page, "input[name='C1']", "用户协议")
    captcha = page.query_selector("input[name='captcha']")
    if captcha and captcha.is_visible():
        log.error("门户要求填图形验证码, 脚本无法自动识别, 需要手动登录一次")
        return False
    return True


def _submit_form(page, kind: str) -> None:
    if kind == "cas":
        try:
            page.click("#passbutton", timeout=10000)
            return
        except Exception:
            log.warning("未找到 #passbutton, 尝试直接提交表单")
        page.evaluate("() => { const f = document.querySelector('#fm1'); if (f) f.submit(); }")
        return

    try:
        page.click("input[name='0MKKey']", timeout=10000)
        return
    except Exception:
        log.warning("未找到登录按钮, 尝试调用页面自带的提交流程")
    page.evaluate(
        """() => {
            const input = document.querySelector("input[name='DDDDD']");
            const forms = [window.f1, window.f2, window.f3, window.f4, window.f5];
            const idx = forms.findIndex(f => f && input && f.contains(input)) + 1;
            if (idx > 0 && typeof ee === 'function') return ee(idx);
            if (input && input.form) input.form.submit();
            return false;
        }"""
    )


def do_login(cfg: dict, account: str, password: str) -> bool:
    from playwright.sync_api import sync_playwright

    # 动手之前再确认一次: 必须是在校园网、且确实未认证
    state, detail = evaluate_state(cfg)
    if state == STATE_ONLINE:
        log.info("已在线, 无需登录 (%s)", detail)
        return True
    if state != STATE_OFFLINE_CAMPUS:
        log.warning("不是校园网未认证状态(%s), 放弃登录尝试", detail)
        return False

    if not acquire_lock():
        log.info("另一次登录正在进行中, 本次跳过")
        return False

    headless = bool(cfg.get("headless", True))
    timeout = int(cfg.get("login_timeout", 90))
    log.info("开始登录流程 (headless=%s)", headless)

    with sync_playwright() as playwright:
        ctx = _open_browser(playwright, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(30000)
            page.on("dialog", lambda d: (log.info("页面弹窗: %s", d.message), d.dismiss()))

            kind = None
            for url in (cfg["portal_url"], cfg["cas_entry_url"], "https://c.nua.edu.cn/cas/login"):
                log.info("打开 %s", url)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as exc:
                    log.warning("打开失败: %s", exc)
                    continue
                page.wait_for_timeout(1500)
                kind = _wait_for_form_kind(page, 20)
                if kind:
                    break
                if is_online(cfg, quiet=True):
                    log.info("页面显示已在线的状态, 无需登录")
                    return True

            if not kind:
                log.error("没有找到登录表单, 截图: %s", _shot(page, "no-login-form"))
                return False

            log.info("已定位登录表单(%s): %s", kind, page.url)

            try:
                if not _fill_login_form(page, kind, account, password):
                    log.error("表单填写失败, 截图: %s", _shot(page, "fill-failed"))
                    return False
            except Exception as exc:
                log.exception("填写登录表单出错: %s", exc)
                log.error("出错时截图: %s", _shot(page, "fill-error"))
                return False

            if kind == "cas":
                try:
                    return _cas_login_flow(page, cfg, account, password, timeout)
                except Exception as exc:
                    log.exception("统一认证登录流程出错: %s", exc)
                    log.error("出错时截图: %s", _shot(page, "cas-error"))
                    return False

            # Dr.COM 门户原生表单(校园无线网段)
            try:
                _shot(page, "before-submit")
                _submit_form(page, kind)
                return _wait_online(cfg, timeout, page)
            except Exception as exc:
                log.exception("Dr.COM 门户登录流程出错: %s", exc)
                log.error("出错时截图: %s", _shot(page, "drcom-error"))
                return False
        finally:
            release_lock()
            try:
                ctx.close()
            except Exception:
                pass


def dry_run(headless: bool = True) -> None:
    """演练: 只检查登录页结构并试解滑块, 不填账号密码、不提交。"""
    from playwright.sync_api import sync_playwright

    url = "https://c.nua.edu.cn/cas/login"
    log.info("演练模式: 打开 %s (不会提交任何凭证, headless=%s)", url, headless)
    with sync_playwright() as playwright:
        ctx = _open_browser(playwright, headless=headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(30000)
            page.on("dialog", lambda d: (log.info("页面弹窗: %s", d.message), d.dismiss()))
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            for sel in ("input#username", "#passwordShow", "#agreement-box-pccheckbox1", "#passbutton", "#slidingbox"):
                log.info("元素 %s -> %s", sel, "存在" if page.query_selector(sel) else "不存在")

            state = _slider_state(page)
            log.info("滑块初始状态: %s", state)
            if state is not None:
                result = _solve_slider(page)
                log.info("滑块试解结果: %s (已对齐=%s)", result, _slider_ok(page))

            log.info("截图: %s", _shot(page, "dry-run"))
        finally:
            ctx.close()


def cmd_set_password() -> None:
    account = input("校园网账号(学号): ").strip()
    if not account:
        raise SystemExit("账号不能为空")
    pwd = getpass.getpass("密码(输入时不显示): ")
    if not pwd:
        raise SystemExit("密码不能为空")
    save_secret(account, pwd)
    log.info("已保存账号 %s, 密码用 Windows DPAPI 加密写入 %s", account, SECRET_FILE.name)


def cmd_check(cfg: dict) -> int:
    state, detail = evaluate_state(cfg)
    note_state(state, detail, always=True)
    if state == STATE_OFFLINE_CAMPUS:
        log.info("判定: 需要登录")
        return 1
    return 0


def cmd_login(cfg: dict) -> int:
    """
    执行一次登录。

    三层"少打扰"处理（与纯 HTTP 版一致）：
      1. 夜间限制时段（默认周一~周五 00:00-06:00）直接不尝试
      2. 已经在线 / 不在校园网 都不尝试
      3. 登录失败后按 2/5/15/30 分钟退避
    """
    now_ts = time.time()

    if in_quiet_hours(cfg):
        quiet = cfg.get("quiet_hours") or {}
        note_mode("quiet", f"进入夜间限制时段（{quiet.get('start', '00:00')}-{quiet.get('end', '06:00')}），"
                           "学校此时不允许学生账号认证，暂停尝试")
        return 0

    state, detail = evaluate_state(cfg)
    note_state(state, detail)

    if state == STATE_ONLINE:
        clear_retry()
        note_mode("normal", "网络已恢复，回到常规检查")
        return 0
    if state == STATE_NOT_CAMPUS:
        note_mode("normal", "不在校园网环境，跳过")
        return 0

    retry = load_retry()
    next_attempt = float(retry.get("next_attempt", 0) or 0)
    if next_attempt > now_ts:
        minutes = int((next_attempt - now_ts) // 60) + 1
        note_mode(f"backoff-{int(next_attempt)}",
                  f"上次登录失败（累计 {retry.get('failures')} 次），{minutes} 分钟后再试")
        return 0

    note_mode("normal", "恢复正常检查，开始尝试登录")
    account, password = load_secret()
    if do_login(cfg, account, password):
        clear_retry()
        return 0
    failures = int(retry.get("failures", 0)) + 1
    delay = backoff_seconds(cfg, failures)
    save_retry({"failures": failures, "next_attempt": now_ts + delay})
    log.warning("登录失败，%s 秒内不再重试（累计失败 %s 次）", delay, failures)
    return 1


def cmd_watch(cfg: dict) -> int:
    interval = int(cfg.get("interval", 30))
    account, password = load_secret()
    log.info("看门狗启动, 每 %s 秒检测一次", interval)
    while True:
        try:
            if in_quiet_hours(cfg):
                quiet = cfg.get("quiet_hours") or {}
                note_mode("quiet", f"进入夜间限制时段（{quiet.get('start', '00:00')}-{quiet.get('end', '06:00')}），"
                                   "学校此时不允许学生账号认证，暂停尝试")
                time.sleep(300)
                continue
            state, detail = evaluate_state(cfg)
            note_state(state, detail)
            if state == STATE_ONLINE:
                clear_retry()
                note_mode("normal", "网络已恢复，回到常规检查")
            elif state == STATE_NOT_CAMPUS:
                note_mode("normal", "不在校园网环境，跳过")
            else:
                retry = load_retry()
                next_attempt = float(retry.get("next_attempt", 0) or 0)
                if next_attempt > time.time():
                    minutes = int((next_attempt - time.time()) // 60) + 1
                    note_mode(f"backoff-{int(next_attempt)}",
                              f"上次登录失败（累计 {retry.get('failures')} 次），{minutes} 分钟后再试")
                else:
                    note_mode("normal", "恢复正常检查，开始尝试登录")
                    if do_login(cfg, account, password):
                        clear_retry()
                    else:
                        failures = int(retry.get("failures", 0)) + 1
                        delay = backoff_seconds(cfg, failures)
                        save_retry({"failures": failures, "next_attempt": time.time() + delay})
                        log.warning("登录失败，%s 秒内不再重试（累计失败 %s 次）", delay, failures)
        except Exception as exc:
            log.exception("看门狗循环异常: %s", exc)
        time.sleep(interval)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="校园网自动登录")
    parser.add_argument("--set-password", action="store_true", help="设置账号密码")
    parser.add_argument("--check", action="store_true", help="只检测在线状态")
    parser.add_argument("--login", action="store_true", help="执行一次登录")
    parser.add_argument("--watch", action="store_true", help="看门狗模式")
    parser.add_argument("--dry-run", action="store_true", help="演练: 只检查登录页结构")
    parser.add_argument("--show", action="store_true", help="显示浏览器窗口(调试用)")
    parser.add_argument("--interval", type=int, help="看门狗轮询间隔秒数")
    parser.add_argument("--portal-url", help="临时指定校园网门户地址(默认 10.255.255.2)")
    parser.add_argument("--quiet", action="store_true", help="不输出到控制台(供计划任务调用)")
    args = parser.parse_args()

    setup_logging(verbose=not args.quiet)
    cfg = load_config()
    if args.show:
        cfg["headless"] = False
    if args.interval:
        cfg["interval"] = args.interval
    if args.portal_url:
        cfg["portal_url"] = args.portal_url

    # 计划任务运行时看不到控制台, 任何异常都必须落到日志里, 不许静默失败
    try:
        if args.set_password:
            cmd_set_password()
            return 0
        if args.check:
            return cmd_check(cfg)
        if args.dry_run:
            dry_run(headless=cfg["headless"])
            return 0
        if args.watch:
            return cmd_watch(cfg)
        if args.login:
            return cmd_login(cfg)
    except SystemExit:
        raise
    except Exception:
        log.exception("程序异常退出")
        return 3

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
