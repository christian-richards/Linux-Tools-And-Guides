#!/usr/bin/env python3
"""
vfio-toggle.py - dynamically bind/unbind a GPU (and its whole IOMMU group)
to/from vfio-pci for VFIO/KVM GPU passthrough.

Design goals (ported from the original vfio-toggle.sh):
  - Use only stable kernel/sysfs/systemd interfaces, never fragile hacks
    like `vfio-pci ids=` in modprobe.d or new_id/remove_id.
  - Discover everything dynamically: which driver a device is on, which
    kernel modules depend on it (and in what order to remove/reload them),
    which display-manager unit is active, which processes are using the
    GPU. Nothing about a specific vendor/DE/distro is hardcoded.
  - Journal every destructive step as it happens and persist it to disk
    (as JSON), so a failure partway through can be rolled back
    automatically.

Usage: vfio_toggle.py <bind|unbind|status|list-devices|rollback|show-log|clear-log> [opts]
Run with --help for details. Must be run as root (except status/list-devices/show-log).

The sysfs/proc/dev roots below are overridable via environment variables
(SYSFS_PCI, SYSFS_MODULE, SYSFS_IOMMU_GROUPS, SYSFS_PLATFORM, SYSFS_CLASS,
SYSFS_VTCONSOLE, DEV_DIR) for testing; on a real system these are always
the standard paths.

Config file format is INI (stdlib configparser) under a [vfio-toggle]
section - see require_config() below for the recognized keys. The state
file is JSON rather than the original's sourced shell assignments; no
code is ever executed to load config or state, only parsed as data.
"""

import configparser
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_NAME = SCRIPT_PATH.name
SCRIPT_DIR = SCRIPT_PATH.parent

LOG_LEVELS = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
PCI_ADDR_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def normalize_module_name(name: str) -> str:
    return name.replace("-", "_")


