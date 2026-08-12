"""Protocol limits and the server-side tool allowlist."""

ALLOWED_CLIENT_TOOLS = frozenset({
    "list_accounts", "get_portfolio_summary", "get_account_balance",
    "check_symbol_held", "get_quote", "list_connections",
    "list_supported_brokerages", "get_orders", "get_all_holdings",
    "find_overlap", "get_connection_health", "get_activities",
    "get_balance_history", "search_symbols", "preview_trade",
    "preview_cancel", "preview_cancel_all",
})

MAX_QUESTION_CHARS = 8_000
MAX_TOOL_RESULT_CHARS = 250_000
MAX_TOOLS = len(ALLOWED_CLIENT_TOOLS)


class ProtocolError(ValueError):
    pass


def validate_tools(raw_tools):
    """Accept schemas for safe local tools only; reject the whole request otherwise."""
    if not isinstance(raw_tools, list) or len(raw_tools) > MAX_TOOLS:
        raise ProtocolError("invalid tool list")

    clean = []
    seen = set()
    for tool in raw_tools:
        if not isinstance(tool, dict):
            raise ProtocolError("invalid tool schema")
        name = tool.get("name")
        if name not in ALLOWED_CLIENT_TOOLS or name in seen:
            raise ProtocolError(f"tool not allowed: {name}")
        if not isinstance(tool.get("description"), str):
            raise ProtocolError(f"invalid description for {name}")
        if not isinstance(tool.get("input_schema"), dict):
            raise ProtocolError(f"invalid input schema for {name}")
        seen.add(name)
        clean.append({
            "name": name,
            "description": tool["description"],
            "input_schema": tool["input_schema"],
        })
    return clean


def validate_start(payload):
    if not isinstance(payload, dict) or payload.get("type") != "start":
        raise ProtocolError("first message must be start")
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ProtocolError("question is required")
    if len(question) > MAX_QUESTION_CHARS:
        raise ProtocolError("question is too long")
    return question.strip(), validate_tools(payload.get("tools"))


def validate_tool_result(payload, pending_ids):
    if not isinstance(payload, dict) or payload.get("type") != "tool_result":
        raise ProtocolError("expected tool_result")
    tool_id = payload.get("id")
    result = payload.get("content")
    if tool_id not in pending_ids:
        raise ProtocolError("unexpected tool result id")
    if not isinstance(result, str) or len(result) > MAX_TOOL_RESULT_CHARS:
        raise ProtocolError("invalid tool result")
    return tool_id, result
