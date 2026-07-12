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
