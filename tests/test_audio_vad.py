"""Silence detection decides when you've stopped talking.

Get it wrong in one direction and it cuts you off mid-sentence; wrong in the other
and the mic hangs open forever. Neither is visible in the code — both are only
visible in the timing — so the state machine is tested directly, with a fake clock
and no microphone.
"""

import audio


def clock(monkeypatch, t):
    """Freeze time.monotonic() at t, inside audio only."""
    monkeypatch.setattr(audio.time, "monotonic", lambda: t)


def arm(monkeypatch, *, started, spoke, last_loud):
    """Put the module in the state a real recording would have produced."""
    monkeypatch.setattr(audio, "_stream", object())   # pretend the mic is open
    monkeypatch.setattr(audio, "_started_at", started)
    monkeypatch.setattr(audio, "_spoke", spoke)
    monkeypatch.setattr(audio, "_last_loud", last_loud)


def test_no_stream_never_autostops(monkeypatch):
    monkeypatch.setattr(audio, "_stream", None)
    assert audio.should_autostop() is False


def test_keeps_listening_while_you_are_talking(monkeypatch):
    arm(monkeypatch, started=0, spoke=True, last_loud=9.8)
    clock(monkeypatch, 10.0)          # loud 0.2s ago — still going
    assert audio.should_autostop() is False


def test_stops_after_you_go_quiet(monkeypatch):
    arm(monkeypatch, started=0, spoke=True, last_loud=9.0)
    clock(monkeypatch, 9.0 + audio.SILENCE_HOLD + 0.01)
    assert audio.should_autostop() is True


def test_pauses_mid_sentence_do_not_cut_you_off(monkeypatch):
    """A breath is shorter than SILENCE_HOLD. This is the cut-you-off failure."""
    arm(monkeypatch, started=0, spoke=True, last_loud=5.0)
    clock(monkeypatch, 5.0 + audio.SILENCE_HOLD - 0.1)
    assert audio.should_autostop() is False


def test_gives_up_if_you_never_speak(monkeypatch):
    """Clicked the icon and walked away. The mic must not stay open forever."""
    arm(monkeypatch, started=0, spoke=False, last_loud=0)
    clock(monkeypatch, audio.NO_SPEECH_GIVE_UP + 0.01)
    assert audio.should_autostop() is True


def test_silent_but_still_within_the_grace_period(monkeypatch):
    """You clicked, and you're drawing breath. Don't bail after 200ms."""
    arm(monkeypatch, started=0, spoke=False, last_loud=0)
    clock(monkeypatch, 2.0)
    assert audio.should_autostop() is False


def test_hard_cap_stops_a_runaway_recording(monkeypatch):
    """Speech that never ends (a TV in the room) must not record forever."""
    arm(monkeypatch, started=0, spoke=True, last_loud=999.0)  # constantly loud
    clock(monkeypatch, audio.MAX_SECONDS + 0.01)
    assert audio.should_autostop() is True


def test_threshold_is_capped_so_speech_can_always_clear_it():
    """The push-to-talk trap.

    You press the key and start talking IMMEDIATELY, so the 0.3s calibration
    window measures your VOICE as the room's noise floor. Uncapped, the threshold
    lands above your own speech and `_spoke` never flips — the mic just sits open
    until the give-up timer. The cap bounds that mistake.
    """
    loud_room = 0.5                                    # calibration caught speech
    threshold = min(max(audio.NOISE_MULT * loud_room, audio.MIN_RMS), audio.MAX_RMS)

    assert threshold == audio.MAX_RMS
    assert threshold < 0.05, "normal speech RMS must be able to exceed the threshold"


def test_quiet_room_still_gets_a_sane_floor():
    """A silent room shouldn't make electrical hum count as speech."""
    silent = 0.0001
    threshold = min(max(audio.NOISE_MULT * silent, audio.MIN_RMS), audio.MAX_RMS)
    assert threshold == audio.MIN_RMS
