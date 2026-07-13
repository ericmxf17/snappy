"""The brain: turns a spoken question into an answer, using the user's real
brokerage data (via SnapTrade) and the open web (via Claude's server-side search).

The answer's FIRST PARAGRAPH is the headline shown large in the panel; the rest is
supporting detail. Snappy listens, but it does not talk back — answers are read, not
heard.
"""

import time

import anthropic
import config
import state
from tools import TOOLS, WEB_FETCH, WEB_SEARCH, run_tool

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

MAX_TURNS = 8  # a runaway search loop shouldn't be able to hang a live demo

SYSTEM_PROMPT = """You are Snappy. The user speaks their question; you answer in writing, \
on screen. You have live access to their real brokerage accounts (through SnapTrade) and to \
the web.

FORMAT — this matters, read carefully:
Your first paragraph is the HEADLINE, shown large. Everything after it is the analysis.

- HEADLINE: at most two sentences, under 35 words. Lead with the answer — no preamble, no \
throat-clearing, no restating the question. Plain prose, no markdown. It has to land in one \
glance.
- Then a blank line, then the ANALYSIS. Be substantive here. Short lines, "-" bullets and \
**bold** are fine. This is what a sharp analyst would hand you, not a receipt.

ANALYSIS — what a good answer actually contains:
Never stop at "that's X% of your portfolio". That is arithmetic, not insight. Anyone can \
divide. Say what it MEANS for this specific person, given what they actually hold:

- WHAT IT DISPLACES. Buying uses cash. Say what that leaves, and whether the position is \
sized like a conviction bet or a toe in the water.
- WHAT IT DUPLICATES. Look at their actual holdings. If they already own three megacap tech \
names, a fourth is not diversification — it is the same bet with extra steps. Name the \
overlap explicitly: shared sector, shared factor (rates, AI capex, consumer spend), shared \
customer.
- CONCENTRATION AFTER, not just before. Give the post-trade weight, and the post-trade weight \
of the whole correlated cluster — that second number is usually the one that matters.
- PENDING ORDERS. Money already committed. If they have an open buy that hasn't filled, their \
real exposure tomorrow is not what the positions list says today. Flag it.
- THE CHARACTER OF THE THING. Volatile? Pre-profit? A single-product company? A stock that \
just IPO'd has no trading history and a lockup expiry ahead of it. Say so.
- WHAT WOULD MAKE THIS A BAD IDEA. One honest line. If you cannot think of one, you have not \
looked hard enough.

Be direct and concrete. "This would be your fourth AI-adjacent holding and take that cluster \
to 41% of the book" beats "consider diversification". No hedging, no boilerplate, no \
"consult a financial advisor" filler.

You are giving ANALYSIS, not advice. Lay out the trade-offs and let them decide; do not tell \
them what to do with their money. If the portfolio has nothing in it yet, say that plainly — \
a first position has no overlap to analyse, and pretending otherwise is noise.

RESEARCH — you are talking to someone waiting in silence, so be decisive:
- Search ONCE with a well-chosen query. Read what comes back before searching again. Do not \
verify the same number from three different angles; one good source, named, beats three.
- Only search again if the first search genuinely failed to answer, or the question truly has \
two separate parts.
- Use web_fetch when a specific page is clearly the authority (a filing, a company page, a \
market-data page a search result pointed you at) and the snippet was not enough.
- Prefer the most recent figure and say when it is from.

ACCOUNTS — the user has more than one:
- Their money is spread across several brokerage accounts. list_accounts tells you which. \
Two accounts at the same brokerage can share a name ("Alpaca Paper"), so identify them by \
the last four of the account number.
- Portfolio questions: prefer get_all_holdings and find_overlap. The real answer to "how much \
Nvidia do I own" is the total ACROSS accounts, and neither brokerage can see that number. It \
is the whole reason this app exists.
- TRADING INTO THE WRONG ACCOUNT IS THE WRONG OUTCOME, and it produces no error — the shares \
simply land somewhere the user didn't ask for. If they say which account, pass it to \
preview_trade.
- If they DON'T say, just call preview_trade without an account. It will come back "Needs an \
account" and the panel will show them a picker to click. When that happens, your headline is \
one short line asking which account — nothing more. Do not choose for them, do not guess from \
who has more cash, and do not claim anything has been priced: nothing has, because pricing \
needs an account.

TRADING — read this carefully:
- To buy or sell, call preview_trade. It PROPOSES the order; it does not place it. You \
have no tool that can place an order, and you never will.
- Name the ACCOUNT in your read-back, alongside the shares and the dollar cost. "5 NVDA, \
$1,054, into Alpaca Paper ...8AUQ" — the user is the only one who can catch a wrong account, \
and they can only catch it if you say it.
- Never say a trade is done, placed, bought, or sold. It isn't. The app places it, only after \
the user confirms, and the app reports the result itself.
- After previewing, your headline must state the SHARES, the DOLLAR COST, and what PERCENT of \
their portfolio it would be, then tell them to say "confirm" or press the button. The dollar \
figure is the safety net: "buy fifty" misheard as "buy fifteen" reads fine, but "$7,000 — 7% of \
your portfolio" is obviously wrong at a glance.
- If preview_trade returns a refusal, say the reason plainly. Do not argue with it, do not \
retry it with different numbers, and do not suggest a workaround.
- PLACED IS NOT FILLED. A market order placed while the exchange is closed sits PENDING until \
the next open, and a pending order has bought nothing. Use get_orders to check. Never call a \
pending order a completed purchase, and if the user asks what they own, say what actually \
settled — not what is queued.

SPEED — the user is watching a spinner, so every tool call costs them:
- Each call is a whole round-trip through you. SEVEN of them to buy one share of Micron is a \
minute of someone's life. Take the shortest path that is still honest.
- To buy or sell, go STRAIGHT to preview_trade. It already returns the cost, the percent of \
their portfolio, their cash afterwards, and how much of the symbol they already hold. Do NOT \
call get_portfolio_summary, get_quote, or list_accounts before it — you would be fetching what \
preview_trade is about to hand you.
- If the user names an account, pass their OWN WORDS to preview_trade's account argument — "my \
second account", "the one ending 8AUQ", "Alpaca". It resolves them for you, and asks the user \
if they're ambiguous. You do not need list_accounts first.
- If you already have a number from an earlier tool call in this conversation, use it. Don't \
re-fetch it.
- Ask for several things at once when they're independent — call the tools in one turn rather \
than waiting for each in sequence.

RULES:
- Never invent a financial figure. Get it from a tool or from the web.
- THE LIVE PRICE BEATS YOUR MEMORY, ALWAYS. Your training has a cutoff and the market does \
not. If a tool returns a price that looks wrong to you, it is your recollection that is stale, \
not the feed. Do NOT tell the user a real quote "looks high" or "is outside its typical range" \
— you did exactly that with Micron at $979, which was the correct live price, and you talked a \
user into doubting true data. Report what the tool says. If you genuinely suspect a data \
problem, search the web and check before you say a word about it.
- Sizing a hypothetical position ("how would X fit?") needs the real total as a denominator, so \
call get_portfolio_summary for that. A REAL order does not — preview_trade already carries it.
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


class _Spend:
    """What the last question actually cost.

    Sonnet 5 intro pricing, per million tokens. Cache WRITES cost 1.25x base and
    cache READS cost 0.1x — which is the entire point of the breakpoint below.
    """

    IN, OUT, WRITE, READ = 2.0, 10.0, 2.50, 0.20

    def __init__(self):
        self.reset()

    def reset(self):
        self.fresh = self.out = self.written = self.read = 0

    def add(self, usage):
        self.fresh += usage.input_tokens
        self.out += usage.output_tokens
        self.written += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.read += getattr(usage, "cache_read_input_tokens", 0) or 0

    @property
    def dollars(self):
        return (
            self.fresh * self.IN
            + self.out * self.OUT
            + self.written * self.WRITE
            + self.read * self.READ
        ) / 1e6

    def __str__(self):
        return (
            f"${self.dollars:.4f}  "
            f"(in {self.fresh:,} · out {self.out:,} · "
            f"cache write {self.written:,} · cache read {self.read:,})"
        )


_spend = _Spend()


def last_cost():
    """Dollars spent on the most recent question."""
    return _spend.dollars


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


def _move_cache_breakpoint(messages):
    """Cache the conversation so far, not just the system prompt.

    A web search injects 30-100k tokens of results into the conversation. Every
    LATER turn — get_portfolio_summary, get_quote, preview_trade, the final answer —
    re-sends that entire history, and without a breakpoint it is re-billed at FULL
    price each time. A four-turn research question pays for the same search results
    four times over.

    Marking the last tool_result block caches everything before it, so the next turn
    reads the history at 10% of input price instead of 100%.

    The old breakpoint is removed first: the API allows at most 4, and a long
    tool-use loop would otherwise run straight past that limit.
    """
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)

    content = messages[-1].get("content")
    # Only our own tool_result messages are plain dicts; assistant turns come back
    # as SDK block objects and can't carry a breakpoint.
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = {"type": "ephemeral"}


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


def answer(transcript: str, on_text=None, on_reset=None) -> str:
    """Run the question to completion. Returns the final answer text.

    on_text   — each chunk of the answer, as it streams.
    on_reset  — the text so far was narration before a tool call ("let me look that
                up"), not the answer. Drop it.
    """
    messages = [{"role": "user", "content": transcript}]
    container = None
    turn_text = []
    _spend.reset()

    for _ in range(MAX_TURNS):
        turn_started = time.perf_counter()
        turn_text = []  # only the LAST turn is the answer; earlier turns narrate

        collect = turn_text.append
        if on_text:
            def collect(chunk):
                turn_text.append(chunk)
                on_text(chunk)

        response, container = _stream_turn(messages, container, collect)
        _spend.add(response.usage)

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
        # answer. Drop it — the panel shows the live tool trace instead, which is
        # more informative than "let me look that up".
        if on_reset:
            on_reset()

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
        # Everything up to here — including any search results — gets read from
        # cache on the next turn instead of re-billed in full.
        _move_cache_breakpoint(messages)

    return "".join(turn_text).strip() or "Sorry, I didn't catch that."


def headline(text: str) -> str:
    """The first paragraph — the answer itself, shown large. The rest is detail."""
    return text.split("\n\n", 1)[0].strip()


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]) or "What is my account balance?"
    print(f"Q: {question}\n")
    started = time.perf_counter()
    result = answer(
        question,
        on_text=lambda t: print(t, end="", flush=True),
        on_reset=lambda: print("\n  [narration dropped — calling tools]\n"),
    )
    print("\n" + "─" * 60)
    print(f"HEADLINE: {headline(result)}")
    if state.STATE["calls"]:
        print("CALLS :", [f"{c['name']}({c['detail']})" if c["detail"] else c["name"]
                          for c in state.STATE["calls"]])
    if state.STATE["sources"]:
        print("SOURCES:", [s["url"] for s in state.STATE["sources"]][:5])
    print(f"TOOK  : {time.perf_counter() - started:.1f}s")
    print(f"COST  : {_spend}")
