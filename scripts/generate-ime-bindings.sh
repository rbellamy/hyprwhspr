#!/bin/bash
# Regenerate pywayland bindings for input-method-unstable-v2.
# Run this when the protocol XML changes (rare).
# Requires: python-pywayland, wayland (for wayland.xml)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$REPO_ROOT/lib/src/ime_protocol"
PROTO_XML="$REPO_ROOT/protocols/input-method-unstable-v2.xml"
WAYLAND_XML="/usr/share/wayland/wayland.xml"

if [[ ! -f "$PROTO_XML" ]]; then
    echo "Error: $PROTO_XML not found"
    echo "Copy from Hyprland source: protocols/input-method-unstable-v2.xml"
    exit 1
fi

if [[ ! -f "$WAYLAND_XML" ]]; then
    echo "Error: $WAYLAND_XML not found (install wayland package)"
    exit 1
fi

# Generate into a temp dir first, then copy what we need
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

python3 -m pywayland.scanner \
    --with-protocols \
    -i "$WAYLAND_XML" "$PROTO_XML" \
    -o "$TMPDIR"

# Keep only the modules we need
rm -rf "$OUTPUT_DIR/input_method_unstable_v2" "$OUTPUT_DIR/wayland"
cp -r "$TMPDIR/input_method_unstable_v2" "$OUTPUT_DIR/"
cp -r "$TMPDIR/wayland" "$OUTPUT_DIR/"

echo "Generated bindings in $OUTPUT_DIR"
