#!/usr/bin/env bash
#
# vfio-toggle.sh — dynamically bind/unbind a GPU (and its whole IOMMU group)
# to/from vfio-pci for VFIO/KVM GPU passthrough.
#
# Design goals (see README.md for the full rationale):
#   - Use only stable kernel/sysfs/systemd interfaces, never fragile hacks
#     like `vfio-pci ids=` in modprobe.d or new_id/remove_id.
#   - Discover everything dynamically: which driver a device is on, which
#     kernel modules depend on it (and in what order to remove/reload
#     them), which display-manager unit is active, which processes are
#     using the GPU. Nothing about a specific vendor/DE/distro is
#     hardcoded.
#   - Journal every destructive step as it happens and persist it to disk,
#     so a failure partway through can be rolled back automatically.
#
# Usage: vfio-toggle.sh <bind|unbind|status|list-devices|rollback> [opts]
# Run with --help for details. Must be run as root (except status/list-devices).
#
set -Eeuo pipefail
shopt -s nullglob

# ============================================================================
# Globals & defaults
# ============================================================================

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# sysfs/proc/dev roots. Overridable via environment for testing; on a real
# system these are always the standard paths.
: "${SYSFS_PCI:=/sys/bus/pci}"
: "${SYSFS_MODULE:=/sys/module}"
: "${SYSFS_IOMMU_GROUPS:=/sys/kernel/iommu_groups}"
: "${SYSFS_PLATFORM:=/sys/bus/platform}"
: "${SYSFS_CLASS:=/sys/class}"
: "${SYSFS_VTCONSOLE:=/sys/class/vtconsole}"
: "${DEV_DIR:=/dev}"

# Built-in defaults; any of these may be overridden by the config file, or
# (mainly for testing) by pre-setting them in the environment before the
# script runs.
: "${LOG_FILE:=/var/log/vfio-toggle.log}"
: "${STATE_FILE:=/var/lib/vfio-toggle/state}"
: "${LOCK_FILE:=/run/vfio-toggle.lock}"
LOG_LEVEL="INFO"
# The log is a circular buffer capped at this many bytes: oldest lines are
# dropped in place as new ones are written. No .1/.2 backup files are ever
# created -- there is always exactly one log file, at LOG_FILE.
LOG_MAX_BYTES=1048576
# Restart the display manager after 'bind' completes (GPU now on
# vfio-pci). Optional -- only useful if the host has a secondary GPU to
# fall back to. See RESTART_DISPLAY_MANAGER_AFTER_BIND handling below;
# unlike this, restarting the display manager after 'unbind' (restoring
# host graphics) is NOT optional and always happens.
RESTART_DISPLAY_MANAGER_AFTER_BIND="true"
ALLOW_FULL_MODULE_UNLOAD="true"
RELEASE_BOOT_FRAMEBUFFER="true"
RELEASE_VT_CONSOLE="true"
ALLOW_FUNCTION_LEVEL_RESET="true"
PROCESS_KILL_GRACE_PERIOD=10
AUTO_ROLLBACK="true"
GPU_PCI_IDS=()

declare -A LOG_LEVELS=([DEBUG]=0 [INFO]=1 [WARN]=2 [ERROR]=3)

# Runtime/state globals (populated as the script runs; persisted to
# STATE_FILE via `declare -p` so they round-trip exactly, including the
# associative array, across separate invocations of the script).
declare -a JOURNAL=()
declare -A ORIG_DRIVER=()
declare -a REMOVED_MODULES=()
declare -a RELEASED_VTCONSOLES=()
declare -a STOPPED_SERVICES=()
declare -A VISITED_MODULES=()
DM_WAS_ACTIVE=""
DM_UNIT=""
STATE_OPERATION=""

declare -a TARGET_DEVICES=()
declare -a RESTORE_DEVICES=()

CONFIG_PATH=""
VERBOSE="false"
COMMAND=""
ROLLING_BACK="0"
LOCK_ACQUIRED=""
LOCK_FD=""

MODPROBE_BLOCK_FILE="/run/modprobe.d/vfio-toggle-block.conf"

# ============================================================================
# Logging
# ============================================================================

_log() {
  local level="$1"; shift
  local msg="$*"
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S%z' 2>/dev/null || date)"
  local line="[$ts] [pid:$$] [$level] $msg"
  if [[ -n "${LOG_FILE:-}" ]]; then
    printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true
    circular_trim_log
  fi
  local threshold="${LOG_LEVELS[${LOG_LEVEL:-INFO}]:-1}"
  local this="${LOG_LEVELS[$level]:-1}"
  if (( this >= threshold )); then
    if [[ "$level" == "ERROR" || "$level" == "WARN" ]]; then
      printf '%s\n' "$line" >&2
    else
      printf '%s\n' "$line"
    fi
  fi
  return 0
}
log_debug() { _log DEBUG "$@"; }
log_info()  { _log INFO  "$@"; }
log_warn()  { _log WARN  "$@"; }
log_error() { _log ERROR "$@"; }

die_early() {
  log_error "$1"
  exit 1
}

circular_trim_log() {
  [[ -f "$LOG_FILE" ]] || return 0
  local size
  size="$(stat -c '%s' "$LOG_FILE" 2>/dev/null || echo 0)"
  if (( size > LOG_MAX_BYTES )); then
    tail -c "$LOG_MAX_BYTES" "$LOG_FILE" 2>/dev/null | tail -n +2 > "${LOG_FILE}.trim" 2>/dev/null \
      && mv -f "${LOG_FILE}.trim" "$LOG_FILE" 2>/dev/null
    chmod 600 "$LOG_FILE" 2>/dev/null || true
  fi
  return 0
}

setup_logging() {
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  touch "$LOG_FILE" 2>/dev/null || true
  chmod 600 "$LOG_FILE" 2>/dev/null || true
  circular_trim_log
  if [[ "$VERBOSE" == "true" ]]; then
    LOG_LEVEL="DEBUG"
  fi
  log_info "===== $SCRIPT_NAME starting: command='$COMMAND' pid=$$ ====="
}

# ============================================================================
# Error handling / rollback plumbing
# ============================================================================

abort() {
  local msg="$1"
  log_error "$msg"
  maybe_rollback
  release_lock
  exit 1
}

maybe_rollback() {
  if [[ "${ROLLING_BACK}" == "1" ]]; then
    return 0
  fi
  if [[ -z "${LOCK_ACQUIRED}" ]]; then
    return 0
  fi
  if (( ${#JOURNAL[@]} == 0 )); then
    log_debug "No journaled steps to roll back."
    return 0
  fi
  if [[ "${AUTO_ROLLBACK}" != "true" ]]; then
    log_warn "AUTO_ROLLBACK is disabled; leaving the system in its current partially-modified state. Run '$SCRIPT_NAME rollback' to roll back manually using $STATE_FILE."
    return 0
  fi
  ROLLING_BACK="1"
  log_warn "Attempting automatic rollback of ${#JOURNAL[@]} recorded step(s)..."
  rollback_from_journal
  ROLLING_BACK="0"
}

on_error_trap() {
  local line_no="$1" cmd="$2" code="$3"
  if [[ "${ROLLING_BACK}" == "1" ]]; then
    log_error "Rollback step itself failed near line $line_no (exit $code): $cmd"
    return 0
  fi
  log_error "Unhandled error (exit $code) at line $line_no while executing: $cmd"
  maybe_rollback
  release_lock
  exit "$code"
}
trap 'on_error_trap "$LINENO" "$BASH_COMMAND" "$?"' ERR

on_signal_trap() {
  local sig="$1"
  # Suspend signal/error traps to prevent recursion while rolling back
  trap '' INT TERM HUP ERR
  log_error "Caught signal $sig! Terminating."
  maybe_rollback
  release_lock

  local exit_code=1
  case "$sig" in
    INT)  exit_code=130 ;;
    HUP)  exit_code=129 ;;
    TERM) exit_code=143 ;;
  esac
  exit "$exit_code"
}
trap 'on_signal_trap INT'  INT
trap 'on_signal_trap TERM' TERM
trap 'on_signal_trap HUP'  HUP

