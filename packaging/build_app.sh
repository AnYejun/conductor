#!/bin/bash
# Build Conductor.app (+ DMG) — macOS.
#   ./packaging/build_app.sh
# Needs: pip install pyinstaller pywebview
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(python3 -c "import conductor; print(conductor.__version__)")
echo "building Conductor.app v${VERSION} (onedir — fast startup)"

python3 -m PyInstaller packaging/entry.py \
  --name Conductor \
  --windowed \
  --onedir \
  --noconfirm \
  --clean \
  --icon "$(pwd)/packaging/conductor.icns" \
  --osx-bundle-identifier dev.anyejun.conductor \
  --hidden-import webview.platforms.cocoa \
  --collect-submodules anthropic \
  --distpath dist --workpath build --specpath build

# version + display name in Info.plist
PLIST="dist/Conductor.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${VERSION}" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string ${VERSION}" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName conductor" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string conductor" "$PLIST"

# ad-hoc deep sign so macOS runs it locally without fuss
codesign --force --deep -s - "dist/Conductor.app"

# DMG
hdiutil create -volname "conductor" -srcfolder "dist/Conductor.app" \
  -ov -format UDZO "dist/Conductor-${VERSION}.dmg" >/dev/null

echo "done:"
du -sh dist/Conductor.app "dist/Conductor-${VERSION}.dmg"
echo "note: distribution beyond your own machines needs a Developer ID + notarization."
