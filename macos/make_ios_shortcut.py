#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚠️ 实验性功能（未完整验证）
   生成的快捷指令能成功导入，请求格式也按实测的 Dr.COM 参数生成，
   但"在手机上真正完成一次认证"这一步没有跑通验证过。
   效果不保证，仅供折腾；想要稳定方案建议直接用系统弹出的门户页登录。

生成 iPhone / iPad 用的「校园网登录」快捷指令文件(.shortcut)。

依据 2026-09-15 实测: 无线登录必须用
    POST http://10.255.255.2:801/eportal/?c=ACSetting&a=Login&ver=1.0
    请求体 = 表单(DDDDD / upass / 0MKKey / R6 / terminal_type / lang / url)
    头部   = X-Requested-With: XMLHttpRequest
这样 AC 才会走登录接口(否则可能返回后台管理页面)。

用法: python3 make_ios_shortcut.py
产物: 校园网登录.shortcut   (里面含明文密码, 别外传)
"""
from __future__ import annotations

import json
import pathlib
import plistlib
import subprocess
import sys
import urllib.parse

APP_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import campus_mac as cm          # noqa: E402

SIGNED = APP_DIR / "校园网登录.shortcut"
UNSIGNED = APP_DIR / "state" / "校园网登录.unsigned.shortcut"


def _text(value: str) -> dict:
    """Shortcuts 里一个纯文本 token 的标准写法。"""
    return {
        "Value": {"string": value, "WFSerializationType": "WFTextTokenString"},
        "WFSerializationType": "WFTextTokenAttachment",
    }


def _dict_field(pairs) -> dict:
    """WFDictionaryFieldValue: 用于表单请求体 / 自定义头部。"""
    items = []
    for key, value in pairs:
        items.append({"WFItemType": 0, "WFKey": _text(key), "WFValue": _text(value)})
    return {
        "Value": {"WFDictionaryFieldValueItems": items},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def build_request(cfg: dict, account: str, password: str):
    host = urllib.parse.urlsplit(cfg["portal_url"]).hostname or "10.255.255.2"
    port = int(cfg.get("portal_api_port", 801))
    url = f"http://{host}:{port}/eportal/?c=ACSetting&a=Login&ver=1.0"
    body = [
        ("DDDDD", account),
        ("upass", password),
        ("0MKKey", "123456"),
        ("R6", "0"),
        ("terminal_type", "1"),
        ("lang", "zh-cn"),
        ("url", "drappall"),
    ]
    headers = [("X-Requested-With", "XMLHttpRequest")]
    return url, body, headers


def build_shortcut(url: str, body, headers) -> dict:
    return {
        "WFWorkflowClientVersion": "1200.0",
        "WFWorkflowClientRelease": "3.0",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983,
                           "WFWorkflowIconGlyphNumber": 59411},
        "WFWorkflowTypes": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowImportQuestions": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowActions": [
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
                "WFWorkflowActionParameters": {
                    "WFURL": url,
                    "WFHTTPMethod": "POST",
                    "WFHTTPBodyType": "Form",
                    "WFRequestVariable": _dict_field(body),
                    "WFHTTPHeaders": _dict_field(headers),
                },
            },
            {
                "WFWorkflowActionIdentifier": "is.workflow.actions.showresult",
                "WFWorkflowActionParameters": {},
            },
        ],
    }


def main() -> int:
    cfg = cm.load_config()
    account, password = cm.load_credentials(cfg, "drcom")
    url, body, headers = build_request(cfg, account, password)

    UNSIGNED.parent.mkdir(exist_ok=True)
    UNSIGNED.write_bytes(plistlib.dumps(build_shortcut(url, body, headers)))
    UNSIGNED.chmod(0o600)

    if SIGNED.exists():
        SIGNED.unlink()
    proc = subprocess.run(["/usr/bin/shortcuts", "sign", "-m", "anyone",
                           "-i", str(UNSIGNED), "-o", str(SIGNED)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print("签名失败:", proc.stdout, proc.stderr)
        return 1
    SIGNED.chmod(0o600)
    try:
        UNSIGNED.unlink()
    except OSError:
        pass
    print(f"已生成: {SIGNED}")
    print(f"账号: {account}")
    print(f"请求: POST {url}")
    print(f"表单字段: {', '.join(k for k, _ in body)}")
    print(f"头部: {', '.join(f'{k}: {v}' for k, v in headers)}")
    print("文件里含明文密码, AirDrop 给 iPhone/iPad 后导入, 别外传。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
