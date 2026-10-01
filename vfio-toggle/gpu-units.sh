#!/bin/sh
# gpu-units.sh - Dynamic, driver-agnostic vfio-toggle hook.
# Handles display managers, user graphical sessions, and vendor daemons dynamically.
#
# Environment variables provided by vfio-toggle:
#   $VFIO_TOGGLE_EVENT   - pre-bind | post-bind | pre-unbind | post-unbind | rollback
#   $VFIO_TOGGLE_DEVICES - Space-separated PCI IDs (e.g. "0000:01:00.0 0000:01:00.1")
#
# Optional tuning (every knob has a safe default):
#   GPU_HOOK_TIMEOUT          Seconds to wait in each release phase.            (default: 10)
#   GPU_HOOK_DM_POLICY        auto   = only tear down the display stack if something still
#                                      holds the GPU after the services were stopped
#                             always = always stop the display manager         (default: auto)
#   GPU_HOOK_FORCE_KILL       1 = SIGKILL user processes that ignore SIGTERM.   (default: 0)
#   GPU_HOOK_PROTECTED_UNITS  Space-separated unit globs that must never be stopped.
#   GPU_HOOK_EXTRA_UNITS      Space-separated units to stop even if they do not hold the GPU.
#
# How "who is using the GPU?" is answered (no fuser/lsof, no unit-name guessing):
#   * Device nodes come from the kernel: DEVNAME in the PCI device's sysfs uevent files.
#   * Holders are found by scanning /proc/<pid>/fd (inode identity) and the DRM fdinfo key
#     "drm-pdev" (Documentation/gpu/drm-usage-stats.rst), plus /proc/<pid>/maps.
#   * Every holder is mapped to its owner through its cgroup path, which systemd documents
#     (https://systemd.io/CGROUP_DELEGATION/): system service, logind session scope,
#     or a unit inside a user manager (user@UID.service).
#   * Everything is verified at the end; if the GPU is still busy the bind is aborted
#     instead of letting the kernel hang in an unbind.
#
# Unlike the original, 'set -e' is NOT used: it silently changes meaning inside
# conditionals/pipelines. Errors are handled explicitly and reported via exit status.

set -u

STATE_DIR="${VFIO_TOGGLE_HOOK_STATE_DIR:-/run/vfio-toggle}"
DM_MARKER="$STATE_DIR/display-manager-stopped"
SERVICES_MARKER="$STATE_DIR/stopped-services.list"
RELEASE_TIMEOUT="${GPU_HOOK_TIMEOUT:-10}"

