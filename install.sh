#!/bin/bash
# Installs (or with -u removes) the assisted_screws Klipper extra.
set -e

KLIPPER_HOME="${KLIPPER_HOME:-$HOME/klipper}"
CONFIG_HOME="${CONFIG_HOME:-$HOME/printer_data/config}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINK="$KLIPPER_HOME/klippy/extras/assisted_screws.py"
MOONRAKER_CONF="$CONFIG_HOME/moonraker.conf"

if [ "$1" = "-u" ]; then
    rm -f "$LINK"
    echo "Removed $LINK. Delete [assisted_screws] and [update_manager assisted_screws]"
    echo "from your config, then restart Klipper."
    exit 0
fi

if [ ! -d "$KLIPPER_HOME/klippy/extras" ]; then
    echo "Klipper not found at $KLIPPER_HOME (set KLIPPER_HOME=...)" >&2
    exit 1
fi

ln -sf "$SRC/assisted_screws.py" "$LINK"
echo "Linked $LINK"

origin="$(git -C "$SRC" remote get-url origin 2>/dev/null || true)"
if [ -n "$origin" ] && [ -f "$MOONRAKER_CONF" ] &&
   ! grep -q '^\[update_manager assisted_screws\]' "$MOONRAKER_CONF"; then
    cat >> "$MOONRAKER_CONF" <<EOF

[update_manager assisted_screws]
type: git_repo
path: $SRC
origin: $origin
primary_branch: main
managed_services: klipper
EOF
    echo "Added [update_manager assisted_screws] to $MOONRAKER_CONF"
fi

echo
echo "Next: add an [assisted_screws] section (see assisted_screws.cfg) to your"
echo "config, then restart the Klipper service: sudo systemctl restart klipper"
