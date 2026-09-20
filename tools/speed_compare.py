#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双线路测速：同时连着有线和无线时，分别测两条线到底能跑多快。

原理和 campus_http.py --bind 一样：把连接绑定到某张网卡的地址，
强制流量从那条线出去，两条线互不干扰。

    python tools/speed_compare.py                 # 自动识别有线和无线
    python tools/speed_compare.py --wired 192.168.5.177 --wifi 10.54.89.230
    python tools/speed_compare.py --secs 15       # 每项测更久一点
    python tools/speed_compare.py --only wifi     # 只测无线

输出：每条线的单连接速率、4 连接速率（看有没有按账号限速），
以及两条线同时跑时会不会互相抢——这直接决定多线负载均衡值不值得做。
"""

from __future__ import annotations

import argparse
import http.client
import pathlib as _pl
import ssl
import sys as _sys
import threading
import time
import urllib.parse

_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import campus_http as c

# 挑几个国内镜像当测试目标：够大、够稳、不会因为连接数被拒
TARGETS = [
    ("腾讯云", "https://mirrors.cloud.tencent.com/ubuntu/ls-lR.gz"),
    ("华为云", "https://mirrors.huaweicloud.com/ubuntu/ls-lR.gz"),
    ("阿里云", "https://mirrors.aliyun.com/ubuntu/ls-lR.gz"),
]

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE      # 学校证书链不完整时也能用


def _open(src_ip: str, url: str, timeout: int = 10):
    """建立到 url 的连接（绑定源地址），并跟随最多 3 次跳转。"""
    for _ in range(4):
        parts = urllib.parse.urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout,
                                               context=_ctx, source_address=(src_ip, 0))
        else:
            conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout,
                                              source_address=(src_ip, 0))
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        conn.request("GET", path, headers={"User-Agent": c.USER_AGENT, "Host": parts.hostname})
        resp = conn.getresponse()
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.getheader("Location")
            conn.close()
            if not location:
                break
            url = urllib.parse.urljoin(url, location)
            continue
        return conn, resp
    raise OSError("跳转次数过多")


def _download(src_ip: str, url: str, secs: float, out: list, idx: int) -> None:
    total = 0
    start = time.time()
    # 文件可能比时间预算先下完。那样按剩余时间算速率会偏低，
    # 所以下完一轮就再开一轮，直到把时间用满。
    while time.time() - start < secs:
        conn = None
        try:
            conn, resp = _open(src_ip, url)
            while time.time() - start < secs:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
        except Exception:  # noqa: BLE001
            break
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
    out[idx] = total


def measure(src_ip: str, url: str, streams: int, secs: float) -> tuple[float, float]:
    """返回 (总字节数, 实际耗时秒)。"""
    out = [0] * streams
    threads = [threading.Thread(target=_download, args=(src_ip, url, secs, out, i))
               for i in range(streams)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sum(out), time.time() - started


def line_report(label: str, src_ip: str, url: str, secs: float) -> dict:
    print(f"  {label}（{src_ip}）")
    result = {}
    for streams in (1, 4):
        total, elapsed = measure(src_ip, url, streams, secs)
        mbps = total * 8 / elapsed / 1e6 if elapsed else 0
        result[streams] = mbps
        print(f"      {streams} 连接  {total/1e6:6.1f} MB / {elapsed:4.1f}s  =  {mbps:6.1f} Mbps")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="同时连着有线和无线时，分别测两条线的速率")
    parser.add_argument("--wired", help="有线网卡的本机地址")
    parser.add_argument("--wifi", help="无线网卡的本机地址")
    parser.add_argument("--secs", type=float, default=10.0, help="每项测试时长（秒），默认 10")
    parser.add_argument("--only", choices=["wired", "wifi"], help="只测其中一条")
    args = parser.parse_args()

    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    wired = args.wired
    wifi = args.wifi
    if not wired or not wifi:
        local = c.list_local_ipv4()
        for name, ip in local:
            is_wifi = any(k in name.lower() for k in ("wlan", "wifi", "wi-fi", "无线"))
            if is_wifi and not wifi:
                wifi = ip
            elif not is_wifi and not wired and ip.startswith(("192.168.", "10.12.")):
                wired = ip
        print("自动识别本机网卡：")
        for name, ip in local:
            print(f"    {name}  {ip}")
        print()

    if args.only != "wifi" and not wired:
        print("没能识别出有线网卡的地址，请用 --wired 指定")
        return 1
    if args.only != "wired" and not wifi:
        print("没能识别出无线网卡的地址，请用 --wifi 指定")
        return 1

    name, url = TARGETS[0]
    print(f"测试目标: {name} {url}")
    print(f"每项时长: {args.secs:.0f} 秒\n")

    results = {}
    if args.only != "wifi":
        print("=" * 64)
        results["wired"] = line_report("有线", wired, url, args.secs)
    if args.only != "wired":
        print("=" * 64)
        results["wifi"] = line_report("无线", wifi, url, args.secs)

    if args.only is None:
        print("=" * 64)
        print("  两条线同时跑（各 1 个连接）")
        shared = {}

        def run(key: str, ip: str) -> None:
            shared[key] = measure(ip, url, 1, args.secs)

        t1 = threading.Thread(target=run, args=("wired", wired))
        t2 = threading.Thread(target=run, args=("wifi", wifi))
        started = time.time()
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        elapsed = time.time() - started
        for key, label in (("wired", "有线"), ("wifi", "无线")):
            total = shared[key][0]
            print(f"      {label}  {total/1e6:6.1f} MB  =  {total*8/args.secs/1e6:6.1f} Mbps")
        combined = sum(v[0] for v in shared.values()) * 8 / elapsed / 1e6
        print(f"      {'合计':4s}  =  {combined:6.1f} Mbps")

    if args.only is None and "wired" in results and "wifi" in results:
        w, f = results["wired"][4], results["wifi"][4]
        print()
        print("=" * 64)
        print(f"  有线 {w:.1f} Mbps + 无线 {f:.1f} Mbps  →  两条线一起用理论上限约 {w+f:.1f} Mbps")
        if w > 0 and f / max(w, 0.1) >= 2:
            print("  两条线差距很大，做多线负载均衡时以快的那条为主更划算")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
