#!/bin/bash
# Build Snappy.app and wrap it in a .dmg for one-click install.
#
#     ./scripts/build_dmg.sh
#
# Produces dist/Snappy.dmg: a disk image with Snappy.app and an Applications
# symlink side by side, so installing is drag-and-drop.
#
# The app is unsigned — there's no Apple Developer account behind this build.
# First launch will need a right-click → Open (or a pass through System
# Settings → Privacy & Security), same as any unsigned app from outside the
# App Store. Notarizing it is a separate step for whenever this gets an
# Apple Developer ID.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="./venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing venv. Create it with: uv venv --python 3.12 venv" >&2
  exit 1
fi

# py2app must already be present as a normal requirement. Do not use setup_requires:
# uv virtual environments may intentionally omit pip, and setuptools' legacy installer
# crashes while trying to run `python -m pip` in those environments.
if ! "$PYTHON" -c 'import py2app' >/dev/null 2>&1; then
  echo "Missing build dependencies. Run:" >&2
  echo "  VIRTUAL_ENV=\"$PWD/venv\" uv pip install -r requirements.txt" >&2
  exit 1
fi

rm -rf build dist
mkdir -p dist

# macOS 26 can attach provenance metadata to an in-progress .app under Documents,
# then refuse py2app's later install-name edits. Build atomically outside Documents
# and move only the finished artifacts back.
BUILD_TMP_DIR="$(mktemp -d -t snappy-build)"
cleanup() {
  rm -rf "$BUILD_TMP_DIR"
}
trap cleanup EXIT

"$PYTHON" setup.py py2app \
  --bdist-base "$BUILD_TMP_DIR/build" \
  --dist-dir "$BUILD_TMP_DIR/dist"

APP="dist/Snappy.app"
DMG="dist/Snappy.dmg"
TMP_APP="$BUILD_TMP_DIR/dist/Snappy.app"
TMP_DMG="$BUILD_TMP_DIR/Snappy.dmg"

# The temporary dist directory is also the DMG staging folder.
ln -s /Applications "$BUILD_TMP_DIR/dist/Applications"
hdiutil create -volname "Snappy" -srcfolder "$BUILD_TMP_DIR/dist" \
  -ov -format UDZO "$TMP_DMG"
rm "$BUILD_TMP_DIR/dist/Applications"

mv "$TMP_APP" "$APP"
mv "$TMP_DMG" "$DMG"

echo
echo "Built $DMG"
