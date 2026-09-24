#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 OpenWrt C++ 版：把二进制 + 安装脚本 + 默认配置压成一个 zip。

    python tools/make_release_openwrt_cpp.py                 # 版本号自动取
    python tools/make_release_openwrt_cpp.py --version 1.5

产物（Release 资产名必须纯英文）：
    NUA-Campus-Network-Auto-Login_v1.5_openwrt-cpp-aarch64.zip

包里放的是已经编译好的 aarch64 静态二进制，用户解压后 scp 上去就能装，
不需要自己准备工具链。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT = ROOT / "openwrt-cpp"
PACKAGING = PROJECT / "openwrt"
BINARY = PROJECT / "build" / "campus-net-login"
DOCS = ROOT / "docs" / "openwrt-cpp.md"


def guess_version() -> str:
    try:
        text = (ROOT / "tools" / "make_release.ps1").read_text(encoding="utf-8")
    except OSError:
        return "0.0"
    m = re.search(r"\$Version\s*=\s*'([^']+)'", text)
    return m.group(1) if m else "0.0"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 OpenWrt C++ 版")
    parser.add_argument("--version", help="版本号，默认从 make_release.ps1 读")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if not BINARY.is_file():
        print(f"还没编译：{BINARY}")
        print("先执行: pwsh -File openwrt-cpp/tools/build.ps1 -Test")
        return 1

    version = args.version or guess_version()
    out_name = f"NUA-Campus-Network-Auto-Login_v{version}_openwrt-cpp-aarch64.zip"
    out_path = ROOT / out_name
    if out_path.exists():
        out_path.unlink()

    # 用 PurePosixPath：zip 里必须是正斜杠，Windows 上 pathlib.Path 会写成反斜杠
    entries = [
        (BINARY, pathlib.PurePosixPath("openwrt-cpp/campus-net-login")),
        (PACKAGING / "install.sh", pathlib.PurePosixPath("openwrt-cpp/install.sh")),
        (PACKAGING / "config.json", pathlib.PurePosixPath("openwrt-cpp/config.json")),
        (PACKAGING / "campus-net-login.init",
         pathlib.PurePosixPath("openwrt-cpp/campus-net-login.init")),
        (PROJECT / "README.md", pathlib.PurePosixPath("openwrt-cpp/README.md")),
    ]
    if DOCS.is_file():
        entries.append((DOCS, pathlib.PurePosixPath("openwrt-cpp/说明.md")))

    missing = [src for src, _ in entries if not src.is_file()]
    if missing:
        for src in missing:
            print(f"缺少文件: {src}")
        return 1

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, arc in entries:
            zf.write(src, arc)

    size_kb = out_path.stat().st_size / 1024
    print(f"已生成: {out_path}")
    print(f"  版本 {version}｜{len(entries)} 个文件｜{size_kb:.0f} KB")
    for _, arc in entries:
        print(f"    {arc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
