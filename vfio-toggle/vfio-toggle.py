#!/usr/bin/env python3
"""
vfio-toggle - move a GPU (and the rest of its IOMMU group) between its host
driver and vfio-pci at runtime, for VFIO/KVM passthrough, and back again.

    vfio-toggle.py [-c CONFIG] [-v] [-n] <command>

    bind          host driver -> vfio-pci
    unbind        vfio-pci    -> the driver each device had before `bind`
    status        show devices, drivers, IOMMU groups and saved state
    list-devices  discover GPU-class devices to put in the config file
    rollback      undo an interrupted `bind` from the saved snapshot
    default-config  print a fully commented sample config file
    show-log | clear-log

Design (why this script should not need editing for a decade)
-------------------------------------------------------------
* Python standard library only. No lspci/lsof/fuser/systemctl/rmmod/udevadm.
  The single external program the script may run is the kernel's own module
  helper (the path in /proc/sys/kernel/modprobe), and only to load vfio-pci
  when it is not already present. Everything else that is distro- or
  desktop-specific (stopping a display manager, stopping vendor daemons) lives
  in user-owned hook scripts, not here. See "Hooks" below.
* Only documented kernel interfaces, and only in the way the kernel's
  "Rules on how to access information in sysfs" says to use them:
    - PCI ABI (Documentation/ABI/testing/sysfs-bus-pci): driver_override,
      drivers/<name>/bind, <dev>/driver/unbind, <dev>/reset, <dev>/class,
      <dev>/modalias
    - Driver binding semantics (Documentation/driver-api/driver-model/binding)
    - IOMMU groups (Documentation/ABI/testing/sysfs-kernel-iommu_groups)
    - /sys/dev/char + per-device `dev` files (ABI/testing/sysfs-dev)
    - synthetic uevents (ABI/testing/sysfs-uevent), to let udev auto-load a
      driver module
    - vtconsole (Documentation/driver-api/console)
    - proc(5): /proc/<pid>/{fd,maps,stat,root}, /proc/sys/kernel/random/boot_id
  Nothing depends on vendor names, module names, distro layout or the
  "device"/class back-links that the sysfs rules say never to rely on.
  Deliberately NOT used: new_id/remove_id, modprobe.d blacklists, module
  unloading, /sys/bus/pci/drivers_probe (undocumented for PCI), remove/rescan.
* One mechanism for undo. `bind` first snapshots the pre-bind state of every
  device it will touch (driver + driver_override). `unbind`, automatic
  rollback after a failure and manual `rollback` all use the same idempotent
  "restore snapshot" code, so a crash at any point can be repaired by re-running
  it. There is no action journal to replay in the right order.
* Fail early: config, IOMMU, driver_override support, vfio-pci availability
  and bridge handling are checked before the first hook runs; the "is the GPU
  still in use?" check runs after the pre-bind hooks and before the first
  device is touched.

Hooks (optional, user-owned)
----------------------------
Executable hook scripts can be supplied either as a single unified script
(handling events via $VFIO_TOGGLE_EVENT), directly inside <hook_dir>, or
partitioned into <hook_dir>/<event>/ subdirectories:

    pre-bind   post-bind   pre-unbind   post-unbind   rollback

`pre-*` hooks abort the operation if they fail; `post-*` and `rollback` hooks
only log a warning. `rollback` hooks run after a failed bind and after the
`rollback` command, and must be idempotent (e.g. "start the display manager if
it is stopped"). Environment: VFIO_TOGGLE_EVENT, VFIO_TOGGLE_DEVICES. Hook
files and directories must be root-owned and not group/other-writable.

Requires Python 3.9+. Exit status: 0 ok, 1 failure, 2 usage error,
128+N when interrupted by signal N.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import dataclasses
import fcntl
import fnmatch
import glob
import json
import logging
import logging.handlers
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

__version__ = "2.0.0"
PROG = "vfio-toggle"
STATE_SCHEMA = 2
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATHS = (
    Path("/etc/vfio-toggle/vfio-toggle.conf"),
    SCRIPT_DIR / "vfio-toggle.conf",
)

log = logging.getLogger(PROG)

# Domain may be more than 4 hex digits (e.g. Intel VMD: "10000:02:05.0").
PCI_ADDR_RE = re.compile(r"^[0-9a-f]{4,8}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
PCI_ADDR_SHORT_RE = re.compile(r"^[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")

# PCI base class codes, from the PCI specification (stable since the 1990s).
CLASS_DISPLAY = 0x03
CLASS_BRIDGE = 0x06
BUSY_CLASSES = (CLASS_DISPLAY, 0x12)  # display controllers, processing accelerators
BASE_CLASS_NAMES = {
    0x00: "Unclassified", 0x01: "Mass storage", 0x02: "Network", 0x03: "Display",
    0x04: "Multimedia", 0x05: "Memory", 0x06: "Bridge", 0x07: "Communication",
    0x08: "System peripheral", 0x09: "Input device", 0x0C: "Serial bus",
    0x0D: "Wireless", 0x12: "Processing accelerator",
}
PCI_IDS_PATHS = (
    "/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids",
)
LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}


# ==========================================================================
# Errors
# ==========================================================================


class ToggleError(Exception):
    """An expected failure. The message is shown to the user as-is."""


class SysfsError(ToggleError):
    """A read/write of a sysfs attribute failed."""


class SysfsTimeout(SysfsError):
    """A sysfs write did not return in time (the kernel is still working)."""


class Interrupted(BaseException):
    """Raised from the signal handler so cleanup runs in normal control flow."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# ==========================================================================
# Configuration (table-driven: one place defines keys, types, defaults, docs)
# ==========================================================================


@dataclasses.dataclass(frozen=True)
class Option:
    name: str
    kind: str  # str | bool | int | list | choice
    default: object
    help: str
    choices: tuple = ()
    minimum: int = 0
    aliases: tuple = ()  # older spellings still accepted (with a warning)


