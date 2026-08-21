# vfio-toggle

A robust, dynamic script to bind/unbind a GPU (and its whole IOMMU group)
to/from `vfio-pci`, for VFIO/KVM GPU passthrough — including single-GPU
passthrough, where the same card is shared between the host and a VM at
different times.

It's meant to be the thing you call from a keyboard shortcut, a systemd
service, or a libvirt hook right before starting the VM (`bind`) and right
after it shuts down (`unbind`).

## Why this exists / what makes it "robust"

Most GPU-passthrough toggle scripts floating around forums hardcode a
vendor's module names, a specific desktop environment's process names, and
use `options vfio-pci ids=10de:xxxx,10de:yyyy` in `modprobe.d` to grab the
card at boot. That approach has real problems:

- It can't tell two identical GPUs apart — `ids=` grabs *every* device with
  that vendor:device ID, system-wide.
- It hardcodes a driver's module stack (`nvidia_drm`, `nvidia_uvm`, ...),
  which changes between driver versions and doesn't work for AMD/Intel.
- It assumes a specific display manager or desktop environment.
- If it fails partway through, you're left with a half-unbound GPU, no
  display, and no idea what state you're in.

vfio-toggle.sh avoids all of that:

| Concern | How it's handled |
|---|---|
| Binding a specific device, not "every device like it" | `driver_override` + `drivers_probe` per PCI address, the documented sysfs mechanism — not `new_id`/`remove_id`/`ids=`. Safe with two identical GPUs. |
| Which functions need to move together | Every configured address is expanded to its full **IOMMU group** via `/sys/.../iommu_group`, so the GPU's HDMI audio / USB-C controller functions always travel with it. |
| Which kernel modules to remove, and in what order | Read live from `/sys/module/*/holders` (the kernel's own reverse-dependency graph) and removed deepest-dependent-first, recursively. Works for any driver stack, not just Nvidia's. |
| Reloading the right driver afterward | The device's own `modalias` sysfs file is used to ask the kernel what currently claims that hardware ID, in addition to replaying the exact modules that were removed, in reverse order. Correct even if the state file is lost or a driver package was updated in between. |
| Desktop environment / display manager | The standard systemd alias `display-manager.service` — whichever DM is enabled symlinks itself onto it — so this works identically under GNOME/GDM, KDE/SDDM, XFCE/LightDM, or anything else, with no per-DE logic. |
| Killing processes safely | SIGTERM, wait for a configurable grace period, SIGKILL only if still alive. Never signals PID 1 or itself. |
| Errors partway through | Every destructive step is journaled and persisted to disk *as it happens*; a failure replays the journal in reverse and undoes exactly what was done, in the correct order. |
| Config safety | The config is `source`d as root, so the script refuses to load it unless it's root-owned and not group/other-writable. |

## Requirements

- A systemd-based Linux distribution.
- IOMMU support enabled: VT-d (Intel) or AMD-Vi (AMD) in BIOS/UEFI, plus
  `intel_iommu=on` or `amd_iommu=on` on the kernel command line.
- `pciutils`, `kmod`, `util-linux`, `psmisc`, `coreutils` (all normally
  present by default; `vfio-toggle.sh bind`/`unbind` will tell you exactly
  what's missing if not).
- Root access.

## Install

```bash
sudo mkdir -p /etc/vfio-toggle
sudo install -m 755 vfio-toggle.sh /usr/local/bin/vfio-toggle.sh
sudo cp vfio-toggle.conf.example /etc/vfio-toggle/vfio-toggle.conf
sudo chmod 600 /etc/vfio-toggle/vfio-toggle.conf

# Find your GPU's PCI address and IOMMU group:
sudo vfio-toggle.sh list-devices

# Edit GPU_PCI_IDS (and anything else you want to change):
sudo nano /etc/vfio-toggle/vfio-toggle.conf

# Dry-run first, so nothing actually changes yet:
sudo vfio-toggle.sh bind --dry-run -v
```

## Usage

```
vfio-toggle.sh <command> [options]

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
  -n, --dry-run       Print what would be done without changing anything.
  -v, --verbose       Verbose console output (DEBUG level).
  -h, --help          Show this help.
```

`bind`, `unbind`, and `rollback` must run as root. `status` and
`list-devices` are read-only and don't require root.

Typical flow around a VM:

```bash
sudo vfio-toggle.sh bind      # right before starting the VM
# ... run your VM, use the GPU inside it ...
sudo vfio-toggle.sh unbind    # right after the VM shuts down
```

Both commands are idempotent — running `bind` when things are already
bound, or `unbind` when they're already back on the host, just logs that
there's nothing to do and exits cleanly.

### libvirt hook example

To run this automatically around a specific VM, add to
`/etc/libvirt/hooks/qemu` (create it if it doesn't exist, then
`chmod +x`):

```bash
#!/usr/bin/env bash
VM_NAME="$1"
ACTION="$2"

if [[ "$VM_NAME" == "my-gaming-vm" ]]; then
  if [[ "$ACTION" == "prepare" ]]; then
    /usr/local/bin/vfio-toggle.sh bind
  elif [[ "$ACTION" == "release" ]]; then
    /usr/local/bin/vfio-toggle.sh unbind
  fi
fi
```

## Configuration

See the comments in `vfio-toggle.conf.example` for the full list of
options (GPU addresses, whether to restart the display manager, whether to
fully unload kernel modules, process-kill grace period, log/state/lock
file locations, log verbosity). Any option you omit falls back to the
script's built-in default.

## Logs, state, and rollback

- **Log**: everything is logged in detail to `LOG_FILE` (default
  `/var/log/vfio-toggle.log`) regardless of console verbosity, so if
  something fails you can always find exactly which step it was on. It's
  timestamped and includes captured command output. Use `-v` for a more
  verbose console too, or set `LOG_LEVEL=DEBUG` permanently in the config.
- **State**: `STATE_FILE` (default `/var/lib/vfio-toggle/state`) records
  which driver each device came from, which modules were removed, and
  whether the display manager was running — written the moment each fact
  is known, not just at the end. `unbind` reads it back to know what to
  restore. `status` shows a summary of it.
- **Rollback**: every destructive action is journaled as it happens. If a
  later step fails, the journal is replayed in reverse automatically
  (unless you set `AUTO_ROLLBACK=false`), undoing exactly what was done.
  You can also trigger this manually at any time with
  `vfio-toggle.sh rollback`, which is useful if a run was killed outright
  (e.g. `kill -9`, power loss) before it could roll back on its own.

## Troubleshooting

- **`bind` fails at "did not bind via drivers_probe"**: check
  `dmesg | tail -50` for the underlying kernel-side reason. Common causes:
  another process still has a device file open (rare, since processes are
  killed before this point — check `LOG_FILE` for what was found), or the
  device is genuinely unable to reset (rare on modern hardware).
- **Display doesn't come back after `unbind`**: check
  `vfio-toggle.sh status` — it shows the current driver per device and
  whether the display manager is active. If a device shows no driver at
  all, its kernel module may not be installed/loadable after a system
  update; check `modinfo <driver>`.
- **Single-GPU passthrough specifically**: if `bind` still can't get the
  GPU driver to unbind cleanly even with `RELEASE_BOOT_FRAMEBUFFER=true`,
  you may need to also blacklist `efifb`/`vesafb`/`simplefb` via a kernel
  command-line option (`video=efifb:off` or similar for your setup), since
  firmware framebuffers are a separate subsystem this script only makes a
  best-effort attempt at releasing.
- **A run was killed outright and didn't roll back**: run
  `sudo vfio-toggle.sh status` to see if a state file with leftover
  journal entries exists, then `sudo vfio-toggle.sh rollback`.
- **"Refusing to load config: not owned by root" / "group/other-writable"**:
  this is deliberate — the config is `source`d as root. Fix with
  `sudo chown root:root` / `sudo chmod 600` on the config file.

## Limitations

- This is a *runtime* toggle. It doesn't change what happens at boot; if
  you reboot while the GPU is bound to vfio-pci, it comes back on its
  normal host driver at the next boot (unless you separately configured
  boot-time binding, which this script doesn't touch and isn't meant to
  be combined with).
- It only manages the GPU's own IOMMU group. Multi-function isolation
  problems caused by poor motherboard ACS support are a hardware/BIOS
  concern outside what any userspace script can fix.
- `list-devices` and `status` show `lspci`'s description if `pciutils` is
  installed; the rest of the script doesn't depend on it being present
  beyond that.

## Files

- `vfio-toggle.sh` — the script.
- `vfio-toggle.conf.example` — template config; copy to
  `/etc/vfio-toggle/vfio-toggle.conf`.
- `tests/` — a self-contained logic test suite (mock `/sys` tree + stubbed
  `modprobe`/`systemctl`/etc., no real hardware or root needed) covering
  IOMMU group expansion, kernel module dependency ordering, state
  persistence, display-manager handling, process termination, and the
  rollback/journal mechanism. Run it with `cd tests && ./run_tests.sh`
  any time you modify the script. It rebuilds its own fixtures from
  scratch on every run via `setup_mocks.sh`.
