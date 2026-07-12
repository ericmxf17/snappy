"""Recording, with an ear for when you've stopped talking.

The mic callback already computes an RMS level for the panel's waveform, so
silence detection rides along on a number we're producing anyway — no second
audio path, no VAD dependency.

Run this file on its own to watch it decide:

    ./venv/bin/python audio.py
"""

import time

import numpy as np
import sounddevice as sd
import soundfile as sf

import state

SAMPLE_RATE = 16_000  # what Whisper expects
RECORDING_PATH = "/tmp/snappy_recording.wav"

CALIBRATE = 0.30  # seconds of room tone to measure before trusting the threshold
NOISE_MULT = 3.0  # speech has to clear the room by this much
MIN_RMS = 0.012  # ...but never trust a floor so low that hum counts as speech
MAX_RMS = 0.045  # ...and never set a bar that speech itself can't clear.
# With push-to-talk you press the key and start talking immediately, so the
# calibration window can end up measuring your VOICE as the room. Left uncapped,
# the threshold lands above your speech and nothing is ever "loud" — the mic just
# sits open until the give-up timer. The cap bounds that mistake.
SILENCE_HOLD = 0.8  # quiet for this long, after speaking, means "done"
NO_SPEECH_GIVE_UP = 6.0  # never spoke at all — don't hold the mic open forever
MAX_SECONDS = 30.0  # hard cap; a stuck mic can't run away with the demo

_stream = None
_chunks = []
_started_at = 0.0
_noise = []  # RMS samples from the calibration window
_threshold = MIN_RMS
_spoke = False  # has the level ever cleared the threshold?
_last_loud = 0.0


def start_recording():
    """Open the mic and start buffering. Raises if mic permission is denied."""
    global _stream, _chunks, _started_at, _noise, _threshold, _spoke, _last_loud
    _chunks = []
    _noise = []
    _threshold = MIN_RMS
    _spoke = False
    _started_at = _last_loud = time.monotonic()

    def callback(indata, frames, time_info, status):
        global _threshold, _spoke, _last_loud
        _chunks.append(indata.copy())

        rms = float(np.sqrt(np.mean(indata**2)))
        elapsed = time.monotonic() - _started_at

        # Calibrate against the actual room rather than a hardcoded number — a
        # demo room is far louder than a bedroom, and a fixed threshold would
        # either cut you off mid-sentence or never trigger at all.
        if elapsed < CALIBRATE:
            _noise.append(rms)
        else:
            if _noise:
                floor = NOISE_MULT * float(np.median(_noise))
                _threshold = min(max(floor, MIN_RMS), MAX_RMS)
                _noise.clear()
            if rms > _threshold:
                _spoke = True
                _last_loud = time.monotonic()

        # Publish loudness so the panel can draw a waveform off the real signal.
        # Speech RMS sits well below 1.0, so scale it into a usable 0..1 range.
        state.set_level(min(1.0, rms * 8))

    _stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=callback
    )
    _stream.start()


def should_autostop():
    """Are we done talking? Called from the UI timer on the main thread.

    Three ways to be done: you spoke and then went quiet; you never spoke at all;
    or you've been going long enough that something is clearly wrong.
    """
    if _stream is None:
        return False

    now = time.monotonic()
    if now - _started_at > MAX_SECONDS:
        return True
    if _spoke:
        return now - _last_loud > SILENCE_HOLD
    return now - _started_at > NO_SPEECH_GIVE_UP


def heard_speech():
    return _spoke


def stop_recording() -> str | None:
    """Close the mic, write the WAV, and return its path (None if nothing captured)."""
    global _stream
    if _stream is None:
        return None

    _stream.stop()
    _stream.close()
    _stream = None
    state.set_level(0.0)

    if not _chunks:
        return None

    audio = np.concatenate(_chunks, axis=0)
    if len(audio) < SAMPLE_RATE * 0.3:  # under ~0.3s is a stray click, not speech
        return None

    sf.write(RECORDING_PATH, audio, SAMPLE_RATE)
    return RECORDING_PATH


if __name__ == "__main__":
    print("Recording. Say something, then stop talking.\n")
    start_recording()
    while not should_autostop():
        time.sleep(0.05)
        bar = "█" * int(state.STATE["level"] * 40)
        print(f"\r{'SPEECH' if _spoke else 'quiet '} |{bar:<40}|", end="")

    wav = stop_recording()
    print(f"\n\nstopped: {'heard you' if heard_speech() else 'never heard speech'}")
    print(f"threshold settled at {_threshold:.4f}")
    print(f"wrote: {wav}")
