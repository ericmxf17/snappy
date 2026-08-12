"""Build Snappy.app with py2app.

    ./venv/bin/python setup.py py2app

Produces dist/Snappy.app. scripts/build_dmg.sh wraps that into a .dmg for
distribution. See scripts/build_dmg.sh for the one-command build.

A real .app bundle also fixes a real bug: running from a venv in a terminal
(worse, VS Code's app-translocated terminal) means the Accessibility grant for
the ⌥ hotkey is tied to a path that moves or a process macOS doesn't trust —
see the README's warning. A signed, stable .app bundle is the fix, not a
workaround for one.
"""

import glob
import os
import sysconfig
import tempfile

from setuptools import setup

# py2app assumes zlib is ALWAYS a loadable .so (its own site.py template says so
# explicitly: "zlib is not a built-in module (!)") and copies zlib.__file__ next
# to the zipped stdlib, for the bootstrap's zipimport — which can't load C
# extensions from inside a zip — to find it.
#
# uv's python-build-standalone links zlib STATICALLY into the interpreter, so
# there is no separate file, and py2app crashes on `zlib.__file__` with an
# AttributeError. That's fine: a statically-linked zlib is already available to
# the bootstrap without a dynload copy at all, so nothing is actually lost by
# skipping it — this just gives py2app's copy_file call a real (inert, unused)
# file to point at instead of failing outright.
import zlib  # noqa: E402

if not hasattr(zlib, "__file__"):
    _dummy = tempfile.NamedTemporaryFile(suffix=".so", delete=False)
    _dummy.close()
    zlib.__file__ = _dummy.name

APP = ["src/main.py"]

# charset_normalizer's compiled wheel imports a hash-named mypyc runtime module.
# Modulegraph cannot see that import from inside the extension, so include whichever
# runtime accompanies the installed wheel rather than pinning its version-specific hash.
MYPYC_MODULES = [
    os.path.basename(path).split(".")[0]
    for path in glob.glob(os.path.join(sysconfig.get_path("platlib"), "*__mypyc*.so"))
]

DATA_FILES = [
    # panel.html sits next to ui.py when running from source; ui.py falls back
    # to RESOURCEPATH (this directory, inside the bundle) when frozen.
    ("", ["src/panel.html"]),
    ("assets", [
        f for f in glob.glob("assets/*")
        if not f.endswith((".DS_Store",)) and "/candidates/" not in f
    ]),
]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/Snappy.icns",
    # py2app's static import scan misses packages that load native extensions
    # or plugins dynamically (ctranslate2, av, tokenizers, onnxruntime under
    # faster_whisper; cryptography's backends) — list them explicitly so the
    # whole package, not just what modulegraph could trace, gets bundled.
    "packages": [
        "rumps",
        "objc",
        "faster_whisper",
        "ctranslate2",
        "av",
        "tokenizers",
        "huggingface_hub",
        "onnxruntime",
        "snaptrade_client",
        "anthropic",
        "cryptography",
        "sounddevice",
        # Not a dependency by name — it's sounddevice's own data package, holding
        # the bundled libportaudio.dylib. It has to be a real unzipped directory:
        # dlopen can't load a .dylib that py2app has zipped into python312.zip.
        "_sounddevice_data",
        "soundfile",
        "_soundfile_data",  # same story — bundled libsndfile.dylib
        "numpy",
        "dotenv",
        "requests",
        "websockets",
        "charset_normalizer",
    ],
    "includes": [
        "AppKit", "WebKit", "Foundation", "Quartz", "ApplicationServices",
        *MYPYC_MODULES,
    ],
    "plist": {
        "CFBundleName": "Snappy",
        "CFBundleDisplayName": "Snappy",
        "CFBundleIdentifier": "com.ericmxf17.snappy",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        # Menubar-only: no Dock icon, no app menu. rumps sets the activation
        # policy itself at runtime, but declaring it here too means no Dock
        # icon flashes during the (slower, cold) first launch.
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": (
            "Snappy records audio only while you hold Option (⌥), so it can "
            "hear the question you're asking."
        ),
        "NSHumanReadableCopyright": "Eric — github.com/ericmxf17/snappy",
    },
}

setup(
    name="Snappy",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
