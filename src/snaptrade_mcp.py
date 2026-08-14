"""Small Streamable-HTTP MCP client for SnapTrade's read-only Personal connector."""

import itertools
import json
import threading

import requests

import auth

ENDPOINT = "https://mcp.snaptrade.com/mcp"
PROTOCOL = "2025-06-18"
TIMEOUT = 60

_ids = itertools.count(1)
_lock = threading.RLock()
_session_id = None
_tools = None


def _payload(response):
    """Decode either a normal JSON response or Streamable HTTP's SSE form."""
    content_type = response.headers.get("Content-Type", "")
    if "text/event-stream" not in content_type:
        return response.json() if response.content else {}
    messages = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            messages.append(json.loads(line[5:].strip()))
    return messages[-1] if messages else {}


def _post(message, *, notification=False):
    global _session_id
    access = auth.token()
    if not access:
        raise auth.AuthError("Not signed in to SnapTrade.")
    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
    }
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id
    response = requests.post(ENDPOINT, headers=headers, json=message, timeout=TIMEOUT)
    if response.status_code == 401:
        reset()
        raise auth.AuthError("SnapTrade sign-in expired. Sign in again.")
    response.raise_for_status()
    _session_id = response.headers.get("Mcp-Session-Id", _session_id)
    if notification or not response.content:
        return {}
    payload = _payload(response)
    if payload.get("error"):
        error = payload["error"]
        raise auth.AuthError(error.get("message") or str(error))
    return payload.get("result", {})


def _initialize():
    if _session_id:
        return
    result = _post({
        "jsonrpc": "2.0",
        "id": next(_ids),
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "Snappy", "version": "0.1.0"},
        },
    })
    if not result.get("protocolVersion"):
        raise auth.AuthError("SnapTrade MCP initialization failed.")
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notification=True)


def _tool_def(name):
    global _tools
    if _tools is None:
        result = _post({
            "jsonrpc": "2.0", "id": next(_ids), "method": "tools/list", "params": {}
        })
        _tools = {tool["name"]: tool for tool in result.get("tools", [])}
    if name not in _tools:
        raise auth.AuthError(f"SnapTrade MCP does not provide {name}.")
    return _tools[name]


def _arguments(name, values):
    """Map SDK-style logical names onto the MCP tool's published JSON schema."""
    properties = _tool_def(name).get("inputSchema", {}).get("properties", {})
    aliases = {
        "account_id": ("accountId", "account_id", "account"),
        "authorization_id": (
            "brokerageAuthorizationId", "authorizationId", "connectionId",
            "brokerage_authorization_id", "authorization_id",
        ),
        "state": ("state", "status"),
    }
    arguments = {}
    for logical, value in values.items():
        if value is None:
            continue
        key = next((candidate for candidate in aliases.get(logical, (logical,))
                    if candidate in properties), None)
        if key:
            arguments[key] = value
    return arguments


def call(name, **values):
    global _session_id
    with _lock:
        _initialize()
        arguments = _arguments(name, values)
        result = _post({
            "jsonrpc": "2.0",
            "id": next(_ids),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if result.get("isError"):
            text = " ".join(item.get("text", "") for item in result.get("content", []))
            raise auth.AuthError(text or f"SnapTrade MCP tool {name} failed.")
        if "structuredContent" in result:
            structured = result["structuredContent"]
            # SnapTrade wraps each Partner API response as {"result": ...}.
            if isinstance(structured, dict) and set(structured) == {"result"}:
                structured = structured["result"]
            # Collection tools add one semantic envelope (for example
            # {"orders": [...]}); the direct REST equivalents return the list.
            if (isinstance(structured, dict) and len(structured) == 1
                    and isinstance(next(iter(structured.values())), list)):
                return next(iter(structured.values()))
            return structured
        content = result.get("content", [])
        texts = [item.get("text") for item in content if item.get("type") == "text"]
        if not texts:
            return None
        try:
            return json.loads(texts[0])
        except (TypeError, json.JSONDecodeError):
            return texts[0]


def items(value):
    """Unwrap common API/MCP collection envelopes."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("result", "data", "results", "accounts", "authorizations"):
            if key in value:
                return items(value[key])
        return [value]
    return []


def reset():
    global _session_id, _tools
    _session_id = None
    _tools = None