class VfioToggle:
    # ------------------------------------------------------------------
    # Construction / defaults
    # ------------------------------------------------------------------
    def __init__(self, command, config_path=None, verbose=False):
        self.command = command
        self.config_path = config_path
        self.verbose = verbose

        self.sysfs_pci = Path(os.environ.get("SYSFS_PCI", "/sys/bus/pci"))
        self.sysfs_module = Path(os.environ.get("SYSFS_MODULE", "/sys/module"))
        self.sysfs_iommu_groups = Path(os.environ.get("SYSFS_IOMMU_GROUPS", "/sys/kernel/iommu_groups"))
        self.sysfs_platform = Path(os.environ.get("SYSFS_PLATFORM", "/sys/bus/platform"))
        self.sysfs_class = Path(os.environ.get("SYSFS_CLASS", "/sys/class"))
        self.sysfs_vtconsole = Path(os.environ.get("SYSFS_VTCONSOLE", "/sys/class/vtconsole"))
        self.dev_dir = Path(os.environ.get("DEV_DIR", "/dev"))

        self.log_file = Path(os.environ.get("LOG_FILE", "/var/log/vfio-toggle.log"))
        self.state_file = Path(os.environ.get("STATE_FILE", "/var/lib/vfio-toggle/state.json"))
        self.lock_file = Path(os.environ.get("LOCK_FILE", "/run/vfio-toggle.lock"))
        self.log_level = "INFO"
        self.log_max_bytes = 1048576

        self.restart_display_manager_after_bind = True
        self.allow_full_module_unload = True
        self.release_boot_framebuffer = True
        self.release_vt_console = True
        self.allow_function_level_reset = True
        self.process_kill_grace_period = 10
        self.auto_rollback = True
        self.gpu_pci_ids = []

        # Runtime/rollback state, persisted to state_file across invocations.
        self.journal = []
        self.orig_driver = {}
        self.removed_modules = []
        self.released_vtconsoles = []
        self.stopped_services = []
        self.visited_modules = set()
        self.dm_was_active = False
        self.dm_unit = ""
        self.state_operation = ""

        self.target_devices = []
        self.restore_devices = []

        self.rolling_back = False
        self.lock_acquired = False
        self.lock_fd = None

        self.modprobe_block_file = Path("/run/modprobe.d/vfio-toggle-block.conf")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def _log(self, level, msg):
        ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
        line = f"[{ts}] [pid:{os.getpid()}] [{level}] {msg}"
        if self.log_file:
            try:
                with open(self.log_file, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass
            self._trim_log()
        threshold = LOG_LEVELS.get(self.log_level, 1)
        this = LOG_LEVELS.get(level, 1)
        if this >= threshold:
            stream = sys.stderr if level in ("ERROR", "WARN") else sys.stdout
            print(line, file=stream)

    def log_debug(self, msg):
        self._log("DEBUG", msg)

    def log_info(self, msg):
        self._log("INFO", msg)

    def log_warn(self, msg):
        self._log("WARN", msg)

    def log_error(self, msg):
        self._log("ERROR", msg)

    def _trim_log(self):
        # Circular buffer capped at log_max_bytes: oldest lines are dropped
        # in place. There is always exactly one log file, at self.log_file.
        try:
            size = self.log_file.stat().st_size
        except OSError:
            return
        if size <= self.log_max_bytes:
            return
        try:
            with open(self.log_file, "rb") as f:
                f.seek(-self.log_max_bytes, os.SEEK_END)
                data = f.read()
            nl = data.find(b"\n")
            trimmed = data[nl + 1 :] if nl != -1 else data
            tmp = self.log_file.with_name(self.log_file.name + ".trim")
            tmp.write_bytes(trimmed)
            tmp.replace(self.log_file)
            os.chmod(self.log_file, 0o600)
        except OSError:
            pass

    def setup_logging(self):
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            self.log_file.touch(exist_ok=True)
            os.chmod(self.log_file, 0o600)
        except OSError:
            pass
        self._trim_log()
        if self.verbose:
            self.log_level = "DEBUG"
        self.log_info(f"===== {SCRIPT_NAME} starting: command='{self.command}' pid={os.getpid()} =====")

    # ------------------------------------------------------------------
    # Error handling / rollback plumbing
    # ------------------------------------------------------------------
    def die_early(self, msg):
        self.log_error(msg)
        sys.exit(1)

    def abort(self, msg):
        self.log_error(msg)
        self.maybe_rollback()
        self.release_lock()
        sys.exit(1)

    def maybe_rollback(self):
        if self.rolling_back:
            return
        if not self.lock_acquired:
            return
        if not self.journal:
            self.log_debug("No journaled steps to roll back.")
            return
        if not self.auto_rollback:
            self.log_warn(
                f"AUTO_ROLLBACK is disabled; leaving the system in its current "
                f"partially-modified state. Run '{SCRIPT_NAME} rollback' to roll "
                f"back manually using {self.state_file}."
            )
            return
        self.rolling_back = True
        self.log_warn(f"Attempting automatic rollback of {len(self.journal)} recorded step(s)...")
        try:
            self.rollback_from_journal()
        finally:
            self.rolling_back = False

    def install_signal_handlers(self):
        def handler(signum, _frame):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            self.log_error(f"Caught signal {signal.Signals(signum).name}! Terminating.")
            self.maybe_rollback()
            self.release_lock()
            code = {signal.SIGINT: 130, signal.SIGHUP: 129, signal.SIGTERM: 143}.get(signum, 1)
            os._exit(code)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGHUP, handler)

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------
    def acquire_lock(self):
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            self.lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            self.die_early(f"Could not open lock file '{self.lock_file}': {e}")
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.die_early(
                f"Another instance of {SCRIPT_NAME} appears to be running "
                f"(lock '{self.lock_file}' is held). Refusing to run concurrently."
            )
        self.lock_acquired = True
        self.log_debug(f"Acquired lock: {self.lock_file}")

    def release_lock(self):
        if self.lock_acquired and self.lock_fd is not None:
            self.allow_driver_autoload()  # Ensure blocklist NEVER leaks on early abort/failure
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_acquired = False

    # ------------------------------------------------------------------
    # Subprocess / sysfs I/O helpers
    # ------------------------------------------------------------------
    def _run(self, cmd, timeout=15, input_text=None):
        """Run an external command, appending its stderr to the log. Returns (ok, stdout)."""
        logf = None
        try:
            if self.log_file:
                logf = open(self.log_file, "a")
        except OSError:
            logf = None
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=(logf if logf else subprocess.DEVNULL),
                input=input_text,
                timeout=timeout,
                text=True,
            )
            return proc.returncode == 0, (proc.stdout or "")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False, ""
        finally:
            if logf:
                logf.close()

    def _sysfs_write(self, path, value):
        try:
            with open(path, "w") as f:
                f.write(value if value.endswith("\n") else value + "\n")
            return True
        except OSError as e:
            self.log_debug(f"Write to {path} failed: {e}")
            return False

    def _poll_driver_equals(self, addr, want, tries=10, interval=0.1):
        cur = self.pci_driver_of(addr)
        n = 0
        while cur != want and n < tries:
            time.sleep(interval)
            cur = self.pci_driver_of(addr)
            n += 1
        return cur

    def _poll_driver_nonempty(self, addr, tries=10, interval=0.1):
        cur = self.pci_driver_of(addr)
        n = 0
        while not cur and n < tries:
            time.sleep(interval)
            cur = self.pci_driver_of(addr)
            n += 1
        return cur

    def _process_alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _process_comm(self, pid):
        try:
            return Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return "unknown"

    def _process_cmdline(self, pid):
        try:
            data = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return ""
        return data.replace(b"\x00", b" ").decode(errors="replace").strip()

    def _process_start_time(self, pid):
        try:
            data = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        # comm can contain spaces/parens, so split after the last ')'.
        rest = data.rsplit(")", 1)[-1].split()
        try:
            return rest[19]  # starttime field, per man proc(5)
        except IndexError:
            return None

    # ------------------------------------------------------------------
    # Kernel module dependency graph
    # ------------------------------------------------------------------
    def module_holders(self, mod):
        d = self.sysfs_module / normalize_module_name(mod) / "holders"
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir())

    def get_all_holders(self, mod):
        """Recursively resolves all modules that depend on the given base module."""
        visited = set()
        result = []

        def recurse(m):
            if m in visited:
                return
            visited.add(m)
            result.append(m)
            for h in self.module_holders(m):
                recurse(h)

        recurse(normalize_module_name(mod))
        return result

    def is_module_loaded(self, mod):
        return (self.sysfs_module / normalize_module_name(mod)).is_dir()

    def is_module_builtin(self, mod):
        d = self.sysfs_module / normalize_module_name(mod)
        return d.is_dir() and not (d / "initstate").exists()

    def unload_module_tree(self, mod):
        mod = normalize_module_name(mod)
        if mod in self.visited_modules:
            return
        self.visited_modules.add(mod)

        for holder in self.module_holders(mod):
            self.unload_module_tree(holder)

        if self.is_module_builtin(mod):
            self.log_info(f"Module '{mod}' is built into the kernel; skipping unload.")
            return
        if not self.is_module_loaded(mod):
            return

        self.log_info(f"Removing kernel module: {mod}")
        removed, _ = self._run(["modprobe", "-r", mod], timeout=15)
        if not removed:
            removed, _ = self._run(["rmmod", mod], timeout=15)
            if removed:
                self.log_debug(f"Removed '{mod}' via rmmod fallback.")

        if removed:
            self.removed_modules.append(mod)
        else:
            self.log_warn(
                f"Could not remove module '{mod}' (still in use?). Continuing without "
                f"full unload for this module tree; the per-device sysfs unbind should "
                f"still let vfio-pci claim the device."
            )

    def other_devices_using_driver(self, driver):
        bus_root = Path("/sys/bus")
        if not bus_root.is_dir():
            return False
        for bus in bus_root.iterdir():
            driver_dir = bus / "drivers" / driver
            if not driver_dir.is_dir():
                continue
            for dev_link in driver_dir.iterdir():
                if not dev_link.is_symlink():
                    continue
                addr = dev_link.name
                if addr == "module":
                    continue
                if addr not in self.target_devices:
                    self.log_debug(
                        f"Driver '{driver}' is also used by '{addr}' on bus "
                        f"{bus.name} (outside the managed device set)."
                    )
                    return True
        return False

    def reload_removed_modules(self):
        if not self.removed_modules:
            self.log_debug("No previously-removed modules recorded; relying on modalias-based auto-detection instead.")
            return
        for mod in reversed(self.removed_modules):
            self.log_info(f"Reloading kernel module: {mod}")
            ok, _ = self._run(["modprobe", mod], timeout=15)
            if not ok:
                self.log_warn(
                    f"Failed to reload module '{mod}' by name (it may have been "
                    f"renamed/removed by a package update); relying on modalias-based "
                    f"detection instead."
                )
        self.journal_push("reload_modules")

    # ------------------------------------------------------------------
    # Modprobe autoload blocking
    # ------------------------------------------------------------------
    def prevent_driver_autoload(self, drivers):
        if not drivers:
            return
        try:
            Path("/run/modprobe.d").mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        to_block = set()
        for d in drivers:
            to_block.update(self.get_all_holders(d))
        lines = "".join(f"install {d} /bin/false\n" for d in sorted(to_block))
        try:
            self.modprobe_block_file.write_text(lines)
        except OSError as e:
            self.log_warn(f"Could not write modprobe blocklist: {e}")
        self.log_debug(f"Created modprobe blocklist to prevent rogue reloads: {' '.join(sorted(to_block))}")

    def allow_driver_autoload(self):
        if self.modprobe_block_file.exists():
            try:
                self.modprobe_block_file.unlink()
            except OSError:
                pass
            self.log_debug("Removed modprobe blocklist.")

    # ------------------------------------------------------------------
    # Config loading & validation
    # ------------------------------------------------------------------
    def resolve_config_path(self):
        if self.config_path:
            if not Path(self.config_path).is_file():
                self.die_early(f"Config file not found: {self.config_path}")
            return
        for candidate in (Path("/etc/vfio-toggle/vfio-toggle.conf"), SCRIPT_DIR / "vfio-toggle.conf"):
            if candidate.is_file():
                self.config_path = str(candidate)
                return
        self.die_early(
            f"No config file found. Looked in /etc/vfio-toggle/vfio-toggle.conf "
            f"and {SCRIPT_DIR}/vfio-toggle.conf."
        )

    def _assert_safe_perms(self, path):
        try:
            st = os.stat(path)
        except OSError as e:
            self.die_early(f"Cannot stat '{path}': {e}")
            return
        if st.st_uid != 0:
            self.die_early(
                f"Refusing to load '{path}': not owned by root (uid={st.st_uid}). "
                f"It is loaded while running as root. Fix with: chown root:root '{path}'"
            )
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            perms = oct(stat.S_IMODE(st.st_mode))[2:]
            self.die_early(f"Refusing to load '{path}': group/other-writable (mode {perms}). Fix with: chmod 600 '{path}'")

    def require_config(self):
        self.resolve_config_path()
        self._assert_safe_perms(Path(self.config_path))
        old_log_file = self.log_file

        parser = configparser.ConfigParser()
        try:
            parser.read(self.config_path)
        except configparser.Error as e:
            self.die_early(f"Could not parse config file '{self.config_path}': {e}")
        if "vfio-toggle" not in parser:
            self.die_early(f"Config file '{self.config_path}' is missing a [vfio-toggle] section.")
        sec = parser["vfio-toggle"]

        if "gpu_pci_ids" in sec:
            ids = re.split(r"[,\s]+", sec.get("gpu_pci_ids").strip())
            self.gpu_pci_ids = [i for i in ids if i]

        def get_bool(key, default):
            try:
                return sec.getboolean(key, fallback=default)
            except ValueError:
                self.die_early(f"Config error: {key} must be a boolean (true/false).")

        def get_int(key, default):
            try:
                return sec.getint(key, fallback=default)
            except ValueError:
                self.die_early(f"Config error: {key} must be an integer.")

        self.restart_display_manager_after_bind = get_bool(
            "restart_display_manager_after_bind", self.restart_display_manager_after_bind
        )
        self.allow_full_module_unload = get_bool("allow_full_module_unload", self.allow_full_module_unload)
        self.release_boot_framebuffer = get_bool("release_boot_framebuffer", self.release_boot_framebuffer)
        self.release_vt_console = get_bool("release_vt_console", self.release_vt_console)
        self.allow_function_level_reset = get_bool("allow_function_level_reset", self.allow_function_level_reset)
        self.auto_rollback = get_bool("auto_rollback", self.auto_rollback)
        self.process_kill_grace_period = get_int("process_kill_grace_period", self.process_kill_grace_period)
        self.log_max_bytes = get_int("log_max_bytes", self.log_max_bytes)
        self.log_level = sec.get("log_level", self.log_level).strip().upper()
        self.log_file = Path(sec.get("log_file", str(self.log_file))).expanduser()
        self.state_file = Path(sec.get("state_file", str(self.state_file))).expanduser()

        self.validate_config()

        if self.verbose:
            self.log_level = "DEBUG"
        if str(self.log_file) != str(old_log_file):
            self.log_info(f"Config sets a different log_file ('{self.log_file}'); switching log output to it.")
            self.setup_logging()
        self.log_info(f"Loaded config: {self.config_path}")

    def validate_config(self):
        if not self.gpu_pci_ids:
            self.die_early("Config error: gpu_pci_ids is empty. Add at least one PCI address (see 'list-devices').")
        for id_ in self.gpu_pci_ids:
            if not PCI_ADDR_RE.match(id_):
                self.die_early(
                    f"Config error: '{id_}' in gpu_pci_ids is not a valid PCI address "
                    f"(expected dddd:bb:dd.f, e.g. 0000:01:00.0)."
                )
        if self.process_kill_grace_period < 0:
            self.die_early("Config error: process_kill_grace_period must be a non-negative integer.")
        if self.log_max_bytes < 10240:
            self.die_early("Config error: log_max_bytes must be an integer of at least 10240 (10 KiB).")
        if self.log_level not in LOG_LEVELS:
            self.die_early("Config error: log_level must be one of DEBUG, INFO, WARN, ERROR.")
        if not str(self.log_file):
            self.die_early("Config error: log_file must not be empty.")
        if not str(self.state_file):
            self.die_early("Config error: state_file must not be empty.")

    def check_dependencies(self):
        required = ["lspci", "systemctl", "modprobe", "rmmod", "fuser", "lsof"]
        missing = [c for c in required if shutil.which(c) is None]
        if missing:
            self.die_early(f"Missing required command(s): {', '.join(missing)}. Install the packages that provide them and re-run.")

    def require_root(self):
        if os.geteuid() != 0:
            print(f"Error: '{self.command}' must be run as root.", file=sys.stderr)
            sys.exit(1)

    def require_iommu(self):
        if not self.sysfs_iommu_groups.is_dir() or not any(self.sysfs_iommu_groups.iterdir()):
            self.abort(
                f"No IOMMU groups found ({self.sysfs_iommu_groups} is missing or empty). "
                f"VFIO passthrough requires IOMMU enabled."
            )

    # ------------------------------------------------------------------
    # PCI / sysfs helpers
    # ------------------------------------------------------------------
    def pci_driver_of(self, addr):
        link = self.sysfs_pci / "devices" / addr / "driver"
        if link.exists():
            return os.path.basename(os.path.realpath(link))
        return ""

    def pci_vendor_device_id(self, addr):
        def read_hex(name):
            try:
                return (self.sysfs_pci / "devices" / addr / name).read_text().strip()
            except OSError:
                return "0x????"

        v = read_hex("vendor")
        d = read_hex("device")
        return f"{v.removeprefix('0x')}:{d.removeprefix('0x')}"

    def pci_description(self, addr):
        ok, out = self._run(["lspci", "-s", addr], timeout=10)
        if ok and out:
            return out.splitlines()[0]
        return ""

    def expand_to_iommu_groups(self, addrs):
        result = set()
        for addr in addrs:
            dev_dir = self.sysfs_pci / "devices" / addr
            if not dev_dir.exists():
                self.abort(f"Configured PCI address '{addr}' does not exist.")
            group_link = dev_dir / "iommu_group"
            if not group_link.exists():
                self.abort(f"PCI device '{addr}' has no IOMMU group.")
            group_dir = Path(os.path.realpath(group_link))
            devices_dir = group_dir / "devices"
            if devices_dir.is_dir():
                for dev in devices_dir.iterdir():
                    result.add(dev.name)
        return sorted(result)

    def build_target_devices(self):
        self.target_devices = self.expand_to_iommu_groups(self.gpu_pci_ids)
        if not self.target_devices:
            self.abort("No target devices resolved from gpu_pci_ids; check your config.")
        self.log_debug(f"Target devices (after IOMMU group expansion): {' '.join(self.target_devices)}")

    def ensure_driver_loaded_for_device(self, addr):
        modalias_file = self.sysfs_pci / "devices" / addr / "modalias"
        if not modalias_file.exists():
            return
        try:
            hw_alias = modalias_file.read_text().strip()
        except OSError:
            return
        if not hw_alias:
            return
        ok, _ = self._run(["modprobe", hw_alias], timeout=15)
        if not ok:
            self.log_debug(f"modprobe by modalias found nothing new for {addr} (driver may already be loaded, or none installed).")

    def ensure_vfio_pci_loaded(self):
        if self.is_module_loaded("vfio-pci"):
            self.log_debug("vfio-pci module already loaded.")
            return
        self.log_info("Loading vfio-pci module.")
        ok, _ = self._run(["modprobe", "vfio-pci"], timeout=15)
        if not ok:
            self.abort(
                f"Failed to load the vfio-pci kernel module. Is it available for your "
                f"running kernel ({os.uname().release})? Check: modinfo vfio-pci"
            )

    # ------------------------------------------------------------------
    # Boot framebuffer (efifb/vesafb/simplefb)
    # ------------------------------------------------------------------
    def release_boot_fb(self):
        if not self.release_boot_framebuffer:
            return
        unbound = set()
        candidates = list(self.sysfs_class.glob("graphics/fb*")) + list(self.sysfs_class.glob("drm/card*"))
        for class_dir in candidates:
            dev_link = class_dir / "device"
            if not dev_link.exists():
                continue
            devname = os.path.basename(os.path.realpath(dev_link))
            drv_link = dev_link / "driver"
            if not drv_link.exists():
                continue
            if devname in unbound:
                continue
            drvname = os.path.basename(os.path.realpath(drv_link))
            subsystem_link = dev_link / "subsystem"
            subsystem = os.path.realpath(subsystem_link) if subsystem_link.exists() else ""
            # Only detach framebuffers on the "platform" bus, so we never knock
            # a real PCIe GPU offline by mistake.
            if subsystem.endswith("/bus/platform"):
                unbound.add(devname)
                self.log_info(
                    f"Releasing boot framebuffer platform device '{devname}' (driver "
                    f"'{drvname}') so the real GPU driver can unbind cleanly."
                )
                if self._sysfs_write(drv_link / "unbind", devname):
                    self.journal_push(f"unbind_platform|{devname}|{drvname}")
                else:
                    self.log_warn(f"Could not unbind boot framebuffer '{devname}'; this is often harmless, continuing.")

    # ------------------------------------------------------------------
    # VT console (fbcon) - /sys/class/vtconsole
    # ------------------------------------------------------------------
    def release_vt_consoles(self):
        if not self.release_vt_console:
            return
        if not self.sysfs_vtconsole.is_dir():
            return
        for path in sorted(self.sysfs_vtconsole.glob("vtcon*")):
            bind_file = path / "bind"
            if not bind_file.exists():
                continue
            vc = path.name
            try:
                bound = bind_file.read_text().strip()
            except OSError:
                bound = ""
            if bound == "0":
                self.log_debug(f"VT console {vc} is already unbound.")
                continue
            self.log_info(f"Unbinding VT console {vc} to ensure the GPU is released.")
            if self._sysfs_write(bind_file, "0"):
                self.released_vtconsoles.append(vc)
                self.journal_push(f"unbind_vtconsole|{vc}")
                self.persist_state()
            else:
                self.log_warn(f"Could not unbind VT console {vc}; this is often harmless, continuing.")

    def restore_vt_consoles(self):
        if not self.release_vt_console:
            return
        if not self.released_vtconsoles:
            self.log_debug("No VT consoles recorded as released; nothing to restore.")
            return
        for vc in self.released_vtconsoles:
            bind_file = self.sysfs_vtconsole / vc / "bind"
            if not bind_file.exists():
                continue
            self.log_info(f"Rebinding VT console {vc}.")
            if self._sysfs_write(bind_file, "1"):
                self.journal_push(f"rebind_vtconsole|{vc}")
            else:
                self.log_warn(f"Could not rebind VT console {vc}; you may need to do it manually: printf 1 > {bind_file}")

    # ------------------------------------------------------------------
    # Function-Level Reset - /sys/bus/pci/devices/<addr>/reset
    # ------------------------------------------------------------------
    def attempt_function_level_reset(self, addr):
        if not self.allow_function_level_reset:
            return
        cur = self.pci_driver_of(addr)
        if cur:
            self.log_debug(f"Skipping reset for {addr}: still bound to '{cur}' (must be unbound first).")
            return
        reset_file = self.sysfs_pci / "devices" / addr / "reset"
        if not reset_file.exists():
            self.log_debug(f"No 'reset' attribute for {addr} (device/slot doesn't expose a safe reset method); skipping.")
            return
        self.log_info(f"Performing a function-level reset on {addr}.")
        if self._sysfs_write(reset_file, "1"):
            self.log_debug(f"Reset succeeded for {addr}.")
        else:
            self.log_warn(f"Reset failed or unsupported for {addr}; continuing without it (this is usually harmless).")

    # ------------------------------------------------------------------
    # GPU device nodes & process management
    # ------------------------------------------------------------------
    def device_nodes_for(self, addr):
        nodes = []
        drm_dir = self.sysfs_pci / "devices" / addr / "drm"
        if drm_dir.is_dir():
            for entry in drm_dir.iterdir():
                p = self.dev_dir / "dri" / entry.name
                if p.exists():
                    nodes.append(str(p))
        drv = self.pci_driver_of(addr)
        if drv == "nvidia":
            nodes.extend(str(p) for p in self.dev_dir.glob("nvidia*"))
        if drv in ("amdgpu", "amdkfd"):
            kfd = self.dev_dir / "kfd"
            if kfd.exists():
                nodes.append(str(kfd))
        return nodes

    def find_pids_using_devices(self, nodes):
        if not nodes:
            return []
        pids = set()
        # lsof -t finds mmap-only mappings that omit open FDs.
        _, out1 = self._run(["lsof", "-t", *nodes], timeout=10)
        for tok in out1.split():
            if tok.isdigit():
                pids.add(int(tok))
        # fuser is the fallback / cross-check.
        _, out2 = self._run(["fuser", *nodes], timeout=10)
        for tok in re.findall(r"\d+", out2):
            pids.add(int(tok))
        return sorted(pids)

    def terminate_pids_safely(self, pids):
        if not pids:
            return

        services_to_stop = set()
        for pid in pids:
            if pid in (os.getpid(), 1):
                self.log_warn(f"Refusing to signal PID {pid} (this script or PID 1).")
                continue
            try:
                cgroup_text = Path(f"/proc/{pid}/cgroup").read_text()
            except OSError:
                continue
            matches = re.findall(r"[^/]+\.service", cgroup_text)
            if not matches:
                continue
            svc = matches[-1]  # deepest (most specific) .service unit
            if svc in (self.dm_unit or "", "display-manager.service") or re.match(r"^user@.*\.service$", svc):
                continue
            _, load_state = self._run(["systemctl", "show", "-p", "LoadState", "--value", svc], timeout=10)
            _, slice_ = self._run(["systemctl", "show", "-p", "Slice", "--value", svc], timeout=10)
            if load_state.strip() == "loaded" and slice_.strip() != "user.slice":
                services_to_stop.add(svc)

        for svc in services_to_stop:
            self.log_info(f"Stopping dynamically detected system service using the GPU: {svc}")
            ok, _ = self._run(["systemctl", "stop", svc], timeout=20)
            if not ok:
                self.log_warn(f"Failed to stop service {svc}")
            self.stopped_services.append(svc)
            self.journal_push(f"stop_service|{svc}")

        if services_to_stop:
            time.sleep(0.2)  # let cgroup PIDs fully drop after synchronous systemctl stop

        remaining = [pid for pid in pids if pid not in (os.getpid(), 1) and self._process_alive(pid)]
        if not remaining:
            return

        starts = {pid: self._process_start_time(pid) for pid in remaining}
        to_wait = []
        for pid in remaining:
            comm = self._process_comm(pid)
            cmdline = self._process_cmdline(pid) or comm
            self.log_info(f"Sending SIGTERM to PID {pid} ({comm}): {cmdline}")
            self.journal_push(f"killed_pid|{pid}|{comm}")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            to_wait.append(pid)

        def still_same_process(pid):
            return starts.get(pid) is not None and self._process_start_time(pid) == starts.get(pid)

        deadline = time.monotonic() + self.process_kill_grace_period
        still = [pid for pid in to_wait if still_same_process(pid)]
        while still and time.monotonic() < deadline:
            time.sleep(0.1)
            still = [pid for pid in still if still_same_process(pid)]

        if still:
            for pid in still:
                self.log_warn(f"PID {pid} still alive after {self.process_kill_grace_period}s grace period; sending SIGKILL.")
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            deadline2 = time.monotonic() + 2.0
            while time.monotonic() < deadline2:
                if not any(self._process_alive(pid) for pid in still):
                    break
                time.sleep(0.1)

    def restart_stopped_services(self):
        for svc in reversed(self.stopped_services):
            self.log_info(f"Restarting vendor service: {svc}")
            ok, _ = self._run(["systemctl", "start", svc], timeout=20)
            if not ok:
                self.log_warn(f"Failed to restart service {svc}")
            self.journal_push(f"start_service|{svc}")

    # ------------------------------------------------------------------
    # systemd display-manager & session handling
    # ------------------------------------------------------------------
    def display_manager_unit(self):
        _, load_state = self._run(["systemctl", "show", "-p", "LoadState", "--value", "display-manager.service"], timeout=10)
        if load_state.strip() == "loaded":
            _, unit = self._run(["systemctl", "show", "-p", "Id", "--value", "display-manager.service"], timeout=10)
            unit = unit.strip()
            if unit:
                return unit
        link = Path("/etc/systemd/system/display-manager.service")
        if link.is_symlink():
            unit = os.path.basename(os.path.realpath(link))
            if unit:
                return unit
        return ""

    def is_unit_active(self, unit):
        if not unit:
            return False
        ok, _ = self._run(["systemctl", "is-active", "--quiet", unit], timeout=10)
        return ok

    def terminate_graphical_sessions(self):
        if shutil.which("loginctl") is None:
            return
        _, out = self._run(["loginctl", "list-sessions", "--no-legend"], timeout=10)
        active_sids = []
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            sid = parts[0]
            _, cls = self._run(["loginctl", "show-session", "-p", "Class", "--value", sid], timeout=10)
            if cls.strip() in ("user", "greeter"):
                self.log_info(f"Terminating active local logind session {sid} (class={cls.strip()})...")
                self._run(["loginctl", "terminate-session", sid], timeout=10)
                active_sids.append(sid)

        if active_sids:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                remaining = 0
                for sid in active_sids:
                    ok, _ = self._run(["loginctl", "show-session", sid], timeout=10)
                    if ok:
                        _, state = self._run(["loginctl", "show-session", "-p", "State", "--value", sid], timeout=10)
                        if state.strip():
                            remaining += 1
                if remaining == 0:
                    break
                time.sleep(0.1)

    def stop_display_manager(self):
        unit = self.display_manager_unit()
        self.dm_unit = unit
        if not unit:
            self.log_info("No display-manager.service alias configured on this system; skipping display manager handling.")
            self.dm_was_active = False
            self.persist_state()
        elif self.is_unit_active(unit):
            self.dm_was_active = True
            self.persist_state()
            self.log_info(f"Stopping display manager: {unit}")
            ok, _ = self._run(["systemctl", "stop", unit], timeout=30)
            if not ok:
                if self.rolling_back:
                    self.log_warn(f"Failed to stop display manager unit '{unit}' during rollback.")
                else:
                    self.abort(f"Failed to stop display manager unit '{unit}'.")
            self.journal_push("stop_dm")
            deadline = time.monotonic() + 5.0
            while self.is_unit_active(unit) and time.monotonic() < deadline:
                time.sleep(0.1)
        else:
            self.log_info(f"Display manager '{unit}' is already inactive; nothing to stop.")
            self.dm_was_active = False
            self.persist_state()
        self.terminate_graphical_sessions()

    def start_display_manager(self):
        unit = self.dm_unit or self.display_manager_unit()
        if not unit:
            self.log_info("No display-manager.service alias configured; nothing to start.")
            return
        self.log_info(f"Starting display manager: {unit}")
        ok, _ = self._run(["systemctl", "start", unit], timeout=30)
        if not ok:
            self.log_error(f"Failed to start display manager '{unit}'. You may need to start your desktop session manually (systemctl start {unit}).")

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------
    def persist_state(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        data = {
            "journal": self.journal,
            "orig_driver": self.orig_driver,
            "removed_modules": self.removed_modules,
            "released_vtconsoles": self.released_vtconsoles,
            "stopped_services": self.stopped_services,
            "dm_was_active": self.dm_was_active,
            "dm_unit": self.dm_unit,
            "state_operation": self.state_operation,
            "written": datetime.now().astimezone().isoformat(),
        }
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.state_file)
            os.chmod(self.state_file, 0o600)
        except OSError:
            self.log_warn(f"Could not write state file {self.state_file} (rollback safety net degraded for this run).")

    def load_state(self):
        if not self.state_file.exists():
            return False
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError) as e:
            self.log_warn(f"Could not parse state file {self.state_file}: {e}")
            return False
        self.journal = data.get("journal", [])
        self.orig_driver = data.get("orig_driver", {})
        self.removed_modules = data.get("removed_modules", [])
        self.released_vtconsoles = data.get("released_vtconsoles", [])
        self.stopped_services = data.get("stopped_services", [])
        self.dm_was_active = data.get("dm_was_active", False)
        self.dm_unit = data.get("dm_unit", "")
        self.state_operation = data.get("state_operation", "")
        return True

    def clear_state(self):
        for p in (self.state_file, self.state_file.with_name(self.state_file.name + ".tmp")):
            try:
                p.unlink()
            except OSError:
                pass
        self.journal = []
        self.orig_driver = {}
        self.removed_modules = []
        self.released_vtconsoles = []
        self.stopped_services = []
        self.dm_was_active = False
        self.dm_unit = ""
        self.state_operation = ""

    def record_original_drivers(self, devices):
        for addr in devices:
            drv = self.pci_driver_of(addr)
            self.orig_driver[addr] = drv
            self.log_debug(f"Recorded original driver for {addr}: '{drv or '<none>'}'")
        self.persist_state()

    # ------------------------------------------------------------------
    # Journal & rollback
    # ------------------------------------------------------------------
    def journal_push(self, entry):
        if self.rolling_back:
            self.log_debug(f"(rollback in progress; not journaling: {entry})")
            return
        self.journal.append(entry)
        self.persist_state()
        self.log_debug(f"Journal += {entry}")

    def rollback_from_journal(self):
        self.allow_driver_autoload()  # drop blocklist BEFORE rolling back, or module reload fails
        for entry in reversed(self.journal):
            self.log_info(f"Rollback: undoing '{entry}'")
            self.rollback_one(entry)
        self.log_warn(f"Rollback finished. Please verify GPU/display state manually. Full detail in: {self.log_file}")
        self.journal = []
        self.persist_state()

    def rollback_one(self, entry):
        etype, _, rest = entry.partition("|")

        if etype == "stop_dm":
            self.start_display_manager()
        elif etype == "killed_pid":
            self.log_warn(f"  (PID from '{entry}' was already terminated and cannot be un-killed.)")
        elif etype == "stop_service":
            if rest:
                ok, _ = self._run(["systemctl", "start", rest], timeout=20)
                if not ok:
                    self.log_warn(f"  Failed to restart service {rest}.")
                self.log_info(f"  Restarted service {rest}.")
        elif etype == "start_service":
            self._run(["systemctl", "stop", rest], timeout=20)
        elif etype == "unbind_driver":
            addr, _, drv = rest.partition("|")
            self.clear_driver_override(addr)
            self._sysfs_write(self.sysfs_pci / "drivers_probe", addr)
            self.log_info(f"  Re-probed {addr} (original driver was '{drv}').")
        elif etype == "remove_modules":
            self.reload_removed_modules()
        elif etype == "override_set":
            self.clear_driver_override(rest)
            self.log_info(f"  Cleared driver_override for {rest}.")
        elif etype == "bind_vfio":
            driver_link = self.sysfs_pci / "devices" / rest / "driver"
            if driver_link.exists():
                self._sysfs_write(driver_link / "unbind", rest)
            self.log_info(f"  Unbound {rest} from vfio-pci.")
        elif etype == "override_cleared":
            self.log_debug(f"  (override_cleared for {rest} needs no rollback action by itself.)")
        elif etype == "unbind_vfio":
            self._sysfs_write(self.sysfs_pci / "devices" / rest / "driver_override", "vfio-pci")
            self._sysfs_write(self.sysfs_pci / "drivers_probe", rest)
            self.log_info(f"  Re-bound {rest} back to vfio-pci.")
        elif etype == "reload_modules":
            self.log_debug("  (reload_modules rollback: leaving restored host modules loaded is safe; no action needed.)")
        elif etype == "rebind_driver":
            addr, _, drv = rest.partition("|")
            self.log_info(f"  Rolling back host driver rebind for {addr} (was '{drv}'). Re-binding to vfio-pci.")
            driver_link = self.sysfs_pci / "devices" / addr / "driver"
            if driver_link.exists():
                self._sysfs_write(driver_link / "unbind", addr)
            self._sysfs_write(self.sysfs_pci / "devices" / addr / "driver_override", "vfio-pci")
            self._sysfs_write(self.sysfs_pci / "drivers_probe", addr)
        elif etype == "unbind_platform":
            fb, _, dn = rest.partition("|")
            if not self._sysfs_write(self.sysfs_platform / "drivers" / dn / "bind", fb):
                self.log_warn(f"  Could not rebind platform device {fb}.")
        elif etype == "unbind_vtconsole":
            p = self.sysfs_vtconsole / rest / "bind"
            if p.exists() and not self._sysfs_write(p, "1"):
                self.log_warn(f"  Could not rebind VT console {rest}.")
            self.log_info(f"  Rebound VT console {rest}.")
        elif etype == "rebind_vtconsole":
            p = self.sysfs_vtconsole / rest / "bind"
            if p.exists() and not self._sysfs_write(p, "0"):
                self.log_warn(f"  Could not unbind VT console {rest}.")
            self.log_info(f"  Re-unbound VT console {rest}.")
        elif etype in ("start_dm_after_bind", "start_dm_after_unbind"):
            self.stop_display_manager()
        else:
            self.log_warn(f"  Unknown journal entry type '{etype}' (from '{entry}'); skipping.")

    # ------------------------------------------------------------------
    # Core bind/unbind primitives
    # ------------------------------------------------------------------
    def set_driver_override(self, addr, driver):
        f = self.sysfs_pci / "devices" / addr / "driver_override"
        if not f.exists():
            self.abort(f"driver_override attribute not found for {addr}.")
        self.log_info(f"Setting driver_override={driver} for {addr}.")
        if not self._sysfs_write(f, driver):
            self.abort(f"Failed to set driver_override={driver} for {addr}.")
        self.journal_push(f"override_set|{addr}")

    def clear_driver_override(self, addr):
        f = self.sysfs_pci / "devices" / addr / "driver_override"
        if not f.exists():
            return
        if not self._sysfs_write(f, ""):
            self.log_warn(f"Could not clear driver_override for {addr}.")

    def unbind_device_from_driver(self, addr):
        drv = self.pci_driver_of(addr)
        if not drv:
            self.log_debug(f"{addr} has no driver currently bound; nothing to unbind.")
            return
        self.log_info(f"Unbinding {addr} from driver '{drv}'.")
        if not self._sysfs_write(self.sysfs_pci / "devices" / addr / "driver" / "unbind", addr):
            self.abort(f"Failed to unbind {addr} from '{drv}'. It may still be in use; check the process list above and 'dmesg'.")
        self.journal_push(f"unbind_driver|{addr}|{drv}")

    def bind_device_to_vfio(self, addr):
        cur = self.pci_driver_of(addr)
        if cur == "vfio-pci":
            self.log_debug(f"{addr} is already bound to vfio-pci.")
            return

        override_file = self.sysfs_pci / "devices" / addr / "driver_override"
        current_override = ""
        if override_file.exists():
            try:
                current_override = override_file.read_text().strip()
            except OSError:
                pass
        if current_override != "vfio-pci":
            self.set_driver_override(addr, "vfio-pci")

        if cur:
            self.log_warn(f"{addr} is currently bound to '{cur}'; unbinding before binding to vfio-pci.")
            self.unbind_device_from_driver(addr)

        self._sysfs_write(self.sysfs_pci / "drivers_probe", addr)
        cur = self._poll_driver_equals(addr, "vfio-pci")

        if cur != "vfio-pci":
            self.log_warn(f"{addr} did not bind via drivers_probe (current: '{cur or 'none'}'); trying a direct bind.")
            self._sysfs_write(self.sysfs_pci / "drivers" / "vfio-pci" / "bind", addr)
            cur = self._poll_driver_equals(addr, "vfio-pci")

        if cur != "vfio-pci":
            self.abort(f"Device {addr} failed to bind to vfio-pci (current driver: '{cur or 'none'}'). Check: dmesg | tail -50")
        self.journal_push(f"bind_vfio|{addr}")
        self.log_info(f"{addr} is now bound to vfio-pci.")

    def unbind_device_from_vfio(self, addr):
        self.clear_driver_override(addr)
        self.journal_push(f"override_cleared|{addr}")
        driver_link = self.sysfs_pci / "devices" / addr / "driver"
        if driver_link.exists():
            if not self._sysfs_write(driver_link / "unbind", addr):
                self.abort(f"Failed to unbind {addr} from vfio-pci.")
            self.journal_push(f"unbind_vfio|{addr}")
        self.log_info(f"{addr} unbound from vfio-pci.")

    def rebind_original_driver(self, addr):
        expected = self.orig_driver.get(addr, "")
        self._sysfs_write(self.sysfs_pci / "drivers_probe", addr)
        cur = self._poll_driver_nonempty(addr)

        if not cur and expected and (self.sysfs_pci / "drivers" / expected).is_dir():
            self.log_warn(f"{addr} did not bind via drivers_probe (current: '{cur or 'none'}'); trying direct bind to '{expected}'.")
            self._sysfs_write(self.sysfs_pci / "drivers" / expected / "bind", addr)
            cur = self._poll_driver_nonempty(addr)

        if not cur:
            self.abort(
                f"Device {addr} has no driver bound after restore attempt (expected "
                f"'{expected or '<unknown>'}'). Its driver module may not be installed. "
                f"Check: modinfo {expected or '<driver>'}; dmesg | tail -50"
            )
        if expected and cur != expected:
            self.log_warn(f"{addr} bound to '{cur}', originally was '{expected}'. Not treating this as fatal (driver may have changed, e.g. after a package update).")
        self.journal_push(f"rebind_driver|{addr}|{cur}")
        self.log_info(f"{addr} is now bound to '{cur}'.")

    def print_summary(self):
        print()
        print("Summary:")
        for addr in self.target_devices:
            print(f"  {addr} : {self.orig_driver.get(addr) or '<none>'} -> {self.pci_driver_of(addr) or '<none>'}")
        print()
        print(f"State saved to: {self.state_file}")
        print(f"Full log:       {self.log_file}")

    # ------------------------------------------------------------------
    # Top-level commands
    # ------------------------------------------------------------------
    def do_bind(self):
        self.check_dependencies()
        self.require_iommu()
        self.build_target_devices()

        need_action = []
        for addr in self.target_devices:
            if self.pci_driver_of(addr) == "vfio-pci":
                self.log_debug(f"{addr} already bound to vfio-pci.")
            else:
                need_action.append(addr)

        if not need_action:
            self.log_info("All target devices are already bound to vfio-pci. Nothing to do.")
            return

        self.log_info(f"Devices to move to vfio-pci: {' '.join(need_action)}")
        self.record_original_drivers(need_action)

        seen_driver = {self.orig_driver.get(a) for a in need_action if self.orig_driver.get(a)}

        # Lock down modprobe dynamically so restarting greeters don't reload
        # components while we unload them.
        self.prevent_driver_autoload(sorted(seen_driver))
        self.ensure_vfio_pci_loaded()
        self.stop_display_manager()

        nodes = []
        for addr in need_action:
            nodes.extend(self.device_nodes_for(addr))

        if nodes:
            self.log_info("Waiting up to 10 seconds for DRM master release...")
            deadline = time.monotonic() + 10.0
            waited_any = False
            active_pids = self.find_pids_using_devices(nodes)
            while active_pids and time.monotonic() < deadline:
                waited_any = True
                time.sleep(0.2)
                active_pids = self.find_pids_using_devices(nodes)

            if not active_pids:
                if waited_any:
                    self.log_info("DRM master released by graceful exit.")
                else:
                    self.log_info("No processes are currently using the GPU device nodes.")
            else:
                self.log_debug(f"Processes still holding DRM nodes after wait: {' '.join(map(str, active_pids))}")

            tries = 0
            while tries < 3:
                pids = self.find_pids_using_devices(nodes)
                if not pids:
                    if tries > 0:
                        self.log_info("No more processes found using GPU device nodes.")
                    break
                self.log_info(f"Checking for processes using GPU device nodes (kill attempt {tries + 1}): {' '.join(map(str, pids))}")
                self.terminate_pids_safely(pids)
                tries += 1
                if tries < 3:
                    check_deadline = time.monotonic() + 1.0
                    while self.find_pids_using_devices(nodes) and time.monotonic() < check_deadline:
                        time.sleep(0.1)
        else:
            self.log_debug("No DRM/device nodes found for target devices.")

        self.release_vt_consoles()
        self.release_boot_fb()

        if shutil.which("loginctl"):
            self.log_info("Flushing logind device references...")
            self._run(["loginctl", "flush-devices"], timeout=10)

        # Force a standard unbind of the device first to detach its internals.
        # This drops driver references, destroys rogue/orphaned CUDA or
        # graphical contexts, and cleanly permits future module unloading.
        for addr in need_action:
            self.set_driver_override(addr, "vfio-pci")
            self.unbind_device_from_driver(addr)

        if shutil.which("udevadm"):
            self.log_info("Waiting for udev to process device teardown events...")
            ok, _ = self._run(["udevadm", "settle", "--timeout=15"], timeout=20)
            if not ok:
                self.log_warn("udevadm settle timed out")

        if self.allow_full_module_unload:
            seen_driver = set()
            for addr in need_action:
                d = self.orig_driver.get(addr, "")
                if not d or d in seen_driver:
                    continue
                seen_driver.add(d)
                if self.other_devices_using_driver(d):
                    self.log_info(f"Skipping full unload of module '{d}': still in use by another device outside the managed set.")
                else:
                    self.unload_module_tree(d)
            if self.removed_modules:
                self.journal_push("remove_modules")
                self.log_info(f"Removed kernel modules: {' '.join(self.removed_modules)}")
        else:
            self.log_info("allow_full_module_unload=false; leaving driver modules loaded (per-device unbind only).")

        for addr in need_action:
            self.attempt_function_level_reset(addr)
        for addr in need_action:
            self.bind_device_to_vfio(addr)

        for addr in self.target_devices:
            cur = self.pci_driver_of(addr)
            if cur != "vfio-pci":
                self.abort(f"Post-check failed: {addr} is bound to '{cur or 'none'}', expected vfio-pci.")

        if self.restart_display_manager_after_bind and self.dm_was_active:
            self.log_info(
                "Restarting the display manager while the GPU is on vfio-pci "
                "(restart_display_manager_after_bind=true). If the host has no secondary "
                "GPU to fall back to, this may not produce a usable display - see "
                "release_vt_console / README for single-GPU setups."
            )
            self.start_display_manager()
            self.journal_push("start_dm_after_bind")
        else:
            self.log_info(
                f"Not restarting the display manager after bind "
                f"(restart_display_manager_after_bind={self.restart_display_manager_after_bind}, "
                f"was_active={self.dm_was_active})."
            )

        self.state_operation = "bind_complete"
        self.journal = []
        self.persist_state()
        self.allow_driver_autoload()

        self.log_info("SUCCESS: all target devices are bound to vfio-pci.")
        self.print_summary()

    def do_unbind(self):
        self.allow_driver_autoload()
        self.check_dependencies()
        self.build_target_devices()

        have_state = self.load_state()
        if have_state:
            self.log_info(f"Loaded saved state from {self.state_file} (previous operation: {self.state_operation or 'unknown'}).")
        else:
            self.log_warn("No saved state file found; proceeding in best-effort mode (will rely on modalias-based driver detection).")

        # Clear stale journal from a previous partial run before beginning unbind.
        self.journal = []
        self.persist_state()

        need_action = []
        for addr in self.target_devices:
            if self.pci_driver_of(addr) == "vfio-pci":
                need_action.append(addr)
            else:
                self.log_debug(f"{addr} is not bound to vfio-pci (driver: '{self.pci_driver_of(addr) or 'none'}'); nothing to restore for it.")

        if not need_action:
            self.log_info("No target devices are currently bound to vfio-pci. Nothing to do.")
            if have_state:
                self.clear_state()
            return

        self.log_info(f"Devices to restore from vfio-pci: {' '.join(need_action)}")
        self.restore_devices = need_action

        for addr in self.restore_devices:
            self.unbind_device_from_vfio(addr)
        for addr in self.restore_devices:
            self.attempt_function_level_reset(addr)

        self.reload_removed_modules()
        for addr in self.restore_devices:
            self.ensure_driver_loaded_for_device(addr)
        for addr in self.restore_devices:
            self.rebind_original_driver(addr)

        for addr in self.target_devices:
            if not self.pci_driver_of(addr):
                self.abort(f"Post-check failed: {addr} has no driver bound after restore.")

        self.restore_vt_consoles()
        self.restart_stopped_services()

        self.start_display_manager()
        self.journal_push("start_dm_after_unbind")

        self.log_info("SUCCESS: all target devices restored to their host driver.")
        self.print_summary()
        self.clear_state()

    def do_status(self):
        self.build_target_devices()
        print("=== vfio-toggle status ===")
        print(f"Config file: {self.config_path}")
        print()
        iommu_ok = self.sysfs_iommu_groups.is_dir() and any(self.sysfs_iommu_groups.iterdir())
        print(f"IOMMU groups present on system: {'yes' if iommu_ok else 'no'}")
        if not iommu_ok:
            print("  WARNING: no IOMMU groups found. VFIO passthrough requires IOMMU")
            print("  (Intel VT-d / AMD-Vi) enabled in BIOS and on the kernel command line.")
        print()
        print("Managed devices (from IOMMU-group expansion of gpu_pci_ids):")
        for addr in self.target_devices:
            drv = self.pci_driver_of(addr)
            desc = self.pci_description(addr)
            print(f"  {addr}  driver={(drv or 'none'):<10} {desc}")
        print()
        dm_unit = self.display_manager_unit()
        if dm_unit:
            active = "active" if self.is_unit_active(dm_unit) else "inactive"
            print(f"Display manager: {dm_unit} ({active})")
        else:
            print("Display manager: none detected (systemd alias display-manager.service not configured)")
        print()
        if self.state_file.is_file():
            print(f"Saved state file found: {self.state_file}")
            self.load_state()
            print(f"  Last operation recorded: {self.state_operation or 'unknown'}")
            print(f"  Journal entries left over: {len(self.journal)}")
            if self.removed_modules:
                print(f"  Modules previously removed: {' '.join(self.removed_modules)}")
            if self.released_vtconsoles:
                print(f"  VT consoles previously released: {' '.join(self.released_vtconsoles)}")
            if self.stopped_services:
                print(f"  Vendor daemon services stopped: {' '.join(self.stopped_services)}")
        else:
            print("No saved state file (clean state).")

    def do_list_devices(self):
        self.log_info("Scanning for GPU-class PCI devices...")
        found = False
        devices_dir = self.sysfs_pci / "devices"
        if devices_dir.is_dir():
            for d in sorted(devices_dir.iterdir()):
                addr = d.name
                try:
                    cls = (d / "class").read_text().strip()
                except OSError:
                    continue
                if not cls.startswith("0x03"):
                    continue
                found = True
                vd = self.pci_vendor_device_id(addr)
                drv = self.pci_driver_of(addr)
                desc = self.pci_description(addr)
                group = ""
                group_link = d / "iommu_group"
                if group_link.exists():
                    group = os.path.basename(os.path.realpath(group_link))
                print(f"\n{addr}")
                print(f"  ID:          {vd}")
                if desc:
                    print(f"  Description: {desc}")
                print(f"  Driver:      {drv or '<none>'}")
                print(f"  IOMMU group: {group or '<none - IOMMU not enabled?>'}")
                if group:
                    print("  Group members:")
                    gdir = self.sysfs_iommu_groups / group / "devices"
                    if gdir.is_dir():
                        for gm in sorted(gdir.iterdir()):
                            gdesc = self.pci_description(gm.name)
                            print(f"    - {gm.name}  {gdesc}")
        if not found:
            self.log_warn("No VGA/3D/Display-class PCI devices found.")
        else:
            print()
            print("Put the primary address(es) (the VGA/3D-controller function, usually")
            print("ending in .0) into gpu_pci_ids in your config. Group members are")
            print("included automatically.")

    def do_manual_rollback(self):
        if not self.load_state():
            self.abort(f"No state file found at {self.state_file}; nothing to roll back.")
        if not self.journal:
            self.log_info("State file has an empty journal; nothing to roll back.")
            return
        self.log_warn(f"Manually replaying rollback for {len(self.journal)} recorded step(s) from {self.state_file}.")
        self.rollback_from_journal()


