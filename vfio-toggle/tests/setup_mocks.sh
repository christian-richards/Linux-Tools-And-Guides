#!/usr/bin/env bash
# Builds a throwaway fake /sys tree + stub commands (modprobe, rmmod,
# systemctl, lspci, fuser) under tests/mock/, so run_tests.sh can exercise
# vfio-toggle.sh's real logic without touching a real system. Safe to
# re-run any time; always wipes and rebuilds from scratch.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

BASE="$PWD/mock"
rm -rf "$BASE"
mkdir -p \
  "$BASE/sys/bus/pci/devices" "$BASE/sys/bus/pci/drivers" \
  "$BASE/sys/module" "$BASE/sys/kernel/iommu_groups" \
  "$BASE/sys/bus/platform/devices" "$BASE/sys/bus/platform/drivers" \
  "$BASE/dev/dri" "$BASE/bin" "$BASE/state"

ABS="$BASE"

# --- One GPU with 2 functions (VGA + HDMI audio) sharing IOMMU group 14 ---

mkdir -p "$ABS/sys/kernel/iommu_groups/14/devices"

D="$ABS/sys/bus/pci/devices/0000:01:00.0"
mkdir -p "$D/drm/card0" "$D/drm/renderD128"
echo "0x10de" > "$D/vendor"
echo "0x1e84" > "$D/device"
echo "0x030000" > "$D/class"
echo "pci:v000010DEd00001E84sv00001458sd00003FE0bc03sc00i00" > "$D/modalias"
mkdir -p "$ABS/sys/bus/pci/drivers/nvidia"
ln -sfn "$ABS/sys/bus/pci/drivers/nvidia" "$D/driver"
ln -sfn "$D" "$ABS/sys/bus/pci/drivers/nvidia/0000:01:00.0"
ln -sfn "$ABS/sys/kernel/iommu_groups/14" "$D/iommu_group"
ln -sfn "$D" "$ABS/sys/kernel/iommu_groups/14/devices/0000:01:00.0"

D="$ABS/sys/bus/pci/devices/0000:01:00.1"
mkdir -p "$D"
echo "0x10de" > "$D/vendor"
echo "0x10f8" > "$D/device"
echo "0x040300" > "$D/class"
echo "pci:v000010DEd000010F8sv00001458sd00003FE0bc04sc03i00" > "$D/modalias"
mkdir -p "$ABS/sys/bus/pci/drivers/snd_hda_intel"
ln -sfn "$ABS/sys/bus/pci/drivers/snd_hda_intel" "$D/driver"
ln -sfn "$D" "$ABS/sys/bus/pci/drivers/snd_hda_intel/0000:01:00.1"
ln -sfn "$ABS/sys/kernel/iommu_groups/14" "$D/iommu_group"
ln -sfn "$D" "$ABS/sys/kernel/iommu_groups/14/devices/0000:01:00.1"

mkdir -p "$ABS/sys/bus/pci/drivers/vfio-pci"
touch "$ABS/sys/bus/pci/drivers_probe"

# --- Writable 'reset' (Function-Level Reset) attribute on the GPU function ---
touch "$ABS/sys/bus/pci/devices/0000:01:00.0/reset"

# --- VT console (fbcon) fixtures: one dummy console, one frame-buffer console ---
mkdir -p "$ABS/sys/class/vtconsole/vtcon0" "$ABS/sys/class/vtconsole/vtcon1"
echo "(S) dummy device" > "$ABS/sys/class/vtconsole/vtcon0/name"
echo "1" > "$ABS/sys/class/vtconsole/vtcon0/bind"
echo "(M) frame buffer device" > "$ABS/sys/class/vtconsole/vtcon1/name"
echo "1" > "$ABS/sys/class/vtconsole/vtcon1/bind"

