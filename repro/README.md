# Reproducing the findings — without any of my code

[`repro.py`](repro.py) imports the **official `snaptrade-python-sdk` and nothing else.** No
wrapper, no parser, no application code. Run it against your own SnapTrade account and it prints
the raw evidence for each finding in [`../FINDINGS.md`](../FINDINGS.md).

```sh
pip install snaptrade-python-sdk
export SNAPTRADE_CLIENT_ID=PERS-...
export SNAPTRADE_CONSUMER_KEY=...
python repro.py
```

It **only reads.** It places no orders and changes nothing. Takes ~2 minutes, most of which is
finding 3 sitting in the stalls it's measuring.

`run.txt` is the output from my machine, unedited.

---

## Why this file exists

Because "an API bug I found" and "a bug in my own code that I blamed on your API" look identical
from the outside — and I have already made that mistake once on this project. I spent an evening
convinced SnapTrade's positions endpoint was failing to sync after fills, wrote up the finding,
and was about to send it. **It was my parser**, reading the wrong payload shape and returning an
empty list, which is indistinguishable from an empty account.

So: none of these findings route through code I wrote. If they reproduce for you, they're real.

## What each section shows

**1 — Symbol search ranking (safety).** Searching `"nvidia"` does not return NVDA in the top six.
Every result is a leveraged or inverse product; the first is a 2× **short**. Any LLM or voice
layer takes result #1, so "buy me some Nvidia" **shorts Nvidia at 2× leverage** and fills cleanly.
The order also isn't stable between calls — an earlier run had NVDA fourth — so a naive
integration fails *differently each time* rather than consistently.

**2 — Dead SDK methods.** `get_all_user_holdings` and `get_activities` are importable, typed and
autocompleted, and return `410 Gone`. The first is the obvious method to reach for when building a
multi-brokerage app. (`get_user_account_return_rates` returns `403` — I assume that's plan
entitlement rather than a defect, and it's filed as a question, not a claim.)

**3 — Reads randomly stall for 20–30 seconds.** Across six cold rounds: a stall of ~25s lands on
`balance` and on `positions` alike, so it is **not a slow endpoint**. It hit both accounts, so it is
**not one bad account** either — though the frequency is wildly uneven (medians of 11.4s vs 0.3s
between two accounts at the same brokerage).

Worth stating what this is *not*: a *balance* call returns cash and never reads positions, so a
27-second balance call cannot be explained by how many holdings an account has.

I got this wrong twice before the data corrected me — first "the balance endpoint is slow" (the
stall moved to positions), then "one account is slow" (the fast account stalled too). The claim is
therefore only what six rounds support: **random, ~25s, uneven by account.**

**4 — The CLI can't create a trade-enabled connection.** `snaptrade connect` takes only `--broker`.
Every connection it makes is read-only, nothing in its output says so, and the trades simply fail
later. `connection_type="trade"` has to be passed to the API directly.
