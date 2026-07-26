#!/usr/bin/env python3
"""FloorZero MCP server —— 让任何 AI agent 直接问期权数据。

⭐ 这是相对 Unusual Whales 的核心差异：他们的 MCP 要先买订阅，这个开箱免费。

零依赖：stdlib JSON-RPC over stdio，不引 mcp SDK。
工具定义从 `tools.py` 自动继承 —— 那里是唯一定义处。

挂载：
    claude mcp add floorzero -- /path/to/python /path/to/backend/mcp_server.py

⚠️ 合规：本 server 跑在用户自己机器上，数据不出本机。
"""
from __future__ import annotations

# ⚠️ 紧跟在 `__future__` 之后 —— 它必须是文件里的第一条语句，
#    而版本闸要在其余 import 之前跑，好在版本不够时给一句人话，
#    而不是让用户撞进某个模块深处的 SyntaxError 去猜哪里不对。
import pyversion  # noqa: F401

import json
import os
import sys

# 允许从 backend/ 直接跑（uvicorn 与 MCP 两种启动方式都能解析 sources/ modules/）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tools  # noqa: E402

# 本 server 实际实现的协议版本。⚠️ 不要把客户端请求的版本原样回显 ——
# 那等于声称支持自己没实现的语义，新版客户端会按它不支持的能力去调用。
SUPPORTED_PROTOCOLS = ("2024-11-05",)
DEFAULT_PROTOCOL = SUPPORTED_PROTOCOLS[0]
SERVER_INFO = {"name": "floorzero", "version": "0.1.0"}


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(rid, result) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _handle(msg: dict) -> None:
    method = msg.get("method")
    rid = msg.get("id")

    # 通知（无 id）不回响应
    if method == "notifications/initialized":
        return

    if method == "initialize":
        params = msg.get("params") or {}
        want = params.get("protocolVersion")
        # 只在客户端请求的版本我们**确实实现**时才认；否则回落到自己支持的版本，
        # 由客户端决定是否继续（这是 MCP 规范建议的协商方式）。
        agreed = want if want in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        _result(rid, {
            "protocolVersion": agreed,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
        return

    if method == "ping":
        _result(rid, {})
        return

    if method == "tools/list":
        _result(rid, {"tools": tools.TOOLS})
        return

    if method == "tools/call":
        params = msg.get("params") or {}
        data = tools.exec_tool(params.get("name", ""), params.get("arguments") or {})
        _result(rid, {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "isError": isinstance(data, dict) and "error" in data,
        })
        return

    if rid is not None:
        _error(rid, -32601, f"未知方法：{method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(msg)
        except Exception as e:  # noqa: BLE001 —— 单条消息出错不能拖垮整个 server
            if msg.get("id") is not None:
                _error(msg["id"], -32603, f"内部错误：{e}")


if __name__ == "__main__":
    main()
