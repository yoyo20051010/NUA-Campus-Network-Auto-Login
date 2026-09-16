#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京艺术学院 校园网自动登录 —— macOS 版

与 Windows 原版的区别（详细说明见 ../README.md）:
  1. 只用 Python 标准库, 不需要 Playwright / Chromium;
  2. 密码存 macOS 钥匙串(Keychain), 不再用 Windows DPAPI;
  3. 所有探测请求都**绑定物理网卡**(IP_BOUND_IF), 不走进 VPN 隧道;
  4. DNS 自己解析(自带的迷你 DNS 客户端), 绕过 Clash/Surge 的 fake-ip;
  5. 同时支持 有线(统一认证 CAS + 滑块) 与 无线(Dr.COM 原生表单) 两套流程;
  6. 开机自启用 launchd(LaunchAgent), 不是计划任务。

认证链路:
    10.255.255.2 (Dr.COM ePortal v4.0, 801 端口)
      ├─ 有线网段(10.x <= 10.51.255.255) -> https://c.nua.edu.cn/cas/wifiLogin/innerLogin.jsp
      │     -> https://c.nua.edu.cn/cas/login  账号密码(RSA) + 滑块拼图
      └─ 无线网段(10.53.x / 10.54.x)      -> Dr.COM 原生表单 DDDDD / upass

常用命令:
    python3 campus_mac.py --diagnose        # 体检: 网卡/VPN/DNS/门户/认证状态
    python3 campus_mac.py --check           # 只看状态
    python3 campus_mac.py --login           # 立即登录一次
    python3 campus_mac.py --login --dry-run # 演练: 走到提交前一步, 不发送密码
    python3 campus_mac.py --watch           # 常驻看门狗(掉线自动重连)
    python3 campus_mac.py --set-password    # 保存账号密码到钥匙串

安全闸: 只有在"校园网门户确实能加载"且"当前未认证"时才会尝试登录。
        连家里 WiFi / 手机热点时门户根本打不开, 一律跳过, 不会乱试密码。
"""

from __future__ import annotations

import argparse
import atexit
import getpass
import http.client
import json
import logging
import os
import pathlib
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.parse

APP_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config.json"
SECRET_FILE = APP_DIR / "secret.json"          # 钥匙串不可用时的兜底(0600)
LOG_DIR = APP_DIR / "logs"
STATE_DIR = APP_DIR / "state"
STATE_FILE = STATE_DIR / "last_state.txt"
HOSTS_CACHE = STATE_DIR / "hosts_cache.json"
LOCK_FILE = STATE_DIR / "run.lock"
RETRY_FILE = STATE_DIR / "retry.json"
MODE_FILE = STATE_DIR / "mode.txt"
# 上次登录成功的"服务类型"后缀(学校三家运营商账号后缀不同, 见 CARRIERS)
# 按客户端网段分别记 —— 移动/电信/联通的校园网可能是不同网段,
# 记错了也没关系, 会依次试其它后缀, 只是多一次请求。
CARRIER_FILE = STATE_DIR / "carrier.json"
LOCK_STALE_SECONDS = 900

# Darwin: setsockopt(IPPROTO_IP, IP_BOUND_IF, ifindex) 把这条连接强制绑到物理网卡,
# 从而绕过 Clash / Surge / WireGuard 等 TUN 模式 VPN 占用的路由表。
IP_BOUND_IF = 25

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "portal_url": "http://10.255.255.2/",
    "portal_api_port": 801,
    "cas_login_url": "https://c.nua.edu.cn/cas/login",
    "service": "https://c.nua.edu.cn/cas/wifiLogin/innerLogin.jsp",
    "status_url": "https://c.nua.edu.cn/cas/wifiLogin/isLogin",
    # 注意 /cas: 学校页面里 contextPath="/cas", 上报地址是 contextPath + /captchValid/...
    # (v1.0 少了这个前缀; 下面还会先尝试从验证页里解析 contextPath)
    "captcha_url": "https://c.nua.edu.cn/cas/captchValid/checkCaptchImg",
    "probe_url": "http://connect.rom.miui.com/generate_204",
    "account": "",
    "keychain_service": "campus-net-login",
    # 留空 = 自动挑选(优先有线, 再无线, 且要求真的能打开门户)
    "interface": "",
    # 校园网自己用的"有线/无线"判据, 抄自门户页 a41.js
    "cas_ip_ranges": [["1.1.1.1", "10.51.255.255"], ["10.128.0.1", "10.129.255.255"]],
    "interval": 30,
    "interval_battery": 120,
    # 夜间静默: 学校这段时间不允许学生账号认证, 干脆完全不发请求
    "quiet_hours": {"enabled": True, "start": "00:00", "end": "06:00",
                    "days": [0, 1, 2, 3, 4]},
    # 登录失败后的重试间隔(秒), 依次取用, 超出后一直用最后一个
    "failure_backoff": [120, 300, 900, 1800],
    "interval_offcampus": 300,
    "login_timeout": 90,
    "http_timeout": 8,
    "dns_timeout": 3,
    # DNS: 留空则用 DHCP 下发的, 再不行用这两个
    "dns_servers": [],
    "fallback_dns": ["223.5.5.5", "114.114.114.114"],
    # 如果 DNS 被 VPN 劫持得无法解析, 可以在这里写死真实 IP
    "pinned_hosts": {},
    "drcom_extra_query": {},
    # 无线门户的"服务类型": 留空=依次试 校园用户/@njxy/@dx/@lt
    "wifi_suffix": "",
    # 有线网段默认也走 Dr.COM 表单 —— 实测(2026-09-15)可行,
    # 且能绕过学校的"人脸识别"安全验证(统一认证那条路需要真人刷脸, 自动化不了)。
    # 想强制走统一认证就把这里改成 "cas"。
    "wired_flow": "drcom",
    # 老版表单接口在本校 AC 上无效(返回管理后台页面), 默认不再尝试;
    # 若换到别的学校/设备且新版接口失效, 把它改成 true 可启用兜底尝试。
    "drcom_legacy_fallback": False,
}

STATE_ONLINE = "online"
STATE_OFFLINE_CAMPUS = "offline_campus"
STATE_NOT_CAMPUS = "not_campus"
STATE_UNKNOWN = "unknown"

STATE_TEXT = {
    STATE_ONLINE: "已认证在线",
    STATE_OFFLINE_CAMPUS: "在校园网且未认证",
    STATE_NOT_CAMPUS: "不在校园网环境",
    STATE_UNKNOWN: "状态不明",
}

log = logging.getLogger("campus_mac")


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def sh(cmd: list, timeout: float = 5.0):
    """跑一条系统命令, 返回 (返回码, stdout+stderr)。失败不抛异常。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:                                  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def ip_to_int(ip: str) -> int:
    parts = [int(x) for x in ip.split(".")]
    if len(parts) != 4:
        raise ValueError(ip)
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def in_ranges(ip: str, ranges: list) -> bool:
    try:
        value = ip_to_int(ip)
    except (ValueError, IndexError):
        return False
    for low, high in ranges:
        try:
            if ip_to_int(low) <= value <= ip_to_int(high):
                return True
        except ValueError:
            continue
    return False


def setup_logging(verbose: bool = True) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    log_file = LOG_DIR / "campus_mac.log"
    try:
        if log_file.exists() and log_file.stat().st_size > 2 * 1024 * 1024:
            log_file.replace(LOG_DIR / "campus_mac.log.1")
    except OSError:
        pass

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    if verbose and sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        log.addHandler(stream)

    log.setLevel(logging.DEBUG if os.environ.get("CAMPUS_DEBUG") else logging.INFO)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log.warning("config.json 读取失败, 用默认值: %s", exc)
    return cfg


# --------------------------------------------------------------------------- #
# 网卡 / 路由 / DNS 发现
# --------------------------------------------------------------------------- #
VIRTUAL_PREFIXES = (
    "utun", "ipsec", "ppp", "bridge", "ap", "awdl", "llw", "nan",
    "gif", "stf", "lo", "vmenet", "tap", "tun", "anpi",
)


def iface_addresses() -> dict:
    """{en0: {ipv4, netmask, mac, up}} —— 来自 ifconfig。"""
    out = {}
    code, text = sh(["/sbin/ifconfig", "-a"])
    if code != 0:
        return out
    current = None
    for line in text.splitlines():
        m = re.match(r"^([a-zA-Z0-9_]+):\s+flags=\d+<([^>]*)>", line)
        if m:
            current = m.group(1)
            out[current] = {"ipv4": "", "netmask": "", "mac": "",
                            "up": "UP" in m.group(2), "running": "RUNNING" in m.group(2)}
            continue
        if current is None:
            continue
        line = line.strip()
        if line.startswith("inet "):
            parts = line.split()
            out[current]["ipv4"] = parts[1]
            for i, tok in enumerate(parts):
                if tok == "netmask" and i + 1 < len(parts):
                    out[current]["netmask"] = parts[i + 1]
        elif line.startswith("ether "):
            out[current]["mac"] = line.split()[1].replace(":", "")
    return out


def hardware_ports() -> dict:
    """{en0: 'Wi-Fi', en3: 'Ethernet Adapter'}"""
    mapping = {}
    code, text = sh(["/usr/sbin/networksetup", "-listallhardwareports"], timeout=8)
    if code != 0:
        return mapping
    port = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port:
            mapping[line.split(":", 1)[1].strip()] = port
            port = None
    return mapping


def iface_kind(name: str, ports: dict) -> str:
    if name.startswith(VIRTUAL_PREFIXES):
        return "virtual"
    port = ports.get(name, "")
    if port == "Wi-Fi":
        return "wifi"
    if "Ethernet" in port or "LAN" in port:
        return "ethernet"
    return "other"


def route_get(target: str) -> dict:
    """route -n get <ip|default> -> {interface, gateway}"""
    info = {"interface": "", "gateway": ""}
    code, text = sh(["/sbin/route", "-n", "get", target])
    if code != 0:
        return info
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("interface:"):
            info["interface"] = line.split(":", 1)[1].strip()
        elif line.startswith("gateway:"):
            info["gateway"] = line.split(":", 1)[1].strip()
    return info


def dhcp_dns_servers(iface: str) -> list:
    """DHCP 下发的 DNS(校园网一般是 112.4.0.55 这种)。"""
    servers = []
    code, text = sh(["/usr/sbin/ipconfig", "getpacket", iface])
    if code == 0:
        m = re.search(r"domain_name_server[^}]*\{([^}]*)\}", text)
        if m:
            for item in m.group(1).split(","):
                item = item.strip()
                if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", item):
                    servers.append(item)
    return servers


