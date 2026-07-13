# SnapTrade — findings from building Snappy

Notes from building [Snappy](README.md), a voice assistant that reads and trades across
brokerages via SnapTrade. Everything below was **reproduced on 13 July 2026** against a live
account (`snaptrade-python-sdk`, two Alpaca Paper connections).

**Every finding here reproduces with a ~120-line script that imports the official SDK and nothing
else** — no wrapper, no parser, none of my application code: [`repro/repro.py`](repro/repro.py).
Run it against your own account. That separation is deliberate; see the note at the bottom.

Ordered by how much I'd care if I worked on the API.

| # | Finding | Severity |
|---|---------|----------|
| 1 | Symbol search has no relevance ranking, and "buy nvidia" resolves to a 2× **short** ETF | **Safety** |
| 2 | The Python SDK ships methods that are `410 Gone` | Developer experience |
| 3 | Reads randomly stall for 20–30 seconds | Performance |
| 4 | The CLI cannot create a trade-enabled connection | Developer experience |
| 5 | `get_user_account_return_rates` → `403` | Question, not a claim |

---

## 1. Symbol search ranks a leveraged inverse ETF above the company

**This is the one I'd fix first, because it is not a UX bug — it can invert a user's position.**

`reference_data.symbol_search_user_account(substring=...)` — **the same two queries, minutes apart,
on the same account:**

```
                    run A                                run B
"nvidia"   1. NVD    2x SHORT Nvidia ETF        1. NVD    2x SHORT Nvidia ETF
           2. NVDW   1.75x Long Nvidia          2. NVDO   2x Capped Accel NVIDIA
           3. NVDQ   2x INVERSE Nvidia          3. NVPS   Nvidia Picks & Shovels
           4. NVDX   2x LNG NVIDIA              4. NVDA   NVIDIA Corporation
           5. NVDO   2x Capped Accel NVIDIA     5. NVDL   2x Long Nvidia
           6. NVPS   Nvidia Picks & Shovels     6. NVDS   1.5x Short Nvidia
           -> NVDA NOT IN TOP SIX               -> NVDA fourth

"apple"    1. MLP    Maui Land & Pineapple      1. MLP    Maui Land & Pineapple
           2. DPS    Dr Pepper Snapple          2. DPS    Dr Pepper Snapple
           3. PEGY   Pineapple Energy           3. APLE   Apple Hospitality REIT
           4. PAPL   Pineapple Financial        4. AAPX   2X Liquid Nat Gas Apple
           5. AAPX   2X Liquid Nat Gas Apple    5. PEGY   Pineapple Energy
           6. AAPY   Kurv Yield Premium Apple   6. AAPL   Apple Inc.
           -> AAPL NOT IN TOP SIX               -> AAPL sixth
```

Three things fall out of this, and the third is the one I'd act on:

1. **It reads as a raw substring match with no relevance ranking.** "apple" matches
   "Pine**apple**" and "Sn**apple**" exactly as well as it matches Apple Inc.

2. **The ordering isn't even stable between calls.** NVDA came back *absent*, then *fourth*. So a
   naive integration doesn't fail predictably — it fails *differently each time*, which is far
   harder to catch in testing than a consistently wrong answer.

3. **But one thing never moves: result #1 is never the company.** Across every run, "nvidia"
   returns the **2× short** first and "apple" returns a **pineapple company** first. That part is
   perfectly reproducible — and it's the part that matters, because result #1 is what an
   integration takes.

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

## 3. Reads randomly stall for 20–30 seconds

Six cold rounds, alternating `get_user_account_balance` and `get_all_account_positions` across two
accounts. Times in seconds, straight from [`repro/run.txt`](repro/run.txt):

```
      acct1: balance=  0.3  positions=  0.3  |  acct2: balance= 0.3  positions=  0.3
      acct1: balance=  0.2  positions=  0.4  |  acct2: balance= 0.3  positions=  0.4
      acct1: balance= 22.3  positions=  0.4  |  acct2: balance= 1.4  positions=  0.4
      acct1: balance=  0.2  positions= 30.6  |  acct2: balance= 0.3  positions=  0.3
      acct1: balance=  2.3  positions=  1.4  |  acct2: balance= 0.2  positions=  0.4
      acct1: balance=  0.2  positions= 22.6  |  acct2: balance= 0.2  positions=  1.6

      acct1 (...WR20):  median 12.3s   worst 30.6s
      acct2 (...8AUQ):  median  0.4s   worst  1.6s
```

**What the data supports, and nothing more:** a stall of roughly 20–30 seconds hits reads at
random. It is **not attached to an endpoint** — it lands on `balance` (22.3s) and on `positions`
(30.6s, 22.6s) alike. It is **not exclusive to one account** either: in an earlier six-round run,
acct2 — clean here — took **25.4s**. But the *frequency* is wildly uneven: a 30× difference in
median between two accounts at the same brokerage, same code, same minute.

I'll be honest that I got this wrong twice before the data corrected me. My first theory was "the
balance endpoint is slow" — the next round the stall moved to positions. My second was "one account
is slow" — the last round the fast account stalled too. So I'm claiming only what six rounds show:
**random, ~25s, uneven by account.** The cause is inside your infrastructure; from out here I can
only see latency.

**Why it matters beyond the number.** A stall this long doesn't degrade an app, it *breaks* it.
Snappy is a voice assistant — a 27-second pause after a question is indistinguishable from a hang,
and no amount of client caching helps, because a *cold* read is exactly what stalls. I ended up
having a background thread re-read every account every 30s purely to absorb this, so the stall
lands where no user is waiting. That works, but it's a workaround for something I don't think
should be there.

**Suggestion:** if this is a lazy re-sync being triggered on read, it would be much easier to build
against as an explicit async job (`202` + a poll) than as a read that usually takes 200ms and
occasionally takes 28 seconds.

---

## 4. The CLI cannot create a trade-enabled connection

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

## 5. `get_user_account_return_rates` returns `403` — is this entitlement?

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

## Why the repro script imports none of my code

Because "a bug in your API" and "a bug in my code that I blamed on your API" look identical from
the outside — and I have already made that exact mistake on this project.

I spent an evening convinced SnapTrade's positions endpoint was failing to sync after fills. I had
the symptom (orders `EXECUTED`, positions empty), I had a theory, I wrote up the finding, and I was
ready to send it. **It was my parser.** `get_all_account_positions` returns `{"results": [...]}`
with the ticker under `instrument`, and I was reading a different shape — so it returned `[]`, and
an empty list is indistinguishable from an empty account. A parsing bug that masquerades as a fact
about your platform.

That's why [`repro/repro.py`](repro/repro.py) imports the official SDK and nothing else. If these
reproduce on your machine, they're yours. If they don't, they were mine, and I'd rather find that
out from you than have you find it out about me.

---

*Eric — [github.com/ericmxf17/snappy](https://github.com/ericmxf17/snappy)*
