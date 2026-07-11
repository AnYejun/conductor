#!/bin/bash
# Build the headless engine as a single-file binary for the Tauri sidecar.
set -euo pipefail
cd "$(dirname "$0")/.."

TRIPLE=$(rustc -vV | awk '/^host:/ {print $2}' 2>/dev/null || echo "aarch64-apple-darwin")
OUT="desktop-tauri/src-tauri/binaries/conductor-serve-${TRIPLE}"

python3 -m PyInstaller packaging/serve_entry.py \
  --name conductor-serve \
  --onefile --noconfirm --clean \
  --distpath dist-sidecar --workpath build-sidecar --specpath build-sidecar 2>&1 | tail -2

mkdir -p desktop-tauri/src-tauri/binaries
cp dist-sidecar/conductor-serve "$OUT"
chmod +x "$OUT"
echo "sidecar → $OUT"
du -sh "$OUT"
