"""Claude tool definitions and the dispatch table that executes them.

The descriptions carry example phrasings on purpose: Claude routes on the
description text, so that's the main lever for getting the right tool picked.
"""

import time

import snaptrade_client_wrapper as st
import state
import trading

# Anthropic runs these two server-side — no function to implement, no scraping.
# They're what let Snappy answer questions SnapTrade has no data for: private
# companies, news, "what does this company even do".
#
# max_uses is a latency budget, not a cost one. Left uncapped, a portfolio-sizing
# question ran FIVE searches and pulled 106k tokens of results — 27 seconds during
# which the user hears nothing at all. Four is enough to research a company and
# still answer while they're still listening.
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 4}

# Search returns snippets; this reads the actual page. It's the difference between
# "a headline said SpaceX is worth $400/share" and reading the tender-offer page.
WEB_FETCH = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3}

TOOLS = [
    {
        "name": "get_portfolio_summary",
        "description": (
            "The user's whole portfolio: total value, cash, and every holding with its "
            "weight as a percentage. Call this for anything about the shape of the "
            "portfolio — 'what do I own', 'am I too concentrated', 'how much am I worth' "
            "— and ALWAYS call it before sizing a hypothetical position, since you need "
            "the real total as the denominator."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_account_balance",
        "description": (
            "Just the cash and buying power. Use for 'how much cash do I have', "
            "'what's my buying power', 'how much can I invest'. If the question is "
            "about holdings too, prefer get_portfolio_summary."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_symbol_held",
        "description": (
            "Whether the user owns a specific stock, and how many shares. Use for "
            "questions about one company: 'do I own Apple', 'am I holding any Tesla'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol, e.g. AAPL for Apple, TSLA for Tesla.",
                }
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_quote",
        "description": (
            "Live market price of a PUBLICLY TRADED stock, from the user's brokerage. "
            "Use for 'what's Apple trading at', 'price of Nvidia'. This only works for "
            "listed tickers — for a private company (SpaceX, Stripe, OpenAI) there is no "
            "quote, so use web_search to find a secondary-market valuation instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol, e.g. AAPL for Apple, TSLA for Tesla.",
                }
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "list_connections",
        "description": (
            "Which brokerages the user has connected to SnapTrade, and whether each "
            "connection is healthy and allowed to trade. Use for 'which brokerages am I "
            "connected to', 'is my Alpaca connection working', 'can I trade through this'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_supported_brokerages",
        "description": (
            "Every brokerage SnapTrade can connect to, and which allow trading. Use for "
            "'can I connect Wealthsimple', 'does this support Robinhood', 'which brokers "
            "can I actually trade through'. This is about SnapTrade's coverage, not the "
            "user's own accounts."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]

TOOLS.append(
    {
        "name": "get_orders",
        "description": (
            "Orders and whether they've actually filled. Use for 'did my order go "
            "through', 'what orders are pending', 'did I get filled', 'what have I "
            "traded'. IMPORTANT: an order can be PENDING for a long time — a market "
            "order placed while the exchange is closed sits unfilled until the next "
            "open, and a PENDING order has NOT bought anything yet. Never describe a "
            "pending order as a completed purchase."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "open_only": {
                    "type": "boolean",
                    "description": "Only orders still waiting to fill. Default false.",
                }
            },
            "required": [],
        },
    }
)

TOOLS.append(
    {
        "name": "get_all_holdings",
        "description": (
            "Everything the user owns across EVERY connected brokerage, with true "
            "combined weights and total net worth. Use for 'what am I worth in total', "
            "'what do I own everywhere', 'my whole portfolio'. This is the cross-brokerage "
            "view no single brokerage can give you."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
)

TOOLS.append(
    {
        "name": "find_overlap",
        "description": (
            "The same stock held at MORE THAN ONE brokerage. Use for 'am I doubled up', "
            "'do I own the same thing twice', 'am I more concentrated than I think'. "
            "Each brokerage only shows its own slice, so a position split across two "
            "accounts looks small in both and nobody shows the real total. That hidden "
            "concentration is the point of this tool."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
)

TOOLS.append(
    {
        "name": "get_connection_health",
        "description": (
            "Are the brokerage connections healthy, and how stale is the data? Use for "
            "'is my data fresh', 'when did Alpaca last sync', 'is my connection broken'. "
            "Reports hours since last sync and whether a link is disabled."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
)

TOOLS.append(
    {
        "name": "get_activities",
        "description": (
            "What actually HAPPENED in the account: trades, dividends, fees, interest. "
            "Use for 'what dividends have I received', 'how much have I paid in fees', "
            "'what did I trade last week', 'show me my history'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How far back to look. Default 90."}
            },
            "required": [],
        },
    }
)

TOOLS.append(
    {
        "name": "get_balance_history",
        "description": (
            "Portfolio value over time, and the return computed from it. Use for 'how am I "
            "doing', 'how has my portfolio performed', 'am I up or down'. To compare against "
            "a benchmark, get the S&P's return over the SAME dates with web_search. If the "
            "tool says there isn't enough history, say so — do not invent a return."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }
)

TOOLS.append(
    {
        "name": "search_symbols",
        "description": (
            "Look up a ticker by company name. You already know the tickers for household "
            "names (Apple is AAPL, Coca-Cola is KO) — do NOT use this for those. Use it when "
            "you are genuinely unsure, when a name is ambiguous, or to check a symbol exists "
            "before trading it. Results may include LEVERAGED or INVERSE ETFs that merely "
            "mention the company: those carry a WARNING field, and picking one would bet "
            "AGAINST the company the user asked to buy. Never choose one. If two real "
            "companies match, ask which they meant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Company name or partial ticker."}
            },
            "required": ["query"],
        },
    }
)

TOOLS.append(
    {
        "name": "preview_cancel",
        "description": (
            "PROPOSE cancelling an open order. This does NOT cancel it — the user must "
            "confirm. Use for 'cancel my order', 'call back that Apple buy', 'cancel "
            "everything pending'. Call get_orders first to find the order_id. To cancel "
            "several, propose them one at a time — each needs its own confirmation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order_id from get_orders.",
                }
            },
            "required": ["order_id"],
        },
    }
)

TOOLS.append(
    {
        "name": "preview_cancel_all",
        "description": (
            "PROPOSE cancelling EVERY open order at once, or every open order for one "
            "symbol. Cancels nothing — the user must confirm. Use for 'cancel all my "
            "orders', 'cancel everything pending', 'cancel all my Apple orders'. When "
            "you read this back, LIST what would be cancelled — one confirmation for a "
            "whole batch means they have to be able to see what they're agreeing to."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Optional: only cancel open orders for this ticker.",
                }
            },
            "required": [],
        },
    }
)

TOOLS.append(
    {
        "name": "preview_trade",
        "description": (
            "PROPOSE a trade and get its cost and impact. This does NOT place the "
            "order — nothing is bought or sold. Use it whenever the user asks to buy "
            "or sell ('buy 5 shares of Apple', 'sell my Nvidia'). Report the cost and "
            "the resulting portfolio weight, and tell them to say 'confirm'. You have "
            "no way to place an order yourself, and you must never claim one was "
            "placed — the app does that, only after the user confirms out loud."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["BUY", "SELL"],
                    "description": "Whether to buy or sell.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol, e.g. AAPL for Apple.",
                },
                "units": {
                    "type": "number",
                    "description": "Number of shares.",
                },
            },
            "required": ["action", "symbol", "units"],
        },
    }
)


def _preview_trade(action, symbol, units):
    """Claude's only trading tool — and it cannot execute anything.

    A refusal is returned as TEXT rather than raised, so Claude can explain out loud
    why it won't do it instead of the question dying with a stack trace.
    """
    try:
        return trading.propose(action, symbol, units)
    except trading.TradeRefused as e:
        return f"Refused: {e}"


def _preview_cancel(order_id):
    """Propose cancelling an order. Cancels nothing — the user still has to confirm."""
    try:
        return trading.propose_cancel(order_id)
    except trading.TradeRefused as e:
        return f"Refused: {e}"


def _preview_cancel_all(symbol=None):
    """Propose cancelling a whole batch. Cancels nothing."""
    try:
        return trading.propose_cancel_all(symbol)
    except trading.TradeRefused as e:
        return f"Refused: {e}"


DISPATCH = {
    "get_portfolio_summary": st.get_portfolio_summary,
    "get_account_balance": st.get_account_balance,
    "check_symbol_held": st.check_symbol_held,
    "get_quote": st.get_quote,
    "list_connections": st.list_connections,
    "list_supported_brokerages": st.list_supported_brokerages,
    "get_orders": st.get_orders,
    "get_all_holdings": st.get_all_holdings,
    "find_overlap": st.find_overlap,
    "get_connection_health": st.get_connection_health,
    "get_activities": st.get_activities,
    "get_balance_history": st.get_balance_history,
    "search_symbols": st.search_symbols,
    # NOTE: there is deliberately NO "place_trade" and no "cancel_order" here.
    # Execution and cancellation live in trading.py, called by main.py after the
    # user confirms — never by the model. See trading.py's header for why.
    "preview_trade": _preview_trade,
    "preview_cancel": _preview_cancel,
    "preview_cancel_all": _preview_cancel_all,
}


def run_tool(name, tool_input):
    """Execute a tool. Errors come back as text so Claude can explain them aloud."""
    started = time.perf_counter()
    try:
        return str(DISPATCH[name](**tool_input))
    except Exception as e:
        return f"Error calling {name}: {e}"
    finally:
        state.record_call(name, round((time.perf_counter() - started) * 1000))
