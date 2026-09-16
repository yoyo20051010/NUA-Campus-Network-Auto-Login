#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网自动登录 —— 纯 HTTP 版（不需要浏览器）。

适用于路由器 / OpenWrt / 树莓派等跑不动浏览器的设备，也方便在电脑上做快速登录。
只依赖 Python 标准库。

认证流程（2026-09-15 实测确认）:
    1. 访问门户 10.255.255.2，门户把浏览器导向统一身份认证
    2. 拉取认证页，取出 execution、表单地址，把密码按学校页面同款算法加密
    3. POST 账号密码；服务器返回"请完成安全验证"页（滑块页）
    4. POST /captchValid/checkCaptchImg 告知已通过验证
    5. 提交登录表单，网络放行

密码加密方式（从学校 security.js 复刻，已逐字节比对验证）:
    教科书式 RSA，无 PKCS#1 填充：消息按小端字节序直接作为大整数，
    不足一块时补零；指数 65537，模数见 MODULUS_HEX。
    注意这里是"零填充"而不是标准 PKCS#1，用标准库或 openssl 的默认填充会失败。

    python campus_http.py --check            检测在线状态
    python campus_http.py --login            执行一次登录
    python campus_http.py --watch            常驻看门狗
    python campus_http.py --set-password     保存账号密码
