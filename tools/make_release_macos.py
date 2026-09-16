#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包 macOS 版为可分发的 zip（与 tools/make_release.ps1 对应）

用法:  python3 tools/make_release_macos.py [版本号]
产物:  仓库根目录下的  校园网自动登录_v<版本>_macOS版.zip

包内结构（平铺，双击第一个文件即可安装）:
    校园网自动登录_v1.1_macOS版/
      AAA一键安装.command      ← 双击这个
      卸载.command
      AAA使用说明.txt
      README.md
      campus_mac.py  config.json  install.sh  uninstall.sh

为什么不用系统自带的 zip 命令:
  macOS 的 Info-ZIP 压缩中文文件名时不写 UTF-8 标记（flag bit 11），
  别的工具解压会显示乱码；Python 的 zipfile 会正确打标记，
  同时能保留 .command / .sh 的可执行权限。
"""
from __future__ import annotations

import pathlib
import sys
import zipfile

HERE = pathlib.Path(__file__).resolve().parent.parent      # 仓库根目录
SRC = HERE / "macos"

FILES = [
    "AAA一键安装.command",
    "卸载.command",
    "AAA使用说明.txt",
    "README.md",
    "campus_mac.py",
    "config.json",
    "install.sh",
    "uninstall.sh",
]
EXECUTABLE = {"AAA一键安装.command", "卸载.command", "install.sh", "uninstall.sh", "campus_mac.py"}


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else "1.1"
    name = f"校园网自动登录_v{version}_macOS版"
    out = HERE / f"{name}.zip"

    missing = [f for f in FILES if not (SRC / f).is_file()]
    if missing:
        print("缺少文件:", ", ".join(missing))
        return 1

    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for filename in FILES:
            info = zipfile.ZipInfo(f"{name}/{filename}")
            info.flag_bits |= 0x800                       # UTF-8 文件名标记
            mode = 0o755 if filename in EXECUTABLE else 0o644
            info.external_attr = (mode | 0o100000) << 16  # 普通文件 + 权限
            info.compress_type = zipfile.ZIP_DEFLATED
            data = (SRC / filename).read_bytes()
            zf.writestr(info, data)

    size_kb = out.stat().st_size / 1024
    print(f"已生成: {out}")
    print(f"  大小: {size_kb:.1f} KB")
    print(f"  文件: {len(FILES)} 个（{name}/）")
    print()
    print("下一步: 把这个 zip 上传到 GitHub Release（或直接发给同学）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
