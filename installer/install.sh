#!/usr/bin/env bash
# Installs (or upgrades) the OpenLP Cloud Sync plugin on macOS and Linux.
#
#   ./install.sh              install / upgrade
#   ./install.sh --uninstall   remove the plugin
#
# An existing install is moved to a timestamped backup first; your sign-in
# token and sync state live elsewhere and are never touched.
set -euo pipefail

PLUGIN="cloudsync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_DIR="$SCRIPT_DIR/$PLUGIN"

if [[ "$(uname)" == "Darwin" ]]; then
    DATA_DIR="$HOME/Library/Application Support/openlp/Data"
else
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/openlp"
fi
TARGET_DIR="$DATA_DIR/contrib/plugins/$PLUGIN"

version() { [[ -f "$PAYLOAD_DIR/version.txt" ]] && tr -d '[:space:]' < "$PAYLOAD_DIR/version.txt" || echo unknown; }

if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ -d "$TARGET_DIR" ]]; then
        backup="$TARGET_DIR.uninstalled-$(date +%Y%m%d-%H%M%S)"
        mv "$TARGET_DIR" "$backup"
        echo "Removed $TARGET_DIR"
        echo "(kept a copy at $backup)"
    else
        echo "Nothing to remove: $TARGET_DIR does not exist."
    fi
    exit 0
fi

[[ -f "$PAYLOAD_DIR/cloudsyncplugin.py" ]] || { echo "ERROR: installer files are incomplete (missing $PAYLOAD_DIR/cloudsyncplugin.py)." >&2; exit 1; }
[[ -d "$DATA_DIR" ]] || { echo "ERROR: OpenLP data folder not found at $DATA_DIR. Install OpenLP and run it once first." >&2; exit 1; }

if pgrep -x openlp >/dev/null 2>&1; then
    echo "WARNING: OpenLP is running. Close it, then press Enter to continue (Ctrl+C to cancel)."
    read -r _
fi

if [[ -d "$TARGET_DIR" ]]; then
    backup="$TARGET_DIR.backup-$(date +%Y%m%d-%H%M%S)"
    mv "$TARGET_DIR" "$backup"
    echo "Backed up previous install to $backup"
fi

mkdir -p "$TARGET_DIR"
cp -R "$PAYLOAD_DIR"/. "$TARGET_DIR"/
[[ -f "$TARGET_DIR/cloudsyncplugin.py" ]] || { echo "ERROR: install failed, cloudsyncplugin.py missing after copy." >&2; exit 1; }

{
    echo "OpenLP Cloud Sync plugin v$(version)"
    echo "Installed $(date '+%Y-%m-%d %H:%M:%S') by install.sh"
} > "$TARGET_DIR/installed-by.txt"

echo
echo "Installed Cloud Sync v$(version) to $TARGET_DIR"
echo
echo "Next steps:"
echo "  1. Start OpenLP."
echo "  2. If the plugin does not appear, enable it under Settings > Plugins."
echo "  3. Open the Cloud Sync settings and sign in with Google if prompted."