OPTIONS = (
    Option("gpu_pci_ids", "list", (),
           "PCI address(es) of the GPU function(s) to manage, e.g. 0000:01:00.0. "
           "Run 'list-devices' to find them. Other members of the IOMMU group "
           "are handled according to group_expansion."),
    Option("vfio_driver", "str", "vfio-pci",
           "Name of the VFIO driver to bind to. Leave as vfio-pci unless your "
           "hardware needs a vendor-supplied variant driver (its name is used "
           "both for driver_override and for loading the module)."),
    Option("group_expansion", "choice", "auto",
           "auto: also move every non-bridge device sharing the IOMMU group "
           "(e.g. the GPU's HDMI audio). strict: refuse unless every such "
           "device is listed in gpu_pci_ids. Bridges are never touched.",
           choices=("auto", "strict")),
    Option("on_busy", "choice", "abort",
           "What to do if a process still uses the GPU when 'bind' runs. "
           "abort: stop and list them. terminate: SIGTERM then SIGKILL them. "
           "ignore: continue anyway (the unbind may hang).",
           choices=("abort", "terminate", "ignore")),
    Option("busy_wait_timeout", "int", 10,
           "Seconds to wait for users of the GPU to exit on their own before "
           "on_busy applies."),
    Option("busy_extra_globs", "list", ("/dev/nvidia*", "/dev/kfd"),
           "Extra device-node globs that count as 'using the GPU'. Needed for "
           "drivers whose nodes are not registered in sysfs. Set empty to "
           "disable."),
    Option("terminate_exclude_units", "list", ("systemd-logind.service", "user@*.service"),
           "systemd units (shell-style patterns) whose processes on_busy = "
           "terminate must never signal, because killing them would take the "
           "login/session infrastructure down with them."),
    Option("kill_grace_period", "int", 10,
           "Seconds between SIGTERM and SIGKILL when on_busy = terminate.",
           aliases=("process_kill_grace_period",)),
    Option("release_vt_console", "bool", True,
           "Unbind the framebuffer console (vtconsole) if a managed GPU "
           "provides it, and rebind it on unbind/rollback."),
    Option("release_platform_framebuffer", "bool", True,
           "Unbind firmware framebuffer platform devices (efifb, simpledrm, "
           "...) when a display-class GPU is managed; restored afterwards.",
           aliases=("release_boot_framebuffer",)),
    Option("platform_framebuffer_globs", "list", ("*framebuffer*",),
           "Platform device names treated as firmware framebuffers."),
    Option("allow_function_level_reset", "bool", True,
           "Reset each device (via its sysfs 'reset' file, if any) while it "
           "is unbound, before handing it to vfio-pci."),
    Option("load_vfio_module", "bool", True,
           "If vfio-pci is not loaded, load it with the kernel's module "
           "helper. Set false if you load it at boot (modules-load.d)."),
    Option("auto_rollback", "bool", True,
           "Restore the pre-bind state automatically if 'bind' fails."),
    Option("hook_dir", "str", "/etc/vfio-toggle/hooks.d",
           "Path to a single hook script, or directory containing hook scripts "
           "or <event>/ subdirectories.",
           aliases=("hook_script", "hook_path")),
    Option("hook_timeout", "int", 60, "Seconds a single hook may run.", minimum=1),
    Option("driver_timeout", "int", 60,
           "Seconds a driver may take to finish one bind or unbind request "
           "before the tool reports a hang.", minimum=1),
    Option("bind_settle_timeout", "int", 10,
           "Seconds to wait for a driver to attach after a bind request.",
           minimum=1),
    Option("log_file", "str", "/var/log/vfio-toggle.log",
           "Log file (always DEBUG level). Rotated at log_max_bytes."),
    Option("log_level", "choice", "INFO", "Console log level.",
           choices=("DEBUG", "INFO", "WARN", "ERROR")),
    Option("log_max_bytes", "int", 1048576,
           "Maximum size of the log file before it is rotated.", minimum=10240),
    Option("log_backups", "int", 1, "Number of rotated log files to keep."),
    Option("state_file", "str", "/var/lib/vfio-toggle/state.json",
           "Where the pre-bind snapshot is stored (JSON)."),
)
OPTION_BY_NAME = {o.name: o for o in OPTIONS}
OPTION_ALIASES = {a: o for o in OPTIONS for a in o.aliases}
DEPRECATED_OPTIONS = {
    "restart_display_manager_after_bind":
        "display manager handling moved to hooks (see hook_dir)",
    "allow_full_module_unload":
        "modules are no longer unloaded; driver_override makes that unnecessary",
}


def normalize_pci_addr(text: str) -> str:
    """Lower-case and add the default domain to lspci-style 'bb:dd.f' input."""
    addr = text.strip().lower()
    if PCI_ADDR_SHORT_RE.match(addr):
        addr = "0000:" + addr
    return addr


def _split_list(text: str) -> list:
    return [t for t in re.split(r"[,\s]+", text.strip()) if t]


class Config:
    def __init__(self, path: Optional[Path] = None):
        self.path = path
        for opt in OPTIONS:
            setattr(self, opt.name, opt.default)

    @classmethod
    def load(cls, path: Path, trusted_uid: int = 0) -> "Config":
        assert_trusted(path, trusted_uid, "config file")
        parser = configparser.ConfigParser(
            interpolation=None,  # '%' is legal in values
            inline_comment_prefixes=("#", ";"),
        )
        try:
            with open(path, encoding="utf-8") as fh:
                parser.read_file(fh)
        except (OSError, UnicodeDecodeError, configparser.Error) as exc:
            raise ToggleError(f"Cannot read config file '{path}': {exc}") from exc
        if not parser.has_section(PROG):
            raise ToggleError(f"Config file '{path}' has no [{PROG}] section.")
        cfg = cls(path)
        section = parser[PROG]
        for key in section:
            if key in DEPRECATED_OPTIONS:
                log.warning("Config key '%s' is ignored: %s.", key, DEPRECATED_OPTIONS[key])
                continue
            opt = OPTION_BY_NAME.get(key)
            if opt is None and key in OPTION_ALIASES:
                opt = OPTION_ALIASES[key]
                log.warning("Config key '%s' is deprecated; use '%s'.", key, opt.name)
            if opt is None:
                log.warning("Unknown config key '%s' in %s (typo?); ignoring.", key, path)
                continue
            setattr(cfg, opt.name, cfg._parse(opt, section, key))
        cfg.gpu_pci_ids = [normalize_pci_addr(a) for a in cfg.gpu_pci_ids]
        for addr in cfg.gpu_pci_ids:
            if not PCI_ADDR_RE.match(addr):
                raise ToggleError(
                    f"Config error: '{addr}' in gpu_pci_ids is not a PCI address "
                    f"(expected dddd:bb:dd.f, e.g. 0000:01:00.0)."
                )
        return cfg

    @staticmethod
    def _parse(opt: Option, section, key: str):
        try:
            if opt.kind == "bool":
                return section.getboolean(key)
            if opt.kind == "int":
                value = section.getint(key)
                if value < opt.minimum:
                    raise ValueError(f"must be >= {opt.minimum}")
                return value
            if opt.kind == "list":
                return _split_list(section.get(key))
            value = section.get(key).strip()
            if opt.kind == "choice":
                value = value.upper() if opt.name == "log_level" else value.lower()
                if opt.name == "log_level" and value == "WARNING":
                    value = "WARN"
                if value not in opt.choices:
                    raise ValueError(f"must be one of {', '.join(opt.choices)}")
            elif not value:
                raise ValueError("must not be empty")
            return value
        except ValueError as exc:
            raise ToggleError(f"Config error: {opt.name}: {exc}") from exc

    def require_devices(self) -> None:
        if not self.gpu_pci_ids:
            raise ToggleError(
                "Config error: gpu_pci_ids is empty. Add at least one PCI address "
                "(see 'list-devices')."
            )


def render_default_config() -> str:
    lines = [f"[{PROG}]", ""]
    for opt in OPTIONS:
        for chunk in re.findall(r".{1,74}(?:\s|$)", opt.help + " "):
            lines.append("# " + chunk.strip())
        if opt.kind == "choice":
            lines.append("# Choices: " + ", ".join(opt.choices))
        value = " ".join(map(str, opt.default)) if opt.kind == "list" else str(opt.default)
        if opt.name == "gpu_pci_ids":
            lines.append("gpu_pci_ids = 0000:01:00.0")
        else:
            lines.append(f"# {opt.name} = {value}".rstrip())
        lines.append("")
    return "\n".join(lines)


def assert_trusted(path: Path, trusted_uid: int, what: str) -> None:
    """Refuse to use root-executed/loaded files that others could have edited."""
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ToggleError(f"Cannot stat {what} '{path}': {exc}") from exc
    if st.st_uid != trusted_uid:
        raise ToggleError(
            f"Refusing to use {what} '{path}': not owned by uid {trusted_uid} "
            f"(owner uid={st.st_uid}). Fix: chown root:root '{path}'"
        )
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ToggleError(
            f"Refusing to use {what} '{path}': writable by group/others "
            f"(mode {stat.S_IMODE(st.st_mode):o}). Fix: chmod go-w '{path}'"
        )


# ==========================================================================
# Logging
# ==========================================================================