def vpn_interfaces(addrs: dict) -> list:
    """带 IPv4 的 utun/ipsec 等隧道接口 —— 有它基本就是 VPN 开着。"""
    found = []
    for name, info in addrs.items():
        if name.startswith(("utun", "ipsec", "ppp", "tap", "tun")) and info.get("ipv4"):
            found.append(name)
    return sorted(found)


# --------------------------------------------------------------------------- #
# 迷你 DNS 客户端(只查 A 记录, 走物理网卡, 绕开 fake-ip)
# --------------------------------------------------------------------------- #
FAKE_IP_RANGES = [
    ("198.18.0.0", "198.19.255.255"),     # Clash / Surge 默认 fake-ip 段
    ("240.0.0.0", "255.255.255.254"),     # 部分客户端用 240/4 做 fake-ip
    ("28.0.0.0", "28.255.255.255"),       # 少数客户端
    ("127.0.0.0", "127.255.255.255"),
    ("169.254.0.0", "169.254.255.255"),
    ("0.0.0.0", "0.255.255.255"),
]


def is_fake_ip(ip: str) -> bool:
    return in_ranges(ip, FAKE_IP_RANGES)


def dns_query_a(name: str, server: str, iface_idx, timeout: float = 3.0) -> list:
    """手工构造一个 A 记录查询, 用 UDP 发给 server(绑定物理网卡)。"""
    tid = random.randint(0, 0xFFFF)
    header = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(label)]) + label.encode("ascii", "ignore")
                     for label in name.split(".") if label) + b"\x00"
    packet = header + qname + struct.pack("!HH", 1, 1)

    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if iface_idx is not None:
            sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, struct.pack("I", iface_idx))
        sock.settimeout(timeout)
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)
    except OSError:
        return []
    finally:
        if sock is not None:
            sock.close()

    if len(data) < 12:
        return []
    rid, flags, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", data[:12])
    if rid != tid or not (flags & 0x8000):
        return []

    def skip_name(offset: int) -> int:
        while offset < len(data):
            length = data[offset]
            if length == 0:
                return offset + 1
            if length & 0xC0 == 0xC0:
                return offset + 2
            offset += 1 + length
        raise ValueError("bad name")

    try:
        offset = 12
        for _ in range(qdcount):
            offset = skip_name(offset) + 4
        answers = []
        for _ in range(ancount):
            offset = skip_name(offset)
            if offset + 10 > len(data):
                break
            rtype, _, _, rdlength = struct.unpack("!HHIH", data[offset:offset + 10])
            offset += 10
            rdata = data[offset:offset + rdlength]
            offset += rdlength
            if rtype == 1 and rdlength == 4:
                answers.append(".".join(str(b) for b in rdata))
        return answers
    except (ValueError, struct.error):
        return []


class Resolver:
    """先问校园 DNS, 再问公共 DNS, 全部走物理网卡; 过滤掉 fake-ip。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.iface_idx = None
        self.servers = []
        self.cache = {}
        self.stats = []
        try:
            self.cache = json.loads(HOSTS_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def configure(self, iface, iface_idx, dhcp_servers: list) -> None:
        self.iface_idx = iface_idx
        servers = list(self.cfg.get("dns_servers") or [])
        servers += [s for s in dhcp_servers if s not in servers]
        servers += [s for s in (self.cfg.get("fallback_dns") or []) if s not in servers]
        self.servers = servers

    def save(self) -> None:
        try:
            STATE_DIR.mkdir(exist_ok=True)
            now = time.time()
            fresh = {k: v for k, v in self.cache.items()
                     if now - v.get("ts", 0) < 7 * 86400}
            HOSTS_CACHE.write_text(json.dumps(fresh, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except OSError:
            pass

    def resolve(self, host: str, allow_stale: bool = True):
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
            return host

        pinned = (self.cfg.get("pinned_hosts") or {}).get(host)
        if pinned:
            return pinned

        entry = self.cache.get(host)
        if entry and time.time() - entry.get("ts", 0) < 6 * 3600 and not is_fake_ip(entry.get("ip", "")):
            return entry["ip"]

        for server in self.servers:
            answers = dns_query_a(host, server, self.iface_idx,
                                  float(self.cfg.get("dns_timeout", 3)))
            real = [ip for ip in answers if not is_fake_ip(ip)]
            if real:
                self.stats.append(f"{host} -> {real[0]} (via {server})")
                self._remember(host, real[0])
                return real[0]
            if answers:
                self.stats.append(f"{host} -> {answers[0]} (via {server}, Fake-IP!)")

        # 退路: 系统解析器(可能被 VPN 换成 fake-ip)
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            for info in infos:
                ip = info[4][0]
                if not is_fake_ip(ip):
                    self.stats.append(f"{host} -> {ip} (via system)")
                    self._remember(host, ip)
                    return ip
            if infos:
                self.stats.append(f"{host} -> {infos[0][4][0]} (system, Fake-IP)")
        except socket.gaierror:
            pass

        if allow_stale and entry and entry.get("ip"):
            self.stats.append(f"{host} -> {entry['ip']} (过期缓存)")
            return entry["ip"]
        self.stats.append(f"{host} -> 解析失败")
        return None

    def _remember(self, host: str, ip: str) -> None:
        self.cache[host] = {"ip": ip, "ts": time.time()}
        self.save()


# --------------------------------------------------------------------------- #
# 绑定网卡的 HTTP 客户端(标准库实现, 无第三方依赖)
# --------------------------------------------------------------------------- #
def bound_socket(iface_idx, source_ip, timeout=None) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if iface_idx is not None:
        try:
            sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, struct.pack("I", iface_idx))
        except OSError as exc:
            log.debug("IP_BOUND_IF 设置失败(%s), 退化为普通 socket", exc)
    if source_ip:
        try:
            sock.bind((source_ip, 0))
        except OSError:
            pass
    if timeout:
        sock.settimeout(timeout)
    return sock


class _BoundConnection(http.client.HTTPConnection):
    def __init__(self, ip, port, iface_idx, source_ip, timeout, server_hostname):
        super().__init__(ip, port, timeout=timeout)
        self._iface_idx = iface_idx
        self._source_ip = source_ip
        self._server_hostname = server_hostname

    def connect(self) -> None:
        self.sock = bound_socket(self._iface_idx, self._source_ip, self.timeout)
        self.sock.connect((self.host, self.port))


class _BoundHTTPSConnection(_BoundConnection):
    def connect(self) -> None:
        super().connect()
        # 校园网证书链经常不完整, 和 Windows 版一样不校验证书
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(self.sock, server_hostname=self._server_hostname)


class Response:
    def __init__(self, status, headers, text, url, error=""):
        self.status = status
        self.headers = headers
        self.text = text
        self.url = url
        self.error = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.url} {len(self.text)}B {self.error}>"


class Session:
    """带 Cookie、跟随跳转、可绑定物理网卡的 HTTP 会话。"""

    def __init__(self, cfg: dict, resolver: Resolver):
        self.cfg = cfg
        self.resolver = resolver
        self.timeout = float(cfg.get("http_timeout", 8))
        self.iface_idx = None
        self.source_ip = None
        self.cookies = {}
        self.last_dns = []

    def bind(self, iface_idx, source_ip) -> None:
        self.iface_idx = iface_idx
        self.source_ip = source_ip

    def request(self, url, method="GET", data=None, headers=None,
                redirects=5, ajax=False, raw_body=None) -> Response:
        current = url
        cur_method = method
        cur_data = data
        cur_raw = raw_body

        for _ in range(redirects + 1):
            resp = self._once(current, cur_method, cur_data, headers, ajax, cur_raw)
            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                location = urllib.parse.urljoin(current, resp.headers["location"])
                if resp.status in (301, 302, 303):
                    cur_method, cur_data, cur_raw = "GET", None, None
                current = location
                continue
            return resp
        return Response(-1, {}, "", current, "too many redirects")

    def _once(self, url, method, data, headers, ajax, raw_body) -> Response:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        ip = self.resolver.resolve(host)
        self.last_dns = list(self.resolver.stats)
        if not ip:
            return Response(-1, {}, "", url, f"DNS 解析失败: {host}")

        body = raw_body
        hdrs = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if port in (80, 443):
            hdrs["Host"] = host
        else:
            hdrs["Host"] = f"{host}:{port}"
        if data is not None:
            body = urllib.parse.urlencode(data, encoding="utf-8").encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if ajax:
            hdrs["X-Requested-With"] = "XMLHttpRequest"
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if headers:
            hdrs.update(headers)

        conn_cls = _BoundHTTPSConnection if scheme == "https" else _BoundConnection
        conn = conn_cls(ip, port, self.iface_idx, self.source_ip, self.timeout, host)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            raw = conn.getresponse()
            payload = raw.read()
        except Exception as exc:                              # noqa: BLE001
            return Response(-1, {}, "", url, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except Exception:                                 # noqa: BLE001
                pass

        text = payload.decode("utf-8", "ignore")
        if not text.strip() or "\ufffd" in text[:4000]:
            text = payload.decode("gbk", "ignore")

        resp_headers = {k.lower(): v for k, v in raw.getheaders()}
        self._eat_cookies(raw.getheaders())
        return Response(raw.status, resp_headers, text, url)

    def _eat_cookies(self, headers) -> None:
        for key, value in headers:
            if key.lower() != "set-cookie":
                continue
            pair = value.split(";", 1)[0].strip()
            if "=" in pair:
                name, val = pair.split("=", 1)
                self.cookies[name.strip()] = val.strip()


# --------------------------------------------------------------------------- #
# 网络环境(挑网卡 / 绑网卡)
# --------------------------------------------------------------------------- #
class NetEnv:
    """负责挑出"应该走哪张物理网卡", 并把所有请求绑到它上面。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.addrs = {}
        self.ports = {}
        self.iface = ""
        self.iface_idx = None
        self.source_ip = ""
        self.mac = ""
        self.vpns = []
        self.default_route = {}
        self.portal_route = {}
        self.dhcp_dns = []
        self.resolver = Resolver(cfg)
        self.session = Session(cfg, self.resolver)

    def refresh(self) -> None:
        self.addrs = iface_addresses()
        try:
            self.ports = hardware_ports()
        except Exception:                                     # noqa: BLE001
            self.ports = {}
        self.vpns = vpn_interfaces(self.addrs)
        self.default_route = route_get("default")
        portal_host = urllib.parse.urlsplit(self.cfg["portal_url"]).hostname or "10.255.255.2"
        self.portal_route = route_get(portal_host)

    def candidates(self) -> list:
        """可用的物理网卡: 有 IPv4、UP、不是虚拟接口。"""
        result = []
        for name, info in self.addrs.items():
            if not info.get("ipv4") or not info.get("up"):
                continue
            if iface_kind(name, self.ports) == "virtual":
                continue
            result.append(name)
        order = {"ethernet": 0, "wifi": 1, "other": 2}
        result.sort(key=lambda n: (order.get(iface_kind(n, self.ports), 3), n))
        configured = self.cfg.get("interface")
        if configured and configured in result:
            result.remove(configured)
            result.insert(0, configured)
        return result

    def portal_reachable_on(self, iface: str, timeout: float = 3.0) -> bool:
        """在指定网卡上做一次 TCP 连接测试(比发 HTTP 快)。"""
        host = urllib.parse.urlsplit(self.cfg["portal_url"]).hostname or "10.255.255.2"
        try:
            idx = socket.if_nametoindex(iface) if iface else None
        except OSError:
            idx = None
        sock = None
        try:
            sock = bound_socket(idx, None, timeout)
            sock.settimeout(timeout)
            sock.connect((host, 80))
            return True
        except OSError:
            return False
        finally:
            if sock is not None:
                sock.close()

    def select(self) -> bool:
        self.refresh()
        chosen = ""
        for name in self.candidates():
            if self.portal_reachable_on(name):
                chosen = name
                break
        if not chosen:
            cands = self.candidates()
            chosen = cands[0] if cands else ""

        self.iface = chosen
        info = self.addrs.get(chosen, {}) or {}
        self.source_ip = info.get("ipv4", "")
        self.mac = info.get("mac", "")
        try:
            self.iface_idx = socket.if_nametoindex(chosen) if chosen else None
        except OSError:
            self.iface_idx = None
        self.dhcp_dns = dhcp_dns_servers(chosen) if chosen else []
        self.resolver.configure(chosen or None, self.iface_idx, self.dhcp_dns)
        self.session.bind(self.iface_idx, self.source_ip or None)
        return bool(chosen)

    def describe(self) -> list:
        lines = []
        kind = iface_kind(self.iface, self.ports) if self.iface else "无"
        port = self.ports.get(self.iface, "-")
        lines.append(f"使用网卡: {self.iface or '(无)'}  [{port} / {kind}]  IP {self.source_ip or '-'}")
        if self.mac:
            lines.append(f"网卡 MAC: {self.mac}")
        dr = self.default_route
        lines.append(f"默认路由: {dr.get('interface') or '-'}  网关 {dr.get('gateway') or '-'}")
        pr = self.portal_route
        lines.append(f"门户 10.255.255.2 的路由: {pr.get('interface') or '-'}  网关 {pr.get('gateway') or '-'}")
        if self.vpns:
            tunnel_default = dr.get("interface", "") in self.vpns
            lines.append(
                "检测到隧道接口: " + ", ".join(self.vpns)
                + ("  <- 默认路由被 VPN 接管(全隧道)" if tunnel_default else "  (默认路由仍在物理网卡上)")
            )
        else:
            lines.append("检测到隧道接口: 无(VPN 未开启)")
        lines.append(f"DNS 服务器: {', '.join(self.resolver.servers) or '-'}")
        for c in self.candidates():
            info = self.addrs.get(c, {}) or {}
            reach = "可达" if self.portal_reachable_on(c, 2.0) else "不可达"
            lines.append(f"  · 候选网卡 {c} ({self.ports.get(c, '?')}) IP {info.get('ipv4')} 门户{reach}")
        return lines


