"""Thin read-only wrapper around the SnapTrade SDK.

Each function returns plain dicts/lists (not SDK response objects) so the results
can be handed straight to Claude as tool output.
"""

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
    for p in get_positions(account_id):
        value = (p["units"] or 0) * (p["price"] or 0)
        holdings_value += value
        positions.append({**p, "market_value": round(value, 2)})

    total = holdings_value + cash
    for p in positions:
        p["weight_pct"] = round(100 * p["market_value"] / total, 2) if total else 0.0

    positions.sort(key=lambda p: p["market_value"], reverse=True)

    return {
        "total_portfolio_value": round(total, 2),
        "cash": round(cash, 2),
        "cash_pct": round(100 * cash / total, 2) if total else 0.0,
        "holdings_value": round(holdings_value, 2),
        "buying_power": usd.get("buying_power"),
        "positions": positions,
    }


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
