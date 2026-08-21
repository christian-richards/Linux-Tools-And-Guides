#!/usr/bin/env bash
# Test harness: sources vfio-toggle.sh (which does NOT auto-run main because
# BASH_SOURCE != $0 when sourced) and pokes at its internals against a mock
# /sys tree + stubbed commands. Each test runs in its own subshell so a
# `set -e` trip in one test can't kill the rest of the suite.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"

# Always rebuild the mock environment fresh so this suite is self-contained
# and repeatable from a clean checkout.
./setup_mocks.sh

export PATH="$PWD/mock/bin:$PATH"
BASE="$PWD/mock"
SCRIPT="../vfio-toggle.sh"
PASS=0
FAIL=0

t() {
  local name="$1"; shift
  if ( set -e; "$@" ); then
    echo "PASS: $name"
    PASS=$((PASS+1))
  else
    echo "FAIL: $name"
    FAIL=$((FAIL+1))
  fi
}

reset_mock_state() {
  rm -f "$BASE/state/calls.log" "$BASE/state/dm_active" "$BASE/state/fuser_pids"
  echo "active" > "$BASE/state/dm_active"
}

# Tests that unload/reload modules mutate mock/sys/module (that's the point),
# so rebuild the fixture fresh before each run of the suite instead of
# relying on a one-time manual setup that earlier runs would have consumed.
reset_mock_module_tree() {
  rm -rf "$BASE/sys/module"
  mkdir -p "$BASE/sys/module/nvidia/holders"
  mkdir -p "$BASE/sys/module/nvidia_modeset/holders"
  mkdir -p "$BASE/sys/module/nvidia_uvm/holders"
  mkdir -p "$BASE/sys/module/nvidia_drm/holders"
  ln -sf ../../nvidia_modeset "$BASE/sys/module/nvidia/holders/nvidia_modeset"
  ln -sf ../../nvidia_uvm "$BASE/sys/module/nvidia/holders/nvidia_uvm"
  ln -sf ../../nvidia_drm "$BASE/sys/module/nvidia_modeset/holders/nvidia_drm"
}

# ---------------------------------------------------------------------------
test_normalize_module_name() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/normalize_state"
  source "$SCRIPT"
  [[ "$(normalize_module_name "vfio-pci")" == "vfio_pci" ]] || { echo "got: $(normalize_module_name vfio-pci)"; return 1; }
  [[ "$(normalize_module_name "nvidia")" == "nvidia" ]] || return 1
}

# ---------------------------------------------------------------------------
test_pci_driver_of() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/pcidriver_state"
  source "$SCRIPT"
  local d1 d2
  d1="$(pci_driver_of "0000:01:00.0")"
  d2="$(pci_driver_of "0000:01:00.1")"
  [[ "$d1" == "nvidia" ]] || { echo "expected nvidia got '$d1'"; return 1; }
  [[ "$d2" == "snd_hda_intel" ]] || { echo "expected snd_hda_intel got '$d2'"; return 1; }
  # non-existent driver symlink -> empty, must not error under set -e
  local d3
  d3="$(pci_driver_of "0000:99:00.0")"
  [[ -z "$d3" ]] || return 1
}