# --------------------------------------------------------------------------- #
# 门户识别 / 在线判断
# --------------------------------------------------------------------------- #
def portal_flow(html: str, client_ip: str, cfg: dict) -> str:
    """判断这个网段该走哪套流程: drcom(原生表单) 还是 cas(统一认证)。"""
    # 【重要】顺序必须和学校门户页 a41.js 一致: 先按客户端 IP 判断!
    # 有线(10.x <= 10.51.255.255)即使拿到的是 Dr.COM 页面, 也必须走统一认证;
    # 因为两种网段返回的 HTML 是一样的(都含 DDDDD), 只看 HTML 会判断错。
    # 以"当前操作的那张网卡的 IP"为准(我们就是绑着它请求的);
    # 拿不到时才退回"门户页面自己写的客户端 IP"。
    ip = client_ip or client_ip_from_portal(html)
    if ip and in_ranges(ip, cfg.get("cas_ip_ranges") or []):
        return "cas"
    if "DDDDD" in html:
        return "drcom"
    lower = html.lower()
    if "wifilogin" in lower or "cas/login" in lower or "innerlogin" in lower:
        return "cas"
    return "drcom"


def portal_probe(sess: Session, cfg: dict):
    """访问门户, 返回 (是否可达, 证据, 页面 HTML)。"""
    resp = sess.request(cfg["portal_url"])
    if resp.status < 0:
        return False, f"门户打不开: {resp.error}", ""
    server = resp.headers.get("server", "")
    if "DrcomServer" in server or "Dr.COMWebLoginID" in resp.text \
            or "eportal" in resp.text.lower() or "DDDDD" in resp.text:
        hit = "DrcomServer 头" if "DrcomServer" in server else "门户页面特征"
        return True, f"校园网门户可加载(识别到 {hit})", resp.text
    return False, f"{cfg['portal_url']} 有响应(HTTP {resp.status})但不是校园网认证页", resp.text


def hijack_probe(sess: Session, cfg: dict):
    """
    最可靠的在线判据: 绑物理网卡 + 真实 DNS 去访问一个外网站点。
      拿到正常内容 -> 已经认证放行;
      被跳到 Dr.COM 登录页 -> 未认证。
    返回 (状态, 说明), 状态是 online / offline / unknown 三选一。
    """
    resp = sess.request(cfg["probe_url"], headers={"Cache-Control": "no-cache"})
    if resp.status < 0:
        return "unknown", f"外网探针不通: {resp.error}"
    body = resp.text[:6000]
    if "Dr.COMWebLoginID" in body or "DrcomServer" in resp.headers.get("server", ""):
        return "offline", "外网请求被校园网门户劫持 -> 未认证"
    if resp.status == 204:
        return "online", "外网可直连(HTTP 204)"
    if resp.status == 200 and body.strip():
        return "online", f"外网可直连(HTTP 200, {len(resp.text)} 字节)"
    return "unknown", f"外网探针返回 HTTP {resp.status}"


def status_endpoint_online(sess: Session, cfg: dict):
    resp = sess.request(cfg["status_url"], method="POST", data={}, ajax=True)
    if resp.status < 0:
        return False, f"统一认证状态接口不可达: {resp.error}"
    try:
        data = json.loads(resp.text.strip())
    except ValueError:
        return False, f"状态接口返回非 JSON: {resp.text[:80]!r}"
    if data.get("success"):
        return True, "统一认证状态接口: 已登录"
    return False, f"状态接口: 未登录 {str(data)[:120]}"


def evaluate(cfg: dict, net: NetEnv):
    """
    判断当前处境:
      online         已认证在线
      offline_campus 在校园网且未认证 -> 需要登录
      not_campus     不在校园网环境   -> 什么都不做

    顺序是按"省电省流量"排的: 先用一次 TCP 握手排除"不在校园网",
    再用最小的 204 探针判断是否已在线, 只有确实需要判断"要不要登录"时才抓门户页。
    """
    evidence = []

    if not net.iface:
        return STATE_NOT_CAMPUS, "没有找到可用的物理网卡(没插网线/没连 WiFi?)", evidence

    where = f"出口地址 {net.source_ip or '未知'} ({net.iface})"
    route_iface = net.portal_route.get("interface") or "-"

    # ① 最便宜的一步: 门户 TCP 通不通(几十毫秒)
    if not net.portal_reachable_on(net.iface, 3.0):
        evidence.append(f"门户 TCP 不可达 | {where} | 路由 {route_iface}")
        return STATE_NOT_CAMPUS, f"校园网门户 {cfg['portal_url']} 打不开 ({where})", evidence
    evidence.append(f"门户 TCP 可达 | {where} | 路由 {route_iface}")

    # ② 极小的直连探针(204 只有几十字节)
    probe_state, probe_detail = hijack_probe(net.session, cfg)
    evidence.append(f"直连探针: {probe_detail}")
    if probe_state == "online":
        return STATE_ONLINE, f"已在线 ({where}, {probe_detail})", evidence

    # ③ 到这一步才值得抓门户页, 判断"该走哪套流程 / 是不是真的没认证"
    reachable, detail, html = portal_probe(net.session, cfg)
    evidence.append(f"门户: {detail}")
    if not reachable:
        return STATE_NOT_CAMPUS, f"{detail} ({where})", evidence

    flow = portal_flow(html, net.source_ip, cfg)
    evidence.append(f"该网段的认证流程: {flow}")
    marker = re.search(r"Dr\.COMWebLoginID_(\d+)\.htm", html)
    if marker:
        evidence.append(f"门户模板编号: {marker.group(1)}")
        if marker.group(1) == "0":
            return STATE_OFFLINE_CAMPUS, f"在校园网且未认证 ({where})", evidence
    if probe_state == "offline":
        return STATE_OFFLINE_CAMPUS, f"在校园网且未认证 ({where})", evidence

    # 探针和门户模板都没给出结论时, 才去问统一认证的状态接口
    online_status, status_detail = status_endpoint_online(net.session, cfg)
    evidence.append(f"状态接口: {status_detail}")
    if online_status:
        return STATE_ONLINE, f"已在线 ({where}, {status_detail})", evidence
    return STATE_UNKNOWN, f"门户在, 但认证状态不确定, 本次不登录 ({where})", evidence


def note_state(state: str, detail: str, always: bool = False) -> bool:
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
# Mac 与 iPhone/iPad 专属优化
# --------------------------------------------------------------------------- #
def on_battery() -> bool:
    """MacBook 是否在用电池(用电池时把轮询放慢, 省电)。"""
    code, out = sh(["/usr/bin/pmset", "-g", "ps"])
    return "Battery Power" in out


def internet_sharing_state():
    """检查 macOS 的"互联网共享"(把校园网通过 Wi-Fi 分享给 iPhone/iPad)。"""
    _, procs = sh(["/bin/ps", "-ax", "-o", "command"])
    running = "InternetSharing" in procs
    _, ifc = sh(["/sbin/ifconfig", "bridge100"])
    ready = "inet " in ifc
    if running and ready:
        return True, "已开启(bridge100 就绪, 手机连上即可上网)"
    if running:
        return False, "进程在跑但网桥未就绪(可能在重启网络)"
    return False, "未开启"


