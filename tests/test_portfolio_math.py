"""The weights are the whole demo.

"How would 5 shares of SpaceX fit into my portfolio?" is answered from
get_portfolio_summary's denominator. If this math drifts, Snappy says a confident,
wrong percentage OUT LOUD — the worst possible failure for this app, because
nothing about it looks broken. So it gets tested against a fake brokerage rather
than trusted.
"""

import pytest

import snaptrade_client_wrapper as st
from conftest import Body


def fake_sdk(monkeypatch, *, cash, positions, currency="USD", buying_power=None):
    """Wire the wrapper to a brokerage that returns exactly what we say."""

    class Accounts:
        def list_user_accounts(self, **kw):
            return Body([{"id": "acct-1", "name": "Paper", "number": "1",
                          "institution_name": "Alpaca", "balance": {"total": 0}}])

        def get_user_account_balance(self, **kw):
            return Body([{"currency": {"code": currency}, "cash": cash,
                          "buying_power": buying_power}])

        def get_all_account_positions(self, **kw):
            return Body([
                {"symbol": {"symbol": {"symbol": s, "description": s}},
                 "units": u, "price": p}
                for s, u, p in positions
            ])

    client = type("C", (), {"account_information": Accounts()})()
    monkeypatch.setattr(st, "_client", client)


def test_weights_sum_with_cash_to_100(monkeypatch):
    fake_sdk(monkeypatch, cash=50_000, positions=[("AAPL", 100, 300.0), ("NVDA", 50, 400.0)])
    s = st.get_portfolio_summary()

    # 30,000 + 20,000 holdings + 50,000 cash = 100,000
    assert s["total_portfolio_value"] == 100_000
    assert s["holdings_value"] == 50_000
    assert s["cash_pct"] == 50.0

    weights = {p["symbol"]: p["weight_pct"] for p in s["positions"]}
    assert weights == {"AAPL": 30.0, "NVDA": 20.0}
    assert sum(weights.values()) + s["cash_pct"] == pytest.approx(100.0)


def test_all_cash_portfolio(monkeypatch):
    """Eric's actual account: $100k, no holdings. The denominator must still work."""
    fake_sdk(monkeypatch, cash=100_000, positions=[])
    s = st.get_portfolio_summary()

    assert s["total_portfolio_value"] == 100_000
    assert s["cash_pct"] == 100.0
    assert s["positions"] == []


def test_empty_portfolio_does_not_divide_by_zero(monkeypatch):
    """A brand-new account has nothing in it. This must not raise."""
    fake_sdk(monkeypatch, cash=0, positions=[])
    s = st.get_portfolio_summary()

    assert s["total_portfolio_value"] == 0
    assert s["cash_pct"] == 0.0


def test_position_with_null_price_is_not_fatal(monkeypatch):
    """Brokerages return null prices for untradeable or halted symbols."""
    fake_sdk(monkeypatch, cash=1_000, positions=[("AAPL", 10, None), ("NVDA", 10, 100.0)])
    s = st.get_portfolio_summary()

    values = {p["symbol"]: p["market_value"] for p in s["positions"]}
    assert values["AAPL"] == 0        # unpriced, not crashed
    assert values["NVDA"] == 1_000
    assert s["total_portfolio_value"] == 2_000


def test_positions_sorted_largest_first(monkeypatch):
    """The panel and the spoken answer both assume biggest-holding-first."""
    fake_sdk(monkeypatch, cash=0, positions=[
        ("SMALL", 1, 10.0), ("BIG", 1, 1000.0), ("MID", 1, 100.0),
    ])
    s = st.get_portfolio_summary()
    assert [p["symbol"] for p in s["positions"]] == ["BIG", "MID", "SMALL"]


def test_the_spacex_question(monkeypatch):
    """The actual demo, computed the way Claude is told to compute it.

    Claude is instructed to call get_portfolio_summary for the denominator and
    size the position against total_portfolio_value. Pin that arithmetic.
    """
    fake_sdk(monkeypatch, cash=100_000, positions=[])
    total = st.get_portfolio_summary()["total_portfolio_value"]

    shares, price = 5, 145.42          # SPCX, post-IPO
    weight = 100 * (shares * price) / total

    assert round(weight, 2) == 0.73    # "roughly 0.7 percent"


