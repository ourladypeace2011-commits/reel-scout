from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from . import tools

SERVER_INFO = {
    "name": "reel-scout",
    "version": "0.1.0",
}

CAPABILITIES = {
    "tools": {},
}


def _readline(stream: Any) -> Any:
    if hasattr(stream, "buffer"):
        return stream.buffer.readline()
    return stream.readline()


def _read_exact(stream: Any, length: int) -> Any:
    if hasattr(stream, "buffer"):
        return stream.buffer.read(length)
    return stream.read(length)


def _write(stream: Any, data: bytes) -> None:
    if hasattr(stream, "buffer"):
        stream.buffer.write(data)
    else:
        stream.write(data.decode("utf-8"))


def read_message(stream: Any = None) -> Optional[Dict[str, Any]]:
    if stream is None:
        stream = sys.stdin

    content_length = None
    while True:
        line = _readline(stream)
        if line in (b"", ""):
            return None
        if isinstance(line, bytes):
            decoded = line.decode("utf-8")
        else:
            decoded = line
        if decoded in ("\r\n", "\n", ""):
            break
        name, separator, value = decoded.partition(":")
        if separator and name.lower().strip() == "content-length":
            content_length = int(value.strip())

    if content_length is None:
        raise ValueError("Missing Content-Length header")

    body = _read_exact(stream, content_length)
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    if not body:
        return None
    return json.loads(body)


def write_message(message: Dict[str, Any], stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout

    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    header = ("Content-Length: %d\r\n\r\n" % len(body)).encode("utf-8")
    _write(stream, header + body)
    if hasattr(stream, "flush"):
        stream.flush()


def _rpc_result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    try:
        if method == "initialize":
            return _rpc_result(
                req_id,
                {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": SERVER_INFO,
                    "capabilities": CAPABILITIES,
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return _rpc_result(req_id, {"tools": tools.list_tools()})
        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            return _rpc_result(req_id, tools.call_tool(tool_name, arguments))
        return _rpc_error(req_id, -32601, "Method not found: %s" % method)
    except Exception as exc:
        return _rpc_error(req_id, -32603, "Internal error: %s" % exc)


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            break
        response = handle_request(message)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    main()