main() {
    case "${VFIO_TOGGLE_EVENT:-}" in

        # =====================================================================
        # HOOK STATE: pre-bind
        # ---------------------------------------------------------------------
        # GENERIC PURPOSE:
        #   Executed BEFORE devices are detached from their host drivers and
        #   bound to vfio-pci. Failures in this state are FATAL and abort the
        #   bind sequence.
        #
        #   Its purpose is to ensure the hardware is completely idle. Any host
        #   processes, graphical compositors, display servers, or background
        #   daemons actively using the hardware must release their handles and
        #   terminate or pause so the kernel can unbind the host driver cleanly
        #   without deadlocks.
        # =====================================================================
        pre-bind)
            mkdir -p "$STATE_DIR"

            # 1. Dynamically identify and stop vendor/system services using the GPU
            save_and_stop_services

            # 2. Stop the Display Manager (skipped when the GPU is already idle)
            stop_display_manager ||
                fail_pre_bind "Could not stop the display manager."

            # 3. Cleanly tear down graphical user sessions, compositors and stragglers
            stop_graphical_sessions ||
                fail_pre_bind "Could not release the GPU from the user sessions."

            # 4. Prove it: never hand a busy GPU to the unbind (it can hang the kernel)
            verify_gpu_released ||
                fail_pre_bind "The GPU is still in use; refusing to unbind it."
            ;;

        # =====================================================================
        # HOOK STATE: post-bind
        # ---------------------------------------------------------------------
        # GENERIC PURPOSE:
        #   Executed AFTER devices have successfully detached from host drivers
        #   and bound to vfio-pci. Failures in this state are NON-FATAL.
        #
        #   Its purpose is post-attachment setup. This phase is used to launch
        #   virtual machines, configure hypervisor permissions, or bring the
        #   host desktop environment back up on any remaining host-managed
        #   display hardware (e.g., an integrated or secondary GPU).
        # =====================================================================
        post-bind)
            start_display_manager || exit 1
            ;;

        # =====================================================================
        # HOOK STATE: pre-unbind
        # ---------------------------------------------------------------------
        # GENERIC PURPOSE:
        #   Executed BEFORE devices are detached from vfio-pci to be returned
        #   to the host. Failures in this state are FATAL and abort the unbind.
        #
        #   Its purpose is pre-restoration teardown. It verifies that virtual
        #   machines or VFIO consumers have completely closed their device
        #   descriptors, cleans up guest-side resources, and prepares the host
        #   subsystems to receive the hardware back safely.
        # =====================================================================
        pre-unbind)
            # Intentionally left untouched: on hybrid/dual-GPU setups, the host
            # display manager running on the iGPU does not need to be interrupted
            # while returning the dGPU to the host.
            ;;

        # =====================================================================
        # HOOK STATE: post-unbind
        # ---------------------------------------------------------------------
        # GENERIC PURPOSE:
        #   Executed AFTER devices have successfully detached from vfio-pci and
        #   re-bound to their original host kernel drivers. Failures in this
        #   state are NON-FATAL.
        #
        #   Its purpose is to resume regular host workloads. This phase is used
        #   to restart host services or vendor daemons that depend on the host
        #   driver (e.g., power managers, compute runtimes), re-enable PRIME
        #   render offloading, or reconfigure display layouts.
        # =====================================================================
        post-unbind)
            restore_stopped_services
            # Single-GPU case: post-bind had no display hardware left to start the
            # display manager on. Retry now that the GPU is back. No-op otherwise.
            start_display_manager || exit 1
            ;;

        # =====================================================================
        # HOOK STATE: rollback
        # ---------------------------------------------------------------------
        # GENERIC PURPOSE:
        #   Executed whenever a 'bind' command fails midway or when an explicit
        #   recovery rollback is executed. Failures in this state are NON-FATAL,
        #   and code here MUST be idempotent (safe to run multiple times).
        #
        #   Its purpose is emergency state reconciliation. It brings the system
        #   back to a coherent, usable state by reverting any changes made
        #   during the interrupted bind attempt (e.g., restarting stopped
        #   services or display managers).
        # =====================================================================
        rollback)
            restore_stopped_services
            start_display_manager || exit 1
            ;;

        *)
            echo "Unknown event: ${VFIO_TOGGLE_EVENT:-<unset>}" >&2
            ;;
    esac

    exit 0
}

# =====================================================================
# CUSTOM FUNCTIONS (everything below the boilerplate above)
# =====================================================================
#
# Conventions:
#  * Helpers are declared with a subshell body, "name() ( ... )". POSIX sh has no
#    `local`, so this keeps their variables from clobbering the caller's, and
#    `exit` inside them behaves like `return`.
#  * Helpers whose stdout is captured with $(...) never call log().
#  * Table rows produced by holder_table() look like:  pid|kind|a|b|comm
#      system|<unit>|          outermost system service owning the process
#      session|<id>|           logind session scope
#      user|<uid>|<unit>       unit inside that user's systemd manager
#      usermgr|<uid>|          the user manager itself (never touched)
#      other|<cgroup>|         anything else: system scopes, containers, root cgroup...

log()  { printf 'gpu-units: %s\n' "$*"; }
warn() { printf 'gpu-units: WARNING: %s\n' "$*" >&2; }

# Abort a failed pre-bind: undo what we did (idempotent) and exit non-zero.
fail_pre_bind() {
    warn "$*"
    warn "Aborting bind and restoring stopped services/display manager."
    restore_stopped_services || true
    start_display_manager || true
    exit 1
}

