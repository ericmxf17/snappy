"""The Snappy panel: a borderless frosted-glass HUD floating over the desktop.

The window is a real NSPanel with an NSVisualEffectView behind a transparent
WKWebView, so the macOS blur shows through the page. The page is loaded ONCE and
then driven by JavaScript — reloading the HTML on every update (what v1 did)
would strobe the waveform and the streaming text.

The page talks back over a script-message bridge: typed questions, hover (which
pauses auto-hide), and Escape.
"""

import json
import os

import AppKit
import WebKit
from Foundation import NSMakeRect, NSObject, NSURL

_panel = None
_webview = None
_delegate = None  # keep strong refs; PyObjC won't retain these for us
_bridge = None
_watch = None
_drag = None
_ready = False  # the page must finish loading before JS can be pushed
_on_ask = None
_on_confirm = None
_on_cancel = None

# Where the user dragged the panel to. Once they've moved it, that's where it
# belongs — show() must stop hauling it back to the corner.
_origin = None
_moving_it_ourselves = False


class _Panel(AppKit.NSPanel):
    """A borderless panel you can still type into.

    Borderless windows refuse key status by default, so the composer would just
    swallow keystrokes. Returning True here, together with the NonactivatingPanel
    style mask below, is the Spotlight arrangement: the panel takes keyboard input
    WITHOUT activating Snappy and yanking focus off whatever app you're using.
    """

    def canBecomeKeyWindow(self):
        return True


class _DragStrip(AppKit.NSView):
    """An invisible grab handle over the panel's header, so it can be dragged.

    It has to be a separate view. Making the WEB VIEW draggable would mean every
    mouse-down in the page starts a window drag instead of reaching the page — which
    is the exact bug that made the Confirm button dead for hours. So the drag area is
    a transparent strip sitting on top of the header, deliberately stopping short of
    the ✕ so that button still gets its clicks.
    """

    def mouseDownCanMoveWindow(self):
        return True

    def hitTest_(self, point):
        # Claim header drags, but never swallow a click meant for the page.
        return self if self.isMousePoint_inRect_(
            self.convertPoint_fromView_(point, self.superview()), self.bounds()
        ) else None

    def resetCursorRects(self):
        # An open hand over the header — otherwise nobody discovers it's draggable.
        self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.openHandCursor())


class _WindowWatch(NSObject):
    """Remembers where the user put the panel."""

    def windowDidMove_(self, notification):
        global _origin
        if _moving_it_ourselves:
            return  # our own repositioning isn't the user expressing a preference
        _origin = _panel.frame().origin


class _WebView(WebKit.WKWebView):
    """A web view whose buttons work on a panel that never takes focus.

    macOS treats a click on an INACTIVE window as a "first mouse" event: it uses
    that click to activate the window and swallows it, rather than delivering it to
    the content. Snappy's panel is deliberately non-activating — it must not steal
    focus from whatever you're working in — so it is never active, so EVERY click is
    a first-mouse click, so every click was being eaten. Buttons looked dead.

    (A synthetic .click() from JavaScript worked fine throughout, because it never
    goes through AppKit. That's what made this so confusing to diagnose.)
    """

    def acceptsFirstMouse_(self, event):
        return True


class _Nav(NSObject):
    """Tells us when the page has actually loaded.

    Without this, the first setState() fires into a page that doesn't have the
    function yet, silently does nothing, and the panel sits on its placeholder.
    """

    def webView_didFinishNavigation_(self, webview, navigation):
        global _ready
        _ready = True


class _Bridge(NSObject):
    """Receives window.webkit.messageHandlers.snappy.postMessage() from the page."""

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = message.body()
        kind = body.get("type")
        # Logged because "the button did nothing" is otherwise unfalsifiable: it
        # could be the click, the binding, the bridge, or the handler. This says
        # which.
        print(f"panel → {kind}")

        if kind == "ask":
            text = (body.get("text") or "").strip()
            if text and _on_ask:
                _on_ask(text)
        elif kind == "confirm":  # the Confirm button on a proposed trade
            if _on_confirm:
                _on_confirm()
        elif kind == "cancel":
            if _on_cancel:
                _on_cancel()
        elif kind == "close":  # the ✕, or Escape
            hide()


WIDTH, HEIGHT = 400, 620
MARGIN = 24
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")


def set_on_ask(callback):
    """callback(text) — fired when a question is typed into the panel."""
    global _on_ask
    _on_ask = callback


def set_on_trade(confirm, cancel):
    """The Confirm / Cancel buttons on a proposed trade."""
    global _on_confirm, _on_cancel
    _on_confirm, _on_cancel = confirm, cancel