def is_private_mac(mac: str) -> bool:
    """本地管理位 = 系统随机 MAC(专用 Wi-Fi 地址)。"""
    try:
        return bool(int(mac[:2], 16) & 0x02)
    except (ValueError, IndexError):
        return False


NET_CONFIG_PLIST = pathlib.Path("/Library/Preferences/SystemConfiguration/preferences.plist")


def net_config_mtime() -> float:
    """系统网络配置文件的修改时间 —— 插网线/切 WiFi/开关 VPN 时都会变。"""
    try:
        return NET_CONFIG_PLIST.stat().st_mtime
    except OSError:
        return 0.0


def wait_for_change(seconds: int) -> str:
    """
    分片睡眠, 期间盯着两个"该立刻醒来干活"的信号:
      合盖睡眠→唤醒(时间跳变)  -> "wake"
      插拔网线/切换网络        -> "network"
    都没发生就睡满, 返回 ""。
    """
    remaining = float(seconds)
    baseline = net_config_mtime()
    while remaining > 0:
        chunk = min(5.0, remaining)
        before = time.time()
        time.sleep(chunk)
        after = time.time()
        remaining -= chunk
        if after - before > chunk + 30:
            return "wake"
        current = net_config_mtime()
        if current and current != baseline:
            return "network"
    return ""


def interval_for(cfg: dict, state: str) -> int:
    """动态轮询间隔: 不在校园网/用电池时都放慢。"""
    interval = int(cfg.get("interval", 30))
    if state == STATE_NOT_CAMPUS:
        interval = max(interval, int(cfg.get("interval_offcampus", 300)))
    if on_battery():
        interval = max(interval, int(cfg.get("interval_battery", 120)))
    return interval


def _hhmm_to_minutes(value):
    try:
        hh, mm = str(value).split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def current_source_ip(cfg: dict) -> str:
    """
    取当前出口地址 —— 只查路由表和网卡, **不发任何包**。
    用于夜间静默这种"连探测请求都不该发"的场景。
    """
    host = urllib.parse.urlsplit(cfg.get("portal_url", "")).hostname or "10.255.255.2"
    iface = (route_get(host) or {}).get("interface") or ""
    if not iface:
        return ""
    return (iface_addresses().get(iface) or {}).get("ipv4", "")


def quiet_hours_exempt(cfg: dict, account: str) -> bool:
    """
    某些账号不受夜间静默限制。

    学校规则(实测确认):
      · 学生账号(学号, B 开头) → 只有周六日 24 小时可用;
                                   非周六日的 00:00-06:00 登不上
      · 教师账号(工号, M 开头) → 所有时段都可用

    **两张校园网(移动/电信)都同时有学生和教师账号**, 所以静默必须按
    【账号】判断, 而不是按网段 —— 同一个网段上两种账号都有。

    因此:
      quiet_hours 的 days 保持 [0,1,2,3,4](周一~周五) 就正好是学生账号的规则;
      教师账号(M 开头)在这里豁免, 不受静默限制。

    配置: "teacher_account_prefixes": ["M"], "quiet_hours_exempt_accounts": []
    """
    exempt = cfg.get("quiet_hours_exempt_accounts") or []
    account = (account or "").strip()
    if not account:
        return False
    prefixes = cfg.get("teacher_account_prefixes") or ["M"]
    if any(account.upper().startswith(str(p).strip().upper()) for p in prefixes if str(p).strip()):
        return True                      # 教师账号: 24 小时可用, 不静默
    return any(account == str(a).strip() for a in exempt)


def in_quiet_hours(cfg: dict, now=None, account: str = "") -> bool:
    """当前是否处在学校禁止认证的时段(默认周一~周五 00:00-06:00)。"""
    import datetime as _dt
    if quiet_hours_exempt(cfg, account):
        return False        # 该账号 24 小时可用, 不静默
    quiet = cfg.get("quiet_hours") or {}
    if not quiet.get("enabled"):
        return False
    now = now or _dt.datetime.now()
    days = quiet.get("days")
    if days is not None and now.weekday() not in days:
        return False
    start = _hhmm_to_minutes(quiet.get("start", "00:00"))
    end = _hhmm_to_minutes(quiet.get("end", "06:00"))
    if start is None or end is None:
        return False
    cur = now.hour * 60 + now.minute
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end          # 跨天


def note_mode(mode: str, message: str) -> bool:
    """模式(正常/夜间静默)变化时才写一行日志, 避免刷屏。"""
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


def failure_backoff(cfg: dict, failures: int) -> int:
    """失败后的重试间隔(秒), 按 failure_backoff 表依次取用(默认 2/5/15/30 分钟)。"""
    table = cfg.get("failure_backoff") or [120]
    idx = min(max(failures, 1), len(table)) - 1
    return int(table[idx])


def cmd_ios(cfg: dict, net: NetEnv) -> int:
    net.refresh()
    net.select()
    wired = [n for n in net.candidates() if iface_kind(n, net.ports) == "ethernet"]
    sharing, share_detail = internet_sharing_state()
    battery = on_battery()
    private = is_private_mac(net.mac)

    print("=" * 68)
    print("iPhone / iPad 接入方案")
    print("=" * 68)
    print(f"Mac 现状: {'电池供电' if battery else '接电源'}  |  "
          f"上网网卡 {net.iface or '无'} ({net.source_ip or '-'})")
    print(f"有线网卡: {', '.join(wired) if wired else '未检测到（没插网线）'}")
    print(f"互联网共享: {share_detail}")
    print(f"Wi-Fi MAC: {net.mac or '-'}{'  ← 系统随机地址(专用 Wi-Fi 地址)' if private else ''}")
    print()
    print("方案 A · Mac 做热点，手机平板全免认证（要一条网线）")
    print("  前提: Mac 用网线连校园网 + 接着电源 + 平时不合盖")
    print("  1) 系统设置 → 通用 → 共享 → 互联网共享")
    print("     来源: 选你插网线的那个接口(如 USB 10/100/1000 LAN)")
    print("     共享给: Wi-Fi → 点“Wi-Fi 选项”设网络名/通道/WPA2 密码")
    print("  2) 打开左侧“互联网共享”开关，提示重启网络时选“好”")
    print("  3) iPhone/iPad: 设置 → 无线局域网 → 连上那个热点")
    print("  效果: 手机/平板/其它设备全部免认证, 只占 1 个校园网名额")
    print("  注意: MacBook Air 合盖或睡眠会断, 手机也跟着断——只适合固定位置常插电")
    if not wired:
        print("  !! 现在没插网线, 且 macOS 不能“Wi-Fi 转发给 Wi-Fi”, 所以此方案暂时不可用")
    if sharing:
        print("  (检测到共享已开启, 手机直接连就行)")
    print()
    print("方案 B · 快捷指令: 手机上一键打开认证页（半自动）")
    print("  1) iPhone/iPad 打开“快捷指令”App → 新建")
    print("  2) 加动作: “打开 URL” → 填 http://10.255.255.2/")
    print("  3) 命名为“校园网登录”, 可加到主屏幕/轻点背面/专注模式自动化")
    print("  4) 打开后用 Safari 自动填充(iCloud 钥匙串)填账号密码, 点登录即可")
    print("  适合: Mac 不在身边、手机需要临时认证的时候")
    print()
    print("方案 C · 什么都不做, 靠系统自动弹门户")
    print("  1) 打开一次 http://10.255.255.2/, 让 Safari 记住账号密码(iCloud 钥匙串)")
    print("  2) 设置 → 无线局域网 → 校园网右侧 (i) → 关闭“自动登录”以外的干扰项;")
    print("     打开“自动加入”, 关闭“低数据模式”")
    print("  3) 设置 → 无线局域网 → 打开“询问是否加入网络”(便利访问门户)")
    print()
    print("─" * 68)
    print("【全局梯子】打开后门户弹不出来的原因与解法:")
    print("  原因: iOS 判断“要不要弹门户”靠探测 captive.apple.com;")
    print("        全局模式下这个探测被塞进隧道, 系统以为网络正常, 于是永不弹窗;")
    print("        同时 10.255.255.2 也可能被隧道抢走, 你自己也打不开门户。")
    print("  解法(按顺序):")
    print("    1) 梯子里把校园网设为直连: IP-CIDR,10.0.0.0/8,DIRECT")
    print("                              DOMAIN-SUFFIX,nua.edu.cn,DIRECT")
    print("       Shadowrocket: 设置 → 全局路由 → 用“配置”而不是“代理”, 并打开“绕过局域网”")
    print("    2) 顺序永远是: 先连校园 WiFi → 完成认证 → 再开梯子")
    print("       梯子的“按需连接/自动连接”别设成连上校园 WiFi 就开")
    print("    3) 干脆不依赖弹窗: 用下面的快捷指令直接发认证请求(全局梯子下也能用)")
    print("    4) 若梯子 App 出现在 设置→通用→VPN与设备管理→VPN 里,")
    print("       快捷指令可用“设定 VPN”动作自动关掉它再认证")
    print()
    print("两个针对校园网的 iOS/iPadOS 关键设置:")
    print("  1) 【专用 Wi-Fi 地址】→ 设为“关闭”或“固定”")
    print("     设置 → 无线局域网 → 校园网 (i) → 专用 Wi-Fi 地址")
    print("     原因: 校园网按 MAC 记认证会话; 轮换 MAC 会让旧会话失效, 就得反复手动认证")
    print("  2) 保持“自动加入”开启, 并让 iCloud 钥匙串同步门户密码(多设备自动填充)")
    if private:
        print()
        print("  (你这台 Mac 的 Wi-Fi 现在就是随机 MAC, 建议同样设为“固定”,")
        print("   否则旋转地址后需要重新认证一次)")
    print("=" * 68)
    return 0


# --------------------------------------------------------------------------- #
# 密码: macOS 钥匙串
# --------------------------------------------------------------------------- #
def keychain_get(service: str, account: str):
    code, out = sh(["/usr/bin/security", "find-generic-password",
                    "-s", service, "-a", account, "-w"], timeout=10)
    if code == 0 and out.strip():
        return out.rstrip("\n")
    return None