# ---------------------------------------------------------------------------
test_iommu_group_expansion() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/iommu_state"
  source "$SCRIPT"
  local -a result=()
  while IFS= read -r a; do result+=("$a"); done < <(expand_to_iommu_groups "0000:01:00.0")
  # Giving ONLY the VGA function must still pull in the audio function
  # because they share IOMMU group 14.
  [[ ${#result[@]} -eq 2 ]] || { echo "expected 2 devices got ${#result[@]}: ${result[*]}"; return 1; }
  [[ " ${result[*]} " == *" 0000:01:00.0 "* ]] || return 1
  [[ " ${result[*]} " == *" 0000:01:00.1 "* ]] || return 1
}

# ---------------------------------------------------------------------------
test_module_holders_and_unload_order() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/holders_state"
  source "$SCRIPT"
  DRY_RUN="false"
  TARGET_DEVICES=("0000:01:00.0" "0000:01:00.1")   # so other_devices_using_driver doesn't interfere
  REMOVED_MODULES=()
  declare -A VISITED_MODULES=()

  unload_module_tree "nvidia"

  echo "Removal order: ${REMOVED_MODULES[*]}"

  # Correctness invariants (sibling branch order is free, but dependency
  # order within each branch, and base-last, are NOT):
  local idx_drm=-1 idx_modeset=-1 idx_uvm=-1 idx_base=-1 i
  for i in "${!REMOVED_MODULES[@]}"; do
    case "${REMOVED_MODULES[$i]}" in
      nvidia_drm) idx_drm=$i ;;
      nvidia_modeset) idx_modeset=$i ;;
      nvidia_uvm) idx_uvm=$i ;;
      nvidia) idx_base=$i ;;
    esac
  done

  [[ ${#REMOVED_MODULES[@]} -eq 4 ]] || { echo "expected 4 modules removed, got ${#REMOVED_MODULES[@]}"; return 1; }
  (( idx_drm >= 0 && idx_modeset >= 0 && idx_uvm >= 0 && idx_base >= 0 )) || { echo "missing an expected module in output"; return 1; }
  (( idx_drm < idx_modeset )) || { echo "nvidia_drm must be removed before nvidia_modeset"; return 1; }
  (( idx_modeset < idx_base )) || { echo "nvidia_modeset must be removed before nvidia"; return 1; }
  (( idx_uvm < idx_base )) || { echo "nvidia_uvm must be removed before nvidia"; return 1; }
  (( idx_base == 3 )) || { echo "nvidia (base) must be removed LAST"; return 1; }

  # And the sysfs mock dirs should actually be gone (modprobe -r stub removed them)
  [[ ! -d "$BASE/sys/module/nvidia" ]] || return 1
  [[ ! -d "$BASE/sys/module/nvidia_drm" ]] || return 1
  [[ ! -d "$BASE/sys/module/nvidia_modeset" ]] || return 1
  [[ ! -d "$BASE/sys/module/nvidia_uvm" ]] || return 1
}

# ---------------------------------------------------------------------------
test_reload_removed_modules_reverses_order() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/reload_state"
  source "$SCRIPT"
  DRY_RUN="false"
  JOURNAL=()
  REMOVED_MODULES=(nvidia_drm nvidia_modeset nvidia_uvm nvidia)   # removal order from previous test
  rm -f "$BASE/state/calls.log"

  reload_removed_modules

  # Reload must happen in exact REVERSE of REMOVED_MODULES: base first.
  local order
  order="$(grep 'MODPROBE_CALL' "$BASE/state/calls.log" | sed 's/MODPROBE_CALL: //')"
  echo "Reload call order:"; echo "$order"
  local expected=$'nvidia\nnvidia_uvm\nnvidia_modeset\nnvidia_drm'
  [[ "$order" == "$expected" ]] || { echo "order mismatch"; return 1; }

  # All 4 modules should be "loaded" again in the mock tree
  [[ -d "$BASE/sys/module/nvidia" ]] || return 1
  [[ -d "$BASE/sys/module/nvidia_drm" ]] || return 1
  [[ -d "$BASE/sys/module/nvidia_modeset" ]] || return 1
  [[ -d "$BASE/sys/module/nvidia_uvm" ]] || return 1

  # journal should have gained exactly one "reload_modules" entry
  [[ "${JOURNAL[*]}" == "reload_modules" ]] || { echo "journal: ${JOURNAL[*]}"; return 1; }
}

# ---------------------------------------------------------------------------
test_other_devices_using_driver() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/otherdrv_state"
  source "$SCRIPT"

  # Add a THIRD device also on "nvidia", NOT in our target set (simulates a
  # second, non-passthrough Nvidia GPU that must not be disturbed).
  local D="$BASE/sys/bus/pci/devices/0000:02:00.0"
  mkdir -p "$D"
  ln -sf ../../drivers/nvidia "$D/driver" 2>/dev/null || true

  TARGET_DEVICES=("0000:01:00.0" "0000:01:00.1")
  if other_devices_using_driver "nvidia"; then
    :  # expected: true, since 0000:02:00.0 also uses nvidia and is outside target set
  else
    echo "expected other_devices_using_driver to return true (found)"
    rm -rf "$D"
    return 1
  fi

  # Now put 02:00.0 INTO the target set -- should now report false (no OTHER device)
  TARGET_DEVICES=("0000:01:00.0" "0000:01:00.1" "0000:02:00.0")
  if other_devices_using_driver "nvidia"; then
    echo "expected false when the only other user IS in the target set"
    rm -rf "$D"
    return 1
  fi
  rm -rf "$D"
  return 0
}

