#!/bin/bash
# Deploys this repo's addon.py to Blender's actual installed addon location.
#
# Real incident this exists to prevent: the repo's addon.py and Blender's
# installed copy ("addon copy.py" in Blender's own addons folder) are two
# separate files with no sync mechanism. An hour was lost editing addon.py,
# reloading Blender, and confirming nothing changed — because the edits
# were never reaching the file Blender actually runs. This script is the
# one-command fix: find the real installed path, back it up (matching the
# existing "addon copy.py.bak-pre-*" naming convention already in use),
# copy the repo file over it, and verify the copy actually matches.
#
# This does NOT reload Blender — Blender must still be told to pick up the
# change (Preferences > Add-ons > toggle BlenderMCP off/on, or restart
# Blender). A full bpy.ops.script.reload() is deliberately NOT triggered
# here or from any Claude-driven code path: it stops the addon's own
# socket server with no reliable way to restart it programmatically,
# confirmed live — that's the other real incident tonight.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ADDON="$REPO_DIR/addon.py"

if [ ! -f "$REPO_ADDON" ]; then
    echo "ERROR: $REPO_ADDON not found." >&2
    exit 1
fi

# Find the real installed addon file(s) — search all installed Blender
# versions rather than hardcoding one version number, since that will
# silently go stale the next time Blender updates.
BLENDER_ADDONS_ROOT="$HOME/Library/Application Support/Blender"
if [ ! -d "$BLENDER_ADDONS_ROOT" ]; then
    echo "ERROR: $BLENDER_ADDONS_ROOT not found — is Blender installed?" >&2
    exit 1
fi

FOUND_TARGETS=()
while IFS= read -r -d '' f; do
    FOUND_TARGETS+=("$f")
done < <(find "$BLENDER_ADDONS_ROOT" -maxdepth 4 -type f -name "addon copy.py" -print0 2>/dev/null)

if [ "${#FOUND_TARGETS[@]}" -eq 0 ]; then
    echo "ERROR: No installed 'addon copy.py' found under $BLENDER_ADDONS_ROOT." >&2
    echo "If Blender's addon file has a different name, edit TARGET_NAME in this script." >&2
    exit 1
fi

if [ "${#FOUND_TARGETS[@]}" -gt 1 ]; then
    echo "Found multiple installed copies (one per Blender version) — deploying to all:"
    printf '  %s\n' "${FOUND_TARGETS[@]}"
fi

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

for TARGET in "${FOUND_TARGETS[@]}"; do
    BACKUP="${TARGET}.bak-${TIMESTAMP}"
    cp "$TARGET" "$BACKUP"
    cp "$REPO_ADDON" "$TARGET"
    if diff -q "$REPO_ADDON" "$TARGET" > /dev/null 2>&1; then
        echo "OK: deployed to $TARGET"
        echo "    backup: $BACKUP"
    else
        echo "ERROR: deployed but diff still shows a difference at $TARGET — investigate before trusting this deploy." >&2
        exit 1
    fi
done

echo ""
echo "Deployed. Blender will NOT pick this up automatically — in Blender:"
echo "  Preferences > Add-ons > toggle BlenderMCP off, then on again"
echo "  (or restart Blender entirely)"
echo "Do NOT use bpy.ops.script.reload() — it stops the socket server with no reliable way back in."
