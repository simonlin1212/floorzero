#!/usr/bin/env python3
"""FloorZero MCP server — lets any AI agent ask the options data directly.

⭐ The core difference from Unusual Whales: their MCP needs a subscription first, this one is free out of the box.

No dependencies: stdlib JSON-RPC over stdio, without the mcp SDK.
Tool definitions are inherited automatically from `tools.py` — the single place they are defined.

Mounting:
    claude mcp add floorzero -- /path/to/python /path/to/backend/mcp_server.py

⚠️ Compliance: this server runs on the user's own machine, and no data leaves it.
"""
from __future__ import annotations

# ⚠️ Immediately after `__future__` — which has to be the first statement in the file —
#    while the version gate has to run before the rest of the imports, so that too old a version
#    gets a sentence in plain words rather than dropping the user into a SyntaxError deep inside some module to puzzle over.
import pyversion  # noqa: F401

import json
import os
import sys

# Allows running straight from backend/ (both uvicorn and MCP startup resolve sources/ and modules/)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tools  # noqa: E402

# The protocol version this server actually implements. ⚠️ Do not echo back the version the client asked for —
# that claims support for semantics we have not implemented, and a newer client then calls capabilities we lack.
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

    # Notifications (no id) get no response
    if method == "notifications/initialized":
        return

    if method == "initialize":
        params = msg.get("params") or {}
        want = params.get("protocolVersion")
        # Accept the client's requested version only when we **do** implement it; otherwise fall back to
        # the version we support and let the client decide whether to continue (the negotiation the MCP spec recommends).
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
        _error(rid, -32601, f"Unknown method: {method}")


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
        except Exception as e:  # noqa: BLE001 — one bad message must not sink the whole server
            if msg.get("id") is not None:
                _error(msg["id"], -32603, f"Internal error: {e}")


if __name__ == "__main__":
    main()
