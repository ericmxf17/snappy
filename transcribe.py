"""Local speech-to-text via Whisper. No network, no API key."""

import whisper

# Kept here rather than in config.py so this module can be tested without
# needing any API credentials.
WHISPER_MODEL = "base"

# Loading the model takes a few seconds, so do it once at import rather than
# per-recording.
_model = whisper.load_model(WHISPER_MODEL)


def transcribe(wav_path: str) -> str:
    # fp16=False: Whisper falls back to fp32 on CPU anyway, and this silences the warning.
    result = _model.transcribe(wav_path, fp16=False)
    return result["text"].strip()
