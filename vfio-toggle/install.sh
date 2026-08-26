#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

echo "==> Removing existing files..."
sudo rm -f /usr/bin/vfio-toggle.py
sudo rm -f /usr/bin/vfio-wrapper.sh
sudo rm -f /etc/vfio-toggle/vfio-toggle.conf

echo "==> Installing vfio-toggle..."
sudo mkdir -p /etc/vfio-toggle
sudo install -m 755 "${SCRIPT_DIR}/vfio-toggle.py" /usr/bin/vfio-toggle.py
sudo install -m 755 "${SCRIPT_DIR}/vfio-wrapper.sh" /usr/bin/vfio-wrapper.sh
sudo cp "${SCRIPT_DIR}/vfio-toggle.conf.example" /etc/vfio-toggle/vfio-toggle.conf
sudo chmod 600 /etc/vfio-toggle/vfio-toggle.conf

echo "==> Done."
echo "    Python script: /usr/bin/vfio-toggle.py"
echo "    Wrapper:       /usr/bin/vfio-wrapper.sh"
echo "    Config:        /etc/vfio-toggle/vfio-toggle.conf"
echo ""
echo "Next steps:"
echo "  1. Edit /etc/vfio-toggle/vfio-toggle.conf to set your gpu_pci_ids"
echo "  2. Run: sudo vfio-toggle.py list-devices"
echo "  3. Run: sudo vfio-toggle.py status"
echo "  4. To safely test bind/unbind without your terminal session being killed:"
echo "          sudo vfio-wrapper.sh bind"
echo "          sudo vfio-wrapper.sh unbind"
