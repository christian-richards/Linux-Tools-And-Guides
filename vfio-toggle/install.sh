#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# DESTDIR=/some/dir stages the install into that directory (for testing/packaging).
DESTDIR="${DESTDIR:-}"
BIN_DIR="${DESTDIR}/usr/bin"
CONF_DIR="${DESTDIR}/etc/vfio-toggle"
HOOK_DIR="${CONF_DIR}/hooks.d"
CONF="${CONF_DIR}/vfio-toggle.conf"

# Unified hook script name
HOOK_NAME="10-gpu-units"
LEGACY_EVENTS=(pre-bind post-bind pre-unbind post-unbind rollback)

if [[ ${EUID} -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi

# Ensure required scripts exist
for f in vfio-toggle.py vfio-wrapper.sh gpu-units.sh; do
    if [[ ! -f "${SCRIPT_DIR}/${f}" ]]; then
        echo "ERROR: ${SCRIPT_DIR}/${f} not found." >&2
        exit 1
    fi
done

# Resolve the source configuration file:
# Always use the config in the current/script directory by default.
if [[ -f "${SCRIPT_DIR}/vfio-toggle.conf" ]]; then
    SRC_CONF="${SCRIPT_DIR}/vfio-toggle.conf"
elif [[ -f "${SCRIPT_DIR}/vfio-toggle.conf.example" ]]; then
    SRC_CONF="${SCRIPT_DIR}/vfio-toggle.conf.example"
else
    echo "ERROR: No config file found in ${SCRIPT_DIR} (expected vfio-toggle.conf or vfio-toggle.conf.example)." >&2
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    echo "ERROR: vfio-toggle needs Python 3.9 or newer (python3 not found or too old)." >&2
    exit 1
fi

echo "==> Removing old installation and config..."
${SUDO} rm -f "${BIN_DIR}/vfio-toggle.py"
${SUDO} rm -f "${BIN_DIR}/vfio-wrapper.sh"

# Unconditionally delete any previous config (no backups, no preservation)
${SUDO} rm -f "${CONF}" "${CONF}.bak" "${CONF}.example"

# Clean up hooks from earlier installs (both single-file and legacy subdirectories)
${SUDO} rm -f "${HOOK_DIR}/${HOOK_NAME}"
for event in "${LEGACY_EVENTS[@]}"; do
    ${SUDO} rm -f "${HOOK_DIR}/${event}/${HOOK_NAME}"
    # Remove old event directory if empty
    if [[ -d "${HOOK_DIR}/${event}" ]]; then
        ${SUDO} rmdir "${HOOK_DIR}/${event}" 2>/dev/null || true
    fi
done

# Clean up any leftover runtime modprobe block files from older crashes
if [[ -z "${DESTDIR}" ]]; then
    ${SUDO} rm -f /run/modprobe.d/vfio-toggle-block.conf
fi

echo "==> Installing vfio-toggle..."
${SUDO} install -d -m 755 "${BIN_DIR}" "${CONF_DIR}"
${SUDO} install -m 755 "${SCRIPT_DIR}/vfio-toggle.py" "${BIN_DIR}/vfio-toggle.py"
${SUDO} install -m 755 "${SCRIPT_DIR}/vfio-wrapper.sh" "${BIN_DIR}/vfio-wrapper.sh"

echo "==> Overwriting config with ${SRC_CONF}..."
${SUDO} install -m 600 "${SRC_CONF}" "${CONF}"

echo "==> Installing display manager hook (single-file format)..."
${SUDO} install -d -m 755 "${HOOK_DIR}"
${SUDO} install -m 755 "${SCRIPT_DIR}/gpu-units.sh" "${HOOK_DIR}/${HOOK_NAME}"

if [[ -z "${DESTDIR}" ]]; then
    echo "==> Checking installation..."
    ${SUDO} "${BIN_DIR}/vfio-toggle.py" --version
fi

echo "==> Done."
echo "    Python script: /usr/bin/vfio-toggle.py"
echo "    Wrapper:       /usr/bin/vfio-wrapper.sh"
echo "    Config:        /etc/vfio-toggle/vfio-toggle.conf (replaced from ${SRC_CONF})"
echo "    Hook:          /etc/vfio-toggle/hooks.d/${HOOK_NAME}"
echo ""
echo "Next steps:"
echo "  1. Review /etc/vfio-toggle/vfio-toggle.conf"
echo "  2. Run: sudo vfio-toggle.py list-devices"
echo "  3. Run: sudo vfio-toggle.py status"
echo "  4. Preview a bind without changing anything:"
echo "          sudo vfio-toggle.py -n bind"
echo "  5. Test bind/unbind using the wrapper:"
echo "          sudo vfio-wrapper.sh bind"
echo "          sudo vfio-wrapper.sh unbind"