def test_fractional_shares(monkeypatch):
    """Alpaca supports fractional shares; units is a float, not an int."""
    fake_sdk(monkeypatch, cash=0, positions=[("AAPL", 0.5, 300.0)])
    s = st.get_portfolio_summary()

    assert s["positions"][0]["market_value"] == 150.0
    assert s["positions"][0]["weight_pct"] == 100.0


# --- SnapTrade says EXECUTED and also says you own nothing -------------------

def _stub(monkeypatch, positions, orders):
    import snaptrade_client_wrapper as st
    monkeypatch.setattr(st, "get_positions", lambda account_id=None: positions)
    monkeypatch.setattr(st, "get_orders", lambda open_only=False, account_id=None: orders)
    monkeypatch.setattr(st, "_default_account_id", lambda: "acct-1")
    monkeypatch.setattr(st, "list_accounts", lambda: [{
        "account_id": "acct-1", "label": "Alpaca Paper ...0001", "is_paper": True,
        "connection_id": "c1", "institution": "Alpaca Paper", "name": "Alpaca Paper",
        "number": "PA0001", "ordinal": 1, "total_value": 10_000,
        "holdings_synced_at": "2026-07-13T00:04:42+00:00",
        "holdings_sync_hours_ago": 14.4,
    }])
    return st


FILLED_NVDA = {"order_id": "o1", "symbol": "NVDA", "action": "BUY", "status": "EXECUTED",
               "units": 45.0, "filled_units": 45.0, "execution_price": 210.0,
               "order_type": "Market", "placed_at": "", "executed_at": ""}


def test_a_filled_order_missing_from_positions_is_reported(monkeypatch):
    """The platform bug, reproduced: EXECUTED with a fill price, and an empty account.

    Observed 13 Jul 2026 — seven orders filled at the open, confirmed in Alpaca's own
    dashboard, and SnapTrade's positions endpoint stayed empty for over 40 minutes.
    An app that trusts positions alone tells the user they own nothing. They do not.
    """
    st = _stub(monkeypatch, positions=[], orders=[FILLED_NVDA])

    gaps = st.unsynced_fills("acct-1")
    assert len(gaps) == 1
    assert gaps[0]["symbol"] == "NVDA"
    assert gaps[0]["missing_units"] == 45.0
    assert gaps[0]["shown_in_positions"] == 0


def test_unsynced_shares_are_never_added_into_the_weights(monkeypatch):
    """The trap. Merging them looks helpful and double-counts the moment the sync lands.

    A confident wrong number is worse than an admitted gap, so the fills are reported
    ALONGSIDE the positions and never folded into them.
    """
    st = _stub(monkeypatch, positions=[], orders=[FILLED_NVDA])
    monkeypatch.setattr(st, "get_account_balance",
                        lambda account_id=None: [{"currency": "USD", "cash": 10_000.0,
                                                  "buying_power": 10_000.0}])

    summary = st.get_portfolio_summary("acct-1")
    assert summary["positions"] == [], "unsynced fills must not become positions"
    assert summary["holdings_value"] == 0.0
    assert summary["total_portfolio_value"] == 10_000.0, "9,450 of NVDA must NOT be added in"
    assert summary["unsynced_fills"], "but it must still SAY the shares exist"
    assert "own nothing" in summary["stale_note"]


def test_a_position_bigger_than_the_orders_we_can_see_is_not_a_gap(monkeypatch):
    """The orders endpoint may not reach back far enough to explain an old holding.

    A position LARGER than the fills we can see is normal. Only fills the position
    fails to account for are the bug — the reverse would flag every mature account.
    """
    st = _stub(
        monkeypatch,
        positions=[{"symbol": "NVDA", "units": 100.0, "price": 210.0,
                    "description": "NVIDIA", "average_purchase_price": 200.0, "open_pnl": 0}],
        orders=[FILLED_NVDA],   # only 45 of the 100 are explained by visible orders
    )
    assert st.unsynced_fills("acct-1") == []


def test_a_sold_position_does_not_look_unsynced(monkeypatch):
    """Buy 45, sell 45, hold nothing. That is not a missing fill."""
    sold = {**FILLED_NVDA, "order_id": "o2", "action": "SELL"}
    st = _stub(monkeypatch, positions=[], orders=[FILLED_NVDA, sold])
    assert st.unsynced_fills("acct-1") == []
