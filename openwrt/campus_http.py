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
    python campus_http.py --mode teacher     切到教师账号模式（不受夜间限制）

账号类型:
    student（学生，默认）—— 受学校夜间断网策略限制，quiet_hours 时段不尝试认证
    teacher（教师）    —— 没有这条限制，整夜照常检测在线状态并自动登录
"""

from __future__ import annotations

import argparse
import contextvars
import datetime
import http.client
import http.cookiejar
import json
import logging
import logging.handlers
import os
import pathlib
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP_DIR = pathlib.Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# 线路（profile）：一条线 = 一个账号 + 一个出口
#
# 每条线路自己一套 config.json / secret.json / logs/，互不干扰。
# 只有一条线时保持老样子（文件直接放在 APP_DIR 下，完全兼容）；
# 配了多条线时，每条线在 profiles/<名字>/ 下自己一份。
#
# 为什么要做成"一个进程多条线"而不是起多个进程：
#   实测这个脚本进程的 RSS 就有 24MB，而路由器只剩 20MB 出头可用，
#   起两个进程根本装不下；一个进程里多一条线只多一个 Session 对象。
#
# 下面这几个名字（LOG_DIR / SECRET_FILE / log ...）在全文里照旧用，
# 一处都不用改：它们是指向"当前线路"的代理，多线程各看各的。
# --------------------------------------------------------------------------- #
class Profile:
    def __init__(self, name: str, base: pathlib.Path,
                 device: str = "", account_type: str = "", mwan3: str = ""):
        self.name = name
        self.base = base
        self.device = device or ""            # 绑定的网卡名；空 = 按系统路由走
        self.default_account_type = account_type or ""
        self.mwan3 = mwan3 or ""              # 对应的 mwan3 接口名；空 = 不联动（默认与线路同名）
        self.log_dir = base / "logs"
        self.config_file = base / "config.json"
        self.secret_file = base / "secret.json"
        self.retry_file = self.log_dir / "retry_state.json"
        self.mode_file = self.log_dir / "last_mode.txt"
        self.log = logging.getLogger(f"campus_http.{name}")

    def __repr__(self) -> str:
        return f"<Profile {self.name} @ {self.base}>"


DEFAULT_PROFILE = Profile("default", APP_DIR)
_current_profile: contextvars.ContextVar = contextvars.ContextVar(
    "profile", default=DEFAULT_PROFILE
)


def current() -> Profile:
    """当前这条线路。"""
    return _current_profile.get()


class _PathProxy:
    """指向"当前线路的某个路径"，用法和 pathlib.Path 完全一样。"""

    def __init__(self, attr: str):
        object.__setattr__(self, "_attr", attr)

    def _path(self) -> pathlib.Path:
        return getattr(current(), self._attr)

    def __truediv__(self, other):  return self._path() / other
    def __rtruediv__(self, other): return other / self._path()
    def __getattr__(self, name):   return getattr(self._path(), name)
    def __fspath__(self):          return str(self._path())
    def __str__(self):             return str(self._path())
    def __repr__(self):            return repr(self._path())
    def __eq__(self, other):       return self._path() == other
    def __hash__(self):            return hash(self._path())


class _LogProxy:
    """log.info(...) 转发到当前线路自己的 logger。"""

    def __getattr__(self, name):
        return getattr(current().log, name)


LOG_DIR = _PathProxy("log_dir")
SECRET_FILE = _PathProxy("secret_file")
CONFIG_FILE = _PathProxy("config_file")
RETRY_FILE = _PathProxy("retry_file")
MODE_FILE = _PathProxy("mode_file")

# 调试用的页面存档最多留几份（高频重试时防止把闪存写满）
DUMP_KEEP = 30

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
    # 有线网段走哪套登录：drcom=优先走 Dr.COM 表单（可绕开统一认证的滑块/人脸验证），
    # 失败再回退统一身份认证；cas=直接走统一身份认证。
    "wired_flow": "drcom",
    # 无线网(校园WiFi)登录用的服务类型后缀，留空 = 自动依次尝试
    "wifi_suffix": "",
    # 账号类型：student=学生账号，teacher=教师账号，**留空 = 自动判断**。
    # 学生账号受学校"夜间断网"限制；教师账号没有这条限制，
    # 所以 teacher 模式会自动忽略 quiet_hours，整夜照常检测并自动登录。
    # 一般不用改这里 —— 用 --set-password / --mode 保存的类型优先级更高。
    "account_type": "",
    # 没手动设过账号类型时，按账号前缀自动判断：
    # 教师工号（M 开头）所有时段都能认证；学生学号（B 开头）只有周六日 24 小时可用，
    # 非周六日 00:00-06:00 无法认证 —— 正好对应下面 quiet_hours 的 days=[0,1,2,3,4]。
    # 实测学校的时段限制是按**账号**分的，不是按网段，所以必须按账号判断。
    "teacher_account_prefixes": ["M"],
    "quiet_hours_exempt_accounts": [],
    # 夜间限制时段：这段时间学校不允许学生账号认证，就没必要每分钟去试。
    # 注意：account_type=teacher 时这一段自动失效（教师账号夜里也能认证）。
    # days 用 Python 的星期编号：0=周一 … 6=周日。默认周一~周五的 00:00-06:00，
    # 正好覆盖"周日到周四晚上 24 点断网"的常见策略（周一 0 点~周五 6 点）。
    "quiet_hours": {
        "enabled": True,
        "start": "00:00",
        "end": "06:00",
        "days": [0, 1, 2, 3, 4],
    },
    # 夜间限制时段开始前多少分钟，就提前把这条线摘出备用池（0 = 不提前）。
    # 学校是整点断网，等断了再切的话，正在跑的连接会直接断；提前几分钟切走，
    # 现有连接能在原线路上跑完、新连接直接走备用线，用户基本无感。
    "pre_switch_minutes": 5,
    # 常驻内存上限（MB）：超过就主动退出让 procd 重启，避免被内核 OOM 杀掉。
    # 设 0 = 不检查。
    "max_rss_mb": 60,
    # 登录失败后的退避秒数：依次 2 分钟、5 分钟、15 分钟、30 分钟（之后一直 30 分钟）
    "failure_backoff": [120, 300, 900, 1800],
    # 教师账号专用覆盖项：留空 = 和其它账号完全一样。
    # 只有学校给教师另开了一套门户/服务地址时才需要填，填了就在这里生效。
    "teacher": {
        "portal_url": "",
        "cas_login_url": "",
        "service": "",
        "status_url": "",
        "wifi_suffix": "",
    },
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

log = _LogProxy()


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
def setup_logging(verbose: bool = True, profile: "Profile | None" = None) -> None:
    """给某条线路配好日志（文件 + 可选控制台）。同一个 logger 只配一次。"""
    p = profile or current()
    p.log_dir.mkdir(parents=True, exist_ok=True)
    p.log.setLevel(logging.INFO)
    if p.log.handlers:
        return
    # 多条线并行时行首标出是哪条线，不然日志混在一起分不清
    tag = "" if p.name == DEFAULT_PROFILE.name else f"[{p.name}] "
    fmt = logging.Formatter(f"%(asctime)s %(levelname)-7s {tag}%(message)s",
                            "%Y-%m-%d %H:%M:%S")
    # 15 秒一轮、失败还不退避的话日志会长得很快，所以限制单文件大小并滚动，
    # 免得出问题的时候把路由器那点闪存写满。
    fh = logging.handlers.RotatingFileHandler(
        p.log_dir / "campus_http.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    p.log.addHandler(fh)
    if verbose and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        p.log.addHandler(sh)


def load_config(profile: "Profile | None" = None) -> dict:
    """
    三层合并：内置默认值 < APP_DIR/config.json（公共） < 本线路的 config.json（覆盖）。

    只有一条线时前后两个文件其实是同一个，读两遍而已。
    """
    p = profile or current()
    cfg = dict(DEFAULT_CONFIG)
    for path in (APP_DIR / "config.json", p.config_file):
        try:
            if path.exists():
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


def load_profiles() -> list["Profile"]:
    """
    读出所有线路。config.json 里的 profiles 长这样：

      "profiles": [
        {"name": "wan",  "device": "wan",    "account_type": "teacher"},
        {"name": "wanb", "device": "br-lan", "account_type": "student"}
      ]

    没配 profiles（或配成空数组）= 老样子，只有一条线。
    """
    top = dict(DEFAULT_CONFIG)
    try:
        top_file = APP_DIR / "config.json"
        if top_file.exists():
            top.update(json.loads(top_file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass

    profiles: list[Profile] = []
    for item in top.get("profiles") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        base = item.get("dir") or (APP_DIR / "profiles" / name)
        profiles.append(Profile(name, pathlib.Path(base),
                                str(item.get("device") or ""),
                                str(item.get("account_type") or ""),
                                str(item.get("mwan3") or "")))
    return profiles or [DEFAULT_PROFILE]


def _profile_names(profiles: list["Profile"]) -> str:
    return "、".join(p.name for p in profiles)


def resolve_profile(name: str | None) -> "Profile":
    """按名字找线路；没给名字且只有一条线时就用那一条。"""
    profiles = load_profiles()
    if name:
        for p in profiles:
            if p.name == name:
                return p
        raise SystemExit(f"没有叫 '{name}' 的线路。已配置的线路：{_profile_names(profiles)}")
    if len(profiles) == 1:
        return profiles[0]
    raise SystemExit("配了多条线路，请用 --profile 指定一条。已配置的线路："
                     + _profile_names(profiles))


# --------------------------------------------------------------------------- #
# 账号类型：学生 / 教师
#
# 学校对学生账号有夜间断网策略（默认周一~周五 00:00-06:00 不允许认证），
# 教师账号没有这条限制。于是把账号类型做成一个开关：
#     student -> 照常遵守 quiet_hours
#     teacher -> 忽略 quiet_hours，整夜正常检测在线状态并自动登录
# 类型跟着账号一起存在 secret.json 里（config.json 的 account_type 只作兜底），
# 用 --mode 随时切换，不用重新输密码。
# --------------------------------------------------------------------------- #
ACCOUNT_TYPES = ("student", "teacher")
ACCOUNT_TYPE_LABEL = {"student": "学生账号", "teacher": "教师账号"}
ACCOUNT_TYPE_ALIASES = {
    "student": "student", "stu": "student", "s": "student", "1": "student", "学生": "student",
    "teacher": "teacher", "tea": "teacher", "t": "teacher", "staff": "teacher",
    "2": "teacher", "教师": "teacher", "老师": "teacher",
}


def normalize_account_type(value) -> str | None:
    """把中文/英文/数字写法统一成 student / teacher，认不出来返回 None。"""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in ACCOUNT_TYPE_ALIASES:
        return ACCOUNT_TYPE_ALIASES[text]
    if "教" in text or "师" in text:
        return "teacher"
    if "学" in text or "生" in text:
        return "student"
    return None


def quiet_hours_exempt(cfg: dict, account: str) -> bool:
    """
    某些账号不受夜间限制时段影响。

    实测（2026-09，南艺）学校对不同账号的时段限制不一样，**而且和连哪张校园网无关**：
      · 学生账号（学号，B 开头）→ 只有周六日 24 小时可用；
                                  非周六日 00:00-06:00 无法认证
      · 教师账号（工号，M 开头）→ 所有时段都可用

    两张校园网（移动 / 电信）上都是学生账号 + 教师账号混着用，所以这里必须按
    **账号**判断，不能按网段判断。教师账号在夜间限制时段里也要照常工作，
    否则半夜掉线就不会自动重连 —— 而那个时间恰恰是能认证的。

    配置：teacher_account_prefixes（默认 ["M"]）、quiet_hours_exempt_accounts
    """
    account = (account or "").strip()
    if not account:
        return False
    prefixes = cfg.get("teacher_account_prefixes") or ["M"]
    for prefix in prefixes:
        prefix = str(prefix).strip()
        if prefix and account.upper().startswith(prefix.upper()):
            return True
    exempt = cfg.get("quiet_hours_exempt_accounts") or []
    return any(account == str(name).strip() for name in exempt)


def stored_account() -> str:
    """取已保存的账号（没保存过/读不到就返回空串）—— 用来判断这个账号是否受夜间限制。"""
    try:
        return load_secret()["account"]
    except (SystemExit, KeyError):
        return ""


def resolve_account_type(cfg: dict, stored: str | None = None, account: str = "") -> str:
    """
    生效的账号类型，优先级从高到低：

      1. secret.json 里存的 account_type（--set-password / --mode 写的）
      2. config.json 里的 account_type
      3. **按账号前缀自动判断**：教师工号（M 开头）→ teacher，其余 → student

    第 3 条来自实测：学校的夜间限制时段是按**账号**分的，不是按网段，
    所以就算没手动设过类型，也应该能正确识别出教师账号。
    """
    for candidate in (stored, cfg.get("account_type")):
        value = normalize_account_type(candidate)
        if value:
            return value
    if quiet_hours_exempt(cfg, account or stored_account()):
        return "teacher"
    return "student"


def apply_account_type(cfg: dict, account_type: str) -> dict:
    """
    按账号类型生成实际生效的配置。

    教师账号目前除了"不受夜间限制"之外流程完全一样；
    万一学校给教师另开了一套门户，只在 config.json 的 teacher 段里填地址即可，
    不用改代码。
    """
    cfg = dict(cfg)
    if account_type == "teacher":
        overrides = cfg.get("teacher") or {}
        for key in ("portal_url", "cas_login_url", "service", "status_url", "wifi_suffix"):
            value = overrides.get(key)
            if value:
                cfg[key] = value
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


def in_quiet_hours(cfg: dict, account_type: str = "student",
                   now: datetime.datetime | None = None) -> bool:
    """
    当前是否处在学校禁止认证的时段（默认周一~周五 00:00-06:00）。

    教师账号不受这条策略限制，所以 account_type=teacher 时永远返回 False，
    夜里照常检测、照常自动登录。
    """
    if account_type == "teacher":
        return False
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


def in_pre_quiet(cfg: dict, account_type: str, lead_minutes: int,
                 now: datetime.datetime | None = None) -> bool:
    """
    是否处在「夜间限制时段开始前 lead_minutes 分钟以内」。

    用来**提前**把整条线摘出备用池。学校是整点断网，等到那一刻才发现就已经晚了
    —— 正在跑的连接会直接断。提前几分钟切走：现有连接还能在原线路上跑完，
    新连接直接走备用线，用户基本无感。

    教师账号不受夜间限制，永远返回 False。
    """
    if account_type == "teacher" or lead_minutes <= 0:
        return False
    now = now or datetime.datetime.now()
    if in_quiet_hours(cfg, account_type, now):
        return False
    return in_quiet_hours(cfg, account_type, now + datetime.timedelta(minutes=lead_minutes))


def note_mode(mode: str, message: str) -> bool:
    """
    模式（正常/夜间静默/退避/离线/尝试中）变化时才写一行日志，避免刷屏。

    状态没变时连状态文件都不写 —— 15 秒一轮的频率下，每轮都写一次闪存是没必要的。
    """
    previous = None
    try:
        previous = MODE_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        previous = None
    if previous == mode:
        return False
    try:
        MODE_FILE.parent.mkdir(exist_ok=True)
        MODE_FILE.write_text(mode, encoding="utf-8")
    except OSError:
        pass
    log.info(message)
    return True


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


def save_secret(account: str, password: str, account_type: str | None = None) -> None:
    payload = {"account": account, "password": password}
    if account_type:
        payload["account_type"] = account_type
    SECRET_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass


def load_secret() -> dict:
    if not SECRET_FILE.exists():
        raise SystemExit("还没有保存账号密码，请先运行: python3 campus_http.py --set-password")
    try:
        obj = json.loads(SECRET_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"读不了 {SECRET_FILE}: {exc}") from exc
    return {
        "account": obj["account"],
        "password": obj["password"],
        "account_type": normalize_account_type(obj.get("account_type")),
    }


def stored_account_type() -> str | None:
    """只读 secret.json 里的账号类型，文件不在/没写就当没设置（不报错）。"""
    try:
        obj = json.loads(SECRET_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return normalize_account_type(obj.get("account_type"))


def save_secret_account_type(account_type: str) -> bool:
    """只改账号类型，保留原来的账号密码。secret.json 不存在时返回 False。"""
    if not SECRET_FILE.exists():
        return False
    try:
        obj = json.loads(SECRET_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"读不了 {SECRET_FILE}: {exc}") from exc
    obj["account_type"] = account_type
    SECRET_FILE.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    return True


class Session:
    """
    带 Cookie 的极简 HTTP 会话。

    bind_ip 可选：把请求绑定到指定网卡的源地址发出。
    这样在电脑同时连着有线和无线时，可以让门户以为请求来自无线网段，
    从而在不拔网线、不影响正常连接的前提下调试无线那套流程。

    bind_device 可选：直接把 socket 绑到某张网卡上（Linux 的 SO_BINDTODEVICE）。
    路由器上两条上行同时在线时，光看目标地址分不清该走哪条，只有绑网卡才可靠 ——
    校园网是按"终端 IP"认证的，认证请求必须从对应那条线出去才有意义。
    """

    def __init__(self, timeout: int = 8, bind_ip: str | None = None,
                 bind_device: str | None = None, route_table: int = 0):
        self.timeout = timeout
        self.bind_ip = bind_ip
        self.bind_device = bind_device
        self.route_table = route_table or route_table_for(bind_device or "default")
        self._resolved_ip: str | None = None
        self._resolved_at = 0.0
        self.last_location = ""      # 最近一次响应的 Location，用来判断是不是被门户劫持
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

    def _bind_source(self) -> tuple[str, int] | None:
        """
        算出这次连接该用的源地址。

        实测（MT7981 + OpenWrt 24.10）：SO_BINDTODEVICE 绑无线网卡会直接
        "Host is unreachable"，但**按源地址绑定是好的**。所以这里优先把网卡名
        解析成它当前的 IPv4，用源地址绑；解析不出来才退回绑网卡。
        DHCP 续约后地址可能变，所以缓存 60 秒就重新解析一次。
        """
        if self.bind_ip:
            return (self.bind_ip, 0)
        if not self.bind_device:
            return None
        now = time.time()
        if self._resolved_ip and now - self._resolved_at < 60:
            return (self._resolved_ip, 0)
        self._resolved_ip = device_ipv4(self.bind_device)
        self._resolved_at = now
        if self._resolved_ip:
            # 光绑源地址没用，还要保证"从这个地址出去的包"查的是专用路由表
            ensure_source_route(self.bind_device, self._resolved_ip, self.route_table)
            return (self._resolved_ip, 0)
        return None

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
        """把请求绑定到指定源地址 / 指定网卡发出（http 和 https 都支持）。"""
        parts = urllib.parse.urlsplit(url)
        source = self._bind_source()
        device = "" if source else (self.bind_device or "")   # 拿到源地址就不用再绑网卡了
        if parts.scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE      # 学校证书链不完整时也能用
            conn = _BoundHTTPSConnection(
                parts.hostname, parts.port or 443, timeout=self.timeout,
                context=ctx, source_address=source, device=device,
            )
        else:
            conn = _BoundHTTPConnection(
                parts.hostname, parts.port or 80, timeout=self.timeout,
                source_address=source, device=device,
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
            self.last_location = resp.headers.get("Location") or ""
            return resp.status, resp.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()

    def request(self, url: str, data: dict | None = None, ajax: bool = False,
                method: str | None = None) -> tuple[int, str]:
        if self.bind_ip or self.bind_device:
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
                self.last_location = resp.headers.get("Location") or ""
                return resp.status, resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            self.last_location = (exc.headers.get("Location") or "") if exc.headers else ""
            return exc.code, exc.read().decode("utf-8", "ignore")
        except Exception as exc:
            return -1, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# 能"绑网卡"的 HTTP/HTTPS 连接
#
# SO_BINDTODEVICE 必须在 connect() 之前设好，连上之后再设是没用的，
# 所以要自己建 socket，不能直接用 http.client 默认那套。
# --------------------------------------------------------------------------- #
def _create_bound_socket(address, timeout, source_address, device):
    host, port = address
    last_error = None
    for res in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        af, socktype, proto, _canon, sockaddr = res
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            if device:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode())
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("getaddrinfo 没有返回可用地址")


def device_ipv4(device: str) -> str | None:
    """取某张网卡当前的 IPv4 地址。"""
    if not device:
        return None
    try:
        out = subprocess.run(["ip", "-4", "addr", "show", "dev", device],
                             capture_output=True, timeout=5)
        text = out.stdout.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", text)
    return m.group(1) if m else None


def route_table_for(name: str) -> int:
    """按线路名算一个固定的路由表号（100~199），重启后不变。"""
    return 100 + (sum(ord(ch) for ch in name) % 100)


def ensure_source_route(device: str, ip: str, table: int) -> bool:
    """
    给"从某个源地址出去的包"单独指定一张路由表。

    为什么必须这么做：Linux 是**按目的地址**选路的，光把 socket 绑到某个源地址
    并不能改变走哪张网卡。两条上行同时在线时，认证请求会顺着默认路由从有线出去，
    结果门户看到的是有线那个地址 —— 绑源地址就白绑了（实测就是这样）。

    所以加一条策略路由：来自该地址的包查这张专用表，表里的默认路由指向对应网卡。
    优先级 500 排在内核 default 之后、mwan3 的 fwmark 规则（1001+）之前。
    """
    if not device or not ip:
        return False
    try:
        out = subprocess.run(["ip", "route", "show", "default", "dev", device],
                             capture_output=True, timeout=5)
        m = re.search(r"via\s+(\d+\.\d+\.\d+\.\d+)", out.stdout.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return False
    if not m:
        return False
    gateway = m.group(1)
    script = "; ".join([
        f"ip route replace default via {gateway} dev {device} table {table}",
        f"ip rule del from {ip} lookup {table} 2>/dev/null",
        f"ip rule add from {ip} lookup {table} priority 500",
    ])
    try:
        return subprocess.run(["sh", "-c", script], capture_output=True, timeout=5).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def self_rss_mb() -> float:
    """读自己的常驻内存（MB）。取不到返回 0。"""
    try:
        for line in pathlib.Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def check_memory(cfg: dict) -> None:
    """
    自己盯着点内存：占用超过阈值就主动退出，让 procd 把服务重新拉起来。

    为什么需要这样：实测这个脚本的常驻内存会缓慢增长（约 1MB/小时 ——
    21 小时从 28MB 涨到 48MB）。路由器内存本来就小（233MB，还要跑 mwan3、
    dnsmasq、WiFi 等），涨到一定程度会触发内核 OOM。之前就发生过一次：
    脚本被 OOM 杀掉，而且前后 mwan3 的跟踪进程还卡死过，导致主备切换失效。

    主动重启比被 OOM 杀掉可控得多：日志里有明确记录，中断只有一两秒，
    起来后立刻恢复检查。阈值用 config.json 的 max_rss_mb 调，设 0 = 不检查。
    """
    limit = int(cfg.get("max_rss_mb", 60) or 0)
    if limit <= 0:
        return
    rss = self_rss_mb()
    if rss and rss > limit:
        log.warning("常驻内存已到 %.1f MB（上限 %s MB），主动退出让服务重新拉起来",
                    rss, limit)
        logging.shutdown()      # 先把日志刷出去
        os._exit(0)             # 整个进程退出；procd 的 respawn 会立刻重启


def sync_mwan3(profile: "Profile", online: bool) -> None:
    """
    把这条线的"认证状态"同步给 mwan3，让主备切换由认证结果驱动。

    为什么不靠 mwan3 自己的 ping 探测：
      路由器上实测踩过一次 —— mwan3 的跟踪进程会卡死（ping 子进程挂着不返回，
      整个跟踪循环冻住）。结果一条线明明已经掉了认证，mwan3 却一直以为它在线，
      主备切换彻底失效，宿舍直接断了网。而本脚本每 15 秒就在查校园网状态接口，
      本来就知道每条线的真相，由它来通知 mwan3 最可靠。

        online=True  -> mwan3 ifup   <名字>   把这条线放回可用池
        online=False -> mwan3 ifdown <名字>   把这条线摘出去，流量走别的线

    状态没变就不重复调用（ifup/ifdown 会重建一堆规则，没必要每 15 秒来一次）；
    但每隔 10 分钟会强制重申一次，免得 mwan3 那边被别的途径改掉
    （比如跟踪进程自己翻转、或者手工 ifup 过），这里能自动纠正回来。
    """
    name = (profile.mwan3 or "").strip()
    if not name:
        return
    want = "up" if online else "down"
    marker = profile.log_dir / "mwan3_state.txt"
    try:
        age = time.time() - marker.stat().st_mtime
        if marker.read_text(encoding="utf-8").strip() == want and age < 600:
            return
    except OSError:
        pass
    try:
        ok = subprocess.run(["mwan3", want, name],
                            capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        profile.log.warning("通知 mwan3 %s %s 失败", name, want)
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(want, encoding="utf-8")
    except OSError:
        pass
    profile.log.info("已通知 mwan3：%s %s（%s）", name, want,
                     "这条线已认证" if online else "这条线未认证，让流量走其它线")


class _BindDeviceMixin:
    """
    注意：http.client 在 __init__ 里会把 self._create_connection 赋成
    socket.create_connection（实例属性会盖住子类的同名方法），
    所以必须在 super().__init__() 之后再覆盖回来，只覆写方法是不够的。
    """

    device = ""

    def _install_device_binding(self, device: str | None) -> None:
        self.device = device or ""
        self._create_connection = lambda address, timeout=None, source_address=None: \
            _create_bound_socket(address, timeout, source_address, self.device)


class _BoundHTTPConnection(_BindDeviceMixin, http.client.HTTPConnection):
    def __init__(self, *args, device: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._install_device_binding(device)


class _BoundHTTPSConnection(_BindDeviceMixin, http.client.HTTPSConnection):
    def __init__(self, *args, device: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._install_device_binding(device)


# --------------------------------------------------------------------------- #
# 在线判断
# --------------------------------------------------------------------------- #
# 未认证时校园网会把请求劫持到门户；这些标记出现在页面或跳转地址里就说明没通
PORTAL_MARKERS = ("Dr.COMWebLogin", "DrcomServer", "eportal", "10.255.255.2")


def _looks_online(sess: Session, code: int, text: str) -> bool:
    """
    判断这次探测是不是"网络真的通了"。

    认证通过后拿到的是正常响应：可能 200，也可能是 3xx 跳转（百度就回 302 跳 HTTPS）。
    绑定出口时走的是底层 http.client，它不自动跟随跳转，所以只认 200 会漏判。
    """
    if any(marker in text for marker in PORTAL_MARKERS):
        return False
    if any(marker in sess.last_location for marker in PORTAL_MARKERS):
        return False
    if code == 200:
        return True
    return 300 <= code < 400


def is_online(sess: Session, cfg: dict, quiet: bool = False) -> bool:
    status, body = sess.request(cfg["status_url"], data={}, ajax=True)
    if status == 200 and body.strip().startswith("{"):
        try:
            if json.loads(body).get("success"):
                return True
        except ValueError:
            pass
    # 接口不可用或者返回未登录时，用真实访问复核（未认证时会被门户劫持）
    code, text = sess.request(cfg["probe_url"])
    if _looks_online(sess, code, text):
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
        # 高频重试时这些存档会飞快堆积，只保留最近的若干份
        old = sorted(LOG_DIR.glob("http-*.html"), key=lambda p: p.stat().st_mtime)
        for stale in old[:-DUMP_KEEP]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
def login(sess: Session, cfg: dict, account: str, password: str) -> str:
    """
    入口：先看门户在哪个网段，再决定走哪套登录流程。

    返回的是状态字符串而不是 bool，调用方才能决定要不要退避：
        "ok"       登录成功
        "offsite"  门户打不开 = 压根不在校园网。这不是失败，不该计入退避
        "failed"   在校园网但没登上（网络/服务端问题，值得下个周期重试）
        "fatal"    账号级问题（密码错 / 在线数超限 / 要验证码），重试也没用
    """
    code, portal_html = sess.request(cfg["portal_url"])
    if code != 200 or not (
        "Dr.COMWebLogin" in portal_html or "eportal" in portal_html or "DrcomServer" in portal_html
    ):
        log.warning("访问不到校园网门户，判定不在校园网，放弃登录（HTTP %s）", code)
        return "offsite"

    ip = client_ip_from_portal(portal_html)
    wired = needs_cas(ip)
    wired_flow = str(cfg.get("wired_flow") or "drcom").lower()

    if wired and wired_flow == "cas":
        log.info("有线网段（客户端 %s）→ 按配置走统一身份认证", ip)
        return "ok" if cas_login(sess, cfg, account, password) else "failed"

    if wired:
        # 实测（2026-09-16）：Dr.COM 的登录接口并不拒绝有线客户端。
        # 直接走表单可以绕开统一认证必须做的滑块 / 人脸验证，也不需要 RSA 加密。
        log.info("有线网段（客户端 %s）→ 优先走 Dr.COM 表单（可绕开统一认证的验证环节）", ip)
    else:
        log.info("无线网段（客户端 %s）→ 走 Dr.COM 门户登录", ip)

    result = drcom_login(sess, cfg, portal_html, account, password)
    if result == "ok":
        return "ok"
    if result == "fatal":
        # 账号级问题（密码错 / 在线数超限 / 要验证码）：换统一认证也一样没用，
        # 再打请求只会增加账号被锁的风险，所以直接停手。
        log.error("Dr.COM 登录遇到需要人工处理的提示，本次不再尝试其它登录方式")
        return "fatal"
    if wired and wired_flow != "cas":
        log.info("Dr.COM 方式没有成功，改用统一身份认证再试一次")
        return "ok" if cas_login(sess, cfg, account, password) else "failed"
    return "failed"


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
def cmd_set_password(account_type: str | None = None) -> None:
    account = input("校园网账号: ").strip()
    if not account:
        raise SystemExit("账号不能为空")
    import getpass
    password = getpass.getpass("密码(输入时不显示): ")
    if not password:
        raise SystemExit("密码不能为空")
    # 没显式指定就沿用已保存的类型，免得"只改个密码"把教师模式重置成学生
    if not account_type:
        account_type = stored_account_type()
    save_secret(account, password, account_type)
    print(f"已保存到 {SECRET_FILE}（建议 chmod 600）")
    if account_type:
        print(f"账号类型: {ACCOUNT_TYPE_LABEL[account_type]}（{account_type}）")
    else:
        print("账号类型沿用 config.json 的设置（默认学生账号）。")
        print("教师账号请再执行一次: python3 campus_http.py --mode teacher")


def cmd_set_mode(cfg: dict, value: str | None) -> int:
    """查看或切换账号类型（student / teacher）。不带参数就是查看。"""
    if value:
        account_type = normalize_account_type(value)
        if not account_type:
            print("账号类型只能是 student（学生）或 teacher（教师）")
            return 2
        if not save_secret_account_type(account_type):
            print(f"还没有 {SECRET_FILE}，请先运行: python3 campus_http.py --set-password")
            return 1
        print(f"账号类型已切换为: {ACCOUNT_TYPE_LABEL[account_type]}（{account_type}）")
        print("改完记得重启服务: /etc/init.d/campus-net-login restart")

    cred = load_secret()
    account_type = resolve_account_type(cfg, cred["account_type"], cred["account"])
    print(f"当前账号: {cred['account']}")
    print(f"当前账号类型: {ACCOUNT_TYPE_LABEL[account_type]}（{account_type}）")
    if account_type == "teacher":
        print("夜间策略: 忽略夜间限制时段，整夜照常检测并自动登录")
    else:
        quiet = cfg.get("quiet_hours") or {}
        if quiet.get("enabled"):
            print(f"夜间策略: {quiet.get('start', '00:00')}-{quiet.get('end', '06:00')} 暂停尝试"
                  "（学生账号被学校限制的时段）")
        else:
            print("夜间策略: 未启用夜间限制，整夜照常检测")
    return 0


def _watch_one(profile: Profile, bind_ip: str | None = None) -> int:
    """一条线路的看门狗循环。配了多条线时，每条线在自己的线程里跑这个。"""
    _current_profile.set(profile)
    setup_logging(verbose=False, profile=profile)
    cfg = load_config(profile)

    cred = load_secret()
    account, password = cred["account"], cred["password"]
    account_type = resolve_account_type(
        cfg, cred["account_type"] or profile.default_account_type, account
    )
    cfg = apply_account_type(cfg, account_type)
    sess = Session(cfg["timeout"], bind_ip=bind_ip,
                   bind_device=profile.device or None)

    interval = max(5, int(cfg["interval"]))
    # 教师模式默认不做失败退避：网络一恢复就立刻登录，不用干等 2/5/15/30 分钟。
    # 想变回和学生一样，把 config.json 里的 teacher_backoff 改成 true。
    use_backoff = account_type != "teacher" or bool(cfg.get("teacher_backoff"))
    # 但"账号级问题"（密码错 / 已在线 / 要验证码）再怎么重试也没用，还容易把账号
    # 撞锁，所以教师模式下这类情况仍然给一个固定冷却。设成 0 = 完全不管。
    fatal_cooldown = int(cfg.get("teacher_fatal_cooldown", 300) or 0) if not use_backoff else 0

    log.info("看门狗启动，每 %s 秒检测一次", interval)
    log.info("线路 %s：出口 %s，账号 %s（%s）",
             profile.name, profile.device or "按系统路由",
             account, ACCOUNT_TYPE_LABEL[account_type])
    if account_type == "teacher":
        log.info("教师账号模式：忽略夜间限制时段；在线时保持静默，不做状态刷屏")
        if use_backoff:
            log.info("  登录失败仍按 failure_backoff 退避（teacher_backoff=true）")
        elif fatal_cooldown:
            log.info("  登录失败不退避，每个周期都重试；账号级问题冷却 %s 秒", fatal_cooldown)
        else:
            log.info("  登录失败不退避，每个周期都重试")

    while True:
        started = time.time()
        try:
            # 占用太高就自己重启（procd 会拉起来），别等到被内核 OOM 杀掉
            check_memory(cfg)

            # 学校是"到点断网"，等断了再切就已经晚了（正在跑的连接会断）。
            # 所以提前几分钟就把这条线摘出备用池，让流量在断网前先走开。
            lead = int(cfg.get("pre_switch_minutes", 5) or 0)
            if lead and in_pre_quiet(cfg, account_type, lead):
                sync_mwan3(profile, False)
                note_mode("pre-quiet",
                          f"距夜间限制时段不到 {lead} 分钟，提前把这条线摘出备用池，"
                          "避免到点断网时的卡顿")
                time.sleep(60)
                continue

            if in_quiet_hours(cfg, account_type):
                quiet = cfg.get("quiet_hours") or {}
                note_mode("quiet", f"进入夜间限制时段({quiet.get('start','00:00')}-{quiet.get('end','06:00')})，"
                                   "学校此时不允许学生账号认证，暂停尝试")
                # 夜间不尝试登录，但**仍然要看这条线是不是已经掉认证了** ——
                # 学校正是断在这个时段，不告诉 mwan3 的话主备切换根本不会发生。
                sync_mwan3(profile, is_online(sess, cfg, quiet=True))
                time.sleep(300)
                continue

            if is_online(sess, cfg, quiet=True):
                clear_retry()
                sync_mwan3(profile, True)
                note_mode("normal", "网络已恢复，回到常规检查")
            else:
                retry = load_retry()
                next_attempt = float(retry.get("next_attempt", 0) or 0)
                if next_attempt > time.time():
                    wait = int(next_attempt - time.time()) + 1
                    span = f"{wait // 60} 分钟" if wait >= 60 else f"{wait} 秒"
                    note_mode(f"backoff-{int(next_attempt)}",
                              f"{retry.get('reason') or '上次登录失败'}，{span}后再试")
                elif not portal_ok(sess, cfg):
                    # 门户都打不开 = 压根不在校园网（网线没插 / 上游断了 / 学校断网）。
                    # 这不算"登录失败"，不能计入退避，否则等网络恢复后还要白等
                    # 最多 30 分钟才去尝试。cmd_login 早就有这个判断，cmd_watch 漏了。
                    clear_retry()
                    note_mode("normal", "不在校园网环境（门户不可达），跳过本次尝试")
                else:
                    note_mode("login", "检测到未认证，开始尝试登录")
                    # 在校园网、但这条线没通过认证 = 这条线现在不可用，先摘出去
                    sync_mwan3(profile, False)
                    result = login(sess, cfg, account, password)
                    if result == "ok":
                        clear_retry()
                        sync_mwan3(profile, True)
                    elif result == "offsite":
                        clear_retry()
                        note_mode("normal", "不在校园网环境（门户不可达），跳过本次尝试")
                    elif not use_backoff and result != "fatal":
                        # 教师模式：普通失败不退避，下个周期马上再试。
                        # 保持静默（note_mode 状态没变就不写日志），细节看 login() 自己的记录。
                        clear_retry()
                    elif not use_backoff and fatal_cooldown > 0:
                        save_retry({"failures": int(retry.get("failures", 0)) + 1,
                                    "next_attempt": time.time() + fatal_cooldown,
                                    "reason": "账号级问题（需要人工处理）"})
                    else:
                        failures = int(retry.get("failures", 0)) + 1
                        delay = backoff_seconds(cfg, failures)
                        save_retry({"failures": failures, "next_attempt": time.time() + delay,
                                    "reason": f"上次登录失败（累计 {failures} 次）"})
                        log.warning("登录失败，%s 秒内不再重试（累计失败 %s 次）", delay, failures)
        except Exception as exc:
            log.exception("循环异常: %s", exc)
        # 让"每 N 秒检测一次"名副其实：扣掉这次检测本身花掉的时间
        time.sleep(max(1.0, interval - (time.time() - started)))


def _watch_thread(profile: Profile, bind_ip: str | None) -> None:
    """线程入口：把异常记下来，别让一条线挂了带崩整个进程。"""
    try:
        _watch_one(profile, bind_ip)
    except BaseException:  # noqa: BLE001
        _current_profile.set(profile)
        try:
            setup_logging(verbose=False, profile=profile)
            log.exception("线路 %s 的看门狗异常退出", profile.name)
        except Exception:  # noqa: BLE001
            pass


def cmd_watch(cfg: dict | None = None, bind_ip: str | None = None) -> int:
    """
    看门狗入口。

    只有一条线时就在当前线程里跑（和以前完全一样）；
    配了多条线时，在同一个进程里给每条线开一个线程 —— 这样只占一份解释器内存，
    而且两条线互不阻塞（一条在登录时，另一条照常检查）。
    """
    profiles = load_profiles()
    if len(profiles) == 1:
        return _watch_one(profiles[0], bind_ip)

    setup_logging(verbose=False, profile=profiles[0])
    profiles[0].log.info("检测到 %s 条线路，在同一个进程里并行看护：%s",
                         len(profiles), _profile_names(profiles))
    threads = []
    for p in profiles:
        t = threading.Thread(target=_watch_thread, args=(p, bind_ip),
                             name=p.name, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return 0


def _login_one(profile: Profile, bind_ip: str | None = None) -> int:
    _current_profile.set(profile)
    setup_logging(verbose=False, profile=profile)
    cfg = load_config(profile)
    return _login_with(cfg, bind_ip, profile)


def cmd_login(cfg: dict | None = None, bind_ip: str | None = None) -> int:
    """执行一次登录。配了多条线时逐条来。"""
    profiles = load_profiles()
    if len(profiles) == 1:
        return _login_one(profiles[0], bind_ip)
    rc = 0
    for p in profiles:
        rc |= _login_one(p, bind_ip)
    return rc


def _login_with(cfg: dict, bind_ip: str | None = None,
                profile: Profile | None = None) -> int:
    """
    执行一次登录。

    这里做了三层"少打扰"处理：
      1. 夜间限制时段（默认周一~周五 00:00-06:00）直接不尝试
      2. 已经在线 / 不在校园网 都不尝试
      3. 登录失败后按 2/5/15/30 分钟退避，避免每分钟都去撞墙
    每种情况只在"状态变化"时写一行日志。
    """
    sess = Session(cfg["timeout"], bind_ip=bind_ip,
                   bind_device=(profile.device or None) if profile else None)
    now_ts = time.time()

    account_type = resolve_account_type(
        cfg, stored_account_type() or (profile.default_account_type if profile else ""),
        stored_account()
    )
    cfg = apply_account_type(cfg, account_type)

    if in_quiet_hours(cfg, account_type):
        quiet = cfg.get("quiet_hours") or {}
        note_mode("quiet", f"进入夜间限制时段（{quiet.get('start', '00:00')}-{quiet.get('end', '06:00')}），"
                           "学校此时不允许学生账号认证，暂停尝试")
        return 0

    if is_online(sess, cfg, quiet=True):
        clear_retry()
        note_mode("normal", "网络已恢复，回到常规检查")
        return 0

    if not portal_ok(sess, cfg):
        clear_retry()
        note_mode("offsite", "不在校园网环境（门户不可达），跳过本次尝试")
        return 0

    retry = load_retry()
    next_attempt = float(retry.get("next_attempt", 0) or 0)
    if next_attempt > now_ts:
        wait = int(next_attempt - now_ts) + 1
        span = f"{wait // 60} 分钟" if wait >= 60 else f"{wait} 秒"
        note_mode(f"backoff-{int(next_attempt)}",
                  f"{retry.get('reason') or '上次登录失败'}，{span}后再试")
        return 0

    note_mode("login", "检测到未认证，开始尝试登录")
    cred = load_secret()
    result = login(sess, cfg, cred["account"], cred["password"])
    if result == "ok":
        clear_retry()
        return 0
    if result == "offsite":
        clear_retry()
        note_mode("offsite", "不在校园网环境（门户不可达），跳过本次尝试")
        return 0

    failures = int(retry.get("failures", 0)) + 1
    delay = backoff_seconds(cfg, failures)
    save_retry({"failures": failures, "next_attempt": now_ts + delay,
                "reason": f"上次登录失败（累计 {failures} 次）"})
    log.warning("登录失败，%s 秒内不再重试（累计失败 %s 次）", delay, failures)
    return 1


def _do_probe(sess: Session, cfg: dict) -> None:
    """探测门户走哪套流程（只读，不登录）。"""
    code, html = sess.request(cfg["portal_url"])
    ip = client_ip_from_portal(html)
    log.info("门户 HTTP %s，页面 %s 字节", code, len(html))
    log.info("门户看到的客户端地址: %s", ip)
    wired_flow = str(cfg.get("wired_flow") or "drcom").lower()
    if not needs_cas(ip):
        flow = "无线网段 → Dr.COM 门户登录"
    elif wired_flow == "cas":
        flow = "有线网段 → 统一身份认证 + 拼图滑块（config.json 里强制指定）"
    else:
        flow = "有线网段 → 先 Dr.COM 表单，失败再回退统一身份认证"
    account_type = resolve_account_type(cfg, stored_account_type(), stored_account())
    log.info("应该走: %s", flow)
    log.info("账号类型: %s", ACCOUNT_TYPE_LABEL[account_type])
    conf = parse_portal_config(html)
    for k in sorted(conf):
        log.info("  门户配置 %s = %s", k, conf[k])
    log.info("当前是否已在线: %s", is_online(sess, cfg, quiet=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="校园网自动登录（纯 HTTP 版）")
    parser.add_argument("--check", action="store_true", help="检测在线状态")
    parser.add_argument("--login", action="store_true", help="执行一次登录")
    parser.add_argument("--watch", action="store_true", help="常驻看门狗")
    parser.add_argument("--set-password", action="store_true", help="保存账号密码")
    parser.add_argument("--mode", "--account-type", dest="mode", nargs="?", const="",
                        default=None, metavar="{student,teacher}",
                        help="查看/切换账号类型：student=学生账号，"
                             "teacher=教师账号（不受夜间限制，整夜也会自动登录）")
    parser.add_argument("--probe", action="store_true", help="探测门户走的是哪套登录流程（不登录）")
    parser.add_argument("--profile", metavar="名字",
                        help="只操作指定线路（config.json 里配了多条线时用）")
    parser.add_argument("--quiet", action="store_true", help="不输出到控制台")
    args = parser.parse_args()

    # 输出统一成 UTF-8：被 PowerShell 捕获时（Select-String 等）才不会变乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        # --set-password / --mode 要写某条线的文件，所以得先定下是哪条线
        if args.set_password:
            prof = resolve_profile(args.profile)
            _current_profile.set(prof)
            setup_logging(verbose=True, profile=prof)
            cmd_set_password(normalize_account_type(args.mode))
            return 0
        if args.mode is not None:
            prof = resolve_profile(args.profile)
            _current_profile.set(prof)
            setup_logging(verbose=True, profile=prof)
            return cmd_set_mode(load_config(prof), args.mode or None)
        if args.watch:
            return cmd_watch()
        if args.login:
            return cmd_login()
        if not (args.probe or args.check):
            parser.print_help()
            return 0

        # --probe / --check：默认把所有线路都过一遍
        profiles = load_profiles()
        if args.profile:
            profiles = [resolve_profile(args.profile)]
        rc = 0
        for prof in profiles:
            _current_profile.set(prof)
            setup_logging(verbose=not args.quiet, profile=prof)
            pcfg = load_config(prof)
            sess = Session(pcfg["timeout"], bind_device=prof.device or None)
            if args.probe:
                _do_probe(sess, pcfg)
                continue
            account_type = resolve_account_type(
                pcfg, stored_account_type() or prof.default_account_type, stored_account()
            )
            online = is_online(sess, pcfg)
            log.info("当前状态: %s", "已在线" if online else "未认证/已断网")
            log.info("账号类型: %s", ACCOUNT_TYPE_LABEL[account_type])
            if not online:
                rc = 1
        return rc
    except SystemExit:
        raise
    except Exception:
        log.exception("程序异常退出")
        return 3

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
