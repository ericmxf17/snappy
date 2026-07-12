"""Thin read-only wrapper around the SnapTrade SDK.

Each function returns plain dicts/lists (not SDK response objects) so the results
can be handed straight to Claude as tool output.
"""

from datetime import datetime, timedelta, timezone

import config
from snaptrade_client import SnapTrade

_client = SnapTrade(
    client_id=config.SNAPTRADE_CLIENT_ID,
    consumer_key=config.SNAPTRADE_CONSUMER_KEY,
)

_USER = {
    "user_id": config.SNAPTRADE_USER_ID,
    "user_secret": config.SNAPTRADE_USER_SECRET,
}


def list_accounts():
    """All connected brokerage accounts."""
    accounts = _client.account_information.list_user_accounts(**_USER).body
    return [
        {
            "account_id": a.get("id"),
            "name": a.get("name"),
            "number": a.get("number"),
            "institution": a.get("institution_name"),
            "total_value": (a.get("balance") or {}).get("total"),
        }
        for a in accounts
    ]


def _default_account_id():
    accounts = list_accounts()
    if not accounts:
        raise RuntimeError("No brokerage accounts are connected to SnapTrade.")
    return accounts[0]["account_id"]


def get_account_balance(account_id=None):
    """Cash and buying power for one account."""
    account_id = account_id or _default_account_id()
    balances = _client.account_information.get_user_account_balance(
        account_id=account_id, **_USER
    ).body
    return [
        {
            "currency": (b.get("currency") or {}).get("code"),
            "cash": b.get("cash"),
            "buying_power": b.get("buying_power"),
        }
        for b in balances
    ]


def get_positions(account_id=None):
    """Current equity holdings for one account."""
    account_id = account_id or _default_account_id()
    result = _client.account_information.get_all_account_positions(
        account_id=account_id, **_USER
    ).body

    # The endpoint returns either a bare list of positions or an object with a
    # "positions" key depending on the brokerage, so normalize both shapes.
    raw = result.get("positions", []) if isinstance(result, dict) else result

    positions = []
    for p in raw:
        symbol = (p.get("symbol") or {}).get("symbol") or {}
        positions.append(
            {
                "symbol": symbol.get("symbol"),
                "description": symbol.get("description"),
                "units": p.get("units"),
                "price": p.get("price"),
                "average_purchase_price": p.get("average_purchase_price"),
                "open_pnl": p.get("open_pnl"),
            }
        )
    return positions


def check_symbol_held(symbol, account_id=None):
    """Whether a given ticker is held, and how many shares.

    Derived client-side from get_positions rather than a dedicated endpoint.
    """
    target = symbol.strip().upper()
    for p in get_positions(account_id):
        if (p["symbol"] or "").upper() == target:
            return {"held": True, **p}
    return {"held": False, "symbol": target}


def get_quote(symbol, account_id=None):
    """Live market quote for a ticker."""
    account_id = account_id or _default_account_id()
    quotes = _client.trading.get_user_account_quotes(
        account_id=account_id,
        symbols=symbol.strip().upper(),
        use_ticker=True,
        **_USER,
    ).body
    return [
        {
            "symbol": (q.get("symbol") or {}).get("symbol"),
            "bid": q.get("bid_price"),
            "ask": q.get("ask_price"),
            "last": q.get("last_trade_price"),
        }
        for q in quotes
    ]


