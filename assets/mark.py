"""The Snappy mark, drawn with CoreGraphics. Run this to regenerate every icon.

    ../venv/bin/python mark.py

THE IDEA. Five bars on a baseline. The heights vary like speech amplitude, so it reads
as a waveform; they sit on a common baseline with rounded caps, so it reads as a bar
chart. Voice and portfolio, drawn as one object — which is the entire product.

Deliberately NOT a rising ramp: "line goes up" is the most exhausted shape in fintech,
and it would say something about returns that we have no business promising.

THE STATE MACHINE IS THE SAME GLYPH. Idle, listening and thinking are one mark at three
amplitudes — quiet, hot, flat. The identity never changes; only the level does, which is
what a waveform is for. The bar count and width are fixed across all three, so the icon
never jitters or resizes in the menubar as the state changes.
"""

import os
import subprocess

import Quartz
from Foundation import NSURL

HERE = os.path.dirname(os.path.abspath(__file__))

# The mark, and its two other amplitudes.
IDLE = [0.42, 0.72, 1.00, 0.55, 0.85]      # speech: a clear peak, but not at the edge
LISTENING = [0.80, 1.00, 0.92, 1.00, 0.86]  # a hot mic — everything loud
THINKING = [0.20, 0.20, 0.20, 0.20, 0.20]   # flat: at rest, working

INK = (0.06, 0.07, 0.09)      # the panel's ground
AMBER = (0.98, 0.68, 0.22)    # the panel's accent
WARM = (0.97, 0.96, 0.94)


def _ctx(size):
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    c = Quartz.CGBitmapContextCreate(
        None, size, size, 8, 0, space, Quartz.kCGImageAlphaPremultipliedLast
    )
    Quartz.CGContextSetShouldAntialias(c, True)
    return c


def _rounded(c, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    Quartz.CGContextAddPath(
        c, Quartz.CGPathCreateWithRoundedRect(Quartz.CGRectMake(x, y, w, h), r, r, None)
    )
    Quartz.CGContextFillPath(c)


def bars(c, size, env, inset, color, accent=None, accent_i=None, snap=False):
    """Draw the mark inside a square of `size`.

    `snap` rounds every edge to a whole pixel. At 1024 that changes nothing you can see;
    at 18px — the size the menubar actually draws — an unsnapped bar straddles two pixel
    columns and antialiasing turns the mark to grey mush. Snapped, it stays crisp. This
    is the difference between a logo and a smudge, and it is only visible at true size.
    """
    n = len(env)
    pad = size * inset
    box = size - 2 * pad

    unit = box / (n * 2 + (n - 1) * 1.15)   # bar : gap = 2 : 1.15
    bw, gap = unit * 2, unit * 1.15

    if snap:
        bw = max(1, round(bw))
        gap = max(1, round(gap))
        pad = round((size - (n * bw + (n - 1) * gap)) / 2)

    base = round(pad) if snap else pad
    span = (size - pad) - base

    for i, v in enumerate(env):
        h = max(span * v, bw)               # never shorter than it is wide, or it's a dot
        if snap:
            h = max(bw, round(h))
        rgb = accent if (accent and i == accent_i) else color
        Quartz.CGContextSetRGBFillColor(c, *rgb, 1.0)
        _rounded(c, pad + i * (bw + gap), base, bw, h, bw / 2)


def squircle(c, size):
    """The macOS app-icon shape, washed with the same amber the panel uses — so the icon
    looks like the thing it opens."""
    m = size * 0.085
    s = size - 2 * m
    Quartz.CGContextSaveGState(c)
    Quartz.CGContextAddPath(
        c,
        Quartz.CGPathCreateWithRoundedRect(
            Quartz.CGRectMake(m, m, s, s), s * 0.2237, s * 0.2237, None  # Big Sur radius
        ),
    )
    Quartz.CGContextClip(c)

    Quartz.CGContextSetRGBFillColor(c, *INK, 1.0)
    Quartz.CGContextFillRect(c, Quartz.CGRectMake(0, 0, size, size))

    space = Quartz.CGColorSpaceCreateDeviceRGB()
    grad = Quartz.CGGradientCreateWithColorComponents(
        space,
        (*AMBER, 0.30, *AMBER, 0.0),
        (0.0, 1.0),
        2,
    )
    at = Quartz.CGPointMake(size * 0.24, size * 0.86)
    Quartz.CGContextDrawRadialGradient(c, grad, at, 0, at, size * 0.72, 0)
    Quartz.CGContextRestoreGState(c)


def _write(c, path):
    dest = Quartz.CGImageDestinationCreateWithURL(
        NSURL.fileURLWithPath_(path), "public.png", 1, None
    )
    Quartz.CGImageDestinationAddImage(dest, Quartz.CGBitmapContextCreateImage(c), None)
    Quartz.CGImageDestinationFinalize(dest)


def menubar(env, size):
    """A template image: pure black with alpha. macOS recolors it for light/dark, so it
    must work as a silhouette and must NOT carry colour of its own."""
    c = _ctx(size)
    bars(c, size, env, 0.11, (0, 0, 0), snap=True)
    return c


def app_icon(size):
    c = _ctx(size)
    squircle(c, size)
    bars(c, size, IDLE, 0.30, WARM, accent=AMBER, accent_i=IDLE.index(max(IDLE)))
    return c


def main():
    for name, env in (("idle", IDLE), ("listening", LISTENING), ("thinking", THINKING)):
        _write(menubar(env, 18), os.path.join(HERE, f"menubar-{name}.png"))
        _write(menubar(env, 36), os.path.join(HERE, f"menubar-{name}@2x.png"))

    _write(app_icon(1024), os.path.join(HERE, "icon.png"))

    # .icns, for the .app bundle. iconutil ships with macOS.
    iconset = os.path.join(HERE, "Snappy.iconset")
    os.makedirs(iconset, exist_ok=True)
    for px in (16, 32, 64, 128, 256, 512, 1024):
        _write(app_icon(px), os.path.join(iconset, f"icon_{px}x{px}.png"))
        if px <= 512:  # every size also needs its @2x twin
            _write(app_icon(px * 2), os.path.join(iconset, f"icon_{px}x{px}@2x.png"))
    subprocess.run(
        ["iconutil", "-c", "icns", iconset, "-o", os.path.join(HERE, "Snappy.icns")],
        check=True,
    )
    print("wrote menubar-{idle,listening,thinking}[@2x].png, icon.png, Snappy.icns")


if __name__ == "__main__":
    main()
