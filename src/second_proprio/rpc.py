"""通过同用户 Unix Socket 调用桌面后台接口。"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import uuid

from second_proprio import config as configuration

MAX_MESSAGE = 16 * 1024 * 1024


class RpcError(RuntimeError):
    """保存可展示的结构化接口错误。"""

    def __init__(self, code: str, message: str, details: object = None):
        super().__init__(message)
        self.code = code
        self.details = details


def socket_path(root: Path, config: dict | None = None) -> Path:
    """返回当前工作空间的本机连接路径。"""
    config = config or configuration.load(root)
    return root / config["paths"]["state"] / "desktop.sock"


def call(root: Path,
         method: str,
         params: dict | None = None,
         timeout: float = 30) -> object:
    """发送一个有界请求并返回结果。

    Args:
        root: 工作空间根目录。
        method: 白名单方法名称。
        params: 参数对象。
        timeout: 等待接口响应的秒数。

    Returns:
        后台返回的 JSON 结果。

    Raises:
        RpcError: 后台未启动、协议错误或业务操作失败。
    """
    request = {"id": uuid.uuid4().hex, "method": method, "params": params or {}}
    encoded = (json.dumps(request, ensure_ascii=False) + "\n").encode()
    if len(encoded) > MAX_MESSAGE:
        raise RpcError("too_large", "请求超过大小限制")
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(timeout)
            client.connect(str(socket_path(root)))
            client.sendall(encoded)
            with client.makefile("rb") as stream:
                line = stream.readline(MAX_MESSAGE + 1)
        if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
            raise RpcError("protocol", "后台响应不完整或过大")
        response = json.loads(line)
        if response.get("id") != request["id"]:
            raise RpcError("protocol", "后台响应标识不匹配")
        if "error" in response:
            error = response["error"]
            raise RpcError(error["code"], error["message"],
                           error.get("details"))
        return response["result"]
    except (OSError, ValueError, KeyError) as error:
        raise RpcError("unavailable", "后台连接不可用，请在 App 中启动后台") from error
