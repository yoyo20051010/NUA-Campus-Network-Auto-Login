#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包 OpenWrt 版：把 openwrt/ 目录压成一个 zip。

    python tools/make_release_openwrt.py             # 版本号取仓库里的（见下）
    python tools/make_release_openwrt.py --version 1.4

产物（Release 资产名必须纯英文，见 docs/发版流程.md）：
    NUA-Campus-Network-Auto-Login_v1.4_openwrt.zip

为什么用 Python 而不是系统 zip / Compress-Archive：
  压缩包**内部**有中文文件名（README.md、install.sh 里的注释等），
  zipfile 会写 UTF-8 标记，解压不会乱码。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "openwrt"


def guess_version() -> str:
    """从 tools/make_release.ps1 里读版本号，保持两个打包脚本一致。"""
    try:
        text = (ROOT / "tools" / "make_release.ps1").read_text(encoding="utf-8")
    except OSError:
        return "0.0"
    m = re.search(r"\$Version\s*=\s*'([^']+)'", text)
    return m.group(1) if m else "0.0"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 OpenWrt 版")
    parser.add_argument("--version", help="版本号，默认从 make_release.ps1 读")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    version = args.version or guess_version()
    out_name = f"NUA-Campus-Network-Auto-Login_v{version}_openwrt.zip"
    out_path = ROOT / out_name

    if not SRC.is_dir():
        print(f"找不到 {SRC}")
        return 1

    # 自带运行环境不需要，日志和调试存档也不该进包
    skip_dirs = {"logs", "profiles", "__pycache__"}
    skip_names = {"secret.json", "config.json.bak", "campus_http.py.bak",
                  "campus_http.py.bak2", "secret.json.teacher", "retry_state.json"}

    files: list[pathlib.Path] = []
    for p in sorted(SRC.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(SRC)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if p.name in skip_names or p.name.endswith(".pyc"):
            continue
        files.append(p)

    if out_path.exists():
        out_path.unlink()

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            # 解压后是 openwrt/xxx，直接 scp 到路由器就能用
            zf.write(p, pathlib.Path("openwrt") / p.relative_to(SRC))

    size_kb = out_path.stat().st_size / 1024
    print(f"已生成: {out_path}")
    print(f"  版本 {version}｜{len(files)} 个文件｜{size_kb:.0f} KB")
    for p in files:
        print(f"    openwrt/{p.relative_to(SRC)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