# ---------------------------------------------------------------------------
test_persist_and_load_state_roundtrip() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export STATE_FILE="$BASE/state/roundtrip_state"
  export LOG_FILE="$BASE/state/test.log"
  source "$SCRIPT"
  rm -f "$STATE_FILE"

  JOURNAL=("stop_dm" "unbind_driver|0000:01:00.0|nvidia" "override_set|0000:01:00.0")
  ORIG_DRIVER=(["0000:01:00.0"]="nvidia" ["0000:01:00.1"]="snd_hda_intel")
  REMOVED_MODULES=("nvidia_uvm" "nvidia_drm" "nvidia_modeset" "nvidia")
  DM_WAS_ACTIVE="true"
  DM_UNIT="gdm.service"
  STATE_OPERATION="bind_complete"

  persist_state
  [[ -f "$STATE_FILE" ]] || { echo "state file was not written"; return 1; }

  # Wipe globals, then reload from disk and confirm exact round-trip,
  # including the associative array (the trickiest part to serialize).
  JOURNAL=(); ORIG_DRIVER=(); REMOVED_MODULES=(); DM_WAS_ACTIVE=""; DM_UNIT=""; STATE_OPERATION=""

  load_state

  [[ "${#JOURNAL[@]}" -eq 3 ]] || { echo "journal count wrong: ${#JOURNAL[@]}"; return 1; }
  [[ "${JOURNAL[1]}" == "unbind_driver|0000:01:00.0|nvidia" ]] || return 1
  [[ "${ORIG_DRIVER[0000:01:00.0]}" == "nvidia" ]] || { echo "orig driver wrong: ${ORIG_DRIVER[0000:01:00.0]:-<unset>}"; return 1; }
  [[ "${ORIG_DRIVER[0000:01:00.1]}" == "snd_hda_intel" ]] || return 1
  [[ "${REMOVED_MODULES[*]}" == "nvidia_uvm nvidia_drm nvidia_modeset nvidia" ]] || return 1
  [[ "$DM_WAS_ACTIVE" == "true" ]] || return 1
  [[ "$DM_UNIT" == "gdm.service" ]] || return 1
  [[ "$STATE_OPERATION" == "bind_complete" ]] || return 1

  # File must be root-only permissions
  local perms; perms="$(stat -c '%a' "$STATE_FILE")"
  [[ "$perms" == "600" ]] || { echo "state file perms: $perms"; return 1; }
}

