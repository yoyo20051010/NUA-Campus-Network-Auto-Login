#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路由器 SSH 小助手（开发/部署时用，不属于发布产物）。

用法:
    python tools/router_ssh.py run "uname -a"
    python tools/router_ssh.py put 本地文件 /tmp/远端文件
    python tools/router_ssh.py get /tmp/远端文件 本地文件

默认连 192.168.5.1（可用环境变量 ROUTER_HOST / ROUTER_USER / ROUTER_PASS 覆盖）。
"""

from __future__ import annotations

import os
import sys

import paramiko

HOST = os.environ.get("ROUTER_HOST", "192.168.5.1")
USER = os.environ.get("ROUTER_USER", "root")
PASSWORD = os.environ.get("ROUTER_PASS", "password")


def connect() -> paramiko.SSHClient:
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=PASSWORD, timeout=15,
                look_for_keys=False, allow_agent=False)
    return cli


def run(cli: paramiko.SSHClient, command: str, timeout: int = 600) -> int:
    _in, out, err = cli.exec_command(command, timeout=timeout)
    sys.stdout.write(out.read().decode("utf-8", "replace"))
    sys.stdout.write(err.read().decode("utf-8", "replace"))
    return out.channel.recv_exit_status()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    action = sys.argv[1]
    cli = connect()
    try:
        if action == "run":
            return run(cli, sys.argv[2])
        if action == "put":
            sftp = cli.open_sftp()
            sftp.put(sys.argv[2], sys.argv[3])
            sftp.close()
            print(f"已上传 {sys.argv[2]} -> {sys.argv[3]}")
            return 0
        if action == "get":
            sftp = cli.open_sftp()
            sftp.get(sys.argv[2], sys.argv[3])
            sftp.close()
            print(f"已下载 {sys.argv[2]} -> {sys.argv[3]}")
            return 0
    finally:
        cli.close()
    print(f"未知操作: {action}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