# ---------------------------------------------------------------------
# Kernel-provided device discovery (sysfs / procfs)
# ---------------------------------------------------------------------

# Host driver bound to a PCI device, as reported by the kernel's uevent file.
device_driver() (
    while IFS='=' read -r key val; do
        if [ "$key" = DRIVER ]; then echo "$val"; exit 0; fi
    done 2>/dev/null < "/sys/bus/pci/devices/$1/uevent"
    exit 1
)

# /dev path of the sysfs device directory $1 (kernel-reported DEVNAME).
uevent_devnode() (
    while IFS='=' read -r key val; do
        if [ "$key" = DEVNAME ]; then echo "/dev/$val"; exit 0; fi
    done 2>/dev/null < "$1/uevent"
    exit 1
)

# DRM card*/renderD* nodes that belong to PCI device $1 (driver independent).
drm_nodes_of() (
    for d in "/sys/bus/pci/devices/$1"/drm/*; do
        [ -e "$d/uevent" ] || continue
        uevent_devnode "$d"
    done
    exit 0
)

# Vendor nodes that are not exposed under the PCI device in sysfs.
# $1 = PCI id, $2 = host driver name. This is the one deliberate vendor extension point.
vendor_nodes_of() (
    dev=$1
    drv=$2
    case "$drv" in
        nvidia)
            # The NVIDIA driver documents the per-GPU minor under /proc/driver/nvidia.
            minor=""
            while IFS=: read -r key val; do
                if [ "$key" = "Device Minor" ]; then set -- $val; minor=${1:-}; fi
            done 2>/dev/null < "/proc/driver/nvidia/gpus/$dev/information"
            if [ -n "$minor" ]; then echo "/dev/nvidia$minor"; exit 0; fi
            ;;
        amdgpu)
            [ -c /dev/kfd ] && echo /dev/kfd
            ;;
    esac
    # Generic fallback: character nodes named after the driver (/dev/<drv>*, /dev/<drv>*/*)
    for node in "/dev/$drv"* "/dev/$drv"*/*; do
        [ -c "$node" ] && echo "$node"
    done
    exit 0
)

# All device nodes whose use must stop before the devices can be unbound.
target_nodes() (
    for dev in ${VFIO_TOGGLE_DEVICES:-}; do
        drm_nodes_of "$dev"
        drv=$(device_driver "$dev") || continue
        [ "$drv" = vfio-pci ] && continue
        vendor_nodes_of "$dev" "$drv"
    done
    exit 0
)

# ---------------------------------------------------------------------
# /proc helpers (shell builtins only on the hot paths)
# ---------------------------------------------------------------------

proc_comm() (
    c=""
    read -r c 2>/dev/null < "/proc/$1/comm" || true
    echo "${c:-$1}"
)

proc_ppid() (
    while read -r key val; do
        if [ "$key" = "PPid:" ]; then echo "$val"; exit 0; fi
    done 2>/dev/null < "/proc/$1/status"
    exit 1
)

# systemd-visible cgroup path of a process (cgroup v2, or the v1 named hierarchy).
proc_cgroup() (
    while IFS= read -r line; do
        case "$line" in
            0::*)                 echo "${line#0::}"; exit 0 ;;
            *:name=systemd:*)     echo "${line#*:name=systemd:}"; exit 0 ;;
        esac
    done 2>/dev/null < "/proc/$1/cgroup"
    exit 1
)

# This script and all of its ancestors (shell, sudo, terminal, vfio-toggle...). Never signalled.
ancestor_pids() (
    p=$$
    out=""
    while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null; do
        out="$out $p"
        p=$(proc_ppid "$p") || break
    done
    echo "$out"
)

# Classify a cgroup path into "kind|a|b" (see table at the top of this section).
cg_classify() (
    path=${1:-}
    IFS=/
    set -f
    # shellcheck disable=SC2086
    set -- $path
    uid=""
    for c in "$@"; do
        if [ -n "$uid" ]; then
            case "$c" in
                init.scope)       echo "usermgr|$uid|"; exit 0 ;;
                *.service|*.scope) echo "user|$uid|$c"; exit 0 ;;
            esac
            continue
        fi
        case "$c" in
            user@*.service) uid=${c#user@}; uid=${uid%.service} ;;
            session-*.scope) c=${c#session-}; echo "session|${c%.scope}|"; exit 0 ;;
            *.service)       echo "system|$c|"; exit 0 ;;
        esac
    done
    if [ -n "$uid" ]; then echo "usermgr|$uid|"; else echo "other|$path|"; fi
)

# ---------------------------------------------------------------------
# Holder detection: which processes keep the GPU open?
# ---------------------------------------------------------------------

# Prints PIDs (possibly duplicated) of processes using any target device.
find_holder_pids() (
    [ -n "${VFIO_TOGGLE_DEVICES:-}" ] || exit 0
    nodes=$(target_nodes)
    seen=" "

    # (a) Open file descriptors. Only character devices are inspected (cheap, builtin test).
    for fd in /proc/[0-9]*/fd/*; do
        [ -c "$fd" ] || continue
        pid=${fd#/proc/}
        pid=${pid%%/*}
        case "$seen" in *" $pid "*) continue ;; esac

        hit=0
        # Same inode as a known node (resolves symlinks, /dev/char, by-path, bind mounts...)
        for n in $nodes; do
            if [ "$fd" -ef "$n" ]; then hit=1; break; fi
        done
        # DRM fdinfo names the PCI device behind the fd. This also covers fds that live in
        # other mount namespaces (containers) where the /dev inode differs.
        if [ "$hit" -eq 0 ]; then
            while IFS=: read -r key val; do
                if [ "$key" = drm-pdev ]; then
                    set -- $val
                    if [ -n "${1:-}" ]; then
                        case " $VFIO_TOGGLE_DEVICES " in *" $1 "*) hit=1 ;; esac
                    fi
                    break
                fi
            done 2>/dev/null < "/proc/$pid/fdinfo/${fd##*/}"
        fi

        if [ "$hit" -eq 1 ]; then seen="$seen$pid "; echo "$pid"; fi
    done

    # (b) Memory mappings that outlive a closed fd (the kernel still counts these as "open").
    if [ -n "$nodes" ] && command -v grep >/dev/null 2>&1; then
        set --
        for n in $nodes; do set -- "$@" -e "$n"; done
        for f in $(grep -lsF "$@" /proc/[0-9]*/maps 2>/dev/null); do
            pid=${f#/proc/}
            echo "${pid%/maps}"
        done
    fi
    exit 0
)

