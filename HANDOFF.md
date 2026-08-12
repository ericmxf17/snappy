# Handoff — Snappy (SnapTrade voice assistant)

Updated 2026-08-12 for whoever picks this up next (Codex or otherwise). `README.md` is the
user-facing doc and it is accurate — read it first. This file covers what the README doesn't:
the current state of the working tree, the decisions that already have reasons behind them, and
the traps that cost real hours.

---

## What this is, in one paragraph

A macOS menubar app. Hold Right ⌥, ask a question out loud, let go → local Whisper transcribes →
Claude answers using SnapTrade tools plus server-side web search → the answer is drawn into a
floating frosted-glass panel showing the math, the API trace, and sources. It can also
place trades, paper-only, behind a confirmation the model cannot forge. Built by Eric against his
own Alpaca Paper account (`ALPACA-PAPER`) as a demo for SnapTrade's CTO.

The question that justifies its existence: *"How would 5 shares of SpaceX fit into my portfolio?"*
— it needs the open web **and** live holdings across every connected brokerage in the same breath,
which no portfolio dashboard can structurally answer.

---

## Run it

```sh
brew install ffmpeg portaudio
uv venv --python 3.12 venv
VIRTUAL_ENV="$PWD/venv" uv pip install -r requirements.txt   # the uv venv has no pip
cp .env.example .env                                          # fill in keys
./venv/bin/python src/main.py
```

Tests: `./venv/bin/python -m pytest -q` → **158 passed** as of this handoff. They run fully
offline with no credentials; nothing in the suite reaches the network.

**Run from Terminal.app, not VS Code.** VS Code is app-translocated here (macOS runs it from a
randomized quarantine path), so the Accessibility grant the ⌥ hotkey needs never sticks and the
hotkey silently does nothing. This is the single most expensive gotcha in the project.

---

## Current delivery state

**1. Packaging as a real `.app`**

- `./venv/bin/python setup.py py2app` → `dist/Snappy.app`; `./scripts/build_dmg.sh` wraps it in a
  drag-to-Applications `.dmg`. Unsigned — no Apple Developer ID — so first launch needs
  right-click → Open.
- This drove the `sys.frozen` / `RESOURCEPATH` branches now in [config.py](src/config.py),
  [main.py](src/main.py), and [ui.py](src/ui.py): running from source, `panel.html` and `assets/`
  sit next to the code; frozen, py2app zips the code and drops loose resources in
  `Contents/Resources`. Both paths must keep working — don't "simplify" one away.
- Packaged builds also read `~/Library/Application Support/Snappy/.env`, which **overrides** the
  repo `.env`. That's deliberate: the packaged user has no repo root, but still needs somewhere to
  configure a local Anthropic key or hosted-service URL.