# ============================================================================
# Locking
# ============================================================================

acquire_lock() {
  mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
  exec {LOCK_FD}>"$LOCK_FILE" || die_early "Could not open lock file '$LOCK_FILE'."
  if ! flock -n "$LOCK_FD"; then
    die_early "Another instance of $SCRIPT_NAME appears to be running (lock '$LOCK_FILE' is held). Refusing to run concurrently."
  fi
  LOCK_ACQUIRED="1"
  log_debug "Acquired lock: $LOCK_FILE"
}

release_lock() {
  if [[ -n "${LOCK_ACQUIRED}" && -n "${LOCK_FD}" ]]; then
    allow_driver_autoload # Ensure blocklist NEVER leaks on early abort/failure
    flock -u "$LOCK_FD" 2>/dev/null || true
    exec {LOCK_FD}>&- 2>/dev/null || true
    LOCK_ACQUIRED=""
  fi
}

# ============================================================================
# Kernel module dependency graph
# ============================================================================

normalize_module_name() {
  printf '%s' "${1//-/_}"
}

module_holders() {
  local mod; mod="$(normalize_module_name "$1")"
  local dir="${SYSFS_MODULE}/${mod}/holders"
  [[ -d "$dir" ]] || return 0
  local h
  for h in "$dir"/*; do
    [[ -e "$h" ]] || continue
    basename "$h"
  done
}

_get_all_holders_recurse() {
  local mod="$1"
  [[ "${_GAH_VISITED[$mod]:-}" == "1" ]] && return 0
  _GAH_VISITED["$mod"]=1
  echo "$mod"
  local holder
  while IFS= read -r holder; do
    [[ -n "$holder" ]] && _get_all_holders_recurse "$holder"
  done < <(module_holders "$mod")
}

# Recursively resolves all modules that depend on the given base module
get_all_holders() {
  local -A _GAH_VISITED=()
  _get_all_holders_recurse "$(normalize_module_name "$1")"
}

is_module_loaded() {
  local mod; mod="$(normalize_module_name "$1")"
  [[ -d "${SYSFS_MODULE}/${mod}" ]]
}

unload_module_tree() {
  local mod; mod="$(normalize_module_name "$1")"
  if [[ "${VISITED_MODULES[$mod]:-}" == "1" ]]; then
    return 0
  fi
  VISITED_MODULES["$mod"]=1

  local holder
  while IFS= read -r holder; do
    [[ -z "$holder" ]] && continue
    unload_module_tree "$holder"
  done < <(module_holders "$mod")

  is_module_loaded "$mod" || return 0

  log_info "Removing kernel module: $mod"

  local removed=0
  if modprobe -r "$mod" 2>>"${LOG_FILE}"; then
    removed=1
  elif rmmod "$mod" 2>>"${LOG_FILE}"; then
    removed=1
    log_debug "Removed '$mod' via rmmod fallback."
  fi

  if (( removed == 1 )); then
    REMOVED_MODULES+=("$mod")
  else
    log_warn "Could not remove module '$mod' (still in use?). Continuing without full unload for this module tree; the per-device sysfs unbind should still let vfio-pci claim the device."
  fi
}

other_devices_using_driver() {
  local driver="$1"
  local d addr is_target t drv
  for d in "${SYSFS_PCI}"/devices/*; do
    [[ -e "$d" ]] || continue
    addr="$(basename "$d")"
    is_target=0
    for t in "${TARGET_DEVICES[@]}"; do
      if [[ "$t" == "$addr" ]]; then
        is_target=1
        break
      fi
    done
    if (( is_target == 1 )); then
      continue
    fi
    drv="$(pci_driver_of "$addr")"
    if [[ "$drv" == "$driver" ]]; then
      log_debug "Driver '$driver' is also used by $addr (outside the managed device set)."
      return 0
    fi
  done
  return 1
}

reload_removed_modules() {
  if (( ${#REMOVED_MODULES[@]} == 0 )); then
    log_debug "No previously-removed modules recorded; relying on modalias-based auto-detection instead."
    return 0
  fi
  local i mod
  for (( i=${#REMOVED_MODULES[@]}-1; i>=0; i-- )); do
    mod="${REMOVED_MODULES[$i]}"
    log_info "Reloading kernel module: $mod"
    modprobe "$mod" 2>>"${LOG_FILE}" \
      || log_warn "Failed to reload module '$mod' by name (it may have been renamed/removed by a package update); relying on modalias-based detection instead."
  done
  journal_push "reload_modules"
}

# ============================================================================
# Modprobe Autoload Blocking
# ============================================================================

prevent_driver_autoload() {
  local -a drivers=("$@")
  if (( ${#drivers[@]} == 0 )); then
    return 0
  fi
  mkdir -p "/run/modprobe.d" 2>/dev/null || true
  > "$MODPROBE_BLOCK_FILE"

  local d h
  local -A to_block=()
  for d in "${drivers[@]}"; do
    # Autodetect and block all dependent modules (e.g. nvidia_drm, amdgpu_core, etc)
    while IFS= read -r h; do
      [[ -n "$h" ]] && to_block["$h"]=1
    done < <(get_all_holders "$d")
  done

  for d in "${!to_block[@]}"; do
    printf 'install %s /bin/false\n' "$d" >> "$MODPROBE_BLOCK_FILE"
  done
  log_debug "Created modprobe blocklist to prevent rogue reloads: ${!to_block[*]}"
}

allow_driver_autoload() {
  if [[ -f "$MODPROBE_BLOCK_FILE" ]]; then
    rm -f "$MODPROBE_BLOCK_FILE" 2>/dev/null || true
    log_debug "Removed modprobe blocklist."
  fi
}

# ============================================================================
# Config loading & validation
# ============================================================================

resolve_config_path() {
  if [[ -n "$CONFIG_PATH" ]]; then
    [[ -f "$CONFIG_PATH" ]] || die_early "Config file not found: $CONFIG_PATH"
    return 0
  fi
  local candidate
  for candidate in "/etc/vfio-toggle/vfio-toggle.conf" "${SCRIPT_DIR}/vfio-toggle.conf"; do
    if [[ -f "$candidate" ]]; then
      CONFIG_PATH="$candidate"
      return 0
    fi
  done
  die_early "No config file found. Looked in /etc/vfio-toggle/vfio-toggle.conf and ${SCRIPT_DIR}/vfio-toggle.conf."
}

assert_safe_to_source() {
  local f="$1"
  local owner perms go
  owner="$(stat -c '%u' "$f" 2>/dev/null || echo "?")"
  perms="$(stat -c '%a' "$f" 2>/dev/null || echo "???")"
  if [[ "$owner" != "0" ]]; then
    die_early "Refusing to load '$f': not owned by root (uid=$owner). It is sourced as root. Fix with: chown root:root '$f'"
  fi
  go="${perms: -2}"
  if [[ "${go:0:1}" =~ [2367] || "${go:1:1}" =~ [2367] ]]; then
    die_early "Refusing to load '$f': group/other-writable (mode $perms). Fix with: chmod 600 '$f'"
  fi
}

validate_config() {
  if (( ${#GPU_PCI_IDS[@]} == 0 )); then
    die_early "Config error: GPU_PCI_IDS is empty. Add at least one PCI address (see '$SCRIPT_NAME list-devices')."
  fi
  local id
  for id in "${GPU_PCI_IDS[@]}"; do
    if [[ ! "$id" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]]; then
      die_early "Config error: '$id' in GPU_PCI_IDS is not a valid PCI address (expected dddd:bb:dd.f, e.g. 0000:01:00.0)."
    fi
  done
  case "$RESTART_DISPLAY_MANAGER_AFTER_BIND" in
    true|false) ;;
    *) die_early "Config error: RESTART_DISPLAY_MANAGER_AFTER_BIND must be 'true' or 'false'." ;;
  esac
  case "$ALLOW_FULL_MODULE_UNLOAD" in
    true|false) ;;
    *) die_early "Config error: ALLOW_FULL_MODULE_UNLOAD must be 'true' or 'false'." ;;
  esac
  case "$RELEASE_BOOT_FRAMEBUFFER" in
    true|false) ;;
    *) die_early "Config error: RELEASE_BOOT_FRAMEBUFFER must be 'true' or 'false'." ;;
  esac
  case "$RELEASE_VT_CONSOLE" in
    true|false) ;;
    *) die_early "Config error: RELEASE_VT_CONSOLE must be 'true' or 'false'." ;;
  esac
  case "$ALLOW_FUNCTION_LEVEL_RESET" in
    true|false) ;;
    *) die_early "Config error: ALLOW_FUNCTION_LEVEL_RESET must be 'true' or 'false'." ;;
  esac
  case "$AUTO_ROLLBACK" in
    true|false) ;;
    *) die_early "Config error: AUTO_ROLLBACK must be 'true' or 'false'." ;;
  esac
  if [[ ! "$PROCESS_KILL_GRACE_PERIOD" =~ ^[0-9]+$ ]]; then
    die_early "Config error: PROCESS_KILL_GRACE_PERIOD must be a non-negative integer."
  fi
  if [[ ! "$LOG_MAX_BYTES" =~ ^[0-9]+$ ]] || (( LOG_MAX_BYTES < 10240 )); then
    die_early "Config error: LOG_MAX_BYTES must be an integer of at least 10240 (10 KiB)."
  fi
  case "$LOG_LEVEL" in
    DEBUG|INFO|WARN|ERROR) ;;
    *) die_early "Config error: LOG_LEVEL must be one of DEBUG, INFO, WARN, ERROR." ;;
  esac
  [[ -n "$LOG_FILE" ]] || die_early "Config error: LOG_FILE must not be empty."
  [[ -n "$STATE_FILE" ]] || die_early "Config error: STATE_FILE must not be empty."
}

require_config() {
  resolve_config_path
  assert_safe_to_source "$CONFIG_PATH"
  local old_log_file="$LOG_FILE"
  # shellcheck disable=SC1090
  source "$CONFIG_PATH"
  validate_config
  if [[ "$VERBOSE" == "true" ]]; then
    LOG_LEVEL="DEBUG"
  fi
  if [[ "$LOG_FILE" != "$old_log_file" ]]; then
    log_info "Config sets a different LOG_FILE ('$LOG_FILE'); switching log output to it."
    setup_logging
  fi
  log_info "Loaded config: $CONFIG_PATH"
}

check_dependencies() {
  local -a required=(lspci systemctl modprobe rmmod fuser flock stat readlink awk sort ps date mkdir cat basename dirname mv rm chmod uname kill grep tail)
  local -a missing=()
  local c
  for c in "${required[@]}"; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
  done
  if (( ${#missing[@]} > 0 )); then
    die_early "Missing required command(s): ${missing[*]}. Install the packages that provide them and re-run."
  fi
}

require_root() {
  if (( EUID != 0 )); then
    echo "Error: '$COMMAND' must be run as root." >&2
    exit 1
  fi
}

require_iommu() {
  if [[ ! -d "$SYSFS_IOMMU_GROUPS" ]] || [[ -z "$(ls -A "$SYSFS_IOMMU_GROUPS" 2>/dev/null)" ]]; then
    abort "No IOMMU groups found ($SYSFS_IOMMU_GROUPS is missing or empty). VFIO passthrough requires IOMMU enabled."
  fi
}

# ============================================================================
# PCI / sysfs helpers
# ============================================================================

pci_driver_of() {
  local addr="$1"
  local link="${SYSFS_PCI}/devices/${addr}/driver"
  if [[ -e "$link" ]]; then
    basename "$(readlink -f "$link")"
  else
    printf ''
  fi
}

pci_vendor_device_id() {
  local addr="$1"
  local v d
  v="$(cat "${SYSFS_PCI}/devices/${addr}/vendor" 2>/dev/null || echo "0x????")"
  d="$(cat "${SYSFS_PCI}/devices/${addr}/device" 2>/dev/null || echo "0x????")"
  printf '%s:%s' "${v#0x}" "${d#0x}"
}

pci_description() {
  local addr="$1"
  if command -v lspci >/dev/null 2>&1; then
    lspci -s "$addr" 2>/dev/null | head -n1 || true
  fi
}

expand_to_iommu_groups() {
  local -a input=("$@")
  local -A result_set=()
  local addr group_link group_dir dev
  for addr in "${input[@]}"; do
    [[ -e "${SYSFS_PCI}/devices/${addr}" ]] || abort "Configured PCI address '$addr' does not exist."
    group_link="${SYSFS_PCI}/devices/${addr}/iommu_group"
    if [[ ! -e "$group_link" ]]; then
      abort "PCI device '$addr' has no IOMMU group."
    fi
    group_dir="$(readlink -f "$group_link")"
    for dev in "$group_dir"/devices/*; do
      [[ -e "$dev" ]] || continue
      result_set["$(basename "$dev")"]=1
    done
  done
  printf '%s\n' "${!result_set[@]}" | sort
}

build_target_devices() {
  TARGET_DEVICES=()
  local addr
  while IFS= read -r addr; do
    [[ -n "$addr" ]] && TARGET_DEVICES+=("$addr")
  done < <(expand_to_iommu_groups "${GPU_PCI_IDS[@]}")
  if (( ${#TARGET_DEVICES[@]} == 0 )); then
    abort "No target devices resolved from GPU_PCI_IDS; check your config."
  fi
  log_debug "Target devices (after IOMMU group expansion): ${TARGET_DEVICES[*]}"
}

ensure_driver_loaded_for_device() {
  local addr="$1"
  local modalias_file="${SYSFS_PCI}/devices/${addr}/modalias"
  [[ -r "$modalias_file" ]] || return 0
  local hw_alias; hw_alias="$(cat "$modalias_file" 2>/dev/null || true)"
  [[ -n "$hw_alias" ]] || return 0
  modprobe "$hw_alias" 2>>"${LOG_FILE}" \
    || log_debug "modprobe by modalias found nothing new for $addr (driver may already be loaded, or none installed)."
}

ensure_vfio_pci_loaded() {
  if is_module_loaded "vfio-pci"; then
    log_debug "vfio-pci module already loaded."
    return 0
  fi
  log_info "Loading vfio-pci module."
  modprobe vfio-pci 2>>"${LOG_FILE}" \
    || abort "Failed to load the vfio-pci kernel module. Is it available for your running kernel ($(uname -r))? Check: modinfo vfio-pci"
}

# ============================================================================
# Boot framebuffer (efifb/vesafb/simplefb)
# ============================================================================

release_boot_framebuffer() {
  [[ "$RELEASE_BOOT_FRAMEBUFFER" == "true" ]] || return 0
  local dev_link drv_link drvname devname class_dir
  local -A unbound_fb=()

  # Dynamically search sysfs graphics instances for any matching platform drivers
  for class_dir in "${SYSFS_CLASS}"/graphics/fb* "${SYSFS_CLASS}"/drm/card*; do
    [[ -e "$class_dir" ]] || continue
    dev_link="${class_dir}/device"
    if [[ -e "$dev_link" ]]; then
      devname="$(basename "$(readlink -f "$dev_link")")"
      drv_link="${dev_link}/driver"

      if [[ -e "$drv_link" ]]; then
        drvname="$(basename "$(readlink -f "$drv_link")")"
        if [[ "${unbound_fb[$devname]:-}" == "1" ]]; then
          continue
        fi

        # Only detach framebuffers sitting on the "platform" bus so we don't accidentally knock offline a real PCIe GPU
        if [[ "$(readlink -f "${dev_link}/subsystem" 2>/dev/null || true)" == *"/bus/platform" ]]; then
          unbound_fb["$devname"]=1
          log_info "Releasing boot framebuffer platform device '$devname' (driver '$drvname') so the real GPU driver can unbind cleanly."
          if printf '%s\n' "$devname" > "${drv_link}/unbind" 2>>"${LOG_FILE}"; then
            journal_push "unbind_platform|${devname}|${drvname}"
          else
            log_warn "Could not unbind boot framebuffer '$devname'; this is often harmless, continuing."
          fi
        fi
      fi
    fi
  done
}

# ============================================================================
# VT console (fbcon) — /sys/class/vtconsole
# ============================================================================

release_vt_consoles() {
  [[ "$RELEASE_VT_CONSOLE" == "true" ]] || return 0
  local path vc bound
  for path in "${SYSFS_VTCONSOLE}"/vtcon*; do
    [[ -e "$path/bind" ]] || continue
    vc="$(basename "$path")"
    bound="$(cat "$path/bind" 2>/dev/null || echo "")"
    if [[ "$bound" == "0" ]]; then
      log_debug "VT console $vc is already unbound."
      continue
    fi
    log_info "Unbinding VT console $vc to ensure the GPU is released."
    if printf '0\n' > "$path/bind" 2>>"${LOG_FILE}"; then
      RELEASED_VTCONSOLES+=("$vc")
      journal_push "unbind_vtconsole|${vc}"
      persist_state
    else
      log_warn "Could not unbind VT console $vc; this is often harmless, continuing."
    fi
  done
}

restore_vt_consoles() {
  [[ "$RELEASE_VT_CONSOLE" == "true" ]] || return 0
  if (( ${#RELEASED_VTCONSOLES[@]} == 0 )); then
    log_debug "No VT consoles recorded as released; nothing to restore."
    return 0
  fi
  local vc path
  for vc in "${RELEASED_VTCONSOLES[@]}"; do
    path="${SYSFS_VTCONSOLE}/${vc}"
    [[ -e "$path/bind" ]] || continue
    log_info "Rebinding VT console $vc."
    if printf '1\n' > "$path/bind" 2>>"${LOG_FILE}"; then
      journal_push "rebind_vtconsole|${vc}"
    else
      log_warn "Could not rebind VT console $vc; you may need to do it manually: printf 1 > ${path}/bind"
    fi
  done
}

# ============================================================================
# Function-Level Reset — /sys/bus/pci/devices/<addr>/reset
# ============================================================================

attempt_function_level_reset() {
  local addr="$1"
  [[ "$ALLOW_FUNCTION_LEVEL_RESET" == "true" ]] || return 0

  local cur; cur="$(pci_driver_of "$addr")"
  if [[ -n "$cur" ]]; then
    log_debug "Skipping reset for $addr: still bound to '$cur' (must be unbound first)."
    return 0
  fi

  local reset_file="${SYSFS_PCI}/devices/${addr}/reset"
  if [[ ! -e "$reset_file" ]]; then
    log_debug "No 'reset' attribute for $addr (device/slot doesn't expose a safe reset method); skipping."
    return 0
  fi

  log_info "Performing a function-level reset on $addr."
  if printf '1\n' > "$reset_file" 2>>"${LOG_FILE}"; then
    log_debug "Reset succeeded for $addr."
  else
    log_warn "Reset failed or unsupported for $addr; continuing without it (this is usually harmless)."
  fi
}

# ============================================================================
# GPU device nodes & process management
# ============================================================================

device_nodes_for() {
  local addr="$1"
  local drm_dir="${SYSFS_PCI}/devices/${addr}/drm"
  local entry name
  if [[ -d "$drm_dir" ]]; then
    for entry in "$drm_dir"/*; do
      [[ -e "$entry" ]] || continue
      name="$(basename "$entry")"
      [[ -e "${DEV_DIR}/dri/${name}" ]] && printf '%s\n' "${DEV_DIR}/dri/${name}"
    done
  fi
  if [[ "$(pci_driver_of "$addr")" == "nvidia" ]]; then
    local n
    for n in "${DEV_DIR}"/nvidia*; do
      [[ -e "$n" ]] && printf '%s\n' "$n"
    done
  fi
}

find_pids_using_devices() {
  local -a nodes=("$@")
  if (( ${#nodes[@]} == 0 )); then
    return 0
  fi

  local raw1="" raw2=""

  # Utilize lsof to locate mmap-only mappings that omit open FDs
  if command -v lsof >/dev/null 2>&1; then
    raw1="$(lsof -t "${nodes[@]}" 2>/dev/null || true)"
  fi

  # Fallback to standard fuser
  raw2="$(fuser "${nodes[@]}" 2>/dev/null || true)"

  # printf '%s\n' applies the format uniformly to each argument, ensuring clean newlines before sorting
  printf '%s\n' "$raw1" "$raw2" | tr -s ' \t' '\n' | grep -E '^[0-9]+$' | sort -un || true
}

terminate_pids_safely() {
  local -a pids=("$@")
  if (( ${#pids[@]} == 0 )); then
    return 0
  fi

  local pid comm cmdline svc
  local -a to_wait=()
  local -A services_to_stop=()

  for pid in "${pids[@]}"; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    if (( pid == $$ )) || (( pid == 1 )); then
      log_warn "Refusing to signal PID $pid (this script or PID 1)."
      continue
    fi

    # Dynamically extract and catalog systemd services operating off the GPU
    if [[ -r "/proc/$pid/cgroup" ]]; then
      # Extract the most specific (deepest) .service unit from the cgroup path
      svc="$(grep -Eo '[^/]+\.service' "/proc/$pid/cgroup" 2>/dev/null | tail -n1 || true)"
      if [[ -n "$svc" && "$svc" != "${DM_UNIT:-}" && "$svc" != "display-manager.service" && "$svc" != user@*.service ]]; then
        local load_state="" slice=""
        if command -v systemctl >/dev/null 2>&1; then
          load_state="$(systemctl show -p LoadState --value "$svc" 2>/dev/null || true)"
          slice="$(systemctl show -p Slice --value "$svc" 2>/dev/null || true)"
        fi

        # Only target loaded system services (actively ignores user session units)
        if [[ "$load_state" == "loaded" && "$slice" != "user.slice" ]]; then
          services_to_stop["$svc"]=1
        fi
      fi
    fi
  done

  # Safely stop dynamically discovered vendor services (nvidia-persistenced, docker, ollama, etc)
  for svc in "${!services_to_stop[@]}"; do
    log_info "Stopping dynamically detected system service using the GPU: $svc"
    systemctl stop "$svc" 2>>"${LOG_FILE}" || log_warn "Failed to stop service $svc"
    STOPPED_SERVICES+=("$svc")
    journal_push "stop_service|${svc}"
  done

  if [[ ${#services_to_stop[@]} -gt 0 ]]; then
    sleep 1
  fi

  # Re-evaluate processes as stopping the services will already reap dependent daemon PIDs
  local -a remaining_pids=()
  for pid in "${pids[@]}"; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    if kill -0 "$pid" 2>/dev/null; then
      if (( pid != $$ )) && (( pid != 1 )); then
        remaining_pids+=("$pid")
      fi
    fi
  done

  if (( ${#remaining_pids[@]} == 0 )); then
    return 0
  fi

  local -A pid_starts=()
  for pid in "${remaining_pids[@]}"; do
    pid_starts["$pid"]="$(ps -o lstart= -p "$pid" 2>/dev/null || true)"
  done

  for pid in "${remaining_pids[@]}"; do
    comm="$(ps -o comm= -p "$pid" 2>/dev/null || echo unknown)"
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    [[ -z "$cmdline" ]] && cmdline="$comm"
    log_info "Sending SIGTERM to PID $pid ($comm): $cmdline"
    journal_push "killed_pid|${pid}|${comm}"
    kill -TERM "$pid" 2>/dev/null || true
    to_wait+=("$pid")
  done

  if (( ${#to_wait[@]} == 0 )); then
    return 0
  fi

  local waited=0
  local -a still=()
  local cur_start
  while (( waited < PROCESS_KILL_GRACE_PERIOD )); do
    still=()
    for pid in "${to_wait[@]}"; do
      cur_start="$(ps -o lstart= -p "$pid" 2>/dev/null || true)"
      if [[ -n "$cur_start" && "$cur_start" == "${pid_starts[$pid]}" ]]; then
        still+=("$pid")
      fi
    done
    if (( ${#still[@]} == 0 )); then
      break
    fi
    to_wait=("${still[@]}")
    sleep 1
    waited=$((waited+1))
  done

  still=()
  for pid in "${to_wait[@]}"; do
    cur_start="$(ps -o lstart= -p "$pid" 2>/dev/null || true)"
    if [[ -n "$cur_start" && "$cur_start" == "${pid_starts[$pid]}" ]]; then
      still+=("$pid")
    fi
  done
  if (( ${#still[@]} > 0 )); then
    for pid in "${still[@]}"; do
      log_warn "PID $pid still alive after ${PROCESS_KILL_GRACE_PERIOD}s grace period; sending SIGKILL."
      kill -KILL "$pid" 2>/dev/null || true
    done
    sleep 1
  fi
}

restart_stopped_services() {
  if (( ${#STOPPED_SERVICES[@]} == 0 )); then
    return 0
  fi
  local i svc
  for (( i=${#STOPPED_SERVICES[@]}-1; i>=0; i-- )); do
    svc="${STOPPED_SERVICES[$i]}"
    log_info "Restarting vendor service: $svc"
    systemctl start "$svc" 2>>"${LOG_FILE}" || log_warn "Failed to restart service $svc"
    journal_push "start_service|$svc"
  done
}

# ============================================================================
# systemd display-manager & session handling
# ============================================================================

display_manager_unit() {
  local svc_load_state unit
  svc_load_state="$(systemctl show -p LoadState --value display-manager.service 2>/dev/null || true)"
  if [[ "$svc_load_state" == "loaded" ]]; then
    unit="$(systemctl show -p Id --value display-manager.service 2>/dev/null || true)"
    if [[ -n "$unit" ]]; then
      printf '%s\n' "$unit"
      return 0
    fi
  fi
  if [[ -L /etc/systemd/system/display-manager.service ]]; then
    unit="$(basename "$(readlink -f /etc/systemd/system/display-manager.service)")"
    if [[ -n "$unit" ]]; then
      printf '%s\n' "$unit"
      return 0
    fi
  fi
  return 1
}

is_unit_active() {
  local unit="$1"
  [[ -n "$unit" ]] || return 1
  systemctl is-active --quiet "$unit"
}

terminate_graphical_sessions() {
  command -v loginctl >/dev/null 2>&1 || return 0
  local sid class

  # Terminate all user/greeter sessions, forcing a complete drop of DRM
  # contexts even if they are raw tty-launched Wayland sessions.
  for sid in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
    [[ -n "$sid" ]] || continue
    class="$(loginctl show-session -p Class --value "$sid" 2>/dev/null || true)"

    if [[ "$class" == "user" || "$class" == "greeter" ]]; then
      log_info "Terminating active local logind session $sid (class=$class)..."
      loginctl terminate-session "$sid" 2>>"${LOG_FILE}" || true
    fi
  done
}

stop_display_manager() {
  local unit
  unit="$(display_manager_unit || true)"
  DM_UNIT="$unit"
  if [[ -z "$unit" ]]; then
    log_info "No display-manager.service alias configured on this system; skipping display manager handling."
    DM_WAS_ACTIVE="false"
    persist_state
  elif is_unit_active "$unit"; then
    DM_WAS_ACTIVE="true"
    persist_state
    log_info "Stopping display manager: $unit"
    systemctl stop "$unit" 2>>"${LOG_FILE}" || abort "Failed to stop display manager unit '$unit'."
    journal_push "stop_dm"
    sleep 1
  else
    log_info "Display manager '$unit' is already inactive; nothing to stop."
    DM_WAS_ACTIVE="false"
    persist_state
  fi

  terminate_graphical_sessions
  sleep 1
}

start_display_manager() {
  local unit="${DM_UNIT:-}"
  if [[ -z "$unit" ]]; then
    unit="$(display_manager_unit || true)"
  fi
  if [[ -z "$unit" ]]; then
    log_info "No display-manager.service alias configured; nothing to start."
    return 0
  fi
  log_info "Starting display manager: $unit"
  systemctl start "$unit" 2>>"${LOG_FILE}" \
    || log_error "Failed to start display manager '$unit'. You may need to start your desktop session manually (systemctl start $unit)."
}

# ============================================================================
# State persistence
# ============================================================================

persist_state() {
  local dir; dir="$(dirname "$STATE_FILE")"
  mkdir -p "$dir" 2>/dev/null || true
  if {
    printf '# vfio-toggle state file - auto-generated, do not hand-edit\n'
    printf '# Last written: %s\n' "$(date -Iseconds 2>/dev/null || date)"

    # Natively serialize structures into bare assignments using %q safety escapes.
    # Because these variables are pre-declared as global arrays/associative arrays at the top of the script,
    # sourcing these bare assignments securely overwrites the global structures without relying on declare -p output hacks.

    local k v

    printf 'JOURNAL=( '
    for v in "${JOURNAL[@]}"; do printf '%q ' "$v"; done
    printf ')\n'

    printf 'ORIG_DRIVER=( '
    for k in "${!ORIG_DRIVER[@]}"; do printf '[%q]=%q ' "$k" "${ORIG_DRIVER[$k]}"; done
    printf ')\n'

    printf 'REMOVED_MODULES=( '
    for v in "${REMOVED_MODULES[@]}"; do printf '%q ' "$v"; done
    printf ')\n'

    printf 'RELEASED_VTCONSOLES=( '
    for v in "${RELEASED_VTCONSOLES[@]}"; do printf '%q ' "$v"; done
    printf ')\n'

    printf 'STOPPED_SERVICES=( '
    for v in "${STOPPED_SERVICES[@]}"; do printf '%q ' "$v"; done
    printf ')\n'

    printf 'DM_WAS_ACTIVE=%q\n' "$DM_WAS_ACTIVE"
    printf 'DM_UNIT=%q\n' "$DM_UNIT"
    printf 'STATE_OPERATION=%q\n' "$STATE_OPERATION"

  } > "${STATE_FILE}.tmp" 2>/dev/null; then
    if mv -f "${STATE_FILE}.tmp" "$STATE_FILE" 2>/dev/null; then
      chmod 600 "$STATE_FILE" 2>/dev/null || true
    else
      log_warn "Could not write state file $STATE_FILE (rollback safety net degraded for this run)."
    fi
  else
    log_warn "Could not write state file $STATE_FILE (rollback safety net degraded for this run)."
  fi
  return 0
}

load_state() {
  if [[ -f "$STATE_FILE" ]]; then
    assert_safe_to_source "$STATE_FILE"
    # shellcheck disable=SC1090
    source "$STATE_FILE"
    return 0
  fi
  return 1
}

clear_state() {
  rm -f "$STATE_FILE" "${STATE_FILE}.tmp" 2>/dev/null || true
  JOURNAL=()
  ORIG_DRIVER=()
  REMOVED_MODULES=()
  RELEASED_VTCONSOLES=()
  STOPPED_SERVICES=()
  DM_WAS_ACTIVE=""
  DM_UNIT=""
  STATE_OPERATION=""
}

record_original_drivers() {
  local -a devices=("$@")
  local addr drv
  for addr in "${devices[@]}"; do
    drv="$(pci_driver_of "$addr")"
    ORIG_DRIVER["$addr"]="$drv"
    log_debug "Recorded original driver for $addr: '${drv:-<none>}'"
  done
  persist_state
}

# ============================================================================
# Journal & rollback
# ============================================================================

journal_push() {
  if [[ "${ROLLING_BACK}" == "1" ]]; then
    log_debug "(rollback in progress; not journaling: $1)"
    return 0
  fi
  JOURNAL+=("$1")
  persist_state
  log_debug "Journal += $1"
}

rollback_from_journal() {
  # Drop blocklist BEFORE rolling back, otherwise reload_removed_modules fails
  allow_driver_autoload
  local i entry
  for (( i=${#JOURNAL[@]}-1; i>=0; i-- )); do
    entry="${JOURNAL[$i]}"
    log_info "Rollback: undoing '$entry'"
    rollback_one "$entry"
  done
  log_warn "Rollback finished. Please verify GPU/display state manually. Full detail in: $LOG_FILE"
  JOURNAL=()
  persist_state
}

rollback_one() {
  local entry="$1"
  local type="${entry%%|*}"
  local rest="${entry#*|}"
  local addr drv fb dn
  case "$type" in
    stop_dm)
      start_display_manager
      ;;
    killed_pid)
      log_warn "  (PID from '$entry' was already terminated and cannot be un-killed.)"
      ;;
    stop_service)
      if [[ -n "$rest" ]]; then
        systemctl start "$rest" 2>>"${LOG_FILE}" || log_warn "  Failed to restart service $rest."
        log_info "  Restarted service $rest."
      fi
      ;;
    start_service)
      systemctl stop "$rest" 2>>"${LOG_FILE}" || true
      ;;
    unbind_driver)
      addr="${rest%%|*}"
      drv="${rest#*|}"
      clear_driver_override "$addr"
      printf '%s\n' "$addr" > "${SYSFS_PCI}/drivers_probe" 2>>"${LOG_FILE}" || true
      log_info "  Re-probed $addr (original driver was '$drv')."
      ;;
    remove_modules)
      reload_removed_modules
      ;;
    override_set)
      clear_driver_override "$rest"
      log_info "  Cleared driver_override for $rest."
      ;;
    bind_vfio)
      if [[ -e "${SYSFS_PCI}/devices/${rest}/driver" ]]; then
        printf '%s\n' "$rest" > "${SYSFS_PCI}/devices/${rest}/driver/unbind" 2>>"${LOG_FILE}" || true
      fi
      log_info "  Unbound $rest from vfio-pci."
      ;;
    override_cleared)
      log_debug "  (override_cleared for $rest needs no rollback action by itself.)"
      ;;
    unbind_vfio)
      printf 'vfio-pci\n' > "${SYSFS_PCI}/devices/${rest}/driver_override" 2>>"${LOG_FILE}" || true
      printf '%s\n' "$rest" > "${SYSFS_PCI}/drivers_probe" 2>>"${LOG_FILE}" || true
      log_info "  Re-bound $rest back to vfio-pci."
      ;;
    reload_modules)
      # When restoring to host drivers (unbind) fails, rolling back implies reverting to vfio-pci.
      # Leaving the host modules loaded in memory is harmless as long as the device itself is re-bound to vfio-pci.
      log_debug "  (reload_modules rollback: leaving restored host modules loaded is safe; no action needed.)"
      ;;
    rebind_driver)
      addr="${rest%%|*}"
      drv="${rest#*|}"
      log_info "  Rolling back host driver rebind for $addr (was '$drv'). Re-binding to vfio-pci."
      if [[ -e "${SYSFS_PCI}/devices/${addr}/driver" ]]; then
        printf '%s\n' "$addr" > "${SYSFS_PCI}/devices/${addr}/driver/unbind" 2>>"${LOG_FILE}" || true
      fi
      printf 'vfio-pci\n' > "${SYSFS_PCI}/devices/${addr}/driver_override" 2>>"${LOG_FILE}" || true
      printf '%s\n' "$addr" > "${SYSFS_PCI}/drivers_probe" 2>>"${LOG_FILE}" || true
      ;;
    unbind_platform)
      fb="${rest%%|*}"
      dn="${rest#*|}"
      printf '%s\n' "$fb" > "${SYSFS_PLATFORM}/drivers/${dn}/bind" 2>>"${LOG_FILE}" || log_warn "  Could not rebind platform device $fb."
      ;;
    unbind_vtconsole)
      if [[ -e "${SYSFS_VTCONSOLE}/${rest}/bind" ]]; then
        printf '1\n' > "${SYSFS_VTCONSOLE}/${rest}/bind" 2>>"${LOG_FILE}" || log_warn "  Could not rebind VT console $rest."
      fi
      log_info "  Rebound VT console $rest."
      ;;
    rebind_vtconsole)
      if [[ -e "${SYSFS_VTCONSOLE}/${rest}/bind" ]]; then
        printf '0\n' > "${SYSFS_VTCONSOLE}/${rest}/bind" 2>>"${LOG_FILE}" || log_warn "  Could not unbind VT console $rest."
      fi
      log_info "  Re-unbound VT console $rest."
      ;;
    start_dm_after_bind|start_dm_after_unbind)
      stop_display_manager
      ;;
    *)
      log_warn "  Unknown journal entry type '$type' (from '$entry'); skipping."
      ;;
  esac
}

# ============================================================================
# Core bind/unbind primitives
# ============================================================================

set_driver_override() {
  local addr="$1"
  local driver="$2"
  local f="${SYSFS_PCI}/devices/${addr}/driver_override"
  [[ -e "$f" ]] || abort "driver_override attribute not found for $addr."
  log_info "Setting driver_override=$driver for $addr."
  printf '%s\n' "$driver" > "$f" || abort "Failed to set driver_override=$driver for $addr."
  journal_push "override_set|${addr}"
}

clear_driver_override() {
  local addr="$1"
  local f="${SYSFS_PCI}/devices/${addr}/driver_override"
  [[ -e "$f" ]] || return 0
  printf '\n' > "$f" 2>>"${LOG_FILE}" || log_warn "Could not clear driver_override for $addr."
}

unbind_device_from_driver() {
  local addr="$1"
  local drv; drv="$(pci_driver_of "$addr")"
  if [[ -z "$drv" ]]; then
    log_debug "$addr has no driver currently bound; nothing to unbind."
    return 0
  fi
  log_info "Unbinding $addr from driver '$drv'."
  printf '%s\n' "$addr" > "${SYSFS_PCI}/devices/${addr}/driver/unbind" 2>>"${LOG_FILE}" \
    || abort "Failed to unbind $addr from '$drv'. It may still be in use; check the process list above and 'dmesg'."
  journal_push "unbind_driver|${addr}|${drv}"
}

bind_device_to_vfio() {
  local addr="$1"
  local cur; cur="$(pci_driver_of "$addr")"
  if [[ "$cur" == "vfio-pci" ]]; then
    log_debug "$addr is already bound to vfio-pci."
    return 0
  fi

  local override_file="${SYSFS_PCI}/devices/${addr}/driver_override"
  local current_override=""
  [[ -e "$override_file" ]] && current_override="$(cat "$override_file" 2>/dev/null || true)"
  if [[ "$current_override" != "vfio-pci" ]]; then
    set_driver_override "$addr" "vfio-pci"
  fi

  if [[ -n "$cur" ]]; then
    log_warn "$addr is currently bound to '$cur'; unbinding before binding to vfio-pci."
    unbind_device_from_driver "$addr"
  fi

  printf '%s\n' "$addr" > "${SYSFS_PCI}/drivers_probe" 2>>"${LOG_FILE}" || true
  sleep 0.3
  cur="$(pci_driver_of "$addr")"

  if [[ "$cur" != "vfio-pci" ]]; then
    log_warn "$addr did not bind via drivers_probe (current: '${cur:-none}'); trying a direct bind."
    printf '%s\n' "$addr" > "${SYSFS_PCI}/drivers/vfio-pci/bind" 2>>"${LOG_FILE}" || true
    sleep 0.3
    cur="$(pci_driver_of "$addr")"
  fi

  if [[ "$cur" != "vfio-pci" ]]; then
    abort "Device $addr failed to bind to vfio-pci (current driver: '${cur:-none}'). Check: dmesg | tail -50"
  fi
  journal_push "bind_vfio|${addr}"
  log_info "$addr is now bound to vfio-pci."
}

unbind_device_from_vfio() {
  local addr="$1"
  clear_driver_override "$addr"
  journal_push "override_cleared|${addr}"
  if [[ -e "${SYSFS_PCI}/devices/${addr}/driver" ]]; then
    printf '%s\n' "$addr" > "${SYSFS_PCI}/devices/${addr}/driver/unbind" 2>>"${LOG_FILE}" \
      || abort "Failed to unbind $addr from vfio-pci."
    journal_push "unbind_vfio|${addr}"
  fi
  log_info "$addr unbound from vfio-pci."
}

rebind_original_driver() {
  local addr="$1"
  local expected="${ORIG_DRIVER[$addr]:-}"
  printf '%s\n' "$addr" > "${SYSFS_PCI}/drivers_probe" 2>>"${LOG_FILE}" || true
  sleep 0.3
  local cur; cur="$(pci_driver_of "$addr")"
  if [[ -z "$cur" && -n "$expected" && -d "${SYSFS_PCI}/drivers/${expected}" ]]; then
    log_warn "$addr did not bind via drivers_probe (current: '${cur:-none}'); trying direct bind to '$expected'."
    printf '%s\n' "$addr" > "${SYSFS_PCI}/drivers/${expected}/bind" 2>>"${LOG_FILE}" || true
    sleep 0.3
    cur="$(pci_driver_of "$addr")"
  fi
  if [[ -z "$cur" ]]; then
    abort "Device $addr has no driver bound after restore attempt (expected '${expected:-<unknown>}'). Its driver module may not be installed. Check: modinfo ${expected:-<driver>}; dmesg | tail -50"
  fi
  if [[ -n "$expected" && "$cur" != "$expected" ]]; then
    log_warn "$addr bound to '$cur', originally was '$expected'. Not treating this as fatal (driver may have changed, e.g. after a package update)."
  fi
  journal_push "rebind_driver|${addr}|${cur}"
  log_info "$addr is now bound to '$cur'."
}

print_summary() {
  echo
  echo "Summary:"
  local addr
  for addr in "${TARGET_DEVICES[@]}"; do
    printf '  %s : %s -> %s\n' "$addr" "${ORIG_DRIVER[$addr]:-<none>}" "$(pci_driver_of "$addr")"
  done
  echo
  echo "State saved to: $STATE_FILE"
  echo "Full log:       $LOG_FILE"
}

# ============================================================================
# Top-level commands
# ============================================================================

do_bind() {
  check_dependencies
  require_iommu
  build_target_devices

  local addr cur
  local -a need_action=()
  for addr in "${TARGET_DEVICES[@]}"; do
    cur="$(pci_driver_of "$addr")"
    if [[ "$cur" == "vfio-pci" ]]; then
      log_debug "$addr already bound to vfio-pci."
    else
      need_action+=("$addr")
    fi
  done

  if (( ${#need_action[@]} == 0 )); then
    log_info "All target devices are already bound to vfio-pci. Nothing to do."
    return 0
  fi

  log_info "Devices to move to vfio-pci: ${need_action[*]}"
  record_original_drivers "${need_action[@]}"

  local -A seen_driver=()
  local d
  for addr in "${need_action[@]}"; do
    d="${ORIG_DRIVER[$addr]:-}"
    [[ -n "$d" ]] && seen_driver["$d"]=1
  done

  # Lock down modprobe dynamically so restarting greeters don't reload components while we unload them
  prevent_driver_autoload "${!seen_driver[@]}"

  ensure_vfio_pci_loaded

  stop_display_manager

  local -a nodes=()
  for addr in "${need_action[@]}"; do
    while IFS= read -r n; do
      [[ -n "$n" ]] && nodes+=("$n")
    done < <(device_nodes_for "$addr")
  done

  if (( ${#nodes[@]} > 0 )); then
    local tries=0
    # Try up to 3 times to account for a display manager eagerly respawning Wayland/X11
    while (( tries < 3 )); do
      local -a pids=()
      while IFS= read -r p; do
        [[ -n "$p" ]] && pids+=("$p")
      done < <(find_pids_using_devices "${nodes[@]}")

      if (( ${#pids[@]} == 0 )); then
        if (( tries == 0 )); then
          log_info "No processes are currently using the GPU device nodes."
        else
          log_info "No more processes found using GPU device nodes."
        fi
        break
      fi

      log_info "Checking for processes using GPU device nodes (attempt $((tries+1))): ${pids[*]}"
      terminate_pids_safely "${pids[@]}"
      tries=$((tries+1))
      sleep 1
    done
  else
    log_debug "No DRM/device nodes found for target devices."
  fi

  release_vt_consoles
  release_boot_framebuffer

  # Force standard unbind of the device first to detach its internals. This drops driver references,
  # destroys rogue/orphaned CUDA or graphical contexts, and cleanly permits future module unloading.
  for addr in "${need_action[@]}"; do
    set_driver_override "$addr" "vfio-pci"
    unbind_device_from_driver "$addr"
  done

  # Give kernel/udev a moment to process the unbind uevents and release transient refs
  if command -v udevadm >/dev/null 2>&1; then
    log_info "Waiting for udev to process device teardown events..."
    udevadm settle
  fi

  # Now properly safe to un-root module trees
  if [[ "$ALLOW_FULL_MODULE_UNLOAD" == "true" ]]; then
    seen_driver=()
    for addr in "${need_action[@]}"; do
      d="${ORIG_DRIVER[$addr]:-}"
      [[ -z "$d" ]] && continue
      [[ "${seen_driver[$d]:-}" == "1" ]] && continue
      seen_driver["$d"]=1
      if other_devices_using_driver "$d"; then
        log_info "Skipping full unload of module '$d': still in use by another PCI device outside the managed set."
      else
        unload_module_tree "$d"
      fi
    done
    if (( ${#REMOVED_MODULES[@]} > 0 )); then
      journal_push "remove_modules"
      log_info "Removed kernel modules: ${REMOVED_MODULES[*]}"
    fi
  else
    log_info "ALLOW_FULL_MODULE_UNLOAD=false; leaving driver modules loaded (per-device unbind only)."
  fi

  for addr in "${need_action[@]}"; do
    attempt_function_level_reset "$addr"
  done

  for addr in "${need_action[@]}"; do
    bind_device_to_vfio "$addr"
  done

  for addr in "${TARGET_DEVICES[@]}"; do
    cur="$(pci_driver_of "$addr")"
    [[ "$cur" == "vfio-pci" ]] || abort "Post-check failed: $addr is bound to '${cur:-none}', expected vfio-pci."
  done

  if [[ "$RESTART_DISPLAY_MANAGER_AFTER_BIND" == "true" && "${DM_WAS_ACTIVE:-false}" == "true" ]]; then
    log_info "Restarting the display manager while the GPU is on vfio-pci (RESTART_DISPLAY_MANAGER_AFTER_BIND=true). If the host has no secondary GPU to fall back to, this may not produce a usable display — see RELEASE_VT_CONSOLE / README for single-GPU setups."
    start_display_manager
    journal_push "start_dm_after_bind"
  else
    log_info "Not restarting the display manager after bind (RESTART_DISPLAY_MANAGER_AFTER_BIND=$RESTART_DISPLAY_MANAGER_AFTER_BIND, was_active=${DM_WAS_ACTIVE:-unknown})."
  fi

  STATE_OPERATION="bind_complete"
  JOURNAL=()
  persist_state

  allow_driver_autoload

  log_info "SUCCESS: all target devices are bound to vfio-pci."
  print_summary
}

do_unbind() {
  allow_driver_autoload
  check_dependencies
  build_target_devices

  local have_state="false"
  if load_state; then
    have_state="true"
    log_info "Loaded saved state from $STATE_FILE (previous operation: ${STATE_OPERATION:-unknown})."
  else
    log_warn "No saved state file found; proceeding in best-effort mode (will rely on modalias-based driver detection)."
  fi

  # Clear stale journal from previous partial run before beginning unbind
  JOURNAL=()
  persist_state

  local addr cur
  local -a need_action=()
  for addr in "${TARGET_DEVICES[@]}"; do
    cur="$(pci_driver_of "$addr")"
    if [[ "$cur" == "vfio-pci" ]]; then
      need_action+=("$addr")
    else
      log_debug "$addr is not bound to vfio-pci (driver: '${cur:-none}'); nothing to restore for it."
    fi
  done

  if (( ${#need_action[@]} == 0 )); then
    log_info "No target devices are currently bound to vfio-pci. Nothing to do."
    if [[ "$have_state" == "true" ]]; then
      clear_state
    fi
    return 0
  fi

  log_info "Devices to restore from vfio-pci: ${need_action[*]}"
  RESTORE_DEVICES=("${need_action[@]}")

  for addr in "${RESTORE_DEVICES[@]}"; do
    unbind_device_from_vfio "$addr"
  done

  for addr in "${RESTORE_DEVICES[@]}"; do
    attempt_function_level_reset "$addr"
  done

  reload_removed_modules
  for addr in "${RESTORE_DEVICES[@]}"; do
    ensure_driver_loaded_for_device "$addr"
  done

  for addr in "${RESTORE_DEVICES[@]}"; do
    rebind_original_driver "$addr"
  done

  for addr in "${TARGET_DEVICES[@]}"; do
    cur="$(pci_driver_of "$addr")"
    [[ -n "$cur" ]] || abort "Post-check failed: $addr has no driver bound after restore."
  done

  restore_vt_consoles
  restart_stopped_services

  start_display_manager
  journal_push "start_dm_after_unbind"

  log_info "SUCCESS: all target devices restored to their host driver."
  print_summary
  clear_state
}

do_status() {
  build_target_devices
  echo "=== vfio-toggle status ==="
  echo "Config file: $CONFIG_PATH"
  echo
  local iommu_ok="no"
  if [[ -d "$SYSFS_IOMMU_GROUPS" ]] && [[ -n "$(ls -A "$SYSFS_IOMMU_GROUPS" 2>/dev/null)" ]]; then
    iommu_ok="yes"
  fi
  echo "IOMMU groups present on system: $iommu_ok"
  if [[ "$iommu_ok" == "no" ]]; then
    echo "  WARNING: no IOMMU groups found. VFIO passthrough requires IOMMU"
    echo "  (Intel VT-d / AMD-Vi) enabled in BIOS and on the kernel command line."
  fi
  echo
  echo "Managed devices (from IOMMU-group expansion of GPU_PCI_IDS):"
  local addr drv desc
  for addr in "${TARGET_DEVICES[@]}"; do
    drv="$(pci_driver_of "$addr")"
    desc="$(pci_description "$addr")"
    printf '  %s  driver=%-10s %s\n' "$addr" "${drv:-none}" "$desc"
  done
  echo
  local dm_unit active
  dm_unit="$(display_manager_unit || true)"
  if [[ -n "$dm_unit" ]]; then
    active="inactive"
    if is_unit_active "$dm_unit"; then
      active="active"
    fi
    echo "Display manager: $dm_unit ($active)"
  else
    echo "Display manager: none detected (systemd alias display-manager.service not configured)"
  fi
  echo
  if [[ -f "$STATE_FILE" ]]; then
    echo "Saved state file found: $STATE_FILE"
    load_state
    echo "  Last operation recorded: ${STATE_OPERATION:-unknown}"
    echo "  Journal entries left over: ${#JOURNAL[@]}"
    if (( ${#REMOVED_MODULES[@]} > 0 )); then
      echo "  Modules previously removed: ${REMOVED_MODULES[*]}"
    fi
    if (( ${#RELEASED_VTCONSOLES[@]} > 0 )); then
      echo "  VT consoles previously released: ${RELEASED_VTCONSOLES[*]}"
    fi
    if (( ${#STOPPED_SERVICES[@]} > 0 )); then
      echo "  Vendor daemon services stopped: ${STOPPED_SERVICES[*]}"
    fi
  else
    echo "No saved state file (clean state)."
  fi
}

do_list_devices() {
  log_info "Scanning for GPU-class PCI devices..."
  local d addr class vd drv desc group gm gaddr gdesc found=0
  for d in "${SYSFS_PCI}"/devices/*; do
    [[ -e "$d" ]] || continue
    addr="$(basename "$d")"
    class="$(cat "$d/class" 2>/dev/null || echo "")"
    [[ "$class" =~ ^0x03 ]] || continue
    found=1
    vd="$(pci_vendor_device_id "$addr")"
    drv="$(pci_driver_of "$addr")"
    desc="$(pci_description "$addr")"
    group=""
    if [[ -e "$d/iommu_group" ]]; then
      group="$(basename "$(readlink -f "$d/iommu_group")")"
    fi
    printf '\n%s\n' "$addr"
    printf '  ID:          %s\n' "$vd"
    [[ -n "$desc" ]] && printf '  Description: %s\n' "$desc"
    printf '  Driver:      %s\n' "${drv:-<none>}"
    printf '  IOMMU group: %s\n' "${group:-<none - IOMMU not enabled?>}"
    if [[ -n "$group" ]]; then
      printf '  Group members:\n'
      for gm in "${SYSFS_IOMMU_GROUPS}/${group}/devices"/*; do
        [[ -e "$gm" ]] || continue
        gaddr="$(basename "$gm")"
        gdesc="$(pci_description "$gaddr")"
        printf '    - %s  %s\n' "$gaddr" "$gdesc"
      done
    fi
  done
  if (( found == 0 )); then
    log_warn "No VGA/3D/Display-class PCI devices found."
  else
    echo
    echo "Put the primary address(es) (the VGA/3D-controller function, usually"
    echo "ending in .0) into GPU_PCI_IDS in your config. Group members are"
    echo "included automatically."
  fi
}

do_manual_rollback() {
  if ! load_state; then
    abort "No state file found at $STATE_FILE; nothing to roll back."
  fi
  if (( ${#JOURNAL[@]} == 0 )); then
    log_info "State file has an empty journal; nothing to roll back."
    return 0
  fi
  log_warn "Manually replaying rollback for ${#JOURNAL[@]} recorded step(s) from $STATE_FILE."
  rollback_from_journal
}

# ============================================================================
# CLI
# ============================================================================

usage() {
  cat <<EOF
Usage: $SCRIPT_NAME <command> [options]

Commands:
  bind            Unbind the configured GPU (and its IOMMU group) from its
                  current driver and bind it to vfio-pci.
  unbind          Reverse of 'bind': unbind from vfio-pci and restore the
                  original driver(s).
  status          Show the current state of configured devices without
                  changing anything.
  list-devices    Discover GPU-class PCI devices and their IOMMU groups,
                  to help you fill in the config file.
  rollback        Manually replay rollback using the last saved journal,
                  e.g. after a crashed run.

Options:
  -c, --config PATH   Use PATH instead of the default config file.
  -v, --verbose       Verbose console output (DEBUG level).
  -h, --help          Show this help.

Config file search order (unless -c is given):
  /etc/vfio-toggle/vfio-toggle.conf
  ${SCRIPT_DIR}/vfio-toggle.conf

Must be run as root, except 'status' and 'list-devices'.
EOF
}

main() {
  if (( $# == 0 )); then
    usage
    exit 1
  fi
  COMMAND="$1"; shift

  while (( $# > 0 )); do
    case "$1" in
      -c|--config)
        [[ $# -ge 2 ]] || { echo "Error: --config requires a path." >&2; exit 1; }
        CONFIG_PATH="$2"; shift 2 ;;
      -v|--verbose) VERBOSE="true"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
  done

  case "$COMMAND" in
    list-devices)
      setup_logging
      check_dependencies
      do_list_devices
      ;;
    status)
      setup_logging
      check_dependencies
      require_config
      do_status
      ;;
    bind)
      require_root
      setup_logging
      check_dependencies
      require_config
      require_iommu
      acquire_lock
      do_bind
      release_lock
      ;;
    unbind)
      require_root
      setup_logging
      check_dependencies
      require_config
      acquire_lock
      do_unbind
      release_lock
      ;;
    rollback)
      require_root
      setup_logging
      check_dependencies
      require_config
      acquire_lock
      do_manual_rollback
      release_lock
      ;;
    help|-h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown command: $COMMAND" >&2
      usage
      exit 1
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