# One "pid|kind|a|b|comm" line per distinct holder (self and ancestors excluded).
holder_table() (
    seen=" "
    for pid in $(find_holder_pids); do
        case "$seen" in *" $pid "*) continue ;; esac
        seen="$seen$pid "
        case " ${PROTECTED_PIDS:-} " in *" $pid "*) continue ;; esac
        [ -d "/proc/$pid" ] || continue
        info=$(cg_classify "$(proc_cgroup "$pid")")
        echo "$pid|$info|$(proc_comm "$pid")"
    done
    exit 0
)

# Success once nothing holds the GPU; fails after $1 seconds.
wait_for_idle() (
    waited=0
    while [ -n "$(holder_table)" ]; do
        [ "$waited" -ge "$1" ] && exit 1
        sleep 1
        waited=$((waited + 1))
    done
    exit 0
)

# Success if a user process (session or user-manager unit) still holds the GPU,
# i.e. there is still something these phases can act on.
user_holders_remain() (
    case "$(holder_table)" in
        *"|session|"*|*"|user|"*) exit 0 ;;
    esac
    exit 1
)

# Final gate for pre-bind.
verify_gpu_released() (
    if wait_for_idle 2; then log "GPU is idle."; exit 0; fi
    warn "These processes still hold the GPU:"
    holder_table | while IFS='|' read -r pid kind a b comm; do
        case "$kind" in
            system)  where="system service $a" ;;
            session) where="logind session $a" ;;
            user)    where="user unit $b (UID $a)" ;;
            usermgr) where="user manager of UID $a" ;;
            *)       where="cgroup $a" ;;
        esac
        warn "  PID $pid ($comm) - $where"
    done
    exit 1
)

