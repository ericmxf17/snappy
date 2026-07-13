# SnapTrade — findings from building Snappy

Notes from building [Snappy](README.md), a voice assistant that reads and trades across
brokerages via SnapTrade. Everything below was **reproduced on 13 July 2026** against a live
account (`snaptrade-python-sdk`, two Alpaca Paper connections).

Ordered by how much I'd care if I worked on the API.

| # | Finding | Severity |
|---|---------|----------|
| 1 | Symbol search has no relevance ranking, and "buy nvidia" resolves to a 2× **short** ETF | **Safety** |
| 2 | The Python SDK ships methods that are `410 Gone` | Developer experience |
| 3 | The CLI cannot create a trade-enabled connection | Developer experience |
| 4 | `get_user_account_return_rates` → `403` | Question, not a claim |

---

## 1. Symbol search ranks a leveraged inverse ETF above the company

**This is the one I'd fix first, because it is not a UX bug — it can invert a user's position.**

`reference_data.symbol_search_user_account(substring="nvidia")` returns, in this order:

```
1. NVD    GraniteShares 2x Short Nvidia ETF USD
2. NVDO   Leverage Shares 2x Capped Accelerated NVIDIA Monthly ETF
3. NVPS   PurePlay Nvidia Ecosystem Picks & Shovels Index ETF
4. NVDA   NVIDIA Corporation
5. NVDL   Graniteshares 2x Long Nvidia Daily ETF
```

NVDA is **fourth**. The top result is a **2× leveraged inverse** ETF.

The results look like a raw substring match with no relevance ordering at all. `"apple"` behaves
the same way — it returns Dr Pepper **Snapple**, Maui Land & **Pineapple**, and a leveraged
natural-gas ETF, with AAPL buried well down the list.

**Why it's a safety issue and not a papercut.** Any natural-language layer on top of this API —
a voice assistant, an LLM tool call, a search box with an "I'm feeling lucky" path — will take
the first result. A user who says *"buy me some Nvidia"* then **shorts Nvidia at 2× leverage**.
The order executes cleanly. Nothing errors. The user finds out when the position moves the wrong
way.

**What Snappy does about it** ([`snaptrade_client_wrapper.py`](src/snaptrade_client_wrapper.py), `search_symbols`):
re-ranks client-side — exact ticker match first, common stock ahead of ETFs, and anything whose
description matches `2x / 3x / short / inverse / bear / bull / leveraged` is pushed last **and
tagged with an explicit `WARNING` field** that the model is told never to silently accept.

```
1. NVDA   NVIDIA Corporation
2. NVPS   PurePlay Nvidia Ecosystem Picks & Shovels Index ETF
3. NVD    GraniteShares 2x Short Nvidia ETF          <-- FLAGGED
4. NVDW   Tadr 1.75x Long Nvidia Weekly ETF          <-- FLAGGED
```

Every consumer of this endpoint has to write that, or ship the bug. It belongs server-side.

**Suggestion:** rank exact ticker matches first, and either expose a `security_type` /
`is_leveraged` / `is_inverse` flag or let callers filter to common stock. The data is evidently
there — it's in the description string.

---

## 2. The Python SDK ships methods that return `410 Gone`

These are importable, callable, fully typed — and dead:

```python
account_information.get_all_user_holdings(...)       # 410 Gone
transactions_and_reporting.get_activities(...)       # 410 Gone
```

The SDK logs `WARNING - ... is deprecated` and then issues the request anyway, which fails.

The deprecation notice is good. The problem is that **the SDK's surface and the API's surface
disagree**: autocomplete offers a method that cannot succeed. `get_all_user_holdings` in
particular is exactly the method you'd reach for when writing a multi-brokerage app — it's the
first thing I tried, and it looked like my bug for a while before I checked the status code.

**Suggestion:** if an endpoint is gone, remove it from the SDK (or make it raise a typed
`DeprecatedEndpointError` at call time that names the replacement). A `410` after a `WARNING` log
line is a slow way to learn.

For what it's worth, the replacement path works fine — Snappy calls
`get_all_account_positions` per account and aggregates client-side.

---

## 3. The CLI cannot create a trade-enabled connection

```
$ snaptrade connect --help
Usage: snaptrade connect [options]

Options:
  --broker <slug>  Brokerage slug to connect
  -h, --help       display help for command
```

There is no `--connection-type`. The CLI always creates a **read-only** connection.

This cost me real time. I reconnected both brokerages through the CLI, everything looked healthy,
and every trade failed — because the connections were read-only and nothing in the CLI output said
so. The fix was to stop using the CLI and call the API directly with
`connection_type="trade"` ([`connect.py`](src/connect.py)).

**Suggestion:** add `--connection-type read|trade` to `snaptrade connect`, and print the
connection type in `snaptrade connections list`. Right now the one property that determines
whether the connection can do anything is invisible from the CLI.

*(Aside: the latest published CLI version was broken when I tried it; I pinned to
`@snaptrade/snaptrade-cli@0.1.38`.)*

---

## 4. `get_user_account_return_rates` returns `403` — is this entitlement?

```python
account_information.get_user_account_return_rates(account_id=...)   # 403 Forbidden
```

I'm listing this as a **question, not a defect** — I assume it's gated by plan tier rather than
broken. If so, a `403` with a body explaining *"not available on your plan"* would be clearer than
a bare Forbidden, which is indistinguishable from a signing or permissions error. I spent a while
double-checking my HMAC before concluding it probably wasn't me.

---

## What worked well

Worth saying, because a list of complaints is not a fair picture of the API:

- **The two-phase trade flow is genuinely well designed.** `get_order_impact` returns a validated
  `trade_id`, and `place_order` accepts *only* that id — no symbol, no size, no account. That makes
  it structurally impossible for the order that executes to differ from the order the user approved.
  Snappy's entire prompt-injection defence is built on that property: the LLM has no tool that can
  execute, and the id it previewed is the only thing that can be filled. I don't think that was an
  accident, and it's the reason I could let a language model near a brokerage account at all.
- Connection health and `holdings_synced_at` made staleness debuggable instead of mysterious.
- HMAC signing worked first try from the SDK.

---

*Eric — [github.com/ericmxf17/snappy](https://github.com/ericmxf17/snappy)*