def get_portfolio_summary(account_id=None):
    """Holdings with their weights, plus cash and total value.

    The weights are computed here rather than left to the model: position sizing
    is the headline question ("how would X fit?"), and that math shouldn't be
    able to drift.
    """
    account_id = account_id or _default_account_id()

    balances = get_account_balance(account_id)
    usd = next((b for b in balances if b["currency"] == "USD"), balances[0] if balances else {})
    cash = usd.get("cash") or 0.0

    positions = []
    holdings_value = 0.0
    unpriced = []
    for p in get_positions(account_id):
        # A halted or illiquid symbol comes back with no price. Counting it as $0
        # quietly shrinks the total, which INFLATES every other weight — and the
        # total is the denominator behind "how would X fit into my portfolio". A
        # wrong number said confidently is worse than an admitted gap, so the gap
        # is reported rather than buried.
        if not p["price"]:
            unpriced.append(p["symbol"])
        value = (p["units"] or 0) * (p["price"] or 0)
        holdings_value += value
        positions.append({**p, "market_value": round(value, 2)})

    total = holdings_value + cash
    for p in positions:
        p["weight_pct"] = round(100 * p["market_value"] / total, 2) if total else 0.0

    positions.sort(key=lambda p: p["market_value"], reverse=True)

    # Cost basis and open P&L come back from the brokerage and are what let Snappy
    # say "you're up 18% on this" instead of only "this is 12% of your portfolio".
    for p in positions:
        basis = p.get("average_purchase_price")
        if basis and p.get("units"):
            cost = basis * p["units"]
            p["cost_basis"] = round(cost, 2)
            p["unrealized_pnl"] = round(p["market_value"] - cost, 2)
            p["unrealized_pct"] = round(100 * (p["market_value"] - cost) / cost, 2) if cost else None

    summary = {
        "total_portfolio_value": round(total, 2),
        "cash": round(cash, 2),
        "cash_pct": round(100 * cash / total, 2) if total else 0.0,
        "holdings_value": round(holdings_value, 2),
        "buying_power": usd.get("buying_power"),
        "positions": positions,
    }

    # Pending orders are money already committed. A portfolio read that ignores them
    # is wrong about what the user will actually own tomorrow morning — and sizing a
    # new position against a stale cash figure double-spends it.
    try:
        pending = get_orders(open_only=True, account_id=account_id)
    except Exception:
        pending = []
    if pending:
        committed = sum(
            (o["units"] or 0) * (o["execution_price"] or 0)
            for o in pending
            if o["action"] == "BUY"
        )
        summary["pending_orders"] = pending
        summary["pending_note"] = (
            f"{len(pending)} order(s) are still open and have NOT filled. They are not in "
            f"the positions above. Factor them in when sizing anything new — the cash they "
            f"will consume is still counted as available here."
        )
        if committed:
            summary["cash_after_pending_fill"] = round(cash - committed, 2)

    return summary
    if unpriced:
        summary["warning"] = (
            f"No live price for {', '.join(unpriced)}, so they are counted as $0. "
            f"The total and every percentage below are therefore UNDERSTATED and "
            f"OVERSTATED respectively. Say so rather than quoting the percentages "
            f"as if they were exact."
        )
    return summary


def resolve_symbol(symbol, account_id=None):
    """Ticker -> SnapTrade's universal_symbol_id, plus the live price.

    One call gets both, because the quote endpoint already returns the symbol
    object with its id — and a trade preview needs the id, not the ticker.
    """
    account_id = account_id or _default_account_id()
    quotes = _client.trading.get_user_account_quotes(
        account_id=account_id,
        symbols=symbol.strip().upper(),
        use_ticker=True,
        **_USER,
    ).body
    if not quotes:
        raise RuntimeError(f"{symbol.upper()} isn't a symbol this brokerage can trade.")

    q = quotes[0]
    sym = dict(q.get("symbol") or {})
    return {
        "universal_symbol_id": sym.get("id"),
        "symbol": sym.get("symbol"),
        "description": sym.get("description"),
        "price": q.get("last_trade_price"),
    }


def preview_trade(action, symbol, units, account_id=None):
    """Ask the brokerage what this order WOULD do. Places nothing.

    Returns a trade_id minted by SnapTrade from these exact parameters. That id is
    the only thing place_previewed_trade will accept — which is what makes it
    impossible for the order that executes to differ from the order that was
    previewed and read aloud.
    """
    account_id = account_id or _default_account_id()
    resolved = resolve_symbol(symbol, account_id)

    impact = _client.trading.get_order_impact(
        account_id=account_id,
        action=action.upper(),                 # BUY | SELL
        universal_symbol_id=resolved["universal_symbol_id"],
        order_type="Market",
        time_in_force="Day",
        units=float(units),
        **_USER,
    ).body

    trade = dict(impact.get("trade") or {})
    return {
        "trade_id": trade.get("id"),
        "action": action.upper(),
        "symbol": resolved["symbol"],
        "description": resolved["description"],
        "units": float(units),
        "price": resolved["price"],
        "estimated_cost": round(float(units) * (resolved["price"] or 0), 2),
        "estimated_commission": impact.get("estimated_commission"),
        "remaining_cash": impact.get("remaining_cash"),
    }