"""

from __future__ import annotations

import argparse
import datetime
import http.cookiejar
import json
import logging
import pathlib
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP_DIR = pathlib.Path(__file__).resolve().parent
LOG_DIR = APP_DIR / "logs"
SECRET_FILE = APP_DIR / "secret.json"
CONFIG_FILE = APP_DIR / "config.json"
RETRY_FILE = LOG_DIR / "retry_state.json"
MODE_FILE = LOG_DIR / "last_mode.txt"

# 学校统一身份认证页面里写死的 RSA 公钥（指数 010001，模数见下）
MODULUS_HEX = (
    "008aed7e057fe8f14c73550b0e6467b023616ddc8fa91846d2613cdb7f7621e3"
    "cada4cd5d812d627af6b87727ade4e26d26208b7326815941492b2204c3167ab"
    "2d53df1e3a2c9153bdb7c8c2e968df97a5e7e01cc410f92c4c2c2fba529b3e"
    "e988ebc1fca99ff5119e036d732c368acf8beba01aa2fdafa45b21e4de4928d"
    "0d403"
)
PUBLIC_EXPONENT = 65537

DEFAULT_CONFIG = {
    "portal_url": "http://10.255.255.2/",
    "cas_login_url": "https://c.nua.edu.cn/cas/login",
    "service": "https://c.nua.edu.cn/cas/wifiLogin/innerLogin.jsp",
    "status_url": "https://c.nua.edu.cn/cas/wifiLogin/isLogin",
    # 注意前缀 /cas：学校页面里 contextPath="/cas"，上报地址是 contextPath + /captchValid/checkCaptchImg。
    # 脚本会优先从验证页里解析 contextPath 自动拼接，这里的值只是后备。
    "captcha_url": "https://c.nua.edu.cn/cas/captchValid/checkCaptchImg",
    "probe_url": "http://www.baidu.com/",
    "interval": 60,
    "login_timeout": 90,
    "timeout": 8,
    # 夜间限制时段：这段时间学校不允许学生账号认证，就没必要每分钟去试
    # days 用 Python 的星期编号：0=周一 … 6=周日。默认周一~周五的 00:00-06:00，
    # 正好覆盖"周日到周四晚上 24 点断网"的常见策略（周一 0 点~周五 6 点）。
    "quiet_hours": {
        "enabled": True,
        "start": "00:00",
        "end": "06:00",
        "days": [0, 1, 2, 3, 4],
    },
    # 登录失败后的退避秒数：依次 2 分钟、5 分钟、15 分钟、30 分钟（之后一直 30 分钟）
    "failure_backoff": [120, 300, 900, 1800],
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

log = logging.getLogger("campus_http")


# --------------------------------------------------------------------------- #
# 密码加密（复刻学校 security.js 的 RSAUtils.encryptedString）
# --------------------------------------------------------------------------- #
def _chunk_size() -> int:
    """每块的字节数 = 2 * (模数的高位 16 位字索引)。"""
    n = int(MODULUS_HEX, 16)
    return 2 * ((n.bit_length() - 1) // 16)


def _digit_hex(value: int) -> str:
    """学校库里每个 16 位字固定输出 4 个十六进制字符。"""
    return format(value, "04x")


def rsa_encrypt(password: str) -> str:
    """
    按学校页面的算法加密密码。

    与标准 PKCS#1 不同：这里直接把明文字节按小端序当整数，尾部补 0 到整块，
    所以同一个密码每次结果都一样（可离线验证）。
    """
    modulus = int(MODULUS_HEX, 16)
    chunk = _chunk_size()

    data = bytearray()
    for ch in password:
        code = ord(ch)
        if code > 0xFF:
            raise ValueError("密码含有非 ASCII 字符，学校页面按单字节处理会出错")
        data.append(code)
    while len(data) % chunk:
        data.append(0)

    blocks = []
    for start in range(0, len(data), chunk):
        block = data[start:start + chunk]
        m = int.from_bytes(block, "little")
        c = pow(m, PUBLIC_EXPONENT, modulus)
        h = format(c, "x")
        # 学校库按 16 位字输出，最高字不足 4 位会补零，这里对齐同样的行为
        if len(h) % 4:
            h = h.rjust(len(h) + (4 - len(h) % 4), "0")
        blocks.append("".join(_digit_hex(int(h[i:i + 4], 16)) for i in range(0, len(h), 4)))
    return " ".join(blocks)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = True) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_DIR / "campus_http.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


# --------------------------------------------------------------------------- #
# 夜间免打扰 + 失败退避（让日志和请求都安静下来）
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
    return current >= start or current < end       # 跨天的情况


def note_mode(mode: str, message: str) -> bool:
    """模式（正常/夜间静默/退避）变化时才写一行日志，避免刷屏。"""
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


def save_secret(account: str, password: str) -> None:
    SECRET_FILE.write_text(
        json.dumps({"account": account, "password": password}), encoding="utf-8"
    )
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass


def load_secret() -> tuple[str, str]:
    if not SECRET_FILE.exists():
        raise SystemExit("还没有保存账号密码，请先运行: python campus_http.py --set-password")
    obj = json.loads(SECRET_FILE.read_text(encoding="utf-8"))
    return obj["account"], obj["password"]


class Session:
    """
    带 Cookie 的极简 HTTP 会话。

    bind_ip 可选：把请求绑定到指定网卡的源地址发出。
    这样在电脑同时连着有线和无线时，可以让门户以为请求来自无线网段，
    从而在不拔网线、不影响正常连接的前提下调试无线那套流程。
    """

    def __init__(self, timeout: int = 8, bind_ip: str | None = None):
        self.timeout = timeout
        self.bind_ip = bind_ip
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # 学校证书链不完整时也能用
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def _cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def _store_cookies(self, headers) -> None:
        from http.cookies import SimpleCookie
        for raw in headers.get_all("Set-Cookie") or []:
            try:
                sc = SimpleCookie()
                sc.load(raw)
                for name, morsel in sc.items():
                    self.jar.set_cookie(http.cookiejar.Cookie(
                        version=0, name=name, value=morsel.value, port=None, port_specified=False,
                        domain="10.255.255.2", domain_specified=False, domain_initial_dot=False,
                        path=morsel["path"] or "/", path_specified=True, secure=False,
                        expires=None, discard=True, comment=None, comment_url=None, rest={},
                    ))
            except Exception:
                pass

    def _request_bound(self, url: str, data: dict | None, ajax: bool, method: str | None):
        """把请求绑定到指定源地址发出（只支持 http，用于调试无线网段）。"""
        parts = urllib.parse.urlsplit(url)
        conn = http.client.HTTPConnection(
            parts.hostname, parts.port or 80, timeout=self.timeout,
            source_address=(self.bind_ip, 0),
        )
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = {"User-Agent": USER_AGENT, "Host": parts.hostname or parts.netloc,
                   "Accept-Language": "zh-CN,zh;q=0.9"}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body))
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        try:
            conn.request(method or ("POST" if data is not None else "GET"), path, body=body, headers=headers)
            resp = conn.getresponse()
            self._store_cookies(resp.headers)
            return resp.status, resp.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()

    def request(self, url: str, data: dict | None = None, ajax: bool = False,
                method: str | None = None) -> tuple[int, str]:
        if self.bind_ip and url.startswith("http://"):
            return self._request_bound(url, data, ajax, method)
        body = None
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "ignore")
        except Exception as exc:
            return -1, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# 在线判断
# --------------------------------------------------------------------------- #
def is_online(sess: Session, cfg: dict, quiet: bool = False) -> bool:
    # 绑定源地址调试时不能查状态接口（HTTPS 不支持绑定），否则会查到别的网卡的状态
    if not sess.bind_ip:
        status, body = sess.request(cfg["status_url"], data={}, ajax=True)
        if status == 200 and body.strip().startswith("{"):
            try:
                if json.loads(body).get("success"):
                    return True
            except ValueError:
                pass
    else:
        body = ""
    # 接口不可用或者返回未登录时，用真实访问复核（未认证时会被门户劫持）
    code, text = sess.request(cfg["probe_url"])
    if code == 200 and "Dr.COMWebLogin" not in text and "DrcomServer" not in text:
        return True
    if not quiet:
        log.info("判定为未认证（状态接口返回 %s）", body[:120])
    return False


def portal_ok(sess: Session, cfg: dict) -> bool:
    code, text = sess.request(cfg["portal_url"])
    if code != 200:
        return False
    return "Dr.COMWebLogin" in text or "eportal" in text or "DrcomServer" in text


# --------------------------------------------------------------------------- #
# 判断走哪套登录流程
#
# 校园网有两套认证界面，门户是看"客户端 IP"自己决定的：
#   有线网段(10.12.x 之类)  -> 跳转统一身份认证，有账号密码 + 拼图滑块
#   无线网段(10.54.x 之类)  -> 显示 Dr.COM 自己的登录页，字段是 DDDDD/upass
# 这里直接复刻门户页面里的那段判断，保证和浏览器看到的一致。
# --------------------------------------------------------------------------- #
CAS_RANGES = [("1.1.1.1", "10.51.255.255"), ("10.128.0.1", "10.129.255.255")]


def _ip_to_int(ip: str) -> int:
    parts = [int(x) for x in ip.split(".")]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def client_ip_from_portal(html: str) -> str | None:
    """门户页面里会写上它看到的客户端地址(v46ip / myv6ip 等)。"""
    for pattern in (r"v46ip\s*=\s*'([0-9.]+)'", r"ss5\s*=\s*\"([0-9.]+)\"",
                    r"v4serip\s*=\s*'([0-9.]+)'"):
        m = re.search(pattern, html)
        if m and m.group(1).count(".") == 3:
            return m.group(1)
    return None


def needs_cas(client_ip: str | None) -> bool:
    if not client_ip:
        return True  # 判断不了就按有线那套试，有后备
    try:
        value = _ip_to_int(client_ip)
    except (ValueError, IndexError):
        return True
    return any(_ip_to_int(low) <= value <= _ip_to_int(high) for low, high in CAS_RANGES)


def parse_portal_config(html: str) -> dict:
    """解析门户页面里那段 js 配置（登录路径、端口、字段名、jsVersion 等）。"""
    wanted = {
        "authloginpath": r"authloginpath\s*=\s*'([^']*)'",
        "authloginport": r"authloginport\s*=\s*(\d+)",
        "authuserfield": r"authuserfield\s*=\s*'([^']*)'",
        "authpassfield": r"authpassfield\s*=\s*'([^']*)'",
        "authloginparam": r"authloginparam\s*=\s*'([^']*)'",
        "authsuccess": r"authsuccess\s*=\s*'([^']*)'",
        "authfail": r"authfail\s*=\s*'([^']*)'",
        "jsVersion": r"var\s+fileVersion\s*=\s*\"(\d+)\"",
        "v4serip": r"v4serip\s*=\s*'([0-9.]+)'",
    }
    out = {}
    for key, pattern in wanted.items():
        m = re.search(pattern, html)
        if m:
            out[key] = m.group(1)
    return out


# --------------------------------------------------------------------------- #
# 极简表单解析（只用标准库）
# --------------------------------------------------------------------------- #
def _parse_attrs(text: str) -> dict:
    attrs = {}
    for m in re.finditer(r"""([A-Za-z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", text):
        attrs[m.group(1).lower()] = m.group(2) or m.group(3) or m.group(4) or ""
    return attrs


def parse_forms(html: str) -> list[dict]:
    forms = []
    for m in re.finditer(r"(?is)<form\b([^>]*)>(.*?)</form>", html):
        attrs = _parse_attrs(m.group(1))
        body = m.group(2)
        inputs: dict[str, str] = {}
        for im in re.finditer(r"(?is)<input\b([^>]*?)/?>", body):
            a = _parse_attrs(im.group(1))
            name = a.get("name")
            if not name:
                continue
            if a.get("type", "text").lower() in ("submit", "button", "image"):
                continue
            inputs[name] = a.get("value", "")
        forms.append({
            "id": attrs.get("id", ""),
            "action": attrs.get("action", ""),
            "inputs": inputs,
            "body": body,
        })
    return forms


def needs_captcha(html: str) -> bool:
    """服务器返回"请完成安全验证"页时，slider 面板会去掉 none 类。"""
    if "请完成安全验证" not in html:
        return False
    return 'class="slidingverification none"' not in html


def _dump(tag: str, text: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"http-{time.strftime('%Y%m%d-%H%M%S')}-{tag}.html"
        path.write_text(text, encoding="utf-8")
        log.info("页面已存档: %s", path.name)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
def login(sess: Session, cfg: dict, account: str, password: str) -> bool:
    """入口：先看门户在哪个网段，再决定走哪套登录流程。"""
    code, portal_html = sess.request(cfg["portal_url"])
    if code != 200 or not (
        "Dr.COMWebLogin" in portal_html or "eportal" in portal_html or "DrcomServer" in portal_html
    ):
        log.warning("访问不到校园网门户，判定不在校园网，放弃登录（HTTP %s）", code)
        return False

    ip = client_ip_from_portal(portal_html)
    wired = needs_cas(ip)
    wired_flow = str(cfg.get("wired_flow") or "drcom").lower()

    if wired and wired_flow == "cas":
        log.info("有线网段（客户端 %s）→ 按配置走统一身份认证", ip)
        return cas_login(sess, cfg, account, password)

    if wired:
        # 实测（2026-09-16）：Dr.COM 的登录接口并不拒绝有线客户端。
        # 直接走表单可以绕开统一认证必须做的滑块 / 人脸验证，也不需要 RSA 加密。
        log.info("有线网段（客户端 %s）→ 优先走 Dr.COM 表单（可绕开统一认证的验证环节）", ip)
    else:
        log.info("无线网段（客户端 %s）→ 走 Dr.COM 门户登录", ip)

    result = drcom_login(sess, cfg, portal_html, account, password)
    if result == "ok":
        return True
    if result == "fatal":
        # 账号级问题（密码错 / 在线数超限 / 要验证码）：换统一认证也一样没用，
        # 再打请求只会增加账号被锁的风险，所以直接停手。
        log.error("Dr.COM 登录遇到需要人工处理的提示，本次不再尝试其它登录方式")
        return False
    if wired and wired_flow != "cas":
        log.info("Dr.COM 方式没有成功，改用统一身份认证再试一次")
        return cas_login(sess, cfg, account, password)
    return False


def drcom_login(sess: Session, cfg: dict, portal_html: str,
                account: str, password: str) -> str:
    """
    校园无线网段的登录：门户自己的表单，字段是 DDDDD / upass，
    一般没有拼图滑块（配置里 password_cut=0、en_md5=0，密码按明文提交）。

    实测（2026-09-15）:
      · 能识别账号字段的接口是 /eportal/?c=ACSetting&a=Login ，
        新版 /eportal/portal/login 会返回"无法获取用户认证账号"。
      · 无线门户的"服务类型"默认是【校园用户】(账号不带后缀)，
        另有【校园电信】(@dx) 和【校园联通】(@lt)。
        没绑定运营商账号时会提示"运营商登录需先绑定运营商账号"。
    """
    conf = parse_portal_config(portal_html)
    host = urllib.parse.urlsplit(cfg["portal_url"]).hostname or "10.255.255.2"
    port = conf.get("authloginport", "801")
    user_field = conf.get("authuserfield", "DDDDD")
    pass_field = conf.get("authpassfield", "upass")
    login_path = conf.get("authloginpath", "/eportal/?c=ACSetting&a=Login")
    if "ver=" not in login_path:
        # 门户页面里这个路径不带 ver，但实测 AC 需要 ver=1.0 才会走登录接口
        # （否则返回的是后台管理页面）
        login_path += ("&" if "?" in login_path else "?") + "ver=1.0"
    js_version = conf.get("jsVersion", "")
    base = f"http://{host}:{port}"

    log.info("门户配置: 登录路径=%s 端口=%s 账号字段=%s 密码字段=%s",
             login_path, port, user_field, pass_field)

    # 服务类型：默认"校园用户"(不带后缀)，另外提供电信/联通。
    # 如果在 config.json 里写了 wifi_suffix（例如 "@dx"），就只用那一种，避免多余尝试。
    pinned = cfg.get("wifi_suffix", "")
    if pinned:
        account_forms = [(f"配置指定 {pinned}", f"{account}{pinned}")]
        log.info("按配置使用服务类型后缀: %s", pinned)
    else:
        account_forms = [("校园用户(默认)", account),
                         ("校园网后缀 @njxy", f"{account}@njxy"),
                         ("校园电信 @dx", f"{account}@dx"),
                         ("校园联通 @lt", f"{account}@lt")]

    # 接口顺序：实测 ACSetting 能识别账号，放前面；新版接口作为后备
    endpoints = [
        ("ACSetting", f"{base}{login_path}", {"url": "drappall"}, account_forms),
        # 新版接口在本校实测始终返回"无法获取用户认证账号"，只在默认服务类型下试一次
        ("portal/login", f"{base}/eportal/portal/login", {}, account_forms[:1]),
    ]

    last_error = ""
    tried_acsetting = False
    for endpoint_name, url, extra, forms in endpoints:
        # 新版接口在本校实测不可用；只有当旧接口连账号字段都认不出时才去试它
        if endpoint_name == "portal/login" and tried_acsetting and "无法获取用户认证账号" not in last_error:
            log.info("旧接口能识别账号，跳过新版接口")
            break
        for form_name, user_value in forms:
            data = {
                user_field: user_value,
                pass_field: password,
                "0MKKey": "123456",
                "R1": "", "R2": "", "R3": "", "R6": "0", "para": "", "v6ip": "",
                "terminal_type": "1",
                "lang": "zh-cn",
            }
            if js_version:
                data["jsVersion"] = js_version
            data.update(extra)

            log.info("Dr.COM 登录：接口 %s，服务类型 %s", endpoint_name, form_name)
            # 必须带 AJAX 头：否则 AC 会返回后台管理页面而不是登录结果
            code, body = sess.request(url, data=data, ajax=True)
            text = body.strip().replace("\n", " ")[:220]
            log.info("  → HTTP %s: %s", code, text)
            _dump(f"drcom-{endpoint_name}-{form_name}", body)

            if "验证码" in body:
                log.error("门户要求图形验证码，纯 HTTP 模式无法自动识别（可改用浏览器模式）")
                return "fatal"

            # 提交后给服务器一点时间放行
            deadline = time.time() + 8
            while time.time() < deadline:
                if is_online(sess, cfg, quiet=True):
                    log.info("登录成功，网络已恢复（服务类型：%s）", form_name)
                    return "ok"
                time.sleep(2)

            last_error = text
            if endpoint_name == "ACSetting":
                tried_acsetting = True

            # 这几类错误换接口、换服务类型都没用，而且继续试可能把账号撞锁，
            # 所以立刻停手，并把 AC 的原话写进日志让用户知道到底怎么了。
            # 注意："账号错误" / "Authentication fail" 不算致命 —— 那往往只是
            # 服务类型(后缀)选错了，应该继续试下一个。
            fatal_words = ("密码", "验证码", "在线数超出限制", "Limit Users", "已在线")
            hit = next((w for w in fatal_words if w in body), "")
            if hit:
                log.error("服务器返回需要人工处理的提示 [%s]：%s", hit, text)
                if hit == "已在线":
                    log.error("  说明该账号已经有一个会话在线（学校限制并发设备数）。")
                    log.error("  旧会话在服务端会残留 7~10 分钟才释放，这期间新设备登录会被拒。")
                return "fatal"

        log.info("  接口 %s 未成功，换下一个接口", endpoint_name)

    log.error("Dr.COM 门户登录失败，最后一次返回: %s", last_error)
    return "failed"


def cas_login(sess: Session, cfg: dict, account: str, password: str) -> bool:
    """有线网段：统一身份认证 + 拼图滑块。"""
    service_q = urllib.parse.quote(cfg["service"], safe="")
    login_url = f"{cfg['cas_login_url']}?service={service_q}"
    log.info("打开认证页 %s", login_url)
    code, html = sess.request(login_url)
    if code != 200:
        log.error("认证页打不开: HTTP %s", code)
        return False

    forms = parse_forms(html)
    form = next((f for f in forms if "password" in f["inputs"] or "username" in f["inputs"]), None)
    if form is None:
        log.error("认证页里没有登录表单")
        _dump("no-form", html)
        return False

    action = urllib.parse.urljoin(login_url, form["action"] or login_url)
    data = dict(form["inputs"])
    data.update({
        "username": account,
        "password": rsa_encrypt(password),
        "encrypted": "true",
        "_eventId": "submit",
    })
    data.setdefault("loginType", "1")
    log.info("提交账号密码 (表单字段: %s)", ", ".join(sorted(data)))

    code, resp = sess.request(action, data=data)
    if code != 200:
        log.error("提交账号密码失败: HTTP %s", code)
        return False

    if needs_captcha(resp):
        _dump("captcha-page", resp)
        if not _pass_captcha(sess, cfg, login_url, action, account, resp):
            log.error("安全验证环节失败")
            return False
    else:
        log.info("服务器未要求安全验证，直接检查结果")

    deadline = time.time() + int(cfg["login_timeout"])
    while time.time() < deadline:
        if is_online(sess, cfg, quiet=True):
            log.info("登录成功，网络已恢复")
            return True
        time.sleep(3)

    log.error("登录后 %s 秒仍未恢复网络", cfg["login_timeout"])
    return False


def _pass_captcha(sess: Session, cfg: dict, login_url: str, action: str,
                  account: str, page: str) -> bool:
    """
    通过安全验证。

    学校页面的做法是：滑块拖到位后 POST /captchValid/checkCaptchImg，
    成功回调里再提交 fm4（一个空表单，action 指向登录地址）。
    这里照样做，并额外准备两个后备提交方式，因为服务端行为可能调整。
    """
    log.info("服务器要求安全验证，上报验证结果")
    # 上报地址必须是 contextPath + /captchValid/checkCaptchImg
    # 学校页面里写的是 var contextPath = "/cas"，少了这个前缀就会打到错误的地方
    for url in _captcha_endpoints(login_url, page):
        code, body = sess.request(
            url,
            data={"request_username": account, "captchResult": "1"},
            ajax=True,
        )
        log.info("  POST %s", url)
        log.info("  → HTTP %s 返回: %s", code, body.strip().replace("\n", " ")[:160])
        if code == 200 and ("true" in body or "success" in body or "1" in body):
            break

    forms = parse_forms(page)
    fm4 = next((f for f in forms if f["id"] == "fm4"), None)
    fm3 = next((f for f in forms if f["id"] == "fm3"), None)

    strategies: list[tuple[str, str, dict]] = []
    if fm4 is not None:
        strategies.append(("fm4(空表单)", urllib.parse.urljoin(login_url, fm4["action"] or action), dict(fm4["inputs"])))
    if fm3 is not None:
        payload = dict(fm3["inputs"])
        payload["_eventId"] = "checkCaptchaSubmit"
        strategies.append(("fm3(checkCaptchaSubmit)", urllib.parse.urljoin(login_url, fm3["action"] or action), payload))
    strategies.append(("直接重提登录地址", action, {}))

    for name, target, payload in strategies:
        log.info("验证后提交方式: %s", name)
        code, resp = sess.request(target, data=payload)
        still = needs_captcha(resp)
        log.info("  -> HTTP %s, 页面 %s 字节, 仍需验证=%s", code, len(resp), still)
        if not still:
            _dump(f"after-{name}", resp)
        deadline = time.time() + 20
        while time.time() < deadline:
            if is_online(sess, cfg, quiet=True):
                return True
            time.sleep(2)
    return False


def _captcha_endpoints(login_url: str, page: str) -> list[str]:
    """按页面里的 contextPath 拼出验证结果的上报地址（并保留一个后备地址）。"""
    parts = urllib.parse.urlsplit(login_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    m = (re.search(r'var\s+contextPath\s*=\s*"([^"]*)"', page)
         or re.search(r"var\s+contextPath\s*=\s*'([^']*)'", page))
    ctx = (m.group(1) if m else "/cas").rstrip("/")

    urls = []
    if ctx:
        urls.append(f"{origin}{ctx}/captchValid/checkCaptchImg")
    urls.append(f"{origin}/captchValid/checkCaptchImg")
    return urls


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_set_password() -> None:
    account = input("校园网账号: ").strip()
    if not account:
        raise SystemExit("账号不能为空")
    import getpass
    password = getpass.getpass("密码(输入时不显示): ")
    if not password:
        raise SystemExit("密码不能为空")
    save_secret(account, password)
    print(f"已保存到 {SECRET_FILE}（建议 chmod 600）")


def cmd_watch(cfg: dict) -> int:
    sess = Session(cfg["timeout"])
    account, password = load_secret()
    log.info("看门狗启动，每 %s 秒检测一次", cfg["interval"])
    while True:
        try:
            if in_quiet_hours(cfg):
                quiet = cfg.get("quiet_hours") or {}
                note_mode("quiet", f"进入夜间限制时段({quiet.get('start','00:00')}-{quiet.get('end','06:00')})，"
                                   "学校此时不允许学生账号认证，暂停尝试")
                time.sleep(300)
                continue
            if is_online(sess, cfg, quiet=True):
                clear_retry()
                note_mode("normal", "网络已恢复，回到常规检查")
            else:
                retry = load_retry()
                next_attempt = float(retry.get("next_attempt", 0) or 0)
                if next_attempt > time.time():
                    minutes = int((next_attempt - time.time()) // 60) + 1
                    note_mode(f"backoff-{int(next_attempt)}",
                              f"上次登录失败（累计 {retry.get('failures')} 次），{minutes} 分钟后再试")
                else:
                    note_mode("normal", "恢复正常检查，开始尝试登录")
                    if not login(sess, cfg, account, password):
                        failures = int(retry.get("failures", 0)) + 1
                        delay = backoff_seconds(cfg, failures)
                        save_retry({"failures": failures, "next_attempt": time.time() + delay})
                        log.warning("登录失败，%s 秒内不再重试（累计失败 %s 次）", delay, failures)
                    else:
                        clear_retry()
        except Exception as exc:
            log.exception("循环异常: %s", exc)
        time.sleep(int(cfg["interval"]))


def cmd_login(cfg: dict) -> int:
    """
    执行一次登录。

    这里做了三层"少打扰"处理：
      1. 夜间限制时段（默认周一~周五 00:00-06:00）直接不尝试
      2. 已经在线 / 不在校园网 都不尝试
      3. 登录失败后按 2/5/15/30 分钟退避，避免每分钟都去撞墙
    每种情况只在"状态变化"时写一行日志。
    """
    sess = Session(cfg["timeout"])
    now_ts = time.time()

    if in_quiet_hours(cfg):
        quiet = cfg.get("quiet_hours") or {}
        note_mode("quiet", f"进入夜间限制时段（{quiet.get('start', '00:00')}-{quiet.get('end', '06:00')}），"
                           "学校此时不允许学生账号认证，暂停尝试")
        return 0

    if is_online(sess, cfg, quiet=True):
        clear_retry()
        note_mode("normal", "网络已恢复，回到常规检查")
        return 0

    if not portal_ok(sess, cfg):
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
    if login(sess, cfg, account, password):
        clear_retry()
        return 0

    failures = int(retry.get("failures", 0)) + 1
    delay = backoff_seconds(cfg, failures)
    save_retry({"failures": failures, "next_attempt": now_ts + delay})
    log.warning("登录失败，%s 秒内不再重试（累计失败 %s 次）", delay, failures)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="校园网自动登录（纯 HTTP 版）")
    parser.add_argument("--check", action="store_true", help="检测在线状态")
    parser.add_argument("--login", action="store_true", help="执行一次登录")
    parser.add_argument("--watch", action="store_true", help="常驻看门狗")
    parser.add_argument("--set-password", action="store_true", help="保存账号密码")
    parser.add_argument("--probe", action="store_true", help="探测门户走的是哪套登录流程（不登录）")
    parser.add_argument("--quiet", action="store_true", help="不输出到控制台")
    args = parser.parse_args()

    # 输出统一成 UTF-8：被 PowerShell 捕获时（Select-String 等）才不会变乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    setup_logging(verbose=not args.quiet)
    cfg = load_config()

    try:
        if args.set_password:
            cmd_set_password()
            return 0
        sess = Session(cfg["timeout"])
        if args.probe:
            code, html = sess.request(cfg["portal_url"])
            ip = client_ip_from_portal(html)
            log.info("门户 HTTP %s，页面 %s 字节", code, len(html))
            log.info("门户看到的客户端地址: %s", ip)
            log.info("应该走: %s", "统一身份认证(有滑块)" if needs_cas(ip) else "Dr.COM 门户登录(无线)")
            conf = parse_portal_config(html)
            for k in sorted(conf):
                log.info("  门户配置 %s = %s", k, conf[k])
            log.info("当前是否已在线: %s", is_online(sess, cfg, quiet=True))
            return 0
        if args.check:
            online = is_online(sess, cfg)
            log.info("当前状态: %s", "已在线" if online else "未认证/已断网")
            return 0 if online else 1
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