# ==========================================================================
# CLI
# ==========================================================================


def print_usage():
    print(
        f"""Usage: {SCRIPT_NAME} <command> [options]

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
  clear-log       Clear the log file.
  show-log        Display the contents of the log file.

Options:
  -c, --config PATH   Use PATH instead of the default config file.
  -v, --verbose       Verbose console output (DEBUG level).
  -h, --help          Show this help.

Config file search order (unless -c is given):
  /etc/vfio-toggle/vfio-toggle.conf
  {SCRIPT_DIR}/vfio-toggle.conf

Must be run as root, except 'status', 'list-devices', and 'show-log'."""
    )


def parse_args(argv):
    command = argv[0]
    rest = argv[1:]
    config_path = None
    verbose = False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-c", "--config"):
            if i + 1 >= len(rest):
                print("Error: --config requires a path.", file=sys.stderr)
                sys.exit(1)
            config_path = rest[i + 1]
            i += 2
        elif a in ("-v", "--verbose"):
            verbose = True
            i += 1
        elif a in ("-h", "--help"):
            print_usage()
            sys.exit(0)
        else:
            print(f"Unknown option: {a}", file=sys.stderr)
            print_usage()
            sys.exit(1)
    return command, config_path, verbose


def main():
    argv = sys.argv[1:]
    if not argv:
        print_usage()
        sys.exit(1)

    command, config_path, verbose = parse_args(argv)
    app = VfioToggle(command=command, config_path=config_path, verbose=verbose)
    app.install_signal_handlers()

    try:
        if command == "list-devices":
            app.setup_logging()
            app.check_dependencies()
            app.do_list_devices()
        elif command == "status":
            app.setup_logging()
            app.check_dependencies()
            app.require_config()
            app.do_status()
        elif command == "bind":
            app.require_root()
            app.setup_logging()
            app.check_dependencies()
            app.require_config()
            app.require_iommu()
            app.acquire_lock()
            app.do_bind()
            app.release_lock()
        elif command == "unbind":
            app.require_root()
            app.setup_logging()
            app.check_dependencies()
            app.require_config()
            app.acquire_lock()
            app.do_unbind()
            app.release_lock()
        elif command == "rollback":
            app.require_root()
            app.setup_logging()
            app.check_dependencies()
            app.require_config()
            app.acquire_lock()
            app.do_manual_rollback()
            app.release_lock()
        elif command == "clear-log":
            app.require_root()
            app.require_config()
            if app.log_file:
                open(app.log_file, "w").close()
                print(f"Log file {app.log_file} cleared.")
        elif command == "show-log":
            app.require_config()
            if app.log_file and app.log_file.is_file():
                print(app.log_file.read_text(), end="")
            else:
                print(f"Log file {app.log_file or 'not configured'} does not exist.")
        elif command in ("help", "-h", "--help"):
            print_usage()
            sys.exit(0)
        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            print_usage()
            sys.exit(1)
    except SystemExit:
        raise
    except Exception:
        app.log_error("Unhandled error:\n" + traceback.format_exc())
        app.maybe_rollback()
        app.release_lock()
        sys.exit(1)


if __name__ == "__main__":
    main()