def configure_logging(console_level: str = "INFO", verbose: bool = False,
                      log_file: Optional[str] = None, max_bytes: int = 1048576,
                      backups: int = 1) -> None:
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    level = logging.DEBUG if verbose else LOG_LEVELS.get(console_level, logging.INFO)
    out = logging.StreamHandler(sys.stdout)
    out.setLevel(level)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(max(level, logging.WARNING))
    console_fmt = logging.Formatter("%(levelname)s: %(message)s")
    for handler in (out, err):
        handler.setFormatter(console_fmt)
        log.addHandler(handler)
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
        except OSError as exc:
            log.warning("Cannot open log file '%s' (%s); logging to console only.", log_file, exc)
            return
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "[%(asctime)s] [pid:%(process)d] [%(levelname)s] %(message)s"))
        log.addHandler(fh)


# ==========================================================================
# Small utilities
# ==========================================================================


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.1) -> bool:
    """Poll `predicate` until true or `timeout` seconds pass (checked at least once)."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


@contextlib.contextmanager
def signals_ignored() -> Iterator[None]:
    """Make cleanup uninterruptible."""
    signums = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    saved = {s: signal.signal(s, signal.SIG_IGN) for s in signums}
    try:
        yield
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


def install_signal_handlers() -> None:
    def handler(signum, _frame):
        raise Interrupted(signum)

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, handler)


@contextlib.contextmanager
def exclusive_lock(path: Path, enabled: bool = True) -> Iterator[None]:
    if not enabled:
        yield
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise ToggleError(f"Cannot open lock file '{path}': {exc}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ToggleError(
                f"Another {PROG} is running (lock '{path}' is held); refusing to run concurrently."
            ) from exc
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


# ==========================================================================
# sysfs access
# ==========================================================================


class Sysfs:
    """Thin layer over the sysfs tree; the only place that writes attributes."""

    def __init__(self, root: Path, dry_run: bool = False):
        self.root = Path(root)
        self.dry_run = dry_run

    def subsystem_dir(self, name: str) -> Optional[Path]:
        """Locate a subsystem, per the sysfs rules: if /sys/subsystem exists it
        is authoritative; otherwise look in both /sys/bus and /sys/class."""
        if (self.root / "subsystem").is_dir():
            candidates = (self.root / "subsystem" / name,)
        else:
            candidates = (self.root / "bus" / name, self.root / "class" / name)
        for cand in candidates:
            if cand.is_dir():
                return cand
        return None

    @staticmethod
    def read(path: Path, default: str = "") -> str:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return default

    @staticmethod
    def link_name(path: Path) -> str:
        """Last element of a symlink's real target ('' if absent), which is how
        the sysfs rules say to obtain a device's driver or subsystem."""
        path = Path(path)
        if not (path.is_symlink() or path.exists()):
            return ""
        return Path(os.path.realpath(path)).name

    def write(self, path: Path, value: str, timeout: Optional[float] = None) -> None:
        if self.dry_run:
            log.info("[dry-run] echo %r > %s", value, path)
            return
        log.debug("sysfs: echo %r > %s", value, path)
        data = (value + "\n").encode()
        if timeout is None:
            self._write_once(path, data)
            return
        # Some drivers block for a long time inside unbind. Do the write in a
        # helper thread so we can report a hang instead of hanging silently.
        outcome: dict = {}

        def worker():
            try:
                self._write_once(path, data)
            except BaseException as exc:  # noqa: BLE001 - relayed to caller
                outcome["error"] = exc

        thread = threading.Thread(target=worker, name="sysfs-write", daemon=True)
        thread.start()
        try:
            thread.join(timeout)
        except BaseException as exc:  # e.g. Interrupted while the kernel is working
            if thread.is_alive():
                raise SysfsTimeout(
                    f"Interrupted while '{value}' -> {path} was still in flight; not "
                    f"rolling back concurrently with the kernel. Check dmesg."
                ) from exc
            raise
        if thread.is_alive():
            raise SysfsTimeout(
                f"Writing '{value}' to {path} did not finish within {timeout:g}s; the "
                f"driver is still working or stuck. Check dmesg. Not rolling back "
                f"while the kernel operation is in flight."
            )
        if "error" in outcome:
            raise outcome["error"]

    @staticmethod
    def _write_once(path: Path, data: bytes) -> None:
        # One os.write() call: sysfs attributes must be written in a single call.
        try:
            fd = os.open(path, os.O_WRONLY)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError as exc:
            raise SysfsError(f"Write to {path} failed: {exc.strerror or exc}") from exc


class PciNames:
    """Best-effort human-readable names from a pci.ids file, if one is installed."""

    def __init__(self):
        self.path = next((p for p in PCI_IDS_PATHS if os.path.isfile(p)), None)
        self._cache: dict = {}

    def lookup(self, vendor: str, device: str) -> str:
        key = (vendor.lower(), device.lower())
        if key not in self._cache:
            self._cache[key] = self._scan(*key)
        return self._cache[key]

    def _scan(self, vendor: str, device: str) -> str:
        if not self.path:
            return ""
        vendor_name = device_name = ""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                in_vendor = False
                for line in fh:
                    if line.startswith("#") or not line.strip():
                        continue
                    if not line.startswith("\t"):
                        if in_vendor:
                            break  # left our vendor's block
                        if line.lower().startswith(vendor + "  "):
                            in_vendor, vendor_name = True, line[6:].strip()
                    elif in_vendor and not line.startswith("\t\t"):
                        if line.strip().lower().startswith(device + "  "):
                            device_name = line.strip()[6:].strip()
                            break
        except OSError:
            return ""
        return f"{vendor_name} {device_name}".strip()


# ==========================================================================
# Saved state
# ==========================================================================


@dataclasses.dataclass
class State:
    schema: int = STATE_SCHEMA
    boot_id: str = ""
    phase: str = ""  # "binding" (incomplete) or "bound"
    devices: dict = dataclasses.field(default_factory=dict)  # addr -> {driver, override}
    vtconsoles: list = dataclasses.field(default_factory=list)
    platform_devices: list = dataclasses.field(default_factory=list)  # [{device, driver}]
    written: str = ""


class StateStore:
    def __init__(self, path: Path, boot_id: str, dry_run: bool = False):
        self.path = Path(path)
        self.boot_id = boot_id
        self.dry_run = dry_run

    def new(self) -> State:
        return State(boot_id=self.boot_id)

    def load(self) -> Optional[State]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except PermissionError as exc:
            raise ToggleError(f"Cannot read state file '{self.path}': {exc}") from exc
        except (OSError, ValueError) as exc:
            aside = self.path.with_name(self.path.name + f".corrupt-{int(time.time())}")
            log.warning("State file %s is unreadable (%s); moved to %s.", self.path, exc, aside)
            if not self.dry_run:
                with contextlib.suppress(OSError):
                    self.path.replace(aside)
            return None
        state = self._from_dict(data)
        if self.boot_id and state.boot_id and state.boot_id != self.boot_id:
            # vfio-pci bindings do not survive a reboot, so the snapshot is moot.
            log.info("Discarding state saved before the last reboot.")
            self.clear()
            return None
        return state

    @staticmethod
    def _from_dict(data: dict) -> State:
        if data.get("schema", 1) < 2:  # original shell-derived format
            legacy = data.get("orig_driver") or {}
            complete = data.get("state_operation") == "bind_complete"
            log.info("Migrating legacy (v1) state file.")
            return State(
                phase="bound" if complete or not data.get("journal") else "binding",
                devices={a: {"driver": d or "", "override": None} for a, d in legacy.items()},
                vtconsoles=list(data.get("released_vtconsoles") or []),
            )
        fields = {f.name for f in dataclasses.fields(State)}
        return State(**{k: v for k, v in data.items() if k in fields})

    def save(self, state: State) -> None:
        if self.dry_run:
            return
        state.boot_id = self.boot_id
        state.written = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dataclasses.asdict(state), fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise ToggleError(
                f"Cannot write state file '{self.path}': {exc}. Refusing to continue "
                f"without a rollback snapshot."
            ) from exc

    def clear(self) -> None:
        if self.dry_run:
            return
        for p in (self.path, self.path.with_name(self.path.name + ".tmp")):
            with contextlib.suppress(OSError):
                p.unlink()


