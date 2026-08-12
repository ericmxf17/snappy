"""Claude orchestration for the hosted service."""

import time

from backend.security import ProtocolError, validate_tool_result

MAX_TURNS = 8
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 4}
WEB_FETCH = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3}


def sources_from(content):
    found = []
    for block in content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        for result in getattr(block, "content", []) or []:
            url = getattr(result, "url", None)
            if url:
                found.append({"url": url, "title": getattr(result, "title", "") or url})
    return found


def move_cache_breakpoint(messages):
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    content = messages[-1].get("content")
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = {"type": "ephemeral"}


async def run_agent(client, websocket, question, tools, system, model):
    """Stream one question, pausing only for allowlisted tools run by the Mac app."""
    messages = [{"role": "user", "content": question}]
    system_block = [{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}]
    container = None
    total_input = total_output = 0
    final_text = ""
    allowed_names = {tool["name"] for tool in tools}

    for _ in range(MAX_TURNS):
        kwargs = {"container": container} if container else {}
        turn_text = []
        turn_started = time.perf_counter()
        async with client.messages.stream(
            model=model, max_tokens=2048, thinking={"type": "adaptive"},
            system=system_block, tools=[*tools, WEB_SEARCH, WEB_FETCH],
            messages=messages, **kwargs,
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    turn_text.append(event.text)
                    await websocket.send_json({"type": "text_delta", "text": event.text})
                elif event.type == "message_delta":
                    found = getattr(event.delta, "container", None)
                    if found:
                        container = found.id
            response = await stream.get_final_message()

        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens
        for block in response.content:
            if getattr(block, "type", None) == "server_tool_use":
                detail = (block.input or {}).get("query") or (block.input or {}).get("url")
                if detail:
                    await websocket.send_json({
                        "type": "trace", "name": block.name, "detail": detail,
                        "milliseconds": round((time.perf_counter() - turn_started) * 1000),
                    })
        sources = sources_from(response.content)
        if sources:
            await websocket.send_json({"type": "sources", "items": sources})

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue

        pending = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not pending:
            final_text = "".join(turn_text).strip()
            break

        if any(block.name not in allowed_names for block in pending):
            raise ProtocolError("model requested an unapproved tool")
        if any(not isinstance(block.input, dict) for block in pending):
            raise ProtocolError("model returned invalid tool input")
        if len({block.id for block in pending}) != len(pending):
            raise ProtocolError("model returned duplicate tool ids")

        await websocket.send_json({"type": "reset"})
        expected = {block.id: block for block in pending}
        for block in pending:
            await websocket.send_json({
                "type": "tool_call", "id": block.id,
                "name": block.name, "input": block.input,
            })
        results = {}
        while expected:
            payload = await websocket.receive_json()
            tool_id, content = validate_tool_result(payload, expected)
            expected.pop(tool_id)
            results[tool_id] = {
                "type": "tool_result", "tool_use_id": tool_id, "content": content,
            }
        messages.append({
            "role": "user", "content": [results[block.id] for block in pending],
        })
        move_cache_breakpoint(messages)
    else:
        raise ProtocolError("agent exceeded its turn limit")

    await websocket.send_json({
        "type": "usage", "input_tokens": total_input, "output_tokens": total_output,
    })
    await websocket.send_json({
        "type": "done", "answer": final_text or "Sorry, I didn't catch that.",
    })