def _corner():
    """Top-right of whichever display the mouse is on.

    The mouse is the best proxy for the screen you're actually looking at:
    mainScreen() follows the focused window and screens()[0] is always the
    menu-bar display, and on a two-monitor setup either can strand the panel on
    a screen you never look at (which is exactly what happened in v1).
    """
    mouse = AppKit.NSEvent.mouseLocation()
    screens = AppKit.NSScreen.screens()
    for s in screens:
        f = s.frame()
        if (
            f.origin.x <= mouse.x <= f.origin.x + f.size.width
            and f.origin.y <= mouse.y <= f.origin.y + f.size.height
        ):
            v = s.visibleFrame()
            break
    else:
        v = screens[0].visibleFrame()

    return (
        v.origin.x + v.size.width - WIDTH - MARGIN,
        v.origin.y + v.size.height - HEIGHT - MARGIN,
    )


def create():
    """Build the panel. Must run on the main thread, after NSApplication exists."""
    global _panel, _webview, _delegate, _bridge, _watch, _drag

    x, y = _corner()

    _panel = _Panel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, WIDTH, HEIGHT),
        AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    # AppKit repositions panels on init, so pin the origin afterwards.
    _panel.setFrameOrigin_((x, y))
    _panel.setOpaque_(False)
    _panel.setBackgroundColor_(AppKit.NSColor.clearColor())
    _panel.setLevel_(AppKit.NSFloatingWindowLevel)
    _panel.setReleasedWhenClosed_(False)
    _panel.setHidesOnDeactivate_(False)  # rumps is a background app; stay put
    _panel.setMovableByWindowBackground_(True)
    _panel.setCollectionBehavior_(  # follow the user, even over fullscreen apps
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
    )

    # The frosted slab: a vibrancy view, rounded, with the web page on top.
    blur = AppKit.NSVisualEffectView.alloc().initWithFrame_(
        NSMakeRect(0, 0, WIDTH, HEIGHT)
    )
    blur.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
    blur.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
    blur.setState_(AppKit.NSVisualEffectStateActive)
    blur.setWantsLayer_(True)
    blur.layer().setCornerRadius_(18.0)
    blur.layer().setMasksToBounds_(True)
    blur.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

    # The page needs a way to reach Python — typed questions, hover, Escape.
    _bridge = _Bridge.alloc().init()
    controller = WebKit.WKUserContentController.alloc().init()
    controller.addScriptMessageHandler_name_(_bridge, "snappy")
    conf = WebKit.WKWebViewConfiguration.alloc().init()
    conf.setUserContentController_(controller)

    _webview = _WebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, WIDTH, HEIGHT), conf
    )
    _webview.setAutoresizingMask_(
        AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
    )
    # Let the vibrancy behind show through the page.
    _webview.setValue_forKey_(False, "drawsBackground")
    _delegate = _Nav.alloc().init()
    _webview.setNavigationDelegate_(_delegate)
    _webview.loadFileURL_allowingReadAccessToURL_(
        NSURL.fileURLWithPath_(PAGE),
        NSURL.fileURLWithPath_(os.path.dirname(PAGE)),
    )

    blur.addSubview_(_webview)

    # Added AFTER the web view, so it sits on top and gets the header's mouse-downs.
    # Stops 46px short of the right edge — that's the ✕, and it needs its clicks.
    STRIP_H = 38
    _drag = _DragStrip.alloc().initWithFrame_(
        NSMakeRect(0, HEIGHT - STRIP_H, WIDTH - 46, STRIP_H)
    )
    _drag.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewMinYMargin)
    blur.addSubview_(_drag)

    _panel.setContentView_(blur)

    _watch = _WindowWatch.alloc().init()
    _panel.setDelegate_(_watch)

    _panel.setAlphaValue_(0.0)  # faded in by show()
    show()


def _js(script):
    if _webview is not None and _ready:
        _webview.evaluateJavaScript_completionHandler_(script, None)


def is_ready():
    """True once the page has loaded and setState() exists."""
    return _ready


def push(s):
    """Send a state snapshot into the page."""
    _js(f"setState({json.dumps(s)})")


def set_level(level):
    """Fast path for the waveform — no full re-render."""
    _js(f"setLevel({level:.3f})")


def focus_composer():
    """Put the caret in the text box, without activating the app."""
    if _panel is not None:
        _panel.makeKeyAndOrderFront_(None)
    _js("focusComposer()")


def show():
    """Fade in — where the user last dragged it, or the corner if they never have."""
    global _moving_it_ourselves
    if _panel is None:
        return

    _moving_it_ourselves = True          # don't mistake this for the user moving it
    _panel.setFrameOrigin_(_origin if _origin is not None else _corner())
    _moving_it_ourselves = False

    _panel.orderFrontRegardless()

    AppKit.NSAnimationContext.beginGrouping()
    AppKit.NSAnimationContext.currentContext().setDuration_(0.22)
    _panel.animator().setAlphaValue_(1.0)
    AppKit.NSAnimationContext.endGrouping()


def hide():
    if _panel is None:
        return
    AppKit.NSAnimationContext.beginGrouping()
    AppKit.NSAnimationContext.currentContext().setDuration_(0.18)
    _panel.animator().setAlphaValue_(0.0)
    AppKit.NSAnimationContext.endGrouping()


def is_visible():
    return _panel is not None and _panel.alphaValue() > 0.5