# ==========================================================================
# Devices that count as "in use"
# ==========================================================================


@dataclasses.dataclass
class NodeSet:
    rdevs: set = dataclasses.field(default_factory=set)  # {(major, minor)} of char devices
    inodes: set = dataclasses.field(default_factory=set)  # {(st_dev, st_ino)}

    def __bool__(self):
        return bool(self.rdevs or self.inodes)

    def matches(self, st: os.stat_result) -> bool:
        if stat.S_ISCHR(st.st_mode) and (os.major(st.st_rdev), os.minor(st.st_rdev)) in self.rdevs:
            return True
        return (st.st_dev, st.st_ino) in self.inodes

    def add_stat(self, st: os.stat_result) -> None:
        if stat.S_ISCHR(st.st_mode):
            self.rdevs.add((os.major(st.st_rdev), os.minor(st.st_rdev)))
        self.inodes.add((st.st_dev, st.st_ino))


@dataclasses.dataclass
class Paths:
    sysfs: Path = Path("/sys")
    proc: Path = Path("/proc")
    dev: Path = Path("/dev")
    lock: Path = Path("/run/vfio-toggle.lock")
    trusted_uid: int = 0  # uid that must own config/hook files


# ==========================================================================
# The tool
# ==========================================================================


class Toggle:
    def __init__(self, cfg: Config, paths: Optional[Paths] = None, dry_run: bool = False):
        self.cfg = cfg
        self.paths = paths or Paths()
        self.dry_run = dry_run
        self.sysfs = Sysfs(self.paths.sysfs, dry_run)
        boot_id = self.sysfs.read(self.paths.proc / "sys/kernel/random/boot_id")
        self.store = StateStore(Path(cfg.state_file), boot_id, dry_run)
        self.names = PciNames()
        self.vfio = cfg.vfio_driver

    # ---------------------------------------------------------------- PCI --
    @property
    def pci_dir(self) -> Path:
        d = self.sysfs.subsystem_dir("pci")
        if d is None:
            raise ToggleError("No PCI subsystem found in sysfs.")
        return d

    def dev_path(self, addr: str) -> Path:
        """Real devpath of a PCI device (symlinks resolved, per the sysfs rules)."""
        link = self.pci_dir / "devices" / addr
        if not link.exists():
            raise ToggleError(f"PCI device '{addr}' does not exist (see 'list-devices').")
        return Path(os.path.realpath(link))

    def driver_of(self, addr: str) -> str:
        return self.sysfs.link_name(self.dev_path(addr) / "driver")

    def pci_class(self, addr: str) -> int:
        text = self.sysfs.read(self.dev_path(addr) / "class")
        try:
            return int(text, 16)
        except ValueError:
            return -1

    def base_class(self, addr: str) -> int:
        cls = self.pci_class(addr)
        return cls >> 16 if cls >= 0 else -1

    def get_override(self, addr: str) -> Optional[str]:
        f = self.dev_path(addr) / "driver_override"
        if not f.exists():
            return None
        value = self.sysfs.read(f)
        return None if value in ("", "(null)") else value

    def set_override(self, addr: str, driver: Optional[str]) -> None:
        f = self.dev_path(addr) / "driver_override"
        if not f.exists():
            raise ToggleError(f"{addr} has no driver_override attribute (kernel too old?).")
        log.debug("driver_override for %s -> %s", addr, driver or "<cleared>")
        self.sysfs.write(f, driver or "")

    def describe(self, addr: str) -> str:
        dev = self.dev_path(addr)
        def hex_attr(name: str) -> str:
            text = self.sysfs.read(dev / name)
            return text[2:] if text.startswith("0x") else text

        vendor, device = hex_attr("vendor"), hex_attr("device")
        base = self.base_class(addr)
        cls_name = BASE_CLASS_NAMES.get(base, f"class {base:#04x}" if base >= 0 else "unknown class")
        name = self.names.lookup(vendor, device)
        return f"{vendor}:{device} [{cls_name}] {name}".rstrip()

    def iommu_group(self, addr: str) -> tuple:
        """(group id, [member addresses]) from the documented iommu_groups ABI."""
        link = self.dev_path(addr) / "iommu_group"
        if not link.exists():
            raise ToggleError(
                f"{addr} has no IOMMU group. Enable the IOMMU (VT-d/AMD-Vi in firmware "
                f"and on the kernel command line) - see 'status'."
            )
        group_dir = Path(os.path.realpath(link))
        devices = group_dir / "devices"
        members = sorted(p.name for p in devices.iterdir()) if devices.is_dir() else []
        return group_dir.name, members

    def iommu_present(self) -> bool:
        d = self.paths.sysfs / "kernel" / "iommu_groups"
        return d.is_dir() and any(d.iterdir())

    def resolve_targets(self) -> list:
        """Configured devices plus their IOMMU-group siblings (never bridges)."""
        configured = list(self.cfg.gpu_pci_ids)
        extras: set = set()
        for addr in configured:
            self.dev_path(addr)
            if self.base_class(addr) == CLASS_BRIDGE:
                raise ToggleError(f"{addr} is a PCI bridge; bridges cannot be bound to vfio-pci.")
            _, members = self.iommu_group(addr)
            for member in members:
                if member in configured:
                    continue
                if not PCI_ADDR_RE.match(member):
                    raise ToggleError(
                        f"IOMMU group of {addr} contains non-PCI device '{member}', "
                        f"which this tool does not handle."
                    )
                if self.base_class(member) == CLASS_BRIDGE:
                    log.debug("Leaving bridge %s (same IOMMU group as %s) on its host driver.",
                              member, addr)
                    continue
                extras.add(member)
        if extras:
            listing = ", ".join(f"{a} ({self.describe(a)})" for a in sorted(extras))
            if self.cfg.group_expansion == "strict":
                raise ToggleError(
                    f"group_expansion=strict: the IOMMU group also contains {listing}. "
                    f"Add them to gpu_pci_ids, or set group_expansion=auto."
                )
            log.warning("Also managing IOMMU-group member(s): %s", listing)
        return sorted(set(configured) | extras)

    # ------------------------------------------------- bind/unbind primitives --
    def unbind_driver(self, addr: str) -> None:
        drv = self.driver_of(addr)
        if not drv:
            return
        log.info("Unbinding %s from '%s'.", addr, drv)
        try:
            self.sysfs.write(self.dev_path(addr) / "driver" / "unbind", addr,
                             timeout=self.cfg.driver_timeout)
        except SysfsTimeout:
            raise
        except SysfsError as exc:
            raise ToggleError(f"Failed to unbind {addr} from '{drv}': {exc}") from exc
        if not self.dry_run and not wait_until(lambda: not self.driver_of(addr), 5):
            raise ToggleError(f"{addr} is still bound to '{self.driver_of(addr)}' after unbind.")

    def bind_driver(self, addr: str, driver: str) -> bool:
        """Ask `driver` to bind `addr` via its documented 'bind' file."""
        if self.dry_run:
            log.info("[dry-run] would bind %s to '%s'.", addr, driver)
            return True
        bind_file = self.pci_dir / "drivers" / driver / "bind"
        if not bind_file.exists():
            log.debug("Driver '%s' is not registered; cannot bind %s.", driver, addr)
            return False
        try:
            self.sysfs.write(bind_file, addr, timeout=self.cfg.driver_timeout)
        except SysfsTimeout:
            raise
        except SysfsError as exc:
            log.debug("bind %s -> %s refused: %s", addr, driver, exc)
            return False
        return wait_until(lambda: self.driver_of(addr) == driver, self.cfg.bind_settle_timeout)

    def reset_device(self, addr: str) -> None:
        if not self.cfg.allow_function_level_reset or self.driver_of(addr):
            return
        reset = self.dev_path(addr) / "reset"
        if not reset.exists():
            log.debug("%s exposes no reset method; skipping reset.", addr)
            return
        try:
            self.sysfs.write(reset, "1")
            log.info("Reset %s.", addr)
        except SysfsError as exc:
            log.warning("Reset of %s failed (%s); continuing.", addr, exc)

    def restore_device(self, addr: str, snap: Optional[dict]) -> bool:
        """Put `addr` back to its snapshot: same driver, same driver_override.

        Idempotent; safe to run from any partially-completed state. With no
        snapshot ('unbind' of a device something else bound) the device is
        released to whichever driver claims it under normal matching rules.
        """
        want_driver = (snap or {}).get("driver") or ""
        want_override = (snap or {}).get("override") or None
        cur = self.driver_of(addr)
        if snap is not None and cur == want_driver:
            self.set_override(addr, want_override)
            return True
        if cur:
            self.unbind_driver(addr)
        self.reset_device(addr)
        if snap is not None and not want_driver:
            self.set_override(addr, want_override)  # it was unbound originally
            return True

        ok = False
        if want_driver:
            self.set_override(addr, want_driver)
            ok = self.bind_driver(addr, want_driver)
            self.set_override(addr, want_override)
        else:
            self.set_override(addr, None)
        if not ok:
            ok = self.auto_probe(addr, want_driver)
        if not ok:
            log.error("%s has no driver after restore (wanted '%s').", addr, want_driver or "<any>")
            return False
        now = self.driver_of(addr)
        if want_driver and now != want_driver and not self.dry_run:
            log.warning("%s is now bound to '%s' instead of '%s'.", addr, now, want_driver)
        log.info("%s restored to driver '%s'.", addr, now or want_driver)
        return True

    def auto_probe(self, addr: str, hint: str) -> bool:
        """Get *some* driver onto `addr` using only documented interfaces."""
        if self.dry_run:
            return True
        # 1. A synthetic 'add' uevent makes udev load the module for the device's
        #    modalias; loading a driver binds it to matching unbound devices.
        log.info("Asking udev to load a driver for %s%s.", addr,
                 f" (wanted '{hint}')" if hint else "")
        with contextlib.suppress(SysfsError):
            self.sysfs.write(self.dev_path(addr) / "uevent", "add")
        if wait_until(lambda: bool(self.driver_of(addr)), self.cfg.bind_settle_timeout):
            return True
        # 2. Offer the device to every registered driver: a driver whose ID table
        #    does not match simply rejects the request.
        drivers = self.pci_dir / "drivers"
        for d in sorted(p.name for p in drivers.iterdir()) if drivers.is_dir() else []:
            if d == self.vfio:
                continue
            if self.bind_driver(addr, d):
                log.warning("%s was bound to '%s' by trial; verify this is the driver you want.", addr, d)
                return True
        return False

    # ----------------------------------------------------- vfio-pci module --
    def ensure_vfio_available(self) -> None:
        drv_dir = self.pci_dir / "drivers" / self.vfio
        if drv_dir.is_dir():
            return
        hint = ("Load it at boot with a modules-load.d entry, or check that your "
                f"kernel provides {self.vfio}.")
        if not self.cfg.load_vfio_module:
            raise ToggleError(f"{self.vfio} is not available and load_vfio_module=false. {hint}")
        # The kernel names its own module helper here; use it instead of guessing PATH.
        helper = self.sysfs.read(self.paths.proc / "sys/kernel/modprobe") or "/sbin/modprobe"
        log.info("Loading %s via %s.", self.vfio, helper)
        if self.dry_run:
            return
        try:
            result = subprocess.run([helper, "--", self.vfio], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToggleError(f"Could not run module helper '{helper}': {exc}. {hint}") from exc
        if result.returncode != 0:
            raise ToggleError(f"Loading {self.vfio} failed: {result.stderr.strip()}. {hint}")
        if not wait_until(drv_dir.is_dir, 5):
            raise ToggleError(f"{self.vfio} was loaded but did not register. {hint}")

    # -------------------------------------------------------- busy devices --
    def _walk_files(self, root: Path, filename: str) -> Iterator[Path]:
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            if filename in files:
                yield Path(dirpath) / filename

    def collect_nodes(self, addrs: list, mode: str = "gpu") -> NodeSet:
        """Device nodes whose users block a bind ("gpu") or an unbind ("vfio").

        "gpu":  nodes of display-class devices only (an audio server holding the
                GPU's HDMI audio card is harmless and handled by the sound core),
                plus the configured busy_extra_globs.
        "vfio": nodes of every device plus its /dev/vfio/<group>, i.e. whatever
                a VM would hold open; unbinding vfio-pci blocks while those exist.
        """
        nodes = NodeSet()
        for addr in addrs:
            if mode == "gpu" and self.base_class(addr) not in BUSY_CLASSES:
                continue
            # Every character device that belongs to the PCI device (DRM card and
            # render nodes, vfio cdev, accel, ...) has a `dev` file in its subtree.
            for dev_file in self._walk_files(self.dev_path(addr), "dev"):
                m = re.fullmatch(r"(\d+):(\d+)", self.sysfs.read(dev_file))
                if m:
                    nodes.rdevs.add((int(m.group(1)), int(m.group(2))))
            if mode == "vfio":
                gid, _ = self.iommu_group(addr)
                with contextlib.suppress(OSError):
                    nodes.add_stat(os.stat(self.paths.dev / "vfio" / gid))
        if mode == "gpu":
            for pattern in self.cfg.busy_extra_globs:
                for path in glob.glob(pattern):
                    with contextlib.suppress(OSError):
                        nodes.add_stat(os.stat(path))
        return nodes

    def _pid_uses(self, pid: int, nodes: NodeSet) -> str:
        base = self.paths.proc / str(pid)
        try:
            fds = os.listdir(base / "fd")
        except OSError:
            return ""
        for fd in fds:
            try:
                if nodes.matches(os.stat(base / "fd" / fd)):
                    return f"open fd {fd}"
            except OSError:
                continue
        try:
            maps = (base / "maps").read_text(errors="replace")
        except OSError:
            return ""
        seen = set()
        for line in maps.splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6 or not parts[5].startswith("/dev/") or parts[5] in seen:
                continue
            seen.add(parts[5])
            try:  # resolve inside the process's own root (containers, chroots)
                if nodes.matches(os.stat(base / "root" / parts[5].lstrip("/"))):
                    return f"mapping of {parts[5]}"
            except OSError:
                continue
        return ""

    def find_busy(self, nodes: NodeSet) -> dict:
        me = os.getpid()
        found = {}
        try:
            entries = [e.name for e in os.scandir(self.paths.proc) if e.name.isdigit()]
        except OSError:
            return found
        for name in entries:
            pid = int(name)
            if pid != me:
                why = self._pid_uses(pid, nodes)
                if why:
                    found[pid] = why
        return found

    def _proc_field(self, pid: int, index: int) -> str:
        """Field `index` (0 = state) of /proc/<pid>/stat after the comm field."""
        try:
            data = (self.paths.proc / str(pid) / "stat").read_text()
            return data.rsplit(")", 1)[-1].split()[index]
        except (OSError, IndexError):
            return ""

    def _ancestors(self) -> set:
        chain, pid = set(), os.getpid()
        while pid > 1 and pid not in chain:
            chain.add(pid)
            try:
                pid = int(self._proc_field(pid, 1))  # ppid
            except ValueError:
                break
        return chain

    def _describe_pid(self, pid: int) -> str:
        comm = self.sysfs.read(self.paths.proc / str(pid) / "comm", "?")
        try:
            cmd = (self.paths.proc / str(pid) / "cmdline").read_bytes()
            cmd = cmd.replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmd = ""
        unit = self._unit_of(pid)
        return f"PID {pid} ({comm}): {cmd or comm}" + (f" [unit {unit}]" if unit else "")

    def _unit_of(self, pid: int) -> str:
        """Owning systemd service, from /proc/<pid>/cgroup (a diagnostic hint only)."""
        try:
            text = (self.paths.proc / str(pid) / "cgroup").read_text(errors="replace")
        except OSError:
            return ""
        units = re.findall(r"([^/\n]+\.service)(?=/|\n|$)", text)
        return units[-1] if units else ""

    def _never_signal(self, pid: int) -> str:
        """Reason why `pid` must not be signalled ('' if it may be)."""
        if pid in self._ancestors() or pid == 1:
            return "it is this script, PID 1 or one of our ancestors (run from a text console or ssh)"
        unit = self._unit_of(pid)
        if unit and any(fnmatch.fnmatch(unit, g) for g in self.cfg.terminate_exclude_units):
            return f"its unit {unit} is listed in terminate_exclude_units"
        return ""

    def _terminate(self, pids: list) -> None:
        victims = []
        for pid in pids:
            reason = self._never_signal(pid)
            if reason:
                log.warning("Not signalling %s: %s.", self._describe_pid(pid), reason)
                continue
            victims.append((pid, self._proc_field(pid, 19)))  # 19 -> starttime (proc(5) field 22)
        for pid, _start in victims:
            log.info("Sending SIGTERM to %s", self._describe_pid(pid))
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

        def same(pid, start):  # guards against PID reuse
            return bool(start) and self._proc_field(pid, 19) == start

        wait_until(lambda: not any(same(p, s) for p, s in victims),
                   self.cfg.kill_grace_period, 0.2)
        for pid, start in victims:
            if same(pid, start):
                log.warning("PID %d ignored SIGTERM for %ds; sending SIGKILL.", pid,
                            self.cfg.kill_grace_period)
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        wait_until(lambda: not any(same(p, s) for p, s in victims), 3, 0.1)

    def ensure_gpu_idle(self, addrs: list) -> None:
        nodes = self.collect_nodes(addrs)
        if not nodes:
            log.debug("No device nodes to check for users.")
            return
        busy = self.find_busy(nodes)
        if busy and self.cfg.busy_wait_timeout > 0:
            log.info("Waiting up to %ds for %d process(es) to release the GPU...",
                     self.cfg.busy_wait_timeout, len(busy))
            wait_until(lambda: not self.find_busy(nodes), self.cfg.busy_wait_timeout, 0.5)
            busy = self.find_busy(nodes)
        for attempt in range(3):
            if not busy:
                return
            listing = "\n  ".join(f"{self._describe_pid(p)} [{w}]" for p, w in sorted(busy.items()))
            if self.cfg.on_busy == "ignore":
                log.warning("Proceeding although the GPU is in use by:\n  %s", listing)
                return
            if self.cfg.on_busy == "abort":
                raise ToggleError(
                    f"The GPU is still in use by:\n  {listing}\n"
                    f"Stop them (e.g. in a pre-bind hook) or set on_busy = terminate.")
            if all(self._never_signal(p) for p in busy):
                raise ToggleError(
                    f"The GPU is used only by processes that must not be terminated:\n  {listing}\n"
                    f"Stop them yourself (e.g. in a pre-bind hook); if this is your own login "
                    f"session, run {PROG} from a text console or ssh session instead.")
            log.warning("Terminating processes using the GPU:\n  %s", listing)
            self._terminate(sorted(busy))
            wait_until(lambda: not self.find_busy(nodes), 2)
            busy = self.find_busy(nodes)
        if busy:
            raise ToggleError("Processes still use the GPU after 3 termination attempts: "
                              + ", ".join(map(str, sorted(busy))))

    def ensure_vfio_released(self, addrs: list) -> None:
        """Unbinding a vfio-pci device blocks while a VM holds it, so check first."""
        busy = self.find_busy(self.collect_nodes(addrs, mode="vfio"))
        if busy and self.cfg.on_busy != "ignore":
            listing = "\n  ".join(f"{self._describe_pid(p)} [{w}]" for p, w in sorted(busy.items()))
            raise ToggleError(f"The devices are still in use (VM running?):\n  {listing}\n"
                              f"Shut it down first; vfio-toggle never kills VMs.")

    # ---------------------------------------------------------- consoles --
    def _gpu_owns_console(self, addrs: list) -> bool:
        for addr in addrs:
            if self.base_class(addr) != CLASS_DISPLAY:
                continue
            for dirpath, dirs, _files in os.walk(self.dev_path(addr), followlinks=False):
                for d in dirs:
                    if self.sysfs.link_name(Path(dirpath) / d / "subsystem") == "graphics":
                        return True
        return False

    def release_consoles(self, state: State, addrs: list) -> None:
        if self.cfg.release_vt_console and self._gpu_owns_console(addrs):
            vt_dir = self.sysfs.subsystem_dir("vtconsole")
            for entry in sorted(vt_dir.iterdir()) if vt_dir else []:
                bind = entry / "bind"
                # Only "(M)odular" backends can be unbound (kernel console docs).
                if not bind.exists() or not self.sysfs.read(entry / "name").startswith("(M)"):
                    continue
                if self.sysfs.read(bind) != "1":
                    continue
                log.info("Unbinding console %s (%s).", entry.name, self.sysfs.read(entry / "name"))
                try:
                    self.sysfs.write(bind, "0")
                    state.vtconsoles.append(entry.name)
                    self.store.save(state)
                except SysfsError as exc:
                    log.warning("Could not unbind console %s: %s", entry.name, exc)
        if self.cfg.release_platform_framebuffer and any(
                self.base_class(a) == CLASS_DISPLAY for a in addrs):
            plat = self.sysfs.subsystem_dir("platform")
            for entry in sorted((plat / "devices").iterdir()) if plat else []:
                if not any(fnmatch.fnmatch(entry.name, g) for g in self.cfg.platform_framebuffer_globs):
                    continue
                real = Path(os.path.realpath(entry))
                drv = self.sysfs.link_name(real / "driver")
                if not drv:
                    continue
                log.info("Releasing firmware framebuffer %s (driver '%s').", entry.name, drv)
                try:
                    self.sysfs.write(real / "driver" / "unbind", entry.name)
                    state.platform_devices.append({"device": entry.name, "driver": drv})
                    self.store.save(state)
                except SysfsError as exc:
                    log.warning("Could not release %s: %s", entry.name, exc)

    def restore_consoles(self, state: State) -> None:
        plat = self.sysfs.subsystem_dir("platform")
        for item in list(state.platform_devices):
            bind = plat / "drivers" / item["driver"] / "bind" if plat else None
            if bind and bind.exists() and not (plat / "devices" / item["device"] / "driver").exists():
                try:
                    self.sysfs.write(bind, item["device"])
                    log.info("Rebound %s to '%s'.", item["device"], item["driver"])
                except SysfsError as exc:
                    log.warning("Could not rebind %s: %s", item["device"], exc)
            state.platform_devices.remove(item)
        vt_dir = self.sysfs.subsystem_dir("vtconsole")
        for name in list(state.vtconsoles):
            bind = vt_dir / name / "bind" if vt_dir else None
            if bind and bind.exists():
                try:
                    self.sysfs.write(bind, "1")
                    log.info("Rebound console %s.", name)
                except SysfsError as exc:
                    log.warning("Could not rebind console %s: %s", name, exc)
            state.vtconsoles.remove(name)

    # -------------------------------------------------------------- hooks --
    @staticmethod
    def _is_valid_hook(p: Path) -> bool:
        ignored = ("~", ".disabled", ".example", ".bak", ".orig",
                   ".rpmsave", ".rpmnew", ".dpkg-old", ".dpkg-dist")
        return (p.is_file() and os.access(p, os.X_OK)
                and not p.name.startswith(".") and not p.name.endswith(ignored))

    def run_hooks(self, event: str, devices: list, fatal: bool) -> None:
        hook_path = Path(self.cfg.hook_dir)
        if not hook_path.exists():
            log.debug("No hooks for '%s' (hook path '%s' does not exist).", event, hook_path)
            return

        hooks = []
        try:
            if hook_path.is_file():
                assert_trusted(hook_path, self.paths.trusted_uid, "hook script")
                if self._is_valid_hook(hook_path):
                    hooks.append(hook_path)
                else:
                    log.warning("Hook file '%s' is not executable or has an ignored suffix.", hook_path)
            elif hook_path.is_dir():
                assert_trusted(hook_path, self.paths.trusted_uid, "hook directory")
                # Top-level executable scripts run for every event:
                hooks.extend(sorted(p for p in hook_path.iterdir() if self._is_valid_hook(p)))
                # Event-specific subdirectories (e.g. <hook_dir>/pre-bind/):
                event_dir = hook_path / event
                if event_dir.is_dir():
                    assert_trusted(event_dir, self.paths.trusted_uid, "hook event directory")
                    hooks.extend(sorted(p for p in event_dir.iterdir() if self._is_valid_hook(p)))
        except (ToggleError, OSError) as exc:
            if fatal:
                raise ToggleError(f"Hooks for '{event}' unusable: {exc}") from exc
            log.error("Hooks for '%s' unusable: %s", event, exc)
            return

        # Deduplicate while preserving order if a file was referenced multiple ways
        seen = set()
        unique_hooks = []
        for h in hooks:
            resolved = h.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique_hooks.append(h)
        hooks = unique_hooks

        if not hooks:
            log.debug("No hooks for '%s'.", event)
            return

        env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "VFIO_TOGGLE_EVENT": event,
            "VFIO_TOGGLE_DEVICES": " ".join(devices),
        }
        for hook in hooks:
            if self.dry_run:
                log.info("[dry-run] would run hook %s for %s", hook, event)
                continue
            try:
                assert_trusted(hook, self.paths.trusted_uid, "hook")
                self._run_one_hook(hook, env)
            except ToggleError as exc:
                if fatal:
                    raise
                log.warning("%s", exc)

    def _run_one_hook(self, hook: Path, env: dict) -> None:
        event = env.get("VFIO_TOGGLE_EVENT", "")
        hook_name = f"{hook.parent.name}/{hook.name}" if hook.parent.name == event else hook.name
        log.info("Running hook %s for event '%s'", hook_name, event)
        proc = subprocess.Popen(
            [str(hook)], env=env, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True,
            umask=0o022,
        )
        try:
            out, _ = proc.communicate(timeout=self.cfg.hook_timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise ToggleError(f"Hook {hook_name} timed out after {self.cfg.hook_timeout}s.")
        except BaseException:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            raise
        for line in (out or "").splitlines():
            log.info("  hook[%s]: %s", hook.name, line)
        if proc.returncode != 0:
            raise ToggleError(f"Hook {hook_name} exited with status {proc.returncode}.")

    # ----------------------------------------------------------- commands --
    def cmd_bind(self) -> None:
        # ---- preflight: everything that can be checked without side effects ----
        targets = self.resolve_targets()
        if not self.iommu_present():
            raise ToggleError("No IOMMU groups found; VFIO passthrough needs the IOMMU enabled.")
        state = self.store.load()
        if state and state.phase == "binding":
            raise ToggleError(
                f"A previous bind was interrupted (state file {self.store.path}). "
                f"Run '{PROG} rollback' first.")
        todo = [a for a in targets if self.driver_of(a) != self.vfio]
        if not todo:
            log.info("All managed devices are already bound to %s. Nothing to do.", self.vfio)
            return
        for addr in todo:
            if not (self.dev_path(addr) / "driver_override").exists():
                raise ToggleError(f"{addr} has no driver_override attribute (kernel too old?).")
        self.ensure_vfio_available()  # loading a driver module is harmless; do it before any hook

        # ---- snapshot: record the truth about each device *before* touching it ----
        log.info("Binding to %s: %s", self.vfio, " ".join(todo))
        state = state or self.store.new()
        for addr in targets:
            if addr in todo or addr not in state.devices:
                state.devices[addr] = {"driver": self.driver_of(addr), "override": self.get_override(addr)}
        state.phase = "binding"
        self.store.save(state)
        try:
            self.run_hooks("pre-bind", targets, fatal=True)
            self.ensure_gpu_idle(todo)
            self.release_consoles(state, todo)
            for addr in todo:
                self.set_override(addr, self.vfio)
                self.unbind_driver(addr)
            for addr in todo:
                self.reset_device(addr)
            for addr in todo:
                if not self.bind_driver(addr, self.vfio):
                    raise ToggleError(f"{addr} did not bind to {self.vfio}; see dmesg.")
                log.info("%s is now bound to %s.", addr, self.vfio)
            if not self.dry_run:
                bad = [a for a in targets if self.driver_of(a) != self.vfio]
                if bad:
                    raise ToggleError(f"Post-check failed; not on {self.vfio}: {' '.join(bad)}")
        except SysfsTimeout as exc:
            log.error("%s", exc)
            log.error("State kept in %s; run '%s rollback' once the kernel is done.",
                      self.store.path, PROG)
            raise
        except BaseException as exc:
            self._fail_bind(state, todo, targets, exc)
            raise
        state.phase = "bound"
        self.store.save(state)
        self.run_hooks("post-bind", targets, fatal=False)
        log.info("SUCCESS: %s bound to %s.", " ".join(targets), self.vfio)

    def _fail_bind(self, state: State, todo: list, targets: list, exc: BaseException) -> None:
        log.error("Bind failed: %s", exc if isinstance(exc, ToggleError) else repr(exc))
        if not self.cfg.auto_rollback:
            log.warning("auto_rollback is off; run '%s rollback' to restore the previous state.", PROG)
            return
        log.warning("Rolling back to the pre-bind state...")
        self._restore_all(state, todo, targets, run_rollback_hooks=True)

    def _restore_all(self, state: State, addrs: list, all_targets: list,
                     run_rollback_hooks: bool) -> list:
        """Restore `addrs` from the snapshot; returns the ones that failed."""
        failed = []
        with signals_ignored():  # a half-restored GPU is worse than a slow exit
            for addr in addrs:
                try:
                    if not self.restore_device(addr, state.devices.get(addr)):
                        failed.append(addr)
                except Exception as exc:  # noqa: BLE001 - keep restoring the others
                    log.error("Restoring %s failed: %s", addr, exc)
                    failed.append(addr)
            try:
                self.restore_consoles(state)
            except Exception as exc:  # noqa: BLE001
                log.error("Restoring consoles failed: %s", exc)
            for addr in addrs:
                if addr not in failed:
                    state.devices.pop(addr, None)
        if failed:
            self.store.save(state)  # phase unchanged: still "binding" or "bound"
            log.error("Could not restore: %s. Fix the cause and run '%s unbind' or '%s rollback'.",
                      " ".join(failed), PROG, PROG)
        elif state.devices:
            state.phase = "bound"
            self.store.save(state)
        else:
            self.store.clear()
        if run_rollback_hooks:
            self.run_hooks("rollback", all_targets, fatal=False)
        return failed

    def cmd_unbind(self) -> None:
        targets = self.resolve_targets()
        state = self.store.load()
        if state is not None and state.phase == "binding":
            log.warning("The last bind never completed; performing a rollback instead.")
            return self.cmd_rollback()
        if state is None:
            log.warning("No saved state; releasing devices to whichever driver claims them.")
        on_vfio = [a for a in targets if self.driver_of(a) == self.vfio]
        if not on_vfio:
            log.info("No managed device is bound to %s. Nothing to do.", self.vfio)
            if state and state.phase == "bound":
                self.store.clear()
            return
        self.ensure_vfio_released(on_vfio)
        self.run_hooks("pre-unbind", targets, fatal=True)
        state = state or self.store.new()
        log.info("Restoring: %s", " ".join(on_vfio))
        failed = self._restore_all(state, on_vfio, targets, run_rollback_hooks=False)
        if failed:
            raise ToggleError(f"Could not restore: {' '.join(failed)} (state kept; unbind is retryable).")
        self.run_hooks("post-unbind", targets, fatal=False)
        log.info("SUCCESS: %s restored.", " ".join(on_vfio))

    def cmd_rollback(self) -> None:
        state = self.store.load()
        if state is None:
            log.info("No saved state; nothing to roll back.")
            return
        try:
            targets = self.resolve_targets()
        except ToggleError as exc:
            log.warning("%s", exc)
            targets = sorted(state.devices)
        addrs = sorted(state.devices)
        self.ensure_vfio_released([a for a in addrs if self.driver_of(a) == self.vfio])
        log.warning("Rolling back %d device(s) from %s.", len(addrs), self.store.path)
        failed = self._restore_all(state, addrs, targets, run_rollback_hooks=True)
        if failed:
            raise ToggleError(f"Rollback incomplete: {' '.join(failed)}")
        log.info("Rollback complete.")

    def cmd_status(self) -> None:
        cfg = self.cfg
        print(f"{PROG} {__version__}   config: {cfg.path}")
        print(f"IOMMU groups present: {'yes' if self.iommu_present() else 'NO - enable VT-d/AMD-Vi'}")
        vfio = (self.pci_dir / "drivers" / self.vfio).is_dir()
        print(f"vfio-pci available:   {'yes' if vfio else 'not loaded (will be loaded on bind)' if cfg.load_vfio_module else 'NO'}")
        print("\nManaged devices:")
        for addr in self.resolve_targets():
            override = self.get_override(addr)
            gid, _ = self.iommu_group(addr)
            print(f"  {addr}  driver={self.driver_of(addr) or '-':<14} group={gid:<4} "
                  f"override={override or '-'}\n      {self.describe(addr)}")
        print()
        try:
            state = self.store.load()
        except ToggleError as exc:
            print(f"State: {exc}")
            return
        if state is None:
            print("State: clean (no snapshot).")
            return
        print(f"State: phase={state.phase or '-'}, snapshot of {len(state.devices)} device(s), "
              f"written {state.written or '?'}")
        for addr, snap in sorted(state.devices.items()):
            print(f"  {addr}: was '{snap.get('driver') or '-'}' (override {snap.get('override') or '-'})")
        if state.vtconsoles or state.platform_devices:
            print(f"  released consoles: {state.vtconsoles + [d['device'] for d in state.platform_devices]}")
        if state.phase == "binding":
            print(f"  ** interrupted bind - run '{PROG} rollback' **")

    def cmd_list_devices(self) -> None:
        devices = self.pci_dir / "devices"
        found = False
        for entry in sorted(devices.iterdir()):
            addr = entry.name
            if self.base_class(addr) != CLASS_DISPLAY:
                continue
            found = True
            print(f"\n{addr}\n  {self.describe(addr)}\n  driver: {self.driver_of(addr) or '<none>'}")
            try:
                gid, members = self.iommu_group(addr)
            except ToggleError:
                print("  IOMMU group: <none - IOMMU not enabled?>")
                continue
            print(f"  IOMMU group {gid}:")
            for m in members:
                tag = " (bridge - left alone)" if self.base_class(m) == CLASS_BRIDGE else ""
                print(f"    {m}  {self.describe(m)}{tag}")
        if not found:
            print("No display-class PCI devices found.")
        else:
            print("\nPut the GPU address(es) into gpu_pci_ids in the config; group members "
                  "follow according to group_expansion.")


# ==========================================================================
# CLI
# ==========================================================================

COMMANDS = ("bind", "unbind", "status", "list-devices", "rollback",
            "default-config", "show-log", "clear-log")
NEEDS_ROOT = {"bind", "unbind", "rollback", "clear-log"}
NEEDS_DEVICES = {"bind", "unbind", "status", "rollback"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=COMMANDS, help="what to do (see the module docstring)")
    p.add_argument("-c", "--config", metavar="PATH",
                   help="config file (default: %s)" % " or ".join(map(str, DEFAULT_CONFIG_PATHS)))
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG output on the console")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="log what would be written/run without changing anything")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def find_config(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ToggleError(f"Config file not found: {path}")
        return path
    return next((p for p in DEFAULT_CONFIG_PATHS if p.is_file()), None)


def run(args: argparse.Namespace, paths: Optional[Paths] = None) -> int:
    paths = paths or Paths()
    cmd = args.command
    configure_logging(verbose=args.verbose)
    if cmd == "default-config":
        print(render_default_config())
        return 0
    if cmd in NEEDS_ROOT and os.geteuid() != 0 and not args.dry_run:
        raise ToggleError(f"'{cmd}' must be run as root (or use --dry-run).")

    cfg_path = find_config(args.config)
    if cfg_path is None:
        if cmd in NEEDS_DEVICES:
            raise ToggleError("No config file found (looked in: "
                              + ", ".join(map(str, DEFAULT_CONFIG_PATHS)) + "). "
                              f"Create one with: {PROG} default-config > /etc/vfio-toggle/vfio-toggle.conf")
        cfg = Config()
    else:
        cfg = Config.load(cfg_path, paths.trusted_uid)
    mutating = cmd in ("bind", "unbind", "rollback") and not args.dry_run
    configure_logging(cfg.log_level, args.verbose, cfg.log_file if mutating else None,
                      cfg.log_max_bytes, cfg.log_backups)
    if cmd in NEEDS_DEVICES:
        cfg.require_devices()

    if cmd == "show-log":
        path = Path(cfg.log_file)
        try:
            print(path.read_text(errors="replace") if path.is_file() else f"No log file at {path}.", end="")
        except OSError as exc:
            raise ToggleError(f"Cannot read log '{path}': {exc} (the log is root-only).") from exc
        return 0
    if cmd == "clear-log":
        for p in (Path(cfg.log_file), *(Path(f"{cfg.log_file}.{i}") for i in range(1, cfg.log_backups + 1))):
            with contextlib.suppress(OSError):
                p.unlink()
        print(f"Log {cfg.log_file} cleared.")
        return 0

    log.debug("%s %s starting: command=%s pid=%d dry_run=%s", PROG, __version__, cmd, os.getpid(), args.dry_run)
    tool = Toggle(cfg, paths, args.dry_run)
    if cmd in ("status", "list-devices"):
        getattr(tool, "cmd_" + cmd.replace("-", "_"))()
        return 0
    with exclusive_lock(paths.lock, enabled=not args.dry_run):
        getattr(tool, "cmd_" + cmd)()
    return 0


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    os.umask(0o077)  # log, state and lock files are root-only
    install_signal_handlers()
    try:
        return run(args)
    except Interrupted as exc:
        log.error("Interrupted by %s.", signal.Signals(exc.signum).name)
        return 128 + exc.signum
    except BrokenPipeError:  # e.g. `... | head`
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except ToggleError as exc:
        log.error("%s", exc)
        return 1
    except Exception:  # noqa: BLE001 - last-resort report
        log.exception("Unhandled error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