def keychain_set(service: str, account: str, password=None) -> bool:
    sh(["/usr/bin/security", "delete-generic-password", "-s", service, "-a", account])
    cmd = ["/usr/bin/security", "add-generic-password", "-U",
           "-s", service, "-a", account, "-T", "/usr/bin/security"]
    if password is None:
        cmd.append("-w")           # 让 security 自己在终端里隐藏输入, 不经过命令行参数
    else:
        cmd += ["-w", password]
    code, out = sh(cmd, timeout=60)
    if code != 0:
        log.error("写入钥匙串失败: %s", out.strip()[:200])
        return False
    return True


def account_for(cfg: dict, client_ip: str = "") -> str:
    """
    按当前网段挑账号 —— 不同运营商(Wi-Fi)可能是不同的账号密码。

    config.json 里可以这么写:
        "account": "B240000000",                    # 默认账号(兜底)
        "accounts": {"10.53": "B240000000",         # 移动网段
                     "10.54": "D12345678"}          # 电信网段
    键是客户端 IP 的前两段(10.53.96.247 -> "10.53")。
    """
    accounts = cfg.get("accounts") or {}
    key = _carrier_key(client_ip)
    entry = accounts.get(key)
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    if isinstance(entry, dict) and str(entry.get("account", "")).strip():
        return str(entry["account"]).strip()
    return (cfg.get("account") or "").strip()


def remember_account(account: str, client_ip: str = "") -> None:
    """把"这个网段该用哪个账号"记进 config.json。"""
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    accounts = cfg.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
    key = _carrier_key(client_ip)
    if key:
        accounts[key] = account
        cfg["accounts"] = accounts
        cfg["_accounts说明"] = ("按网段记的账号: 键是客户端 IP 前两段。"
                                "不同运营商(Wi-Fi)用不同账号时在这里配。")
    cfg["account"] = account
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    except OSError as exc:
        log.warning("写入 config.json 失败: %s", exc)


def credential_candidates(cfg: dict, client_ip: str = "") -> list:
    """
    返回 [(说明, 账号, 密码), ...] —— 按"最可能成功"排序。
    主账号(按网段匹配) 排前面, 其它在 config 里出现过的账号排后面。
    这样换到另一张校园网(账号不同)时, 不用先手动配置也能自动试对。
    """
    service = cfg.get("keychain_service", "campus-net-login")
    cas_service = cfg.get("keychain_cas_service", "campus-net-cas")

    def get_pw(acc: str):
        return keychain_get(service, acc) or keychain_get(cas_service, acc)

    out = []
    seen = set()

    def add(acc: str, label: str) -> None:
        acc = (acc or "").strip()
        if not acc or acc in seen:
            return
        seen.add(acc)
        pw = get_pw(acc)
        if pw:
            out.append((f"{label} {acc}", acc, pw))

    add(account_for(cfg, client_ip), "主账号")
    accounts = cfg.get("accounts") or {}
    for value in accounts.values():
        if isinstance(value, str):
            add(value, "备用账号")
        elif isinstance(value, dict):
            add(str(value.get("account", "")), "备用账号")
    add(cfg.get("account", ""), "备用账号")
    return out



def load_credentials(cfg: dict, kind: str = "drcom", client_ip: str = ""):
    """
    kind="drcom" -> 上网密码(无线 Dr.COM 门户用)
    kind="cas"   -> 统一身份认证密码(有线走 CAS 用, 可能和上网密码不同)
    client_ip    -> 当前网段, 用来挑对应的账号(多运营商场景)
    找不到时回退到另一个, 再不行用 secret.json。
    """
    account = account_for(cfg, client_ip)
    service = cfg.get("keychain_service", "campus-net-login")
    cas_service = cfg.get("keychain_cas_service", "campus-net-cas")
    password = None
    if account:
        password = keychain_get(service if kind == "drcom" else cas_service, account)
        if not password:
            # 另一套没设过就用这套(很多人两个密码确实一样)
            password = keychain_get(cas_service if kind == "drcom" else service, account)
    if not password and SECRET_FILE.exists():
        try:
            obj = json.loads(SECRET_FILE.read_text(encoding="utf-8"))
            account = account or obj.get("account", "")
            password = obj.get("password")
            if password:
                log.warning("正在使用 %s 的明文密码, 建议改用 --set-password 存进钥匙串",
                            SECRET_FILE.name)
        except (OSError, ValueError):
            pass
    if not account or not password:
        hint = ("--set-password" if kind == "drcom" else "--set-cas-password")
        raise SystemExit(
            f"还没有保存{'上网' if kind == 'drcom' else '统一身份认证'}密码。请先执行:\n"
            f"    python3 campus_mac.py {hint}"
        )
    return account, password


def cmd_set_cas_password(cfg: dict) -> None:
    """单独设置/更新统一身份认证密码(有线 CAS 用)。"""
    account = (cfg.get("account") or "").strip() or input("校园网账号(学号): ").strip()
    if not account:
        raise SystemExit("账号不能为空")
    service = cfg.get("keychain_cas_service", "campus-net-cas")
    if sys.stdin.isatty():
        print("接下来由 macOS 钥匙串直接读取【统一身份认证】密码:")
        ok = keychain_set(service, account)
    else:
        ok = keychain_set(service, account, getpass.getpass("统一身份认证密码(输入时不显示): "))
    if ok:
        save_account_to_config(account)
        print(f"已保存: 统一身份认证密码 → 钥匙串条目 “{service}” / {account}")
    else:
        print("写入钥匙串失败, 请重试")


def save_account_to_config(account: str) -> None:
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    cfg["account"] = account
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_set_password(cfg: dict, net: "NetEnv | None" = None) -> None:
    """
    保存账号密码。
    会记住"当前这个网段用哪个账号" —— 不同运营商(移动/电信/联通)的
    Wi-Fi 账号密码不同时, 各连一次、各存一次即可。
    """
    client_ip = ""
    if net is not None:
        try:
            net.select()
            client_ip = net.source_ip or ""
        except Exception:                                     # noqa: BLE001
            client_ip = ""
    current = account_for(cfg, client_ip)

    tip = "校园网账号(学号)"
    if client_ip:
        tip += f"【当前网段 {_carrier_key(client_ip)}.x】"
    if current:
        tip += f"，直接回车沿用 {current}"
    account = input(f"{tip}: ").strip() or current
    if not account:
        raise SystemExit("账号不能为空")
    service = cfg.get("keychain_service", "campus-net-login")
    if sys.stdin.isatty():
        print("接下来由 macOS 钥匙串直接读取密码(不显示、不进命令行历史):")
        ok = keychain_set(service, account)
    else:
        password = getpass.getpass("密码(输入时不显示): ")
        ok = keychain_set(service, account, password)
    if ok:
        remember_account(account, client_ip)
        clear_retry()          # 存了新账号就把失败退避清掉, 让看门狗立刻重新试
        where = f"（已记住 {_carrier_key(client_ip)}.x 网段用这个账号）" if client_ip else ""
        print(f"已保存: 账号 {account} {where}, 密码写入钥匙串条目 “{service}”")
    else:
        password = getpass.getpass("钥匙串写入失败, 改为保存明文到 secret.json: ")
        SECRET_FILE.write_text(json.dumps({"account": account, "password": password}),
                               encoding="utf-8")
        try:
            SECRET_FILE.chmod(0o600)
        except OSError:
            pass
        remember_account(account, client_ip)
        print(f"已保存到 {SECRET_FILE}(权限 600)")


# --------------------------------------------------------------------------- #
# 密码加密: 复刻学校 security.js 的 RSA(教科书式、零填充、小端序)
# --------------------------------------------------------------------------- #
MODULUS_HEX = (
    "008aed7e057fe8f14c73550b0e6467b023616ddc8fa91846d2613cdb7f7621e3"
    "cada4cd5d812d627af6b87727ade4e26d26208b7326815941492b2204c3167ab"
    "2d53df1e3a2c9153bdb7c8c2e968df97a5e7e01cc410f92c4c2c2fba529b3e"
    "e988ebc1fca99ff5119e036d732c368acf8beba01aa2fdafa45b21e4de4928d"
    "0d403"
)
PUBLIC_EXPONENT = 65537