# ---------------------------------------------------------------------
# System services
# ---------------------------------------------------------------------

# Units that must never be stopped: infrastructure, our own unit, the display manager
# (handled separately), and anything the user protected.
is_protected_unit() (
    unit=$1
    set -f
    for pat in systemd-logind.service 'user@*.service' 'user-runtime-dir@*.service' \
               ${GPU_HOOK_PROTECTED_UNITS:-}; do
        # shellcheck disable=SC2254
        case "$unit" in $pat) exit 0 ;; esac
    done
    [ "$unit" = "$(self_unit)" ] && exit 0
    [ "$unit" = "$(display_manager_unit)" ] && exit 0
    exit 1
)

# The system service this script itself runs in, if any.
self_unit() (
    IFS='|' read -r kind unit _ <<EOF
$(cg_classify "$(proc_cgroup $$)")
EOF
    [ "$kind" = system ] && echo "$unit"
    exit 0
)

# Services to stop: the owners of every holder, plus any explicitly requested extras.
gpu_service_units() (
    seen=" "
    holders=$(holder_table)
    while IFS='|' read -r pid kind unit b comm; do
        [ "$kind" = system ] || continue
        is_protected_unit "$unit" && continue
        case "$seen" in *" $unit "*) continue ;; esac
        seen="$seen$unit "
        echo "$unit"
    done <<EOF
$holders
EOF
    for unit in ${GPU_HOOK_EXTRA_UNITS:-}; do
        case "$seen" in *" $unit "*) continue ;; esac
        seen="$seen$unit "
        echo "$unit"
    done
    exit 0
)

# True if line $2 is present in file $1.
list_has() (
    while IFS= read -r l; do
        [ "$l" = "$2" ] && exit 0
    done 2>/dev/null < "$1"
    exit 1
)

record_stopped() (
    list_has "$SERVICES_MARKER" "$1" || echo "$1" >> "$SERVICES_MARKER"
)

# Stop a unit together with whatever would re-activate it (sockets, paths, timers),
# discovered through systemd's own TriggeredBy= property.
stop_unit() (
    unit=$1
    systemctl is-active --quiet "$unit" 2>/dev/null || exit 0
    for trig in $(systemctl show --property=TriggeredBy --value "$unit" 2>/dev/null); do
        systemctl is-active --quiet "$trig" 2>/dev/null || continue
        record_stopped "$trig"
        log "Stopping trigger: $trig"
        systemctl stop "$trig" || warn "Failed to stop $trig"
    done
    record_stopped "$unit"
    log "Stopping service: $unit"
    systemctl stop "$unit" || warn "Failed to stop $unit"
    exit 0
)

save_and_stop_services() (
    for unit in $(gpu_service_units); do
        stop_unit "$unit"
    done
    exit 0
)

# Restart everything we stopped, in reverse order. Idempotent: the marker is consumed first.
restore_stopped_services() (
    [ -f "$SERVICES_MARKER" ] || exit 0
    reversed=""
    while IFS= read -r unit; do
        [ -n "$unit" ] && reversed="$unit $reversed"
    done < "$SERVICES_MARKER"
    rm -f "$SERVICES_MARKER"
    for unit in $reversed; do
        log "Restoring service: $unit"
        systemctl start "$unit" ||
            warn "Failed to restore $unit. Start it manually: systemctl start $unit"
    done
    exit 0
)

# ---------------------------------------------------------------------
# Display manager
# ---------------------------------------------------------------------

# Real unit behind the display-manager.service alias (systemd's documented convention,
# created by `systemctl enable <dm>`); fails if no display manager is active.
display_manager_unit() (
    systemctl is-active --quiet display-manager.service 2>/dev/null || exit 1
    id=$(systemctl show --property=Id --value display-manager.service 2>/dev/null)
    echo "${id:-display-manager.service}"
)

