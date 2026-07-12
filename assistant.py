"""The brain: turns a spoken question into an answer, using the user's real
brokerage data (via SnapTrade) and the open web (via Claude's server-side search).

The answer's FIRST PARAGRAPH is what gets spoken; the rest is detail for the panel.
"""

import time

import anthropic
import config
import state
from tools import TOOLS, WEB_FETCH, WEB_SEARCH, run_tool

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

MAX_TURNS = 8  # a runaway search loop shouldn't be able to hang a live demo

SYSTEM_PROMPT = """You are Snappy, a voice assistant with live access to the user's real \
brokerage accounts (through SnapTrade) and to the web.

FORMAT — this matters, read carefully:
Your first paragraph is SPOKEN OUT LOUD. Everything after it is only shown on screen.

- First paragraph: AT MOST two short sentences, under 35 words total. Lead with the answer — \
no preamble, no throat-clearing, no restating the question. Say numbers the way a person would \
("about two thousand dollars", "roughly two percent"). No markdown, no symbols, no lists — it \
is going through a speech synthesizer. If a figure is a rough estimate, one word ("roughly", \
"around") is enough; save the caveats for the detail below.
- Then a blank line, then the supporting detail: the figures you found, the arithmetic, and \
any caveats. Short lines. You may use "-" bullets here.

RESEARCH — you are talking to someone waiting in silence, so be decisive:
- Search ONCE with a well-chosen query. Read what comes back before searching again. Do not \
verify the same number from three different angles; one good source, named, beats three.
- Only search again if the first search genuinely failed to answer, or the question truly has \
two separate parts.
- Use web_fetch when a specific page is clearly the authority (a filing, a company page, a \
market-data page a search result pointed you at) and the snippet was not enough.
- Prefer the most recent figure and say when it is from.

RULES:
- Never invent a financial figure. Get it from a tool or from the web.
- Before sizing a hypothetical position, call get_portfolio_summary — you need the user's real \
total as the denominator, and the tool computes the percentages for you.
- Private companies (Stripe, OpenAI, Anthropic) have no market quote, so get_quote will fail \
on them. Search the web for a secondary-market or last-round valuation and say plainly that \
it's an estimate. Do NOT assume a company is private because you remember it that way — \
companies list, and your memory has a cutoff. If get_quote returns a price, it's public.
- For questions about which brokerages are supported or connected, use the SnapTrade tools — \
that is real data, not something to search for.
- If the transcript is empty or garbled, just say you didn't catch that. Call no tools."""

# The system prompt and tool schemas are identical on every turn of every question,
# and a search-heavy turn re-sends the whole transcript. Cache the fixed prefix.
_SYSTEM = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]


def _sources_from(content):
    """Pull the web pages Claude actually read out of the search result blocks."""
    found = []
    for block in content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        for result in getattr(block, "content", []) or []:
            url = getattr(result, "url", None)
            if url:
                found.append({"url": url, "title": getattr(result, "title", "") or url})
    return found


def _stream_turn(messages, container, on_text):
    """One turn. Returns (message, container_id).

    We iterate raw events rather than `stream.text_stream` for one reason: the
    server-side web search runs inside a sandbox container, and once that exists
    every later request must name it — but `get_final_message()` does NOT carry
    the container through (it comes back None). The id only ever appears on the
    raw `message_delta` event, so that's where we take it from. Miss it and the
    next request dies with "container_id is required".
    """
    kwargs = {"container": container} if container else {}

    with _client.messages.stream(
        model=config.CLAUDE_MODEL,
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=_SYSTEM,
        tools=[*TOOLS, WEB_SEARCH, WEB_FETCH],
        messages=messages,
        **kwargs,
    ) as stream:
        for event in stream:
            if event.type == "text":
                on_text(event.text)
            elif event.type == "message_delta":
                found = getattr(event.delta, "container", None)
                if found:
                    container = found.id
        return stream.get_final_message(), container


def answer(transcript: str, on_text=None, on_reset=None, on_narration=None) -> str:
    """Run the question to completion. Returns the final answer text.

    on_text       — each chunk of the answer, as it streams.
    on_reset      — the text so far was narration before a tool call, not the answer;
                    drop it.
    on_narration  — that same narration, handed over rather than thrown away. A
                    web-searching answer can take 30 seconds, and "I'll look that up"
                    spoken at second two is the difference between thinking and dead.
    """
    messages = [{"role": "user", "content": transcript}]
    container = None
    turn_text = []

    for _ in range(MAX_TURNS):
        turn_started = time.perf_counter()
        turn_text = []  # only the LAST turn is the answer; earlier turns narrate

        collect = turn_text.append
        if on_text:
            def collect(chunk):
                turn_text.append(chunk)
                on_text(chunk)

        response, container = _stream_turn(messages, container, collect)

        # Server-side searches already ran; surface them in the trace and sources.
        for block in response.content:
            if getattr(block, "type", None) != "server_tool_use":
                continue
            query = (block.input or {}).get("query") or (block.input or {}).get("url")
            if not query:  # partial block from the stream — nothing worth showing
                continue
            state.record_call(
                block.name,
                round((time.perf_counter() - turn_started) * 1000),
                detail=query,
            )
        sources = _sources_from(response.content)
        if sources:
            state.add_sources(sources)

        # Preserve thinking and server-tool blocks exactly as received.
        messages.append({"role": "assistant", "content": response.content})

        # A paused turn means the server hit its search cap mid-answer. Re-send to
        # let it continue; there's nothing for us to execute.
        if response.stop_reason == "pause_turn":
            continue

        # Only client-side tools need a result from us. Server tools (web_search,
        # web_fetch) are already resolved in the content above.
        pending = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        if not pending:
            break

        # Whatever it said this turn was narration ahead of a tool call, not the
        # answer. Drop it from the panel — but say it out loud, because the user is
        # otherwise listening to nothing at all while the searches run.
        said = "".join(turn_text).strip()
        if on_reset:
            on_reset()
        if said and on_narration:
            on_narration(said)

        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": run_tool(b.name, b.input),
                    }
                    for b in pending
                ],
            }
        )

    return "".join(turn_text).strip() or "Sorry, I didn't catch that."


def spoken_part(text: str) -> str:
    """The first paragraph — the only bit that should be read aloud."""
    return text.split("\n\n", 1)[0].strip()


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "What is my account balance?"
    print(f"Q: {question}\n")
    started = time.perf_counter()
    result = answer(
        question,
        on_text=lambda t: print(t, end="", flush=True),
        on_reset=lambda: print("\n  [narration — spoken, then dropped]\n"),
    )
    print("\n" + "─" * 60)
    print(f"SPOKEN: {spoken_part(result)}")
    if state.STATE["calls"]:
        print("CALLS :", [f"{c['name']}({c['detail']})" if c["detail"] else c["name"]
                          for c in state.STATE["calls"]])
    if state.STATE["sources"]:
        print("SOURCES:", [s["url"] for s in state.STATE["sources"]][:5])
    print(f"TOOK  : {time.perf_counter() - started:.1f}s")