- `setup.py` carries two non-obvious workarounds, both explained in comments at the top and in
  `OPTIONS["packages"]`: a fake `zlib.__file__` shim (uv's python-build-standalone links zlib
  statically; py2app assumes it's a loadable `.so`), and explicitly-listed packages that py2app's
  static scan cannot trace (`_sounddevice_data`, `_soundfile_data` must be unzipped directories or
  `dlopen` can't load their bundled dylibs).
- A real `.app` bundle isn't only cosmetic — it's the fix for the translocation problem above, and
  it's what would unlock Apple's `SFSpeechRecognizer` (TCC-gated, unusable from a plain script).

**2. SnapTrade Personal OAuth**

- `src/auth.py` does SnapTrade Personal OAuth: authorization-code + PKCE,
  public native client, loopback redirect on a fixed port from `PORTS = (8765, 8919, 9137)`,
  tokens stored in the **macOS Keychain** via the `security` CLI — never `.env`, never on disk.
- The app exposes a one-click sign-in from both the menu and panel. Tokens live in Keychain and
  the bearer client reads accounts the user already connected at dashboard.snaptrade.com.
- **OAuth is read-only, by SnapTrade's server-side rule** — `POST /oauth/register/` rejects any
  scope but `read`, and the discovery doc agrees (`"scopes_supported": ["read"]`). So trading
  still requires Personal API keys. `can_trade()` returns true only in `keys` mode. The moment
  SnapTrade ships a write scope, that fallback can delete itself.

**3. Hosted agent**

- The backend code, authenticated WebSocket client, one-time invites, rate limits, and execution
  tool exclusion are implemented and covered offline. The service is not deployed yet.
- Until it is deployed, a tester needs a local `ANTHROPIC_API_KEY`; this is independent of
  SnapTrade authentication and read-only OAuth users need no SnapTrade API keys.

The remaining release check is to rebuild the unsigned DMG and run Personal OAuth end-to-end from
inside the installed bundle on a fresh Keychain. First launch requires right-click → Open.

---

## Architecture, and the rules that are load-bearing

`README.md` has the full file-by-file table. The rules that will bite you if you don't know them:

- **The model has no tool that executes a trade.** Its only trading tool is `preview_trade()`,
  which calls SnapTrade's `get_order_impact()` and returns an opaque `trade_id`. Python executes
  via `place_order(trade_id)` only after a **regex** — never the model — matches a clear yes.
  Ambiguity leaves the order standing rather than destroying a wanted trade. A test asserts no
  execute tool ever appears in the dispatch table. This is the design's headline; if you find
  yourself adding an execute tool to `tools.py`, you have misunderstood the project.
- **AppKit is main-thread only.** Workers mutate `state.py`; a main-thread timer reads it and
  pushes into the panel. Every threading bug this app has had came from breaking that rule.
- **Snappy never opens the mic by itself.** It used to, when a trade was proposed, and that one
  behavior caused nearly every trading bug in its history. See the comment in `main.py`.
- **Model is Sonnet 5, deliberately.** Measured on the SpaceX question: Sonnet 11.7s vs Opus 38s,
  and Sonnet's answer was *more* correct. For a voice assistant, latency is the product. Don't
  "upgrade" to Opus without re-timing. Cost is ~$0.07 per research question, ~$0.01 without
  search; searches are capped at 4 and the system prompt is cached (an uncapped Opus run once
  pulled 106k input tokens in a single turn).
- **Personal account model**: `user_id` / `user_secret` are the literal string `"personal"`, not
  per-user values. A multi-user build would use the real registration flow.
- Use `./venv/bin/python connect.py ALPACA-PAPER` to link a brokerage — **not** `snaptrade
  connect` from the CLI, which never passes `connection_type="trade"` and silently gives you a
  read-only connection that looks fine until an order is refused.
- If the `snaptrade` CLI is needed at all, it is pinned to **`@snaptrade/snaptrade-cli@0.1.38`**.
  0.1.39+ depends on canary SDK builds that are broken for Personal auth on both paths (`personalOAuth is not
  a function`; and `401 Unable to verify signature sent` from an extra `/api/v1` hardcoded into
  the HMAC path). Not user error — confirmed by reading the installed package source.

---

## Documents already in the repo

- **`README.md`** — user-facing: what it's for, the safety argument, install, permissions, the
  tool surface, debugging each piece in isolation, and the file layout table.
- **`FINDINGS.md`** — five reproducible findings against the live SnapTrade API (symbol search
  ranking a leveraged inverse ETF above the company; SDK methods returning `410 Gone`; reads
  stalling 20–30s; the CLI's read-only-connection trap; `403` on
  `get_user_account_return_rates`). Plus what worked well.
- **`repro/`** — a standalone reproduction script that **imports none of Snappy's code**, so every
  finding above is verifiable without trusting this repo.

---

## Working with Eric

He is an incoming Brown Applied Math-CS & Econ student, not an experienced software engineer.
Explain plainly and walk him through errors rather than silently fixing them — and never touch
his brokerage credentials; he does connection flows himself in the browser.