# --- Module dependency chain for unload/reload ordering tests ---
#   nvidia (base) <- nvidia_modeset (mid) <- nvidia_drm (leaf)
#   nvidia (base) <- nvidia_uvm (leaf, independent branch)
# holders/X = modules that DEPEND ON X (must be removed before X).

mkdir -p "$ABS/sys/module/nvidia/holders"
mkdir -p "$ABS/sys/module/nvidia_modeset/holders"
mkdir -p "$ABS/sys/module/nvidia_uvm/holders"
mkdir -p "$ABS/sys/module/nvidia_drm/holders"
ln -sf ../../nvidia_modeset "$ABS/sys/module/nvidia/holders/nvidia_modeset"
ln -sf ../../nvidia_uvm "$ABS/sys/module/nvidia/holders/nvidia_uvm"
ln -sf ../../nvidia_drm "$ABS/sys/module/nvidia_modeset/holders/nvidia_drm"

# --- Stub commands (prepended to PATH by run_tests.sh) ---

cat > "$ABS/bin/modprobe" << EOF
#!/usr/bin/env bash
echo "MODPROBE_CALL: \$*" >> "$ABS/state/calls.log"
if [[ "\$1" == "-r" ]]; then
  mod="\${2//-/_}"
  if [[ "\${FAIL_MODPROBE_R:-}" == "\$mod" ]]; then
    echo "modprobe: FATAL: Module \$mod is in use" >&2
    exit 1
  fi
  rm -rf "$ABS/sys/module/\$mod"
  exit 0
else
  arg="\$1"
  if [[ "\$arg" == pci:* ]]; then
    case "\$arg" in
      *v000010DEd00001E84*) mod="nvidia" ;;
      *v000010DEd000010F8*) mod="snd_hda_intel" ;;
      *) exit 1 ;;
    esac
  else
    mod="\${arg//-/_}"
  fi
  mkdir -p "$ABS/sys/module/\$mod/holders"
  exit 0
fi
EOF

cat > "$ABS/bin/rmmod" << EOF
#!/usr/bin/env bash
echo "RMMOD_CALL: \$*" >> "$ABS/state/calls.log"
mod="\${1//-/_}"
rm -rf "$ABS/sys/module/\$mod"
exit 0
EOF

cat > "$ABS/bin/systemctl" << EOF
#!/usr/bin/env bash
STATE_DIR="$ABS/state"
echo "SYSTEMCTL_CALL: \$*" >> "\$STATE_DIR/calls.log"
case "\$1" in
  show)
    if [[ "\${*: -1}" == "display-manager.service" ]]; then
      [[ "\$*" == *"-p LoadState"* ]] && echo "loaded"
      [[ "\$*" == *"-p Id"* ]] && echo "gdm.service"
    fi
    ;;
  is-active)
    st="\$(cat "\$STATE_DIR/dm_active" 2>/dev/null || echo active)"
    [[ "\$st" == "active" ]] && exit 0 || exit 3
    ;;
  stop)  echo "inactive" > "\$STATE_DIR/dm_active"; exit 0 ;;
  start) echo "active"   > "\$STATE_DIR/dm_active"; exit 0 ;;
  cat)   exit 0 ;;
esac
exit 0
EOF

cat > "$ABS/bin/lspci" << 'EOF'
#!/usr/bin/env bash
if [[ "$1" == "-s" ]]; then
  echo "$2 VGA compatible controller: Mock GPU Inc. Big Fast GPU (rev a1)"
fi
exit 0
EOF

cat > "$ABS/bin/fuser" << EOF
#!/usr/bin/env bash
PIDFILE="$ABS/state/fuser_pids"
echo "FUSER_CALL: \$*" >> "$ABS/state/calls.log"
if [[ -f "\$PIDFILE" ]]; then
  echo "/dev/dri/card0:  \$(cat "\$PIDFILE")"
  exit 0
fi
exit 1
EOF

chmod +x "$ABS"/bin/*
echo "active" > "$ABS/state/dm_active"

echo "Mock environment built under: $BASE"