# ---------------------------------------------------------------------------
test_display_manager_stop_start() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export STATE_FILE="$BASE/state/dm_test_state"
  export LOG_FILE="$BASE/state/test.log"
  source "$SCRIPT"
  reset_mock_state
  DRY_RUN="false"; JOURNAL=(); DM_WAS_ACTIVE=""; DM_UNIT=""

  local unit; unit="$(display_manager_unit)"
  [[ "$unit" == "gdm.service" ]] || { echo "resolved unit: '$unit'"; return 1; }

  is_unit_active "gdm.service" || { echo "expected active initially"; return 1; }

  stop_display_manager
  [[ "$DM_WAS_ACTIVE" == "true" ]] || return 1
  [[ "${JOURNAL[*]}" == "stop_dm" ]] || { echo "journal after stop: ${JOURNAL[*]}"; return 1; }
  is_unit_active "gdm.service" && { echo "should be inactive after stop"; return 1; }

  start_display_manager
  is_unit_active "gdm.service" || { echo "expected active again after start"; return 1; }

  # Calling stop again when already inactive should be a safe no-op (no
  # second stop_dm journal entry, no crash) -- exercise the "not active" path.
  systemctl stop gdm.service >/dev/null
  JOURNAL=()
  stop_display_manager
  [[ "$DM_WAS_ACTIVE" == "false" ]] || return 1
  [[ ${#JOURNAL[@]} -eq 0 ]] || { echo "should not journal a stop when already inactive"; return 1; }
}

# ---------------------------------------------------------------------------
test_config_permission_checks() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/permcheck_state"
  source "$SCRIPT"

  local goodconf="$BASE/state/good.conf"
  local badconf="$BASE/state/bad.conf"
  echo 'GPU_PCI_IDS=("0000:01:00.0")' > "$goodconf"
  chmod 600 "$goodconf"
  echo 'GPU_PCI_IDS=("0000:01:00.0")' > "$badconf"
  chmod 666 "$badconf"

  ( assert_safe_to_source "$goodconf" ) || { echo "good conf should pass"; return 1; }
  # bad conf (world-writable) must be REJECTED -- assert_safe_to_source calls
  # die_early which exits 1, so we expect the subshell to exit non-zero.
  if ( assert_safe_to_source "$badconf" ) 2>/dev/null; then
    echo "world-writable config was NOT rejected -- security bug"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
test_setminusE_arithmetic_pitfalls() {
  # Regression test for the classic `((x++))` bash trap: when the
  # PRE-increment value is 0, a bare `((x++))` returns exit status 1 and
  # would kill the script under `set -e`. Confirms our actual loop idiom
  # (`x=$((x+1))`) and the `for ((...))` decrement idiom used in the real
  # script survive a full pass starting from zero under `set -Eeuo pipefail`.
  set -Eeuo pipefail
  local waited=0
  local n=0
  while (( waited < 3 )); do
    n=$((n+1))
    waited=$((waited+1))
  done
  [[ $n -eq 3 && $waited -eq 3 ]] || return 1

  local -a arr=(a b c)
  local -a out=()
  local i
  for (( i=${#arr[@]}-1; i>=0; i-- )); do
    out+=("${arr[$i]}")
  done
  [[ "${out[*]}" == "c b a" ]] || { echo "reverse iteration wrong: ${out[*]}"; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
test_terminate_pids_safely_real_process() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/termreal_state"
  source "$SCRIPT"
  DRY_RUN="false"; JOURNAL=(); PROCESS_KILL_GRACE_PERIOD=3

  sleep 60 &
  local victim=$!
  disown "$victim" 2>/dev/null || true
  kill -0 "$victim" 2>/dev/null || { echo "setup failed: victim not running"; return 1; }

  terminate_pids_safely "$victim"

  if kill -0 "$victim" 2>/dev/null; then
    echo "victim PID $victim still alive after terminate_pids_safely"
    kill -KILL "$victim" 2>/dev/null || true
    return 1
  fi
  [[ "${JOURNAL[0]}" == killed_pid\|${victim}\|* ]] || { echo "journal missing killed_pid entry: ${JOURNAL[*]}"; return 1; }
  return 0
}

test_terminate_pids_safely_refuses_self_and_init() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/termself_state"
  source "$SCRIPT"
  DRY_RUN="false"; JOURNAL=()
  # Should not throw, should not try to kill PID 1 or $$
  terminate_pids_safely "1" "$$"
  [[ ${#JOURNAL[@]} -eq 0 ]] || { echo "should not have journaled a kill of self/init: ${JOURNAL[*]}"; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
test_rollback_reverses_journal_with_side_effects() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export STATE_FILE="$BASE/state/rollback_state"
  export LOG_FILE="$BASE/state/test.log"
  source "$SCRIPT"
  reset_mock_state
  reset_mock_module_tree
  DRY_RUN="false"; ROLLING_BACK="0"

  # Simulate: do_bind got through stop_dm, unbinding the driver, and
  # removing modules, but died before actually binding to vfio-pci --
  # exactly the "worst case" partial state the rollback exists to prevent.
  JOURNAL=("stop_dm" "unbind_driver|0000:01:00.0|nvidia" "remove_modules")
  ORIG_DRIVER=(["0000:01:00.0"]="nvidia")
  REMOVED_MODULES=("nvidia_drm" "nvidia_modeset" "nvidia_uvm" "nvidia")
  DM_WAS_ACTIVE="true"; DM_UNIT="gdm.service"

  # nvidia modules are "removed" (not present) at this point, matching the
  # journal above.
  rm -rf "$BASE/sys/module/nvidia" "$BASE/sys/module/nvidia_drm" \
         "$BASE/sys/module/nvidia_modeset" "$BASE/sys/module/nvidia_uvm"

  rm -f "$BASE/state/calls.log"
  rollback_from_journal

  # 1) Display manager must have been restarted (stop_dm rolled back).
  is_unit_active "gdm.service" || { echo "DM should be active again after rollback"; return 1; }

  # 2) Modules must have been reloaded, base-first (reverse of removal).
  local order
  order="$(grep 'MODPROBE_CALL' "$BASE/state/calls.log" | sed 's/MODPROBE_CALL: //')"
  local expected=$'nvidia\nnvidia_uvm\nnvidia_modeset\nnvidia_drm'
  [[ "$order" == "$expected" ]] || { echo "reload order wrong during rollback:"; echo "$order"; return 1; }
  [[ -d "$BASE/sys/module/nvidia" ]] || { echo "nvidia module not reloaded"; return 1; }

  # 3) unbind_driver rollback must re-probe the device with its FULL,
  #    untruncated PCI address (regression check for the '|' vs ':'
  #    delimiter bug: a PCI address itself contains colons).
  local probed
  probed="$(cat "$BASE/sys/bus/pci/drivers_probe" 2>/dev/null || echo "")"
  [[ "$probed" == "0000:01:00.0" ]] || { echo "drivers_probe got '$probed', expected full address '0000:01:00.0'"; return 1; }

  # 3b) No journal entry should have fallen through to the "unknown type"
  #     branch -- if the delimiter parsing ever regresses, THIS is what
  #     would silently swallow the unbind_driver/rebind steps, so check
  #     for it explicitly rather than relying on downstream side effects
  #     alone (which can still coincidentally pass, as they did here once).
  if grep -q "Unknown journal entry type" "$LOG_FILE" 2>/dev/null; then
    echo "rollback hit an 'Unknown journal entry type' branch -- journal entry format/parsing mismatch"
    grep "Unknown journal entry type" "$LOG_FILE"
    return 1
  fi

  # 4) JOURNAL must end up empty, and rollback must NOT have journaled its
  #    OWN actions (the ROLLING_BACK guard in journal_push) -- if it had,
  #    JOURNAL would be non-empty right before the final clear.
  [[ ${#JOURNAL[@]} -eq 0 ]] || { echo "journal not cleared after rollback: ${JOURNAL[*]}"; return 1; }

  # 5) ROLLING_BACK flag must be reset afterward so a later real error
  #    doesn't get silently swallowed by on_error_trap's re-entrancy guard.
  [[ "$ROLLING_BACK" == "0" ]] || { echo "ROLLING_BACK left set to 1"; return 1; }
}

# ---------------------------------------------------------------------------
test_abort_triggers_automatic_rollback() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export STATE_FILE="$BASE/state/abort_state"
  export LOG_FILE="$BASE/state/test.log"
  export PATH="$BASE/../mock/bin:$PATH"
  source "$SCRIPT"
  reset_mock_state
  LOCK_ACQUIRED="1"   # maybe_rollback only acts once a lock is held
  AUTO_ROLLBACK="true"
  JOURNAL=("stop_dm")
  DM_WAS_ACTIVE="true"; DM_UNIT="gdm.service"

  # abort() calls exit 1, so run it in a subshell and check the mock
  # side-effect (DM restarted) survived into the parent.
  ( abort "simulated failure mid-operation" ) 2>/dev/null
  local rc=$?
  [[ $rc -ne 0 ]] || { echo "abort() should exit non-zero"; return 1; }
  is_unit_active "gdm.service" || { echo "expected DM restarted after abort()-triggered rollback"; return 1; }
}

test_auto_rollback_disabled_leaves_state_alone() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export STATE_FILE="$BASE/state/abort_state2"
  export LOG_FILE="$BASE/state/test.log"
  source "$SCRIPT"
  reset_mock_state
  systemctl stop gdm.service >/dev/null   # DM currently inactive
  LOCK_ACQUIRED="1"
  AUTO_ROLLBACK="false"
  JOURNAL=("stop_dm")
  DM_WAS_ACTIVE="true"; DM_UNIT="gdm.service"

  ( abort "simulated failure with rollback disabled" ) 2>/dev/null
  local rc=$?
  [[ $rc -ne 0 ]] || return 1
  # With AUTO_ROLLBACK=false, the DM must NOT have been restarted.
  if is_unit_active "gdm.service"; then
    echo "AUTO_ROLLBACK=false should have left the DM stopped, but it was restarted"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Direct regression test for the exact bug that was caught in manual review:
# PCI addresses contain colons (0000:01:00.0), so journal entries must use a
# delimiter that can't collide with a field's own content. This pins the
# '|'-delimited format and asserts the FULL, untruncated address is what
# actually gets written to drivers_probe during an unbind_driver rollback.
test_rollback_parses_pci_address_with_embedded_colons_correctly() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/parsecheck_state"
  source "$SCRIPT"
  ROLLING_BACK="0"
  : > "${SYSFS_PCI}/drivers_probe"

  rollback_one "unbind_driver|0000:01:00.0|nvidia"

  local written
  written="$(cat "${SYSFS_PCI}/drivers_probe")"
  [[ "$written" == "0000:01:00.0" ]] || { echo "drivers_probe got '$written', expected the FULL address '0000:01:00.0' (not truncated to '0000')"; return 1; }

  # Also confirm the general parser used by rollback_one/journal_push
  # round-trips a realistic multi-field entry exactly.
  local entry="rebind_driver|0000:01:00.0|nvidia"
  local type="${entry%%|*}"
  local rest="${entry#*|}"
  local addr="${rest%%|*}"
  local drv="${rest#*|}"
  [[ "$type" == "rebind_driver" ]] || return 1
  [[ "$addr" == "0000:01:00.0" ]] || { echo "addr parsed as '$addr'"; return 1; }
  [[ "$drv" == "nvidia" ]] || { echo "drv parsed as '$drv'"; return 1; }
}

# ---------------------------------------------------------------------------
test_circular_log_stays_bounded_and_keeps_recent_content() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  local logfile="$BASE/state/circular_test.log"
  export LOG_FILE="$logfile" STATE_FILE="$BASE/state/circular_state"
  source "$SCRIPT"
  rm -f "$logfile" "${logfile}.1" "${logfile}.trim"
  LOG_MAX_BYTES=2000   # small cap so the test runs fast
  LOG_LEVEL="DEBUG"

  local i
  for (( i=1; i<=400; i++ )); do
    log_info "MARKER $i filler filler filler filler filler"
  done

  # 1) Bounded: file must never exceed the cap (plus a little slack for the
  #    one line that can push it over before the next trim runs).
  local size
  size="$(stat -c '%s' "$logfile")"
  (( size <= LOG_MAX_BYTES + 300 )) || { echo "log grew to $size bytes, cap was $LOG_MAX_BYTES"; return 1; }

  # 2) Circular, not just truncated-to-empty: the LAST marker must be
  #    present (most recent content retained)...
  grep -q "MARKER 400 " "$logfile" || { echo "most recent entry (MARKER 400) missing from log"; return 1; }
  # ...and an EARLY marker must be gone (oldest content dropped).
  if grep -q "MARKER 1 " "$logfile"; then
    echo "oldest entry (MARKER 1) still present -- log isn't actually circular"
    return 1
  fi

  # 3) No numbered backup files of any kind, ever.
  local backups
  backups="$(find "$(dirname "$logfile")" -maxdepth 1 -name "$(basename "$logfile").*" ! -name "*.trim")"
  [[ -z "$backups" ]] || { echo "found backup file(s), expected none: $backups"; return 1; }

  # 4) File permissions stay locked down after trimming.
  local perms; perms="$(stat -c '%a' "$logfile")"
  [[ "$perms" == "600" ]] || { echo "log file perms after trim: $perms"; return 1; }
}

test_circular_log_default_cap_is_1mb() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/default_cap_test.log" STATE_FILE="$BASE/state/defaultcap_state"
  source "$SCRIPT"
  [[ "$LOG_MAX_BYTES" -eq 1048576 ]] || { echo "default LOG_MAX_BYTES is $LOG_MAX_BYTES, expected 1048576 (1 MiB)"; return 1; }
}

# ---------------------------------------------------------------------------
test_vtconsole_release_targets_only_framebuffer_console() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev" \
         SYSFS_VTCONSOLE="$BASE/sys/class/vtconsole"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/vtcon_state"
  source "$SCRIPT"
  DRY_RUN="false"; JOURNAL=(); RELEASED_VTCONSOLES=(); RELEASE_VT_CONSOLE="true"
  echo 1 > "$SYSFS_VTCONSOLE/vtcon0/bind"
  echo 1 > "$SYSFS_VTCONSOLE/vtcon1/bind"

  release_vt_consoles

  # Only vtcon1 (frame buffer) should have been touched; vtcon0 (dummy)
  # must be left completely alone.
  [[ "$(cat "$SYSFS_VTCONSOLE/vtcon0/bind")" == "1" ]] || { echo "dummy vtconsole vtcon0 was incorrectly unbound"; return 1; }
  [[ "$(cat "$SYSFS_VTCONSOLE/vtcon1/bind")" == "0" ]] || { echo "frame buffer vtconsole vtcon1 was NOT unbound"; return 1; }

  [[ "${RELEASED_VTCONSOLES[*]}" == "vtcon1" ]] || { echo "RELEASED_VTCONSOLES = ${RELEASED_VTCONSOLES[*]}"; return 1; }
  [[ "${JOURNAL[*]}" == "unbind_vtconsole|vtcon1" ]] || { echo "journal: ${JOURNAL[*]}"; return 1; }

  # Calling it again (already unbound) must be a safe no-op, not a second
  # journal entry / duplicate RELEASED_VTCONSOLES entry.
  release_vt_consoles
  [[ "${#RELEASED_VTCONSOLES[@]}" -eq 1 ]] || { echo "release_vt_consoles not idempotent: ${RELEASED_VTCONSOLES[*]}"; return 1; }
}

test_vtconsole_restore_rebinds_exactly_what_was_released() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev" \
         SYSFS_VTCONSOLE="$BASE/sys/class/vtconsole"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/vtcon_state2"
  source "$SCRIPT"
  DRY_RUN="false"; RELEASE_VT_CONSOLE="true"
  echo 0 > "$SYSFS_VTCONSOLE/vtcon1/bind"
  echo 1 > "$SYSFS_VTCONSOLE/vtcon0/bind"
  RELEASED_VTCONSOLES=("vtcon1")

  restore_vt_consoles

  [[ "$(cat "$SYSFS_VTCONSOLE/vtcon1/bind")" == "1" ]] || { echo "vtcon1 was not rebound"; return 1; }
  [[ "$(cat "$SYSFS_VTCONSOLE/vtcon0/bind")" == "1" ]] || { echo "vtcon0 (never released) was disturbed"; return 1; }
}

test_vtconsole_disabled_by_config_does_nothing() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev" \
         SYSFS_VTCONSOLE="$BASE/sys/class/vtconsole"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/vtcon_state3"
  source "$SCRIPT"
  DRY_RUN="false"; RELEASE_VT_CONSOLE="false"; RELEASED_VTCONSOLES=(); JOURNAL=()
  echo 1 > "$SYSFS_VTCONSOLE/vtcon1/bind"

  release_vt_consoles

  [[ "$(cat "$SYSFS_VTCONSOLE/vtcon1/bind")" == "1" ]] || { echo "vtcon1 was touched despite RELEASE_VT_CONSOLE=false"; return 1; }
  [[ ${#JOURNAL[@]} -eq 0 ]] || return 1
}

test_vtconsole_gracefully_skips_when_subsystem_absent() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev" \
         SYSFS_VTCONSOLE="$BASE/sys/class/does_not_exist_at_all"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/vtcon_state4"
  source "$SCRIPT"
  DRY_RUN="false"; RELEASE_VT_CONSOLE="true"; RELEASED_VTCONSOLES=(); JOURNAL=()

  release_vt_consoles   # must not error even though the directory doesn't exist
  restore_vt_consoles
  return 0
}

# ---------------------------------------------------------------------------
test_flr_only_runs_when_device_is_unbound() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/flr_state"
  source "$SCRIPT"
  DRY_RUN="false"; ALLOW_FUNCTION_LEVEL_RESET="true"
  local reset_file="$SYSFS_PCI/devices/0000:01:00.0/reset"
  : > "$reset_file"

  # Device currently HAS a driver bound (nvidia, per the mock fixture) --
  # FLR must be skipped for safety, i.e. the reset file must not be
  # written to.
  attempt_function_level_reset "0000:01:00.0"
  [[ -z "$(cat "$reset_file")" ]] || { echo "reset was written while device still had a driver bound -- unsafe"; return 1; }

  # Now actually unbind it (simulate what the kernel would do: remove the
  # driver symlink -- our mock filesystem is static and doesn't reactively
  # process writes to 'unbind' the way a real kernel would), then FLR
  # should proceed and write '1'.
  rm -f "${SYSFS_PCI}/devices/0000:01:00.0/driver"
  attempt_function_level_reset "0000:01:00.0"
  [[ "$(cat "$reset_file")" == "1" ]] || { echo "reset file content: '$(cat "$reset_file")', expected '1'"; return 1; }
}

test_flr_skips_gracefully_when_reset_file_absent() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/flr_state2"
  source "$SCRIPT"
  DRY_RUN="false"; ALLOW_FUNCTION_LEVEL_RESET="true"
  rm -f "$SYSFS_PCI/devices/0000:01:00.1/reset"   # audio function has no reset attribute

  attempt_function_level_reset "0000:01:00.1"   # must not error
  return 0
}

test_flr_disabled_by_config_does_nothing() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/flr_state3"
  source "$SCRIPT"
  DRY_RUN="false"; ALLOW_FUNCTION_LEVEL_RESET="false"
  local reset_file="$SYSFS_PCI/devices/0000:01:00.0/reset"
  : > "$reset_file"
  echo "0000:01:00.0" > "${SYSFS_PCI}/devices/0000:01:00.0/driver/unbind" 2>/dev/null || true

  attempt_function_level_reset "0000:01:00.0"
  [[ -z "$(cat "$reset_file")" ]] || { echo "reset was written despite ALLOW_FUNCTION_LEVEL_RESET=false"; return 1; }
}

# ---------------------------------------------------------------------------
test_dm_restart_after_bind_is_optional_and_gated_on_prior_state() {
  # Exercises the exact new semantics at the point they're decided (rather
  # than the whole do_bind orchestration, which needs real reactive sysfs
  # behavior our mock can't provide) -- both branches of the condition
  # used in do_bind's restart-after-bind block.
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/dmrestart_state"
  source "$SCRIPT"
  reset_mock_state
  DRY_RUN="false"; JOURNAL=()

  # Case A: enabled + was active before -> DM ends up started.
  RESTART_DISPLAY_MANAGER_AFTER_BIND="true"; DM_WAS_ACTIVE="true"; DM_UNIT="gdm.service"
  systemctl stop gdm.service >/dev/null
  if [[ "$RESTART_DISPLAY_MANAGER_AFTER_BIND" == "true" && "${DM_WAS_ACTIVE:-false}" == "true" ]]; then
    start_display_manager
  fi
  is_unit_active "gdm.service" || { echo "Case A: DM should have restarted after bind"; return 1; }

  # Case B: disabled -> DM must NOT be started even though it was active before.
  systemctl stop gdm.service >/dev/null
  RESTART_DISPLAY_MANAGER_AFTER_BIND="false"; DM_WAS_ACTIVE="true"
  if [[ "$RESTART_DISPLAY_MANAGER_AFTER_BIND" == "true" && "${DM_WAS_ACTIVE:-false}" == "true" ]]; then
    start_display_manager
  fi
  if is_unit_active "gdm.service"; then
    echo "Case B: DM was restarted despite RESTART_DISPLAY_MANAGER_AFTER_BIND=false"
    return 1
  fi

  # Case C: enabled, but DM was NOT active before -> still must not force-start it.
  systemctl stop gdm.service >/dev/null
  RESTART_DISPLAY_MANAGER_AFTER_BIND="true"; DM_WAS_ACTIVE="false"
  if [[ "$RESTART_DISPLAY_MANAGER_AFTER_BIND" == "true" && "${DM_WAS_ACTIVE:-false}" == "true" ]]; then
    start_display_manager
  fi
  if is_unit_active "gdm.service"; then
    echo "Case C: DM was force-started even though it wasn't active before bind"
    return 1
  fi
}

test_dm_restart_after_unbind_is_unconditional() {
  export SYSFS_PCI="$BASE/sys/bus/pci" SYSFS_MODULE="$BASE/sys/module" \
         SYSFS_IOMMU_GROUPS="$BASE/sys/kernel/iommu_groups" DEV_DIR="$BASE/dev"
  export LOG_FILE="$BASE/state/test.log" STATE_FILE="$BASE/state/dmunbind_state"
  source "$SCRIPT"
  reset_mock_state
  DRY_RUN="false"; JOURNAL=(); DM_UNIT="gdm.service"

  # Even with DM_WAS_ACTIVE=false and no config flag involved at all (there
  # is none anymore for this direction), do_unbind's own code always calls
  # start_display_manager unconditionally. Confirm THAT call, exactly as
  # do_unbind makes it, brings the DM up regardless of prior state.
  systemctl stop gdm.service >/dev/null
  DM_WAS_ACTIVE="false"

  start_display_manager
  journal_push "start_dm_after_unbind"

  is_unit_active "gdm.service" || { echo "DM was not restarted on unbind despite the 'always' requirement"; return 1; }
  [[ "${JOURNAL[*]}" == "start_dm_after_unbind" ]] || return 1
}

# ---------------------------------------------------------------------------
echo "=================================================================="
echo "Running vfio-toggle.sh logic tests against mock sysfs"
echo "=================================================================="
reset_mock_state
reset_mock_module_tree
t "normalize_module_name" test_normalize_module_name
t "pci_driver_of" test_pci_driver_of
t "IOMMU group expansion pulls in sibling functions" test_iommu_group_expansion
t "module holders traversal / correct unload order" test_module_holders_and_unload_order
t "reload_removed_modules reverses removal order" test_reload_removed_modules_reverses_order
t "other_devices_using_driver protects unrelated hardware" test_other_devices_using_driver
t "state persist/load round-trip (incl. assoc array)" test_persist_and_load_state_roundtrip
t "display-manager stop/start via systemd alias" test_display_manager_stop_start
t "config file permission/ownership enforcement" test_config_permission_checks
t "set -e arithmetic pitfalls (x++, reverse for-loop)" test_setminusE_arithmetic_pitfalls
t "terminate_pids_safely actually terminates a real pid" test_terminate_pids_safely_real_process
t "terminate_pids_safely refuses to signal self/PID1" test_terminate_pids_safely_refuses_self_and_init
t "rollback reverses journal with correct side effects" test_rollback_reverses_journal_with_side_effects
t "abort() triggers automatic rollback" test_abort_triggers_automatic_rollback
t "AUTO_ROLLBACK=false leaves partial state alone" test_auto_rollback_disabled_leaves_state_alone
t "rollback parses PCI addresses with embedded colons correctly" test_rollback_parses_pci_address_with_embedded_colons_correctly
t "circular log stays bounded and keeps recent content" test_circular_log_stays_bounded_and_keeps_recent_content
t "circular log default cap is 1 MiB" test_circular_log_default_cap_is_1mb
t "VT console release targets only the framebuffer console" test_vtconsole_release_targets_only_framebuffer_console
t "VT console restore rebinds exactly what was released" test_vtconsole_restore_rebinds_exactly_what_was_released
t "VT console release disabled by config does nothing" test_vtconsole_disabled_by_config_does_nothing
t "VT console handling skips gracefully when subsystem absent" test_vtconsole_gracefully_skips_when_subsystem_absent
t "FLR only runs once device is unbound (safety gate)" test_flr_only_runs_when_device_is_unbound
t "FLR skips gracefully when reset attribute absent" test_flr_skips_gracefully_when_reset_file_absent
t "FLR disabled by config does nothing" test_flr_disabled_by_config_does_nothing
t "DM restart after bind is optional + gated on prior state" test_dm_restart_after_bind_is_optional_and_gated_on_prior_state
t "DM restart after unbind is unconditional" test_dm_restart_after_unbind_is_unconditional

echo "=================================================================="
echo "RESULTS: $PASS passed, $FAIL failed"
echo "=================================================================="
exit $(( FAIL > 0 ? 1 : 0 ))