stop_display_manager() (
    dm=$(display_manager_unit) || { log "No active display manager found."; exit 0; }
    if [ "${GPU_HOOK_DM_POLICY:-auto}" = auto ] && [ -z "$(holder_table)" ]; then
        log "GPU is idle; leaving $dm running."
        exit 0
    fi
    guard_self_preservation || exit 1
    echo "$dm" > "$DM_MARKER"
    log "Stopping $dm"
    systemctl stop "$dm" || { warn "Failed to stop $dm"; exit 1; }
    exit 0
)

start_display_manager() (
    [ -f "$DM_MARKER" ] || exit 0
    dm=display-manager.service
    read -r dm < "$DM_MARKER" 2>/dev/null || true
    settle
    log "Starting $dm"
    if systemctl start "$dm"; then
        rm -f "$DM_MARKER"
        exit 0
    fi
    warn "Failed to start $dm. Start it manually: systemctl start $dm"
    exit 1
)

# ---------------------------------------------------------------------
# User sessions (logind) and user managers (systemd --user)
# ---------------------------------------------------------------------

# "sid uid" for each local graphical logind session.
graphical_sessions() (
    command -v loginctl >/dev/null 2>&1 || exit 0
    sessions=$(loginctl list-sessions --no-legend 2>/dev/null) || exit 0
    while read -r sid uid _; do
        [ -n "$sid" ] || continue
        props=$(loginctl show-session "$sid" --property=Remote --property=Type 2>/dev/null) ||
            continue
        remote=""
        type=""
        while IFS='=' read -r key val; do
            case "$key" in Remote) remote=$val ;; Type) type=$val ;; esac
        done <<EOF
$props
EOF
        [ "$remote" = no ] || continue
        case "$type" in wayland|x11|mir) echo "$sid $uid" ;; esac
    done <<EOF
$sessions
EOF
    exit 0
)

session_uid() (
    loginctl show-session "$1" --property=User --value 2>/dev/null
)

# Sessions to terminate: every local graphical session, plus any session owning a GPU
# holder even if logind does not call it graphical (e.g. sway started from a TTY).
teardown_sessions() (
    holders=$(holder_table)
    {
        graphical_sessions
        while IFS='|' read -r pid kind sid b comm; do
            [ "$kind" = session ] || continue
            echo "$sid $(session_uid "$sid")"
        done <<EOF
$holders
EOF
    } | {
        seen=" "
        while read -r sid uid; do
            case "$seen" in *" $sid "*) continue ;; esac
            seen="$seen$sid "
            echo "$sid $uid"
        done
    }
    exit 0
)

# Run systemctl --user against the manager of UID $1 (remaining args = systemctl args).
# Primary: the documented `--machine=<user>@.host` form. Fallback: talk to the manager's
# private socket through XDG_RUNTIME_DIR (root is allowed to).
user_systemctl() (
    uid=$1
    shift
    name=$(loginctl show-user "$uid" --property=Name --value 2>/dev/null) || name=""
    if [ -n "$name" ] && systemctl --user --machine="$name@.host" "$@" 2>/dev/null; then
        exit 0
    fi
    XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user "$@"
)

# Stop the user-manager units (compositor services, app scopes...) that still hold the GPU.
stop_user_units() (
    seen=" "
    holders=$(holder_table)
    while IFS='|' read -r pid kind uid unit comm; do
        [ "$kind" = user ] || continue
        case "$seen" in *" $uid/$unit "*) continue ;; esac
        seen="$seen$uid/$unit "
        for trig in $(user_systemctl "$uid" show --property=TriggeredBy --value "$unit" 2>/dev/null); do
            log "Stopping user trigger: $trig (UID $uid)"
            user_systemctl "$uid" stop "$trig" >/dev/null 2>&1 || true
        done
        log "Stopping user unit: $unit (UID $uid)"
        user_systemctl "$uid" stop "$unit" 2>/dev/null ||
            warn "Failed to stop $unit for UID $uid"
    done <<EOF
$holders
EOF
    exit 0
)

