#!/usr/bin/env bash
#
# vfio-wrapper.sh - Wrapper to run vfio-toggle.sh detached from user sessions.
# This prevents the script from being killed when it tears down the GUI/SSH session.

if (( EUID != 0 )); then
  echo "Error: This wrapper must be run as root." >&2
  echo "Try: sudo $0 $*" >&2
  exit 1
fi

# Find the toggle script in the same directory
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
TARGET="$DIR/vfio-toggle.sh"

if [[ ! -x "$TARGET" ]]; then
  # Fallback to PATH if not in the same directory
  TARGET="$(command -v vfio-toggle.sh || true)"
fi

if [[ ! -x "$TARGET" ]]; then
  echo "Error: Cannot find executable vfio-toggle.sh" >&2
  exit 1
fi

UNIT_NAME="vfio-toggle-run"

# Clear out any leftover state from a previous background run
systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true

echo "Launching vfio-toggle.sh as a detached system service..."

# systemd-run hands execution over to PID 1, completely outside your user session slice
systemd-run \
  --unit="$UNIT_NAME" \
  --property="TimeoutStopSec=300" \
  --collect \
  "$TARGET" "$@"

echo "========================================================="
echo "Script is now running safely in the background."
echo "Watch the live output by running:"
echo "  sudo tail -f /var/log/vfio-toggle.log"
echo "========================================================="
