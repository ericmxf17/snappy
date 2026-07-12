"""Local speech-to-text. No network, no API key — your voice stays on this Mac.

Runs Whisper on the CTranslate2 runtime rather than PyTorch. Same model, same
accuracy, but it drops a 491 MB dependency and is several times faster on CPU.

Apple's SFSpeechRecognizer would be lighter still, and was tried first — but it's
TCC-gated and macOS won't grant Speech Recognition to a plain script: there's no
bundle for it to attribute the permission to, so the request hangs with no dialog
and no error. It becomes available if this is ever packaged as a real .app.

Run it directly to hear what it hears:

    ./venv/bin/python transcribe.py            # speaks and transcribes a test phrase
    ./venv/bin/python transcribe.py some.wav
"""

from faster_whisper import WhisperModel

MODEL = "base"  # the speed/accuracy sweet spot for one-line questions

# Whisper picks between similar-sounding words using context, so telling it what
# domain it's in genuinely changes the output. Without this it hears "Nvidia" as
# "and video", "SnapTrade" as "snap trade", and — worst of all — "BUY five shares"
# as "BY five shares", which is a trading command quietly losing its verb.
#
# This has to read as a SENTENCE, not a word list. Whisper prepends it as if it
# were the preceding speech, so a comma-separated list of keywords gets parroted
# straight back into the transcript ("buy, buy, buy, shares, of Apple"). Prose
# primes the vocabulary; a list contaminates the output.
CONTEXT = (
    "The following is a spoken question about a stock brokerage portfolio on "
    "SnapTrade, connected to Alpaca, Wealthsimple, Robinhood, Webull or Questrade. "
    "It may mention tickers such as AAPL, NVDA, TSLA, SPCX or MSFT, and may ask to "
    "buy or sell shares, or say confirm or cancel."
)

_prompt = CONTEXT

# int8 on CPU: roughly 4x faster than fp32 with no meaningful accuracy loss at this
# model size. Loading once at import keeps it off the critical path of a question.
_model = WhisperModel(MODEL, device="cpu", compute_type="int8")


def set_hints(held):
    """Tell the transcriber which tickers the user actually holds.

    Kept as a setter rather than a config import so this module stays testable
    without any credentials. Folded into the sentence rather than appended as a
    list, for the reason above.
    """
    global _prompt
    if not held:
        _prompt = CONTEXT
        return
    _prompt = f"{CONTEXT} The user currently holds {', '.join(held)}."


def transcribe(wav_path: str) -> str:
    segments, _ = _model.transcribe(
        wav_path,
        language="en",
        initial_prompt=_prompt,
        vad_filter=True,  # drop leading/trailing silence before it reaches the model
        beam_size=5,
    )
    return " ".join(s.text for s in segments).strip()


if __name__ == "__main__":
    import subprocess
    import sys
    import time

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        path = "/tmp/snappy_selftest.wav"
        phrase = "how would five shares of Nvidia fit into my portfolio"
        print(f'speaking : "{phrase}"')
        subprocess.run(
            ["say", "-o", path, "--data-format=LEF32@16000", phrase], check=True
        )

    started = time.perf_counter()
    heard = transcribe(path)
    print(f'heard    : "{heard}"')
    print(f"took     : {time.perf_counter() - started:.2f}s")