# Signal lingering holders that belong to a user (never system services or unknown cgroups).
signal_holders() (
    sig=$1
    holders=$(holder_table)
    while IFS='|' read -r pid kind a b comm; do
        case "$kind" in session|user) ;; *) continue ;; esac
        log "Sending SIG$sig to lingering process: $comm (PID $pid)"
        kill "-$sig" "$pid" 2>/dev/null || true
    done <<EOF
$holders
EOF
    exit 0
)

# Refuse to tear down the very session/service this hook is running in: that would
# kill the hook (and vfio-toggle) halfway through and leave the GPU half-detached.
guard_self_preservation() (
    IFS='|' read -r kind a _ <<EOF
$(cg_classify "$(proc_cgroup $$)")
EOF
    hit=""
    case "$kind" in
        system)
            dm=$(display_manager_unit) || dm=""
            [ -n "$dm" ] && [ "$a" = "$dm" ] && hit="the display manager ($dm)"
            ;;
        session|user|usermgr)
            sessions=$(teardown_sessions)
            while read -r sid uid; do
                [ -n "$sid" ] || continue
                if [ "$kind" = session ] && [ "$sid" = "$a" ]; then hit="logind session $sid"; fi
                if [ "$kind" != session ] && [ "$uid" = "$a" ]; then hit="the session of UID $uid"; fi
            done <<EOF2
$sessions
EOF2
            ;;
    esac
    if [ -n "$hit" ]; then
        warn "This hook is running inside $hit, which has to be shut down to free the GPU."
        warn "Run vfio-toggle from a text console, over SSH, or from a standalone systemd service."
        exit 1
    fi
    exit 0
)

# Escalating release: graphical-session.target -> logind sessions -> user units ->
# SIGTERM -> (optional) SIGKILL. Stops as soon as the GPU is idle or nothing is left to try.
stop_graphical_sessions() (
    if [ -z "$(holder_table)" ] && [ ! -f "$DM_MARKER" ]; then
        log "No session is using the GPU."
        exit 0
    fi
    guard_self_preservation || exit 1

    sessions=$(teardown_sessions)

    # 1. Ask each affected user manager to wind down its graphical session units.
    uids=" "
    while read -r sid uid; do
        [ -n "$uid" ] || continue
        case "$uids" in *" $uid "*) ;; *) uids="$uids$uid " ;; esac
    done <<EOF
$sessions
EOF
    for uid in $uids; do
        log "Stopping graphical-session.target for UID $uid"
        user_systemctl "$uid" stop graphical-session.target >/dev/null 2>&1 || true
    done

    # 2. Terminate the logind sessions (SIGTERM to the whole session scope).
    while read -r sid uid; do
        [ -n "$sid" ] || continue
        log "Ending logind session $sid (UID $uid)"
        loginctl terminate-session "$sid" || warn "Failed to terminate session $sid"
    done <<EOF
$sessions
EOF
    wait_for_idle "$RELEASE_TIMEOUT" && exit 0
    user_holders_remain || exit 0

    # 3. Compositors and apps living in user-manager units.
    stop_user_units
    wait_for_idle "$RELEASE_TIMEOUT" && exit 0
    user_holders_remain || exit 0

    # 4. Whatever is left: SIGTERM, then SIGKILL only if explicitly allowed.
    signal_holders TERM
    wait_for_idle "$RELEASE_TIMEOUT" && exit 0
    user_holders_remain || exit 0

    if [ "${GPU_HOOK_FORCE_KILL:-0}" = 1 ]; then
        warn "Processes ignored SIGTERM; sending SIGKILL (GPU_HOOK_FORCE_KILL=1)."
        signal_holders KILL
        wait_for_idle "$RELEASE_TIMEOUT"
    fi
    exit 0   # verify_gpu_released reports anything that is still left
)

settle() {
    command -v udevadm >/dev/null 2>&1 && udevadm settle --timeout=15
    return 0
}

# Computed once: this script and its ancestors must never be signalled.
PROTECTED_PIDS=$(ancestor_pids)

main "$@"
