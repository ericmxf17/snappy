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
