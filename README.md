# Snappy

A voice assistant for your brokerage account. **Hold ⌥, ask a question out loud, let go** —
and hear the answer, with live data pulled from your real connected brokerage via
[SnapTrade](https://snaptrade.com) and, when the question needs it, the open web.

> "What's my balance?" → *"You have about a hundred thousand dollars in cash."*
>
> "How would 5 shares of SpaceX fit into my portfolio?" → *"About two percent. Five SpaceX
> shares would run you roughly two thousand dollars against your hundred thousand dollar
> portfolio — and note it's private stock you can't just buy on the open market."*

That second one is the point. SpaceX is **private**, so no brokerage quote exists: Snappy has
to search the web for a secondary-market valuation, then size it against your *real* holdings.
A portfolio dashboard structurally cannot answer that.

Read-only: it can look things up, but it can't trade or move money.

## How it works

```
hold ⌥  →  record mic  →  Whisper (local)  →  Claude ─┬─→ SnapTrade API  (your real accounts)
                                                      └─→ web search     (prices, news, valuations)
                                                              ↓
     glass panel: math, sources, API trace  ←──────  full written answer
     macOS `say` speaks the summary         ←──────  first paragraph only
```

Speech-to-text runs locally — no audio leaves the machine. Claude decides which SnapTrade
endpoint answers the question, searches the web when the answer isn't in your account, and
writes an answer whose **first paragraph is spoken** while the detail stays on screen.

## Three ways to ask

| | |
|---|---|
| **Hold ⌥** (right Option) | Speak, let go. Works from any app. Needs Accessibility — see below. |
| **Click the menubar icon** | Speak, then just stop talking — it hears the silence and sends. |
| **Type in the panel** | For when the room is too loud to talk. |

Tapping ⌥ instead of holding it also works: it leaves the mic open and silence ends it.

## Setup

**System dependencies** (Homebrew):

```sh
brew install ffmpeg portaudio
```

**Python environment** — needs Python 3.12:

```sh
uv venv --python 3.12 venv
VIRTUAL_ENV="$PWD/venv" uv pip install -r requirements.txt
```

**Credentials** — copy `.env.example` to `.env` and fill in:

- `SNAPTRADE_CLIENT_ID` / `SNAPTRADE_CONSUMER_KEY` — from the SnapTrade dashboard
  (Personal account API keys, the `PERS-...` pair).
- `ANTHROPIC_API_KEY` — from [console.anthropic.com](https://console.anthropic.com).

`.env` is gitignored. Nothing else stores credentials.

## Run

```sh
./venv/bin/python main.py
```

A waveform icon appears in the menubar and the glass panel fades in at the top-right.

### Two permissions macOS will want

**Microphone** — prompted on first run. Grant it to whichever app runs Python (usually
Terminal). If you miss the prompt: System Settings → Privacy & Security → Microphone.
Without it, recording silently captures nothing.

**Accessibility** — only needed for the ⌥ hotkey, because watching for a keypress while
another app is focused is a privileged thing to do. System Settings → Privacy & Security →
Accessibility → enable Terminal (or whichever app runs Python), **then relaunch Snappy** —
the permission is cached per process, so a running app won't pick it up.

Not granted? Snappy says so in the panel and carries on: clicking the icon needs no
permission. Check it in isolation with:

```sh
./venv/bin/python hotkey.py     # prints PRESS/RELEASE, or tells you it's not granted
```

## Try it

- "What's my account balance?" · "Do I own Apple?"
- "What's Tesla trading at right now?"
- "How would 5 shares of SpaceX fit into my portfolio?" *(needs the web)*
- "Which brokerages am I connected to?" · "Can I connect Wealthsimple?"

## Testing pieces in isolation

Each stage runs on its own, which makes debugging far easier than chasing a failure through
the whole pipeline:

```sh
# Is the ⌥ hotkey getting through macOS at all?
./venv/bin/python hotkey.py

# Silence detection — talk, then stop, and watch it decide
./venv/bin/python audio.py

# Speech-to-text (no credentials needed)
say -o /tmp/t.wav --data-format=LEF32@16000 "what is my account balance"
./venv/bin/python -c "import transcribe; print(transcribe.transcribe('/tmp/t.wav'))"

# SnapTrade auth + data
./venv/bin/python -c "import snaptrade_client_wrapper as st; print(st.get_portfolio_summary())"

# Claude + tools + web search, skipping audio entirely
./venv/bin/python assistant.py "how would 5 shares of SpaceX fit into my portfolio"
```

## Layout

| File | Role |
|---|---|
| `main.py` | Menubar app; owns the trigger state machine and the answer threads |
| `hotkey.py` | Hold-⌥-to-talk, system-wide; Accessibility check |
| `audio.py` | Mic capture + adaptive silence detection (knows when you've stopped) |
| `transcribe.py` | Whisper speech-to-text, runs locally |
| `assistant.py` | Claude streaming tool-use loop (SnapTrade + web search) → spoken + written answer |
| `tools.py` | Tool schemas Claude picks from + dispatch |
| `snaptrade_client_wrapper.py` | Thin read-only wrapper over the SnapTrade SDK |
| `ui.py` | The floating glass panel (NSPanel + vibrancy + WKWebView) |
| `panel.html` | Everything the panel draws — waveform, aurora, streaming text, composer |
| `state.py` | Thread-safe handoff: workers mutate it, the UI timer reads it |
| `config.py` | Loads `.env`, fails loudly if a key is missing |

## Notes

- Uses SnapTrade's **Personal** account model, where `user_id` / `user_secret` are the literal
  string `"personal"` rather than per-end-user values.
- AppKit is main-thread-only. Worker threads never touch the UI — they mutate `state.py` and a
  timer on the main thread pushes it into the panel. Every threading bug in this app came from
  breaking that rule.
- Whisper's `base` model is the speed/accuracy sweet spot for short commands. `transcribe.py`
  has the size if you want to trade one for the other.