def place_previewed_trade(trade_id):
    """Execute a previously previewed trade. THE ONLY EXECUTION PATH IN THIS APP.

    Takes an id, never raw order parameters — so nothing can slip a different
    symbol or size in between the preview and the fill. (The SDK also exposes
    place_force_order, which skips validation entirely. It is never called.)
    """
    result = _client.trading.place_order(trade_id=trade_id, **_USER).body

    fill = {
        "order_id": result.get("brokerage_order_id"),
        "status": result.get("status"),
        "symbol": ((result.get("universal_symbol") or {}).get("symbol")),
        "units": result.get("total_quantity") or result.get("units"),
        "filled_units": result.get("filled_quantity"),
        "price": result.get("execution_price") or result.get("price"),
    }
    # Drop the empty keys. A market order that hasn't filled yet comes back with
    # units and price as null, and the caller merges this over the preview — so
    # leaving them in would BLANK OUT the numbers we already know from the preview.
    # That is exactly what happened: `units` became None, formatting the success
    # message threw, and the app told the user the order had failed. It had not.
    return {k: v for k, v in fill.items() if v is not None}


def _num(v):
    """SnapTrade sends quantities as decimal strings ("5.000000000000000000")."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_orders(open_only=False, account_id=None):
    """Orders and their fill status.

    Without this, Snappy could place an order and then have no idea what became of
    it — and a market order placed outside trading hours sits PENDING until the next
    open, so "did it fill?" is the very next question a user asks.
    """
    account_id = account_id or _default_account_id()
    orders = _client.account_information.get_user_account_orders(
        account_id=account_id, **_USER
    ).body

    out = []
    for o in orders:
        o = dict(o)
        status = o.get("status")
        if open_only and status not in ("PENDING", "OPEN", "ACCEPTED", "PARTIAL"):
            continue
        out.append(
            {
                "order_id": o.get("brokerage_order_id"),
                "symbol": (o.get("universal_symbol") or {}).get("symbol"),
                "action": o.get("action"),
                "status": status,
                "units": _num(o.get("total_quantity")),
                "filled_units": _num(o.get("filled_quantity")),
                "order_type": o.get("order_type"),
                "execution_price": _num(o.get("execution_price")),
                "placed_at": o.get("time_placed"),
                "executed_at": o.get("time_executed"),
            }
        )
    return out


def cancel_order(order_id, account_id=None):
    """Cancel a still-open order. A mutation — gated behind confirmation in trading.py."""
    account_id = account_id or _default_account_id()
    result = _client.trading.cancel_order(
        account_id=account_id, brokerage_order_id=order_id, **_USER
    ).body
    return {"order_id": order_id, "status": dict(result).get("status")}


def get_all_holdings():
    """Every position across EVERY connected brokerage, with true combined weights.

    This is the feature that cannot exist without SnapTrade. Alpaca can tell you what
    you hold at Alpaca; it has no idea what you hold at Wealthsimple. The unification
    layer is the whole product, and this is what it buys you.

    (SnapTrade's own get_all_user_holdings returns 410 Gone, so it's assembled here
    from list_user_accounts + per-account positions.)
    """
    accounts = list_accounts()
    if not accounts:
        raise RuntimeError("No brokerage accounts are connected to SnapTrade.")

    books, total_cash, total_holdings = [], 0.0, 0.0
    combined = {}  # symbol -> the true, cross-account position

    for account in accounts:
        aid = account["account_id"]
        try:
            balances = get_account_balance(aid)
            positions = get_positions(aid)
        except Exception as e:  # one broken connection must not blind the others
            books.append({**account, "error": str(e)})
            continue

        usd = next((b for b in balances if b["currency"] == "USD"), balances[0] if balances else {})
        cash = usd.get("cash") or 0.0
        total_cash += cash

        value = 0.0
        for p in positions:
            worth = (p["units"] or 0) * (p["price"] or 0)
            value += worth
            slot = combined.setdefault(
                p["symbol"], {"symbol": p["symbol"], "units": 0.0, "market_value": 0.0, "accounts": []}
            )
            slot["units"] += p["units"] or 0
            slot["market_value"] = round(slot["market_value"] + worth, 2)
            slot["accounts"].append({"brokerage": account["institution"], "units": p["units"]})

        total_holdings += value
        books.append({**account, "cash": round(cash, 2), "holdings_value": round(value, 2),
                      "positions": positions})

    net_worth = total_cash + total_holdings
    for slot in combined.values():
        slot["weight_pct"] = round(100 * slot["market_value"] / net_worth, 2) if net_worth else 0.0
        slot["held_in"] = len(slot["accounts"])

    holdings = sorted(combined.values(), key=lambda h: h["market_value"], reverse=True)

    return {
        "net_worth": round(net_worth, 2),
        "total_cash": round(total_cash, 2),
        "total_holdings_value": round(total_holdings, 2),
        "account_count": len(accounts),
        "accounts": books,
        "combined_holdings": holdings,
    }


def find_overlap():
    """The same stock held at MORE THAN ONE brokerage.

    The number nobody can see. Each brokerage shows you its own slice, so a position
    split across two accounts looks small twice and nobody shows you the real total.
    That is a concentration risk you cannot detect from inside either account.
    """
    book = get_all_holdings()
    doubled = [h for h in book["combined_holdings"] if h["held_in"] > 1]

    return {
        "net_worth": book["net_worth"],
        "account_count": book["account_count"],
        "overlapping": doubled,
        "note": (
            "No symbol is held in more than one account."
            if not doubled
            else (
                f"{len(doubled)} symbol(s) are held at more than one brokerage. Neither "
                f"brokerage can show the combined weight — each sees only its own slice. "
                f"That is real, invisible concentration."
            )
        ),
    }


def search_symbols(query, account_id=None):
    """Company name -> tickers. "dr pepper" -> KDP, DPS.

    Not breadth — a correctness fix, and a safety one. Voice gives you company NAMES;
    preview_trade needs a ticker. Without this the model guesses.

    And the raw endpoint's ranking is dangerous. "nvidia" returns, in order:

        NVD   GraniteShares 2x SHORT Nvidia ETF     <-- an INVERSE ETF
        NVDW  Tadr 1.75x Long Nvidia Weekly ETF
        ...   NVDA is not even in the top two

    So "buy some Nvidia", naively resolved, shorts the thing you meant to buy at 2x
    leverage. Ordinary shares are therefore ranked ahead of ETFs, and anything
    leveraged or inverse is flagged rather than quietly offered.
    """
    account_id = account_id or _default_account_id()
    want = query.strip()
    results = _client.reference_data.symbol_search_user_account(
        account_id=account_id, substring=want, **_USER
    ).body

    LEVERAGED = ("2x", "3x", "1.5x", "1.75x", "short", "inverse", "bear", "bull", "leveraged")

    found = []
    for r in results:
        s = dict(r)
        symbol = s.get("symbol") or ""
        description = s.get("description") or ""
        kind = (s.get("type") or {}).get("description") or ""
        risky = any(w in description.lower() for w in LEVERAGED)

        found.append(
            {
                "symbol": symbol,
                "description": description,
                "exchange": (s.get("exchange") or {}).get("code"),
                "type": kind,
                "currency": (s.get("currency") or {}).get("code"),
                # Loud, because it's the difference between owning a company and
                # betting against it with borrowed money.
                **({"WARNING": "LEVERAGED OR INVERSE ETF — not the underlying company"}
                   if risky else {}),
            }
        )

    # SnapTrade's search is a raw substring match with NO relevance ranking. Searching
    # "apple" returns, in order: Dr Pepper SNAPPLE, Maui Land & PINEAPPLE, a 2x
    # leveraged natural-gas ETF... with AAPL buried 7th out of 17. So the ranking is
    # done here, or "buy some Apple" buys pineapples.
    def rank(item):
        symbol = (item["symbol"] or "").upper()
        description = (item["description"] or "").lower()
        common = "Common Stock" in (item["type"] or "")
        return (
            symbol != want.upper(),               # an exact ticker match always wins
            "WARNING" in item,                    # leveraged/inverse dead last
            not (description.startswith(want.lower()) and common),  # "Apple Inc." > "Pineapple Energy"
            not description.startswith(want.lower()),
            not common,                           # real shares before ETFs
            len(symbol),                          # AAPL before AAPL42 / AAPL26
            # Then the plain company wins: "Apple Inc." is shorter than "Apple
            # Hospitality REIT, Inc.", and "Keurig Dr Pepper Inc." is shorter than
            # "Dr Pepper Snapple Group, Inc Dr Pepper Snapple Group, Inc".
            len(description),
        )

    return sorted(found, key=rank)[:8]


def get_connection_health():
    """Are the brokerage links healthy, and how stale is the data?

    Brokerage connections break and data goes stale — SnapTrade's real, unglamorous
    pain point, and the reason a unification layer needs to be watched, not trusted.
    """
    out = []
    for conn in _client.connections.list_brokerage_authorizations(**_USER).body:
        conn = dict(conn)
        updated = conn.get("updated_date")
        stale_hours = None
        if updated:
            try:
                then = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                stale_hours = round(
                    (datetime.now(timezone.utc) - then).total_seconds() / 3600, 1
                )
            except ValueError:
                pass
        out.append(
            {
                "connection_id": conn.get("id"),
                "brokerage": (conn.get("brokerage") or {}).get("name"),
                "disabled": conn.get("disabled"),
                "disabled_since": conn.get("disabled_date"),
                "type": conn.get("type"),
                "last_synced": updated,
                "hours_since_sync": stale_hours,
                "stale": bool(stale_hours and stale_hours > 24),
            }
        )
    return out


def refresh_connection(connection_id):
    """Force a re-sync with the brokerage. A MUTATION — gated in trading.py."""
    _client.connections.refresh_brokerage_authorization(
        authorization_id=connection_id, **_USER
    )
    return {"connection_id": connection_id, "refreshed": True}


ACTIVITY_TYPES = ("BUY", "SELL", "DIVIDEND", "FEE", "CONTRIBUTION", "WITHDRAWAL", "INTEREST")


def get_activities(days=90, account_id=None):
    """Trades, dividends, fees — what actually happened in the account.

    Paginated: the endpoint returns {data, pagination}, so page until it runs dry.
    """
    account_id = account_id or _default_account_id()
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    rows, offset = [], 0
    while True:
        body = dict(
            _client.account_information.get_account_activities(
                account_id=account_id, start_date=start, offset=offset, limit=200, **_USER
            ).body
        )
        page = body.get("data") or []
        for a in page:
            a = dict(a)
            rows.append(
                {
                    "type": a.get("type"),
                    "symbol": ((a.get("symbol") or {}).get("symbol")),
                    "description": a.get("description"),
                    "units": _num(a.get("units")),
                    "price": _num(a.get("price")),
                    "amount": _num(a.get("amount")),
                    "currency": (a.get("currency") or {}).get("code"),
                    "date": a.get("trade_date") or a.get("settlement_date"),
                }
            )
        pagination = body.get("pagination") or {}
        offset += len(page)
        if not page or offset >= (pagination.get("total") or 0):
            break

    totals = {}
    for r in rows:
        kind = (r["type"] or "OTHER").upper()
        totals[kind] = round(totals.get(kind, 0) + (r["amount"] or 0), 2)

    return {"since": str(start), "count": len(rows), "totals_by_type": totals, "activities": rows}


def get_balance_history(account_id=None):
    """Portfolio value over time, so performance can be computed rather than guessed."""
    account_id = account_id or _default_account_id()
    body = dict(
        _client.account_information.get_account_balance_history(
            account_id=account_id, **_USER
        ).body
    )

    # Newest first from the API, and zeros before the account existed — those aren't
    # a 100% drawdown, they're the absence of an account. Dropping them is the
    # difference between "you're flat" and "you lost everything".
    points = [
        {"date": str(dict(p).get("date")), "value": _num(dict(p).get("total_value"))}
        for p in (body.get("history") or [])
    ]
    points = [p for p in points if p["value"]]
    points.sort(key=lambda p: p["date"])

    if len(points) < 2:
        return {
            "points": points,
            "note": (
                "Not enough history to compute a return — this account has only been "
                "connected for a day or so. Say that plainly rather than quoting a "
                "meaningless number."
            ),
        }

    first, last = points[0], points[-1]
    change = last["value"] - first["value"]
    return {
        "points": points,
        "from": first["date"],
        "to": last["date"],
        "start_value": first["value"],
        "end_value": last["value"],
        "change": round(change, 2),
        "return_pct": round(100 * change / first["value"], 2) if first["value"] else None,
    }


def list_connections():
    """The user's brokerage connections and their health."""
    conns = _client.connections.list_brokerage_authorizations(**_USER).body
    return [
        {
            "connection_id": c.get("id"),
            "brokerage": (c.get("brokerage") or {}).get("name"),
            "disabled": c.get("disabled"),
            # "read" means the connection can't place trades, only view data.
            "type": c.get("type"),
            "created": c.get("created_date"),
        }
        for c in conns
    ]


def list_supported_brokerages():
    """Every brokerage SnapTrade can connect to, and whether it allows trading.

    Answers "can I connect <broker>?" — SnapTrade's actual product question.
    """
    brokers = _client.reference_data.list_all_brokerages().body
    return [
        {
            "name": b.get("name"),
            "slug": b.get("slug"),
            "allows_trading": b.get("allows_trading"),
            "enabled": b.get("enabled"),
        }
        for b in brokers
    ]
