#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 C++ RSA 实现的对照测试向量。

直接从 Python 参考实现（openwrt/campus_http.py）里算出密文，落成 JSON。
C++ 那边只要和这份文件一致，就说明两边加密结果逐字节相同 —— 这是重构里
最容易出错、也最要命的一环（错了就是统一身份认证密码错）。

用法: python openwrt-cpp/tests/gen_rsa_vectors.py
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "openwrt" / "campus_http.py"
OUTPUT = pathlib.Path(__file__).resolve().parent / "rsa_vectors.json"


def load_reference():
    spec = importlib.util.spec_from_file_location("campus_http_ref", REFERENCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["campus_http_ref"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    ref = load_reference()
    samples = [
        "a",
        "123456",
        "password",
        "914520",
        "Abc123!@#",
        "0123456789abcdef",
        "x" * 126,       # 正好一块
        "y" * 127,       # 一块多一点，会分成两块
        "z" * 252,       # 正好两块
        "~!@#$%^&*()_+-=[]{}|;:',.<>/?`",  # 各种符号
    ]
    payload = {
        "_说明": "由 gen_rsa_vectors.py 从 Python 参考实现算出，勿手工修改",
        "chunk_size": ref._chunk_size(),
        "samples": [{"password": p, "cipher": ref.rsa_encrypt(p)} for p in samples],
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {OUTPUT}（{len(samples)} 组样本，块大小 {payload['chunk_size']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
