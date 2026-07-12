"""Hold Right ⌥ anywhere to talk.

A modifier key is a deliberate choice: it can't type a stray character into
whatever app you happen to be in, so there's no chord to memorise and nothing to
go wrong if Snappy is listening while you're mid-sentence in Slack.

Run this file on its own to check whether macOS is letting the key through:

    ./venv/bin/python hotkey.py
"""

import time

import AppKit
from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

# NSEventModifierFlagOption can't tell the two Option keys apart — it's set for
# either. The device-dependent bits can. (NX_DEVICERALTKEYMASK)
RIGHT_OPTION = 0x40

TAP_SECONDS = 0.25  # under this, a press+release is a tap, not a hold

_monitors = []
_held = False
_pressed_at = 0.0
_on_press = None
_on_release = None


def is_trusted():
    """Has the user granted Accessibility to whatever is running us?

    Without it the monitors below install fine and simply never fire, which is a
    miserable thing to debug — so callers check this and say so out loud.
    """
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False}))


def request_trust():
    """Same check, but macOS shows the 'open System Settings' prompt."""
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))


def _handle(event):
    global _held, _pressed_at

    down = bool(event.modifierFlags() & RIGHT_OPTION)

    if down and not _held:
        _held = True
        _pressed_at = time.monotonic()
        if _on_press:
            _on_press()
    elif not down and _held:
        _held = False
        if _on_release:
            # A tap is someone who wants hands-free, not someone who recorded 80ms
            # of nothing. Tell the caller so it can leave the mic open and let
            # silence end the recording instead.
            _on_release(time.monotonic() - _pressed_at < TAP_SECONDS)


def start(on_press, on_release):
    """Watch for Right ⌥ system-wide.

    on_release(was_tap) — was_tap is True if the key was only tapped.
    """
    global _on_press, _on_release
    _on_press, _on_release = on_press, on_release

    def local(event):  # fires when Snappy itself has focus
        _handle(event)
        return event  # must be returned or the event is swallowed

    _monitors.append(
        AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskFlagsChanged, _handle
        )
    )
    _monitors.append(
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskFlagsChanged, local
        )
    )


if __name__ == "__main__":
    import rumps

    if not is_trusted():
        print("Accessibility is NOT granted — the key will not fire.\n")
        print("System Settings → Privacy & Security → Accessibility → enable the app")
        print("running this (Terminal, or VS Code). Then RE-RUN: the permission is")
        print("cached per process, so an already-running app won't pick it up.\n")
        request_trust()  # pops the macOS dialog
    else:
        print("Accessibility granted.\n")

    print("Hold Right ⌥.  Ctrl-C to quit.\n")
    start(
        lambda: print("PRESS"),
        lambda tap: print("RELEASE", "(tap)" if tap else "(hold)"),
    )

    # The monitors are run-loop driven, so there has to be one running.
    app = rumps.App("hotkey probe")
    rumps.Timer(lambda _: None, 1).start()
    app.run()