def rsa_encrypt(password: str) -> str:
    modulus = int(MODULUS_HEX, 16)
    chunk = 2 * ((modulus.bit_length() - 1) // 16)

    data = bytearray()
    for ch in password:
        code = ord(ch)
        if code > 0xFF:
            raise ValueError("密码含非 ASCII 字符, 学校页面按单字节处理会出错")
        data.append(code)
    while len(data) % chunk:
        data.append(0)

    blocks = []
    for start in range(0, len(data), chunk):
        value = int.from_bytes(data[start:start + chunk], "little")
        cipher = pow(value, PUBLIC_EXPONENT, modulus)
        hexed = format(cipher, "x")
        if len(hexed) % 4:
            hexed = hexed.rjust(len(hexed) + (4 - len(hexed) % 4), "0")
        blocks.append("".join(format(int(hexed[i:i + 4], 16), "04x")
                              for i in range(0, len(hexed), 4)))
    return " ".join(blocks)


# --------------------------------------------------------------------------- #
# 表单解析(CAS 用)
# --------------------------------------------------------------------------- #
def _parse_attrs(text: str) -> dict:
    attrs = {}
    for m in re.finditer(r"""([A-Za-z_:][-\w:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", text):
        attrs[m.group(1).lower()] = m.group(2) or m.group(3) or m.group(4) or ""
    return attrs


def parse_forms(html: str) -> list:
    forms = []
    for m in re.finditer(r"(?is)<form\b([^>]*)>(.*?)</form>", html):
        attrs = _parse_attrs(m.group(1))
        body = m.group(2)
        inputs = {}
        for im in re.finditer(r"(?is)<input\b([^>]*?)/?>", body):
            a = _parse_attrs(im.group(1))
            name = a.get("name")
            if not name:
                continue
            if a.get("type", "text").lower() in ("submit", "button", "image"):
                continue
            inputs[name] = a.get("value", "")
        forms.append({"id": attrs.get("id", ""), "action": attrs.get("action", ""),
                      "inputs": inputs, "body": body})
    return forms


def needs_captcha(html: str) -> bool:
    if "请完成安全验证" not in html:
        return False
    return 'class="slidingverification none"' not in html


def captcha_endpoints(login_url: str, page: str) -> list:
    """按验证页里的 contextPath 拼上报地址(v1.0 少了 /cas 前缀, 会 404)。"""
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


def client_ip_from_portal(html: str):
    """门户页面里会写明它看到的客户端地址(v46ip/ss5/v4serip)。"""
    for pattern in (r"v46ip\s*=\s*'([0-9.]+)'", r'ss5\s*=\s*"([0-9.]+)"',
                    r"v4serip\s*=\s*'([0-9.]+)'"):
        m = re.search(pattern, html)
        if m and m.group(1).count(".") == 3:
            return m.group(1)
    return None


def _dump(tag: str, text: str):
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.html"
        path.write_text(text, encoding="utf-8")
        log.info("页面已存档: %s", path.name)
        return path
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 登录: 统一认证(CAS + 滑块) —— 有线网段
# --------------------------------------------------------------------------- #
def login_cas(net: NetEnv, cfg: dict, account: str, password: str,
              dry_run: bool = False) -> bool:
    sess = net.session
    service_q = urllib.parse.quote(cfg["service"], safe="")
    login_url = f"{cfg['cas_login_url']}?service={service_q}"
    log.info("打开统一认证页 %s", login_url)

    resp = sess.request(login_url)
    if resp.status != 200:
        log.error("认证页打不开: HTTP %s %s", resp.status, resp.error)
        return False

    forms = parse_forms(resp.text)
    form = next((f for f in forms if "password" in f["inputs"] or "username" in f["inputs"]), None)
    if form is None:
        log.error("认证页里没有登录表单")
        _dump("cas-no-form", resp.text)
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
    log.info("准备提交账号密码(字段: %s)", ", ".join(sorted(data)))

    if dry_run:
        log.info("[演练] 到此为止, 不发送密码。表单地址: %s", action)
        return False

    resp = sess.request(action, method="POST", data=data)
    if resp.status < 0:
        log.error("提交账号密码失败: %s", resp.error)
        return False
    log.info("已提交账号密码, HTTP %s", resp.status)

    if needs_captcha(resp.text):
        _dump("cas-captcha", resp.text)
        log.info("服务器要求安全验证(滑块), 上报验证结果")
        endpoints = captcha_endpoints(login_url, resp.text) + [cfg["captcha_url"]]
        for endpoint in endpoints:
            r = sess.request(endpoint, method="POST", ajax=True,
                             data={"request_username": account, "captchResult": "1"})
            log.info("上报验证结果 → %s (HTTP %s)", endpoint, r.status)
            if r.status == 200:
                break
        time.sleep(1)

        forms = parse_forms(resp.text)
        fm4 = next((f for f in forms if f["id"] == "fm4"), None)
        fm3 = next((f for f in forms if f["id"] == "fm3"), None)
        strategies = []
        if fm4 is not None:
            strategies.append(("fm4(空表单)",
                               urllib.parse.urljoin(login_url, fm4["action"] or action),
                               dict(fm4["inputs"])))
        if fm3 is not None:
            payload = dict(fm3["inputs"])
            payload["_eventId"] = "checkCaptchaSubmit"
            strategies.append(("fm3(checkCaptchaSubmit)",
                               urllib.parse.urljoin(login_url, fm3["action"] or action),
                               payload))
        strategies.append(("直接重提登录地址", action, {}))
        for name, target, payload in strategies:
            log.info("验证后提交方式: %s", name)
            r = sess.request(target, method="POST", data=payload)
            log.info("  -> HTTP %s, %s 字节", r.status, len(r.text))
            # 每种提交方式只等 8 秒(以前是 20 秒), 真正兜底的是最后那次长等待
            if _wait_online(net, cfg, 8):
                return True
    else:
        log.info("服务器未要求安全验证, 直接检查结果")

    if _wait_online(net, cfg, int(cfg.get("login_timeout", 90))):
        return True
    log.error("统一认证登录未成功")
    return False


# --------------------------------------------------------------------------- #
# 登录: Dr.COM 原生表单 —— 无线网段(10.53.x / 10.54.x)
# --------------------------------------------------------------------------- #

# 学校的「服务类型」(运营商): 三家走同一个门户, 靠账号后缀区分。
# 这份清单来自门户页里写死的 carrier 配置:
#   {"id":"1","name":"校园用户","suffix":""},
#   {"id":"2","name":"校园电信","suffix":"@dx"},
#   {"id":"3","name":"校园联通","suffix":"@lt"}
# 移动的同学用「校园用户」(不带后缀); @njxy 是备用写法。
CARRIERS = [
    ("校园用户(默认)", ""),
    ("校园电信 @dx", "@dx"),
    ("校园联通 @lt", "@lt"),
    ("校园网后缀 @njxy", "@njxy"),
]


def _carrier_key(client_ip: str) -> str:
    """按客户端 IP 的前两段做键，例如 10.53.96.247 -> '10.53'。"""
    parts = (client_ip or "").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else ""


def _carrier_store() -> dict:
    try:
        data = json.loads(CARRIER_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_carrier(client_ip: str = ""):
    """
    该网段上次成功的服务类型后缀。
    返回 None = 没记录过；返回 "" = 记录过"不带后缀"。
    """
    data = _carrier_store()
    key = _carrier_key(client_ip)
    if key and key in data:
        return data[key]
    if "" in data:
        return data[""]
    return None


def save_carrier(suffix: str, client_ip: str = "") -> None:
    data = _carrier_store()
    data[_carrier_key(client_ip)] = suffix
    data[""] = suffix          # 同时记一份全局的，换网段时当兜底
    try:
        CARRIER_FILE.parent.mkdir(exist_ok=True)
        CARRIER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    except OSError:
        pass


def parse_portal_vars(html: str) -> dict:
    """抓门户页里 sv=0;... 那段变量, 里面写着认证接口与端口。"""
    out = {}
    for key in ("v4serip", "v46ip", "vid", "mip", "vlanid", "aolno", "timet",
                "portalid", "authloginIP", "authloginport", "authloginpath",
                "authloginparam", "authuserfield", "authpassfield", "authtype"):
        m = (re.search(rf"{key}\s*=\s*'([^']*)'", html)
             or re.search(rf"{key}\s*=\s*(\d+)", html))
        if m:
            out[key] = m.group(1).strip()
    return out


def login_drcom(net: NetEnv, cfg: dict, creds: list, dry_run: bool = False) -> bool:
    """
    校园无线网段的登录: 门户自己的表单, 字段是 DDDDD / upass。

    creds: [(说明, 账号, 密码), ...] —— 会按顺序试(主账号在前)。
    每个账号会配各种"服务类型"后缀一起试:

    2026-09-15 依据 v1.1 实测结论修正了三个关键点:
      1. 登录路径必须带 ver=1.0, 否则 AC 返回的是后台管理页面(不是登录接口);
      2. 必须带 AJAX 头(X-Requested-With), 否则同样会拿到后台页面;
      3. 新版 /eportal/portal/login 在本校会返回"无法获取用户认证账号",
         只能作为后备; 主力是 /eportal/?c=ACSetting&a=Login&ver=1.0。
    """
    sess = net.session
    reached, detail, html = portal_probe(sess, cfg)
    if not reached:
        log.error("门户不可达: %s", detail)
        return False

    pv = parse_portal_vars(html)
    host = urllib.parse.urlsplit(cfg["portal_url"]).hostname or "10.255.255.2"
    port = int(pv.get("authloginport") or cfg.get("portal_api_port", 801))
    user_field = pv.get("authuserfield") or "DDDDD"
    pass_field = pv.get("authpassfield") or "upass"
    login_path = pv.get("authloginpath") or "/eportal/?c=ACSetting&a=Login"
    if "ver=" not in login_path:
        login_path += ("&" if "?" in login_path else "?") + "ver=1.0"
    js_version = pv.get("jsVersion", "")
    url = f"http://{host}:{port}{login_path}"
    log.info("Dr.COM 登录接口: %s (账号字段 %s / 密码字段 %s)", url, user_field, pass_field)

    # 服务类型顺序: 配置指定 > 上次成功的 > 依次试
    pinned = cfg.get("wifi_suffix", "")
    cached = pinned if pinned else load_carrier(net.source_ip)
    if pinned:
        suffixes = [(f"配置指定 {pinned}", pinned)]
    else:
        suffixes = []
        if cached is not None:
            suffixes.append(("上次成功(默认)" if cached == "" else f"上次成功({cached})", cached))
        for label, suffix in CARRIERS:
            if suffix != cached:
                suffixes.append((label, suffix))

    # 组合顺序: 先"上次成功的服务类型"配各个账号, 再换服务类型 —— 常见情况 1~2 次就能中
    attempts = []
    for suf_label, suffix in suffixes:
        for cred_label, account, password in creds:
            attempts.append((f"{cred_label} + {suf_label}", account, password, suffix))
    attempts = attempts[:8]          # 兜底上限, 别打太多请求

    modern_url = (f"http://{host}:{port}/eportal/portal/login?callback=dr1003"
                  "&login_method=1&user_account=%2C0%2C"
                  + urllib.parse.quote(creds[0][1] if creds else "")
                  + "&user_password=" + urllib.parse.quote(creds[0][2] if creds else "")
                  + "&jsVersion=4.1.3&terminal_type=1&lang=zh-cn")

    if dry_run:
        for label, _acc, _pw, suffix in attempts:
            body = {user_field: f"<账号>{suffix}", pass_field: "***", "0MKKey": "123456",
                    "R1": "", "R2": "", "R3": "", "R6": "0", "para": "", "v6ip": "",
                    "terminal_type": "1", "lang": "zh-cn", "url": "drappall"}
            log.info("[演练] POST %s  %s  body=%s", url, label, body)
        log.info("[演练] 到此为止, 不发送密码")
        return False

    errors: list[str] = []
    for label, account, password, form_suffix in attempts:
        data = {
            user_field: f"{account}{form_suffix}",
            pass_field: password,
            "0MKKey": "123456",
            "R1": "", "R2": "", "R3": "", "R6": "0", "para": "", "v6ip": "",
            "terminal_type": "1", "lang": "zh-cn",
            "url": "drappall",
        }
        if js_version:
            data["jsVersion"] = js_version

        log.info("Dr.COM 登录: %s", label)
        resp = sess.request(url, method="POST", data=data, ajax=True)
        text = resp.text.strip().replace("\n", " ")[:200]
        log.info("  → HTTP %s: %s", resp.status, text)
        _dump("drcom-ACSetting", resp.text)

        # 这些是"换账号/换服务类型也没用"的硬错误 —— 立刻停手, 不要反复撞
        # 注意: 「账号错误 / Authentication fail / 请先绑定运营商」都**不算**硬错误,
        #      因为它们正是"账号或服务类型选错了"的表现, 需要换下一个再试。
        fatal = ("密码", "验证码", "在线数超出限制", "Limit Users")
        hit = next((w for w in fatal if w in resp.text), "")
        if hit:
            if hit == "验证码":
                log.error("门户要求图形验证码, 纯 HTTP 模式无法识别(需用浏览器模式)")
            elif "在线数超出限制" in resp.text or "Limit Users" in resp.text:
                log.error("账号在线设备数超限(该账号同时在别的设备/会话上在线)。"
                          "这不是密码问题 —— 已停止重试。"
                          "处理: 等旧会话超时, 或先在别的设备上退出登录。")
            else:
                log.error("服务器明确拒绝(%s), 停止重试以免反复撞: %s", hit, text)
            return False

        if _wait_online(net, cfg, 8):
            log.info("Dr.COM 登录成功(%s)", label)
            if cached is None or cached != form_suffix:
                save_carrier(form_suffix, net.source_ip)
                log.info("已记住: 本网段用 %s + 服务类型 %s",
                         account, "不带后缀" if form_suffix == "" else form_suffix)
            if cached is not None and cached != form_suffix:
                # 记一下这个网段该用哪个账号
                remember_account(account, net.source_ip)
            return True
        errors.append(text)
        log.info("  %s 未成功, 换下一个组合", label)

    log.error("Dr.COM 门户登录失败(试过 %s 种组合)", len(attempts))
    if errors and all(("Authentication fail" in e or "账号错误" in e) for e in errors):
        log.error("所有组合都被拒。常见原因:")
        log.error("  1) 账号密码不对 —— 用 --set-password 重新存")
        log.error("  2) 这张校园网的账号还没存过 —— 连着这张网运行一次:")
        log.error("       python3 campus_mac.py --set-password")
        log.error("  3) 这家运营商还没绑定账号(提示里会写「请先绑定运营商账号」)")
    return False


def _wait_online(net: NetEnv, cfg: dict, timeout: float) -> bool:
    """
    登录后等网络恢复。
    关键: 未认证时 AC 会把探针请求丢掉(不是立刻拒绝), 所以这里把超时压到 3 秒、
    每 2 秒探一次 —— 否则光是等超时就会白白拖掉一两分钟。
    """
    deadline = time.time() + max(0.0, float(timeout))
    original = net.session.timeout
    net.session.timeout = 3.0
    try:
        while time.time() < deadline:
            state, _ = hijack_probe(net.session, cfg)
            if state == "online":
                log.info("登录成功, 网络已恢复")
                return True
            time.sleep(2)
        return False
    finally:
        net.session.timeout = original


def do_login(cfg: dict, net: NetEnv, dry_run: bool = False, force: bool = False) -> bool:
    state, detail, _ = evaluate(cfg, net)
    if force and dry_run:
        log.info("[演练] 跳过状态闸门(仅用于检查流程, 不会提交)")
        state = STATE_OFFLINE_CAMPUS
    if state == STATE_ONLINE:
        log.info("已在线, 无需登录 (%s)", detail)
        return True
    if state == STATE_NOT_CAMPUS:
        log.warning("不是校园网未认证状态(%s), 放弃登录尝试", detail)
        return False

    if not acquire_lock():
        log.info("另一次登录正在进行中, 本次跳过")
        return False
    try:
        # 先定流程, 再取对应的密码: 有线统一认证和无线上网密码可能是两套
        flow = "cas"
        _, _, html = portal_probe(net.session, cfg)
        if html:
            flow = portal_flow(html, net.source_ip, cfg)
        # 有线网段默认也走 Dr.COM 表单: 实测能绕过"人脸识别"安全验证
        if flow == "cas" and str(cfg.get("wired_flow", "drcom")).lower() == "drcom":
            log.info("有线网段: 按配置改用 Dr.COM 表单登录(绕过统一认证的人脸识别)")
            flow = "drcom"
        if flow == "cas":
            try:
                account, password = load_credentials(cfg, "cas", net.source_ip)
            except SystemExit:
                if not dry_run:
                    raise
                account, password = "TEST-ACCOUNT", "TEST-PASSWORD"
                log.info("[演练] 尚未保存账号密码, 用占位符走一遍流程")
            log.info("开始登录流程: cas (网卡 %s, 源地址 %s)", net.iface, net.source_ip)
            return login_cas(net, cfg, account, password, dry_run)

        # Dr.COM: 把所有存过密码的账号都带上 —— 换到另一张校园网(账号不同)时也能自动试对
        creds = credential_candidates(cfg, net.source_ip)
        if not creds:
            if not dry_run:
                raise SystemExit(
                    "还没有保存账号密码。请先执行:\n"
                    "    python3 campus_mac.py --set-password"
                )
            creds = [("演练账号", "TEST-ACCOUNT", "TEST-PASSWORD")]
            log.info("[演练] 尚未保存账号密码, 用占位符走一遍流程")
        log.info("开始登录流程: drcom (网卡 %s, 源地址 %s, 候选账号 %d 个)",
                 net.iface, net.source_ip, len(creds))
        return login_drcom(net, cfg, creds, dry_run)
    finally:
        release_lock()


def acquire_lock() -> bool:
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass



# --------------------------------------------------------------------------- #
# 版本 / 通知 / 更新检查
# --------------------------------------------------------------------------- #
VERSION = "1.1"
DEFAULT_UPDATE_API = ("https://api.github.com/repos/zacharyli001/"
                      "NUA-Campus-Network-Auto-Login/releases?per_page=1")
UPDATE_CHECK_FILE = STATE_DIR / "update_check.json"


def notify(title: str, message: str, cfg: dict | None = None) -> None:
    """
    弹一条 macOS 通知。失败就静默忽略 —— 通知绝不能影响主流程。
    (用系统自带的 osascript, 不引入任何依赖)
    """
    if cfg is not None and cfg.get("notify") is False:
        return
    try:
        safe_title = title.replace('"', "'").replace("\\", "")[:80]
        safe_body = message.replace('"', "'").replace("\\", "")[:200]
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification "{safe_body}" with title "{safe_title}"'],
            capture_output=True, timeout=8)
    except Exception:                                         # noqa: BLE001
        pass


def check_update(cfg: dict, force: bool = False) -> str:
    """
    查 GitHub Release 有没有新版本。返回一句说明(没有新版本则返回 "")。
    最多 12 小时查一次; 失败静默(不联网/被墙都不影响主流程)。
    """
    now = time.time()
    last = 0.0
    try:
        last = float(json.loads(UPDATE_CHECK_FILE.read_text(encoding="utf-8"))
                     .get("ts", 0))
    except (OSError, ValueError):
        last = 0.0
    if not force and now - last < 12 * 3600:
        return ""
    try:
        STATE_DIR.mkdir(exist_ok=True)
        UPDATE_CHECK_FILE.write_text(json.dumps({"ts": now}), encoding="utf-8")
    except OSError:
        pass

    url = cfg.get("update_api") or DEFAULT_UPDATE_API
    code, out = sh(["/usr/bin/curl", "-sL", "-m", "10",
                    "-H", "Accept: application/vnd.github+json", url], timeout=15)
    if code != 0 or not out.strip():
        return ""
    try:
        data = json.loads(out)
    except ValueError:
        return ""
    # /releases 返回的是列表; /releases/latest 返回单个对象。两种都兼容。
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return ""
    tag = str(data.get("tag_name") or "").strip()
    mine = str(cfg.get("version") or VERSION).strip()
    if not tag or tag == mine:
        return ""
    return f"有新版本 {tag}（当前 {mine}）: {data.get('html_url') or url}"


def cmd_status(cfg: dict, net: NetEnv) -> int:
    """给普通人看的简明状态(双击「查看状态」用的就是它)。"""
    net.select()
    state, detail, _ = evaluate(cfg, net)
    icon = {STATE_ONLINE: "✅", STATE_OFFLINE_CAMPUS: "⚠️",
            STATE_NOT_CAMPUS: "➖"}.get(state, "❓")
    account = account_for(cfg, net.source_ip) or "（未配置）"

    running = False
    code, out = sh(["/usr/bin/pgrep", "-f", "campus_mac.py --watch"])
    running = code == 0 and out.strip() != ""

    last_login = ""
    try:
        for line in reversed(LOG_DIR.joinpath("campus_mac.log")
                             .read_text(encoding="utf-8", errors="ignore").splitlines()):
            if "登录成功" in line:
                last_login = line.strip()
                break
    except OSError:
        pass

    print("=" * 62)
    print(f"  校园网自动登录 · 当前状态      （版本 {cfg.get('version') or VERSION}）")
    print("=" * 62)
    print(f"  {icon} {STATE_TEXT.get(state, state)}")
    print(f"      出口地址：{net.source_ip or '-'}   （{net.iface or '-'}）")
    print(f"      使用账号：{account}")
    print(f"      后台服务：{'运行中 ✅' if running else '未运行 ⚠️  （双击 AAA一键安装.command 重新安装）'}")
    if last_login:
        print(f"      最近登录：{last_login.split(' INFO ')[-1][:60]}")
    print("-" * 62)
    update = check_update(cfg)
    if update:
        print(f"  🔔 {update}")
        print("-" * 62)
    print(f"  详细日志：{LOG_DIR / 'campus_mac.log'}")
    print("=" * 62)
    return 0


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_diagnose(cfg: dict, net: NetEnv) -> int:
    print("=" * 68)
    print("校园网自动登录 —— 环境体检 (macOS)")
    print("=" * 68)
    net.refresh()
    if not net.select():
        print("!! 没有找到可用的物理网卡(没插网线 / 没连 WiFi?)")
        return 1
    for line in net.describe():
        print(line)

    print("-" * 68)
    print("DNS 解析(绕过 VPN fake-ip 的关键):")
    for host in ("c.nua.edu.cn", "www.baidu.com"):
        ip = net.resolver.resolve(host)
        print(f"  {host} -> {ip or '解析失败'}")
    for line in net.resolver.stats[-6:]:
        print(f"    · {line}")

    print("-" * 68)
    state, detail, evidence = evaluate(cfg, net)
    print("认证状态判定:")
    for line in evidence:
        print(f"  · {line}")
    print(f"  => {STATE_TEXT.get(state, state)}: {detail}")

    print("-" * 68)
    if net.vpns:
        drift = net.portal_route.get("interface") in net.vpns
        bound_ok = net.portal_reachable_on(net.iface, 3.0) if net.iface else False
        print("VPN 提示:")
        if drift and not bound_ok:
            print("  !! 到门户的路由被 VPN 隧道抢走了, 会导致脚本以为'不在校园网'。")
            print("     解决: 在 VPN 客户端里把 10.0.0.0/8 和 *.nua.edu.cn 设为直连(DIRECT)。")
        elif drift:
            print("  · 到门户的路由虽然指向隧道, 但本工具绑定物理网卡后仍然可达, 不受影响。")
            print("    (说明 VPN 客户端把 10.0.0.0/8 当成直连处理了)")
        else:
            print("  · VPN 已开, 但 10.255.255.2 仍走物理网卡, 不影响本工具。")
        print("  · 本工具所有请求都绑定物理网卡, 不随 VPN 改道;")
        print("    但如果 DNS 被 fake-ip 劫持, 需要 VPN 客户端放行 *.nua.edu.cn。")
    else:
        print("VPN 提示: 未检测到隧道接口, 当前是直连环境。")
    print("=" * 68)
    if state == STATE_OFFLINE_CAMPUS:
        return 1
    return 0


def cmd_check(cfg: dict, net: NetEnv) -> int:
    net.select()
    state, detail, _ = evaluate(cfg, net)
    note_state(state, detail, always=True)
    if state == STATE_OFFLINE_CAMPUS:
        log.info("判定: 需要登录")
        return 1
    return 0


def cmd_verify_password(cfg: dict, net: NetEnv) -> int:
    """
    安全地验证账号密码对不对 —— 只走统一身份认证的"提交账号密码"这一步,
    不点滑块、不提交、不碰你当前的网络会话。
    服务器返回"请完成安全验证"= 账号密码正确;
    返回"用户名或密码错误"= 密码不对。
    """
    net.select()
    account, password = load_credentials(cfg, "cas", net.source_ip)
    sess = net.session

    service_q = urllib.parse.quote(cfg["service"], safe="")
    login_url = f"{cfg['cas_login_url']}?service={service_q}"
    log.info("① 打开统一认证页(仅用于验证密码, 不影响网络): %s", login_url)
    resp = sess.request(login_url)
    if resp.status != 200:
        print(f"✗ 认证页打不开: HTTP {resp.status} {resp.error}")
        return 2

    forms = parse_forms(resp.text)
    form = next((f for f in forms if "password" in f["inputs"] or "username" in f["inputs"]), None)
    if form is None:
        print("✗ 没找到登录表单, 页面已存档到 logs/")
        _dump("verify-no-form", resp.text)
        return 2

    action = urllib.parse.urljoin(login_url, form["action"] or login_url)
    data = dict(form["inputs"])
    data.update({"username": account, "password": rsa_encrypt(password),
                 "encrypted": "true", "_eventId": "submit"})
    data.setdefault("loginType", "1")
    log.info("② 提交账号 %s 验证【统一身份认证密码】(RSA 加密, 只提交这一次)", account)
    resp = sess.request(action, method="POST", data=data)
    page = resp.text
    _dump("verify-password", page)

    wrong_words = ("密码错误", "用户名或密码", "账号或密码", "用户不存在", "认证失败",
                   "密码不正确", "账号不存在")
    ok_words = ("请完成安全验证", "slidingverification", "captchValid", "checkCaptchImg")

    if any(w in page for w in wrong_words):
        print("=" * 60)
        print("✗ 统一身份认证的账号或密码不对 —— 服务器明确返回了错误")
        for w in wrong_words:
            if w in page:
                idx = page.find(w)
                print("   服务器原话:", page[max(0, idx - 40):idx + 40].replace("\n", " ").strip())
        print("   修复: python3 campus_mac.py --set-cas-password 重新输入统一身份认证密码")
        print("=" * 60)
        return 1
    if any(w in page for w in ok_words):
        print("=" * 60)
        print("✓ 统一身份认证密码正确! 服务器已经过了密码校验, 进入下一步(滑块)")
        print(f"  账号: {account}")
        print("  (本次没有提交滑块、也没有改动你的网络会话)")
        print("=" * 60)
        return 0

    print("=" * 60)
    print("? 没看出明确结论, 原样返回如下(已存档到 logs/ 里):")
    print(page[:600].replace("\n", " "))
    print("=" * 60)
    return 3


def cmd_login(cfg: dict, net: NetEnv, dry_run: bool, force: bool = False) -> int:
    net.select()
    state, detail, _ = evaluate(cfg, net)
    note_state(state, detail)
    if state == STATE_OFFLINE_CAMPUS or (dry_run and force):
        return 0 if do_login(cfg, net, dry_run, force) else 1
    return 0


def cmd_watch(cfg: dict, net: NetEnv) -> int:
    log.info("看门狗启动(在线 %s 秒一轮; 不在校园网 %s 秒; 电池模式不低于 %s 秒)",
             cfg.get("interval", 30), cfg.get("interval_offcampus", 300),
             cfg.get("interval_battery", 120))
    data = load_retry()
    failures = int(data.get("failures", 0))
    next_attempt = float(data.get("next_attempt", 0))
    if failures:
        log.info("续用上次的失败计数: %s 次", failures)
    last_iface = ""
    state = STATE_UNKNOWN

    while True:
        try:
            # ① 夜间静默: 学校这段时间不允许学生账号认证, 完全不发请求
            #    (但有些账号 24 小时可用, 那种账号不静默 —— 按账号判断, 不按网段)
            quiet_account = account_for(cfg, net.source_ip or current_source_ip(cfg))
            if in_quiet_hours(cfg, account=quiet_account):
                quiet = cfg.get("quiet_hours") or {}
                note_mode("quiet", "进入夜间静默时段 %s-%s: 学校这段时间不让认证, "
                                   "暂停所有网络请求, 到点自动恢复" % (
                                       quiet.get("start", "00:00"), quiet.get("end", "06:00")))
                while in_quiet_hours(cfg, account=quiet_account):
                    wait_for_change(300)
                note_mode("normal", "夜间静默时段结束, 恢复常规检查")
                state = STATE_UNKNOWN
                continue

            net.select()
            state, detail, _ = evaluate(cfg, net)
            changed = note_state(state, detail)
            if changed and state == STATE_OFFLINE_CAMPUS:
                notify("校园网掉线了", f"检测到未认证，正在自动重连…\n{net.source_ip}",
                       cfg)

            if net.iface != last_iface:
                log.info("当前出口网卡: %s (%s)", net.iface, net.source_ip)
                last_iface = net.iface

            if state in (STATE_ONLINE, STATE_NOT_CAMPUS):
                if failures:
                    log.info("已恢复, 清空失败计数(原 %s 次)", failures)
                failures = 0
                next_attempt = 0
                clear_retry()
            elif state == STATE_OFFLINE_CAMPUS:
                if time.time() < next_attempt:
                    log.debug("退避中, 还有 %.0f 秒再试", next_attempt - time.time())
                else:
                    log.info("检测到校园网未认证, 开始自动登录")
                    ok = do_login(cfg, net)
                    if ok:
                        failures = 0
                        next_attempt = 0
                        clear_retry()
                        notify("校园网已自动登录", f"网络已恢复\n{net.source_ip}", cfg)
                    else:
                        failures += 1
                        wait = failure_backoff(cfg, failures)
                        next_attempt = time.time() + wait
                        save_retry({"failures": failures, "next_attempt": next_attempt,
                                    "updated": time.time()})
                        log.error("第 %s 次登录失败, %s 分钟后再试(成功即恢复常规检查)",
                                  failures, wait // 60)
                        notify("校园网登录失败",
                               f"第 {failures} 次失败，{wait // 60} 分钟后重试\n"
                               f"详见 logs/campus_mac.log", cfg)
            else:
                log.debug("状态不明, 本轮不做任何动作: %s", detail)
        except Exception as exc:                              # noqa: BLE001
            log.exception("看门狗循环异常: %s", exc)

        reason = wait_for_change(interval_for(cfg, state))
        if reason == "wake":
            log.info("检测到睡眠唤醒(合盖/休眠结束), 立即重新检查")
        elif reason == "network":
            log.info("检测到网络变化(插拔网线/切换 WiFi/VPN), 立即重新检查")


def main() -> int:
    parser = argparse.ArgumentParser(description="校园网自动登录(macOS)")
    parser.add_argument("--set-password", action="store_true", help="保存账号密码到钥匙串")
    parser.add_argument("--set-cas-password", action="store_true",
                        help="单独保存【统一身份认证】密码(有线 CAS 用)")
    parser.add_argument("--check", action="store_true", help="只检测在线状态")
    parser.add_argument("--status", action="store_true",
                        help="简明状态总览(给普通人看的, 双击「查看状态」用的就是它)")
    parser.add_argument("--verify-password", action="store_true",
                        help="安全校验账号密码是否正确(不影响当前网络)")
    parser.add_argument("--diagnose", dest="diagnose", action="store_true",
                        help="环境体检: 网卡/VPN/DNS/门户/认证状态")
    parser.add_argument("--ios", action="store_true",
                        help="iPhone/iPad 接入方案(Mac 热点共享 / 快捷指令 / 系统自动弹门户)")
    parser.add_argument("--make-ios-url", action="store_true",
                        help="生成 iPhone/iPad 快捷指令用的认证请求(含明文密码, 谨慎)")
    parser.add_argument("--login", action="store_true", help="执行一次登录")
    parser.add_argument("--dry-run", action="store_true", help="演练: 不发送密码")
    parser.add_argument("--force", action="store_true",
                        help="配合 --dry-run: 跳过状态闸门, 只检查流程")
    parser.add_argument("--watch", action="store_true", help="看门狗模式")
    parser.add_argument("--interval", type=int, help="看门狗轮询间隔秒数")
    parser.add_argument("--interface", help="强制指定网卡(如 en0 / en7)")
    parser.add_argument("--quiet", action="store_true", help="不输出到控制台")
    args = parser.parse_args()

    setup_logging(verbose=not args.quiet)
    cfg = load_config()
    if args.interval:
        cfg["interval"] = args.interval
    if args.interface:
        cfg["interface"] = args.interface

    net = NetEnv(cfg)
    try:
        if args.set_password:
            cmd_set_password(cfg, net)
            return 0
        if args.set_cas_password:
            cmd_set_cas_password(cfg)
            return 0
        if args.diagnose:
            return cmd_diagnose(cfg, net)
        if args.ios:
            return cmd_ios(cfg, net)
        if args.make_ios_url:
            return cmd_make_ios_url(cfg, net)
        if args.check:
            return cmd_check(cfg, net)
        if args.status:
            return cmd_status(cfg, net)
        if args.verify_password:
            return cmd_verify_password(cfg, net)
        if args.watch:
            return cmd_watch(cfg, net)
        if args.login:
            return cmd_login(cfg, net, args.dry_run, args.force)
    except SystemExit:
        raise
    except Exception:                                         # noqa: BLE001
        log.exception("程序异常退出")
        return 3

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
