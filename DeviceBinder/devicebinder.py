#!/usr/bin/env python3
import sys
import time
import subprocess
import argparse
import logging
import json
import os
import tempfile
import shutil
import glob
import shlex
import re
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

# ==============================================================================
# SECTION 1: CONFIGURATION & DEFAULTS
# ==============================================================================

# Feature: Configuration File Path
CONFIG_FILE = Path("/etc/devicebinder.json")

# Feature 5: Logging
LOG_FILE = Path("/var/log/devicebinder.log")

# Feature 4: State Saver File
STATE_FILE = Path("/tmp/devicebinder_state.json")

# ROLLBACK State File
ROLLBACK_FILE = Path("/tmp/devicebinder_rollback.json")

# Default Retry Config (can be overridden by CLI)
DEFAULT_RETRIES = 5
DEFAULT_TIMEOUT = 2.0

# Configurable sysfs paths
SYSFS_PATHS = {
    'pci_bus': Path('/sys/bus/pci'),
    'pci_devices': Path('/sys/bus/pci/devices'),
    'pci_drivers': Path('/sys/bus/pci/drivers'),
    'module': Path('/sys/module'),
    'iommu_groups': Path('/sys/kernel/iommu_groups'),
    'vtconsole': Path('/sys/class/vtconsole'),
    'platform_drivers': Path('/sys/bus/platform/drivers'),
    'drm_devices': Path('/dev/dri'),
    'sound_devices': Path('/dev/snd'),
}

# ==============================================================================
# SECTION 2: LOGGING & BASE UTILITIES
# ==============================================================================

def setup_logging():
    """Configures logging with rotation. Idempotent-safe."""
    root_logger = logging.getLogger()
    
    if root_logger.handlers:
        return

    if not LOG_FILE.parent.exists():
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass 

    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    root_logger.setLevel(logging.INFO)

    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, 
            maxBytes=5 * 1024 * 1024, # 5 MB
            backupCount=1,            
            encoding='utf-8'
        )
        file_handler.setFormatter(log_formatter)
        root_logger.addHandler(file_handler)
    except (PermissionError, OSError):
        print(f"Warning: Could not open log file {LOG_FILE}", file=sys.stderr)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)

def log(msg, level="info"):
    """Unified logging wrapper."""
    if level == "info":
        logging.info(msg)
    elif level == "error":
        logging.error(msg)
    elif level == "warning":
        logging.warning(msg)
    elif level == "debug":
        logging.debug(msg)

def run_command(cmd, ignore_errors=False):
    """Executes a shell command with full output capture."""
    log(f"Exec: {cmd}", "debug")
    try:
        result = subprocess.run(
            cmd, 
            shell=True, 
            check=True, 
            capture_output=True, 
            text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if not ignore_errors:
            log(f"Command failed: {cmd}", "error")
            log(f"STDOUT: {e.stdout}", "error")
            log(f"STDERR: {e.stderr}", "error")
            raise 
        return None

# ==============================================================================
# SECTION 3: CONFIGURATION MANAGEMENT
# ==============================================================================

def validate_pci_id(pci_id):
    """
    FIX #3: Sanitize PCI IDs early.
    Expects format: 0000:01:00.0 (Domain:Bus:Device.Function)
    """
    pattern = re.compile(r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$')
    if not pattern.match(pci_id):
        raise ValueError(f"Invalid PCI ID format: '{pci_id}'. Expected format '0000:00:00.0'")
    return True

def load_config():
    """
    Loads device configuration from /etc/devicebinder.json.
    """
    if not CONFIG_FILE.exists():
        default_config = {
            "vm_driver": "vfio-pci", 
            "single_gpu_passthrough": False,
            "module_match_strategy": "prefix",
            "editor": "/usr/bin/nano", # Feature: Default editor path
            "devices": [
                {"id": "0000:01:00.0", "driver": "nvidia"},
                {"id": "0000:01:00.1", "driver": "snd_hda_intel"}
            ]
        }
        
        try:
            if not CONFIG_FILE.parent.exists():
                CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
                
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=2)
            
            os.chmod(CONFIG_FILE, 0o644)
            
            log(f"Configuration file created at {CONFIG_FILE}.")
            print(f"\n[INFO] Configuration file created: {CONFIG_FILE}")
            print("Please edit this file to match your specific PCI IDs and drivers before running the script again.\n")
            sys.exit(1)
            
        except OSError as e:
            log(f"CRITICAL: Cannot create {CONFIG_FILE}: {e}", "error")
            sys.exit(1)

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        if "devices" not in config or not isinstance(config["devices"], list):
            raise ValueError("Invalid config format: 'devices' list missing.")
        
        for dev in config["devices"]:
            if "id" not in dev:
                raise ValueError("Device entry missing 'id' field.")
            validate_pci_id(dev["id"])

        if "vm_driver" not in config:
            config["vm_driver"] = "vfio-pci"
        
        if "module_match_strategy" not in config:
            config["module_match_strategy"] = "prefix"
            
        if "single_gpu_passthrough" not in config:
            config["single_gpu_passthrough"] = False

        # Feature: Ensure editor key exists
        if "editor" not in config:
            config["editor"] = "/usr/bin/nano"
            
        return config
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log(f"Failed to load configuration file: {e}", "error")
        sys.exit(1)

# ==============================================================================
# SECTION 4: SYSFS & DEVICE HELPERS
# ==============================================================================

def write_to_sysfs(path_obj, content, must_exist=True, newline=True):
    """
    Writes a string to a system file with validation.
    """
    if not path_obj.exists():
        if must_exist:
            log(f"Critical Error: Sysfs path {path_obj} does not exist. Cannot write '{content}'.", "error")
            raise FileNotFoundError(f"Sysfs path missing: {path_obj}")
        else:
            log(f"Warning: Path {path_obj} does not exist. Skipping write.", "warning")
            return

    try:
        final_content = content
        if newline and not content.endswith('\n'):
            final_content += '\n'
            
        log(f"Writing '{final_content.strip()}' to {path_obj}", "debug")
        with path_obj.open('w', encoding='utf-8') as f:
            f.write(final_content)
    except OSError as e:
        log(f"Error writing to {path_obj}: {e}", "error")
        if must_exist:
            raise

def get_driver(pci_id):
    """Checks which driver is currently bound to the device."""
    path = SYSFS_PATHS['pci_devices'] / pci_id / "driver"
    if path.exists():
        return path.resolve().name
    return None

def get_device_name(pci_id):
    """Gets the readable device name via lspci."""
    try:
        cmd = f"lspci -s {pci_id}"
        output = run_command(cmd, ignore_errors=True)
        if output:
            parts = output.split(':', 2)
            if len(parts) > 2:
                return parts[2].strip()
            return output
    except Exception:
        pass
    return "Unknown Device"

def detach_efi_framebuffer():
    """
    Scans all platform drivers for framebuffer-related devices.
    """
    log("Checking for framebuffer locks...")
    
    fb_patterns = ['framebuffer', 'efifb', 'simpledrm', 'vesafb', 'offb']
    
    platform_drivers = SYSFS_PATHS['platform_drivers']
    if not platform_drivers.exists():
        log("Platform drivers path not found, skipping framebuffer detach.", "warning")
        return
    
    for driver_dir in platform_drivers.iterdir():
        if not driver_dir.is_dir():
            continue
            
        driver_name = driver_dir.name
        is_fb_driver = any(pattern in driver_name.lower() for pattern in fb_patterns)
        
        if is_fb_driver:
            log(f"Found framebuffer driver: {driver_name}")
            for item in driver_dir.iterdir():
                if item.is_symlink() and item.name not in ["bind", "unbind", "uevent"]:
                    log(f"Detaching {item.name} from {driver_name}...")
                    try:
                        write_to_sysfs(driver_dir / "unbind", item.name, must_exist=True, newline=True)
                    except (OSError, FileNotFoundError):
                        log(f"Failed to detach {item.name}", "warning")

def trigger_device_reset(pci_id):
    """Triggers a Function Level Reset (FLR) if available."""
    reset_path = SYSFS_PATHS['pci_devices'] / pci_id / "reset"
    if reset_path.exists():
        log(f"Triggering FLR (Function Level Reset) for {pci_id}...")
        try:
            write_to_sysfs(reset_path, "1", newline=True)
            time.sleep(1) 
        except OSError:
             log(f"FLR failed for {pci_id}.", "warning")
    else:
        # Not all devices support FLR, this is informational only
        log(f"No FLR capability found for {pci_id} (reset file missing).", "debug")


# ==============================================================================
# SECTION 5: MODULE MANAGEMENT (ROBUST TOKEN MATCHING)
# ==============================================================================

def normalize_mod_name(name):
    """Normalizes module names (dashes to underscores)."""
    return name.replace("-", "_") if name else ""

def get_loaded_modules_sysfs():
    """Scans /sys/module to find all currently loaded modules."""
    sys_module = SYSFS_PATHS['module']
    if not sys_module.exists():
        return set()
    
    modules = set()
    for item in sys_module.iterdir():
        if item.is_dir():
            modules.add(normalize_mod_name(item.name))
            
    return modules

def get_module_dependents_recursive(root_module, visited=None):
    """
    Finds all modules that depend on 'root_module' (recursively).
    Returns a list of modules starting with the leaves (most dependent).
    """
    if visited is None:
        visited = set()
    
    stack_order = []
    
    def visit(mod_name, path_stack):
        norm_name = normalize_mod_name(mod_name)
        
        if norm_name in path_stack:
            log(f"Circular dependency detected: {' -> '.join(path_stack)} -> {norm_name}", "warning")
            return
        
        if norm_name in visited:
            return

        holders_path = SYSFS_PATHS['module'] / norm_name / "holders"
        
        children = []
        if holders_path.exists():
            for holder in holders_path.iterdir():
                children.append(holder.name)
        
        new_path_stack = path_stack + [norm_name]
        for child in children:
            visit(child, new_path_stack)
            
        if norm_name not in visited:
            visited.add(norm_name)
            stack_order.append(norm_name)

    visit(root_module, [])
    return stack_order

def match_module_by_strategy(module_name, root_name, strategy):
    """
    Module matching: prefix, exact, or contains.
    """
    norm_module = normalize_mod_name(module_name)
    norm_root = normalize_mod_name(root_name)
    
    if strategy == "exact":
        return norm_module == norm_root
    elif strategy == "contains":
        return norm_root in norm_module
    else:  # prefix (default)
        root_tokens = norm_root.split('_')
        mod_tokens = norm_module.split('_')
        
        if len(mod_tokens) >= len(root_tokens):
            return mod_tokens[:len(root_tokens)] == root_tokens
        return False

def get_modules_to_unload(root_drivers, match_strategy="prefix"):
    """
    Calculates the full unload list based on config roots.
    """
    loaded_modules = get_loaded_modules_sysfs()
    final_unload_list = []
    seen = set()

    for root in root_drivers:
        candidates = []
        
        for mod in loaded_modules:
            if match_module_by_strategy(mod, root, match_strategy):
                candidates.append(mod)

        for candidate in candidates:
            stack = get_module_dependents_recursive(candidate)
            for mod in stack:
                if mod not in seen:
                    final_unload_list.append(mod)
                    seen.add(mod)
                
    return final_unload_list

def is_parameter_restorable(param_name, param_value):
    """
    Determines if a parameter should be restored.
    Filters out read-only, null, and problematic values.
    """
    if not param_value:
        return False
    if param_value == "(null)" or "(null)" in param_value:
        return False
    if param_value in ["Y", "N"] and param_name in ["dmic_detect", "ctl_dev_id", "power_save_controller"]:
        return False
    if "," in param_value:
        parts = param_value.split(",")
        if len(set(parts)) == 1:
            return False
    if param_value == "-1" or (param_value.startswith("-") and param_value.replace("-", "").replace(",", "").isdigit()):
        return False
    problematic_chars = ["(", ")", "[", "]", "{", "}", ";", "|", "&", "$", "`"]
    if any(char in param_value for char in problematic_chars):
        return False
    return True

def get_module_parameters(module_name):
    """
    Captures current module parameters before unload.
    """
    params = {}
    params_path = SYSFS_PATHS['module'] / module_name / "parameters"
    
    if not params_path.exists():
        return params
    
    try:
        for param_file in params_path.iterdir():
            if param_file.is_file():
                try:
                    value = param_file.read_text(encoding='utf-8').strip()
                    if is_parameter_restorable(param_file.name, value):
                        params[param_file.name] = value
                except (OSError, UnicodeDecodeError):
                    pass
    except OSError:
        pass
    
    return params

def is_module_bound_to_managed_devices(module_name, managed_devices):
    """
    Checks if the given module is currently driving any of the devices
    defined in the configuration file.
    """
    normalized_module = normalize_mod_name(module_name)
    
    for dev in managed_devices:
        current_driver = get_driver(dev['id'])
        if current_driver:
            normalized_driver = normalize_mod_name(current_driver)
            if normalized_driver == normalized_module:
                return True
            if normalized_driver.startswith(normalized_module):
                return True
                
    return False

def unload_modules_safe(modules_list, managed_devices, retries=3):
    """
    Unloads modules intelligently. 
    Returns dict of module -> parameters for restoration.
    """
    failed_modules = []
    module_params = {}
    
    for module in modules_list:
        if not (SYSFS_PATHS['module'] / module).exists():
            continue 

        module_params[module] = get_module_parameters(module)

        log(f"Attempting to unload module: {module}...")
        
        unloaded = False
        delay = 1
        
        for attempt in range(retries):
            # Attempt unload
            proc = subprocess.run(
                f"modprobe -r {module}", 
                shell=True, 
                capture_output=True, 
                text=True
            )
            
            if proc.returncode == 0:
                unloaded = True
                break
            else:
                if not is_module_bound_to_managed_devices(module, managed_devices):
                    log(f"Module {module} is in use by other system devices (e.g., iGPU). Skipping unload.", "info")
                    unloaded = True # Treat as success for our workflow
                    break
                
                log(f"Unload failed for {module} (Attempt {attempt+1}/{retries}). It is still busy.", "debug")
                time.sleep(delay)
                delay *= 2

        if not unloaded:
            if (SYSFS_PATHS['module'] / module).exists():
                log(f"Failed to unload {module} after {retries} attempts.", "error")
                failed_modules.append(module)

    if failed_modules:
        log(f"WARNING: The following modules could not be unloaded: {failed_modules}", "warning")
    else:
        log("Module preparation complete.")
    
    return module_params

# ==============================================================================
# SECTION 6: STATE MANAGEMENT
# ==============================================================================

def save_driver_state(devices, modules_to_unload=None, killed_procs=None, module_params=None):
    """
    Saves the state atomically including module parameters.
    """
    if modules_to_unload is None: modules_to_unload = []
    if killed_procs is None: killed_procs = []
    if module_params is None: module_params = {}

    device_states = []
    for dev in devices:
        pci_id = dev['id']
        current_driver = get_driver(pci_id)
        device_states.append({
            "id": pci_id,
            "previous_driver": current_driver
        })

    state = {
        "timestamp": datetime.now().isoformat(),
        "devices": device_states,
        "unloaded_modules": modules_to_unload,
        "module_parameters": module_params,
        "killed_processes": killed_procs
    }
    
    try:
        parent_dir = STATE_FILE.parent if STATE_FILE.parent.exists() else Path("/tmp")
        with tempfile.NamedTemporaryFile(mode='w', dir=parent_dir, delete=False) as tf:
            os.chmod(tf.name, 0o600)
            json.dump(state, tf, indent=2)
            temp_name = tf.name
            
        shutil.move(temp_name, STATE_FILE)
        os.chmod(STATE_FILE, 0o600) 
        log(f"State saved to {STATE_FILE}")
    except (OSError, IOError) as e:
        log(f"Failed to save state: {e}", "error")

def load_driver_state():
    """Loads the previous driver state."""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        log(f"Failed to load state: {e}", "error")
        return None

def save_rollback_checkpoint(checkpoint_name, data):
    """
    Saves a rollback checkpoint at various stages of operation.
    """
    try:
        if ROLLBACK_FILE.exists():
            existing = json.loads(ROLLBACK_FILE.read_text(encoding='utf-8'))
        else:
            existing = {"checkpoints": []}
        
        checkpoint = {
            "name": checkpoint_name,
            "timestamp": datetime.now().isoformat(),
            "data": data
        }
        
        existing["checkpoints"].append(checkpoint)
        
        parent_dir = ROLLBACK_FILE.parent if ROLLBACK_FILE.parent.exists() else Path("/tmp")
        with tempfile.NamedTemporaryFile(mode='w', dir=parent_dir, delete=False) as tf:
            os.chmod(tf.name, 0o600)
            json.dump(existing, tf, indent=2)
            temp_name = tf.name
        
        shutil.move(temp_name, ROLLBACK_FILE)
        os.chmod(ROLLBACK_FILE, 0o600)
        log(f"Rollback checkpoint '{checkpoint_name}' saved.", "debug")
    except (OSError, IOError, json.JSONDecodeError) as e:
        log(f"Warning: Could not save rollback checkpoint: {e}", "warning")

def execute_rollback():
    """
    Executes rollback to last known good state.
    """
    log("!!! EXECUTING ROLLBACK !!!", "warning")
    
    if not ROLLBACK_FILE.exists():
        log("No rollback data available. Manual recovery required.", "error")
        return False
    
    try:
        rollback_data = json.loads(ROLLBACK_FILE.read_text(encoding='utf-8'))
        checkpoints = rollback_data.get("checkpoints", [])
        
        if not checkpoints:
            log("No checkpoints in rollback file.", "error")
            return False
        
        # Process checkpoints in reverse order (LIFO)
        for checkpoint in reversed(checkpoints):
            name = checkpoint.get("name", "unknown")
            data = checkpoint.get("data", {})
            
            log(f"Rolling back: {name}", "info")
            
            if name == "pre_unbind":
                for dev_info in data.get("devices", []):
                    pci_id = dev_info["id"]
                    driver = dev_info.get("driver")
                    if driver:
                        log(f"Restoring {pci_id} -> {driver}")
                        try:
                            bind_device(pci_id, driver, DEFAULT_TIMEOUT, DEFAULT_RETRIES)
                        except Exception as e:
                            log(f"Failed to restore {pci_id}: {e}", "error")
            
            elif name == "pre_module_unload":
                for module in data.get("modules", []):
                    log(f"Restoring module: {module}")
                    try:
                        run_command(f"modprobe {module}", ignore_errors=True)
                    except Exception as e:
                        log(f"Failed to restore module {module}: {e}", "error")
        
        bind_vt_consoles()
        run_command("systemctl isolate graphical.target", ignore_errors=True)
        
        log("Rollback complete. Check system state manually.", "warning")
        return True
        
    except (OSError, json.JSONDecodeError) as e:
        log(f"Rollback failed: {e}", "error")
        return False
    finally:
        if ROLLBACK_FILE.exists():
            try:
                ROLLBACK_FILE.unlink()
            except OSError:
                pass

# ==============================================================================
# SECTION 7: PROCESS & BIND MANAGEMENT
# ==============================================================================

def get_device_nodes_for_pci(pci_id):
    """
    Dynamically discovers all device nodes associated with a PCI device.
    """
    device_nodes = set()
    pci_path = SYSFS_PATHS['pci_devices'] / pci_id
    
    if not pci_path.exists():
        return device_nodes
    
    # DRM devices (graphics)
    drm_path = pci_path / "drm"
    if drm_path.exists():
        for item in drm_path.iterdir():
            dev_node = SYSFS_PATHS['drm_devices'] / item.name
            if dev_node.exists():
                device_nodes.add(str(dev_node))
                log(f"Found DRM device: {dev_node}", "debug")
    
    # Sound devices
    sound_path = pci_path / "sound"
    if sound_path.exists():
        for card in sound_path.iterdir():
            if card.is_dir():
                card_name = card.name
                snd_dev_path = SYSFS_PATHS['sound_devices']
                if snd_dev_path.exists():
                    for snd_dev in snd_dev_path.iterdir():
                        try:
                            dev_realpath = snd_dev.resolve()
                            if card_name in str(dev_realpath):
                                device_nodes.add(str(snd_dev))
                                log(f"Found sound device: {snd_dev}", "debug")
                        except (OSError, RuntimeError):
                            pass
    
    # Other subsystems
    for subsystem in ['video4linux', 'hwmon', 'input']:
        subsys_path = pci_path / subsystem
        if subsys_path.exists():
            for item in subsys_path.iterdir():
                if item.is_dir():
                    dev_node = Path(f"/dev/{item.name}")
                    if dev_node.exists():
                        device_nodes.add(str(dev_node))
                        log(f"Found {subsystem} device: {dev_node}", "debug")
    
    return device_nodes

def terminate_gpu_processes(driver_roots, devices):
    """
    Kills processes on devices associated with the configured PCI IDs.
    """
    log(f"Scanning for processes holding resources...")
    
    #testing if this works to prevent libvirt hook kernel oops
    if any("nvidia" in root for root in driver_roots):
        log("Stopping Nvidia helper services (persistenced, powerd) to prevent kernel panic...", "info")
        run_command("systemctl stop nvidia-persistenced nvidia-powerd nvidia-fabricmanager", ignore_errors=True)
        time.sleep(1) # Allow services to release locks
    
    targets = set()
    
    for dev in devices:
        pci_id = dev['id']
        nodes = get_device_nodes_for_pci(pci_id)
        targets.update(nodes)
    
    if any("nvidia" in d.get('driver', '').lower() for d in devices):
        log("Nvidia driver detected in config. Scanning for /dev/nvidia*...", "debug")
        for p in glob.glob("/dev/nvidia*"):
            targets.add(p)

    if not targets:
        return []

    cmd_targets = " ".join(list(targets))
    log(f"Killing processes on: {cmd_targets}")
    
    killed_info = run_command(f"fuser -v {cmd_targets}", ignore_errors=True)
    
    run_command(f"fuser -k -v -TERM {cmd_targets}", ignore_errors=True)
    time.sleep(1)
    run_command(f"fuser -k -v -KILL {cmd_targets}", ignore_errors=True)
    
    return [killed_info] if killed_info else []

def wait_for_condition(condition_func, timeout=5.0, retries=5, desc="operation"):
    """Retries a condition function with exponential backoff."""
    delay = 0.2
    start_time = time.time()
    
    for i in range(retries):
        if condition_func():
            return True
        
        elapsed = time.time() - start_time
        if elapsed > timeout:
            break
            
        time.sleep(delay)
        delay *= 2 
    
    return False

def unbind_device(pci_id, timeout, retries):
    """Unbinds a device."""
    driver = get_driver(pci_id)
    if not driver:
        log(f"{pci_id} is not bound to any driver.")
        return

    log(f"Unbinding {pci_id} from {driver}...")
    unbind_path = SYSFS_PATHS['pci_devices'] / pci_id / "driver" / "unbind"
    
    if unbind_path.exists():
        try:
            write_to_sysfs(unbind_path, pci_id, newline=True)
            
            success = wait_for_condition(
                lambda: get_driver(pci_id) is None,
                timeout=timeout,
                retries=retries,
                desc=f"unbind {pci_id}"
            )
            if not success:
                log(f"Timeout waiting for {pci_id} to unbind.", "warning")
        except OSError as e:
            log(f"Failed to unbind {pci_id}: {e}", "error")

def bind_device(pci_id, driver_name, timeout, retries):
    """
    Binds a device with fallback logic.
    """
    
    # 1. Driver Override Setup
    override_path = SYSFS_PATHS['pci_devices'] / pci_id / "driver_override"
    use_override = True
    
    if not override_path.exists():
        use_override = False
        log(f"driver_override not available for {pci_id}, will try fallback method.", "warning")

    if use_override:
        try:
            write_to_sysfs(override_path, driver_name, must_exist=True, newline=True)
        except OSError:
            use_override = False

    current_driver = get_driver(pci_id)
    if current_driver == driver_name:
        log(f"{pci_id} is already bound to {driver_name}.")
        if use_override:
            try:
                write_to_sysfs(override_path, "\n", must_exist=True, newline=False)
            except OSError:
                pass
        return

    log(f"Binding {pci_id} to {driver_name}...")
    
    bind_path = SYSFS_PATHS['pci_drivers'] / driver_name / "bind"
    if not bind_path.exists():
        log(f"Driver {driver_name} is not loaded or does not exist.", "error")
        return

    # 2. Generic ID Injection
    if not use_override:
        try:
            vendor_path = SYSFS_PATHS['pci_devices'] / pci_id / "vendor"
            device_path = SYSFS_PATHS['pci_devices'] / pci_id / "device"
            if vendor_path.exists() and device_path.exists():
                vendor = vendor_path.read_text().strip().replace("0x", "")
                device = device_path.read_text().strip().replace("0x", "")
                new_id_path = SYSFS_PATHS['pci_drivers'] / driver_name / "new_id"
                if new_id_path.exists():
                    try:
                        write_to_sysfs(new_id_path, f"{vendor} {device}", newline=True)
                    except OSError:
                        pass 
        except Exception as e:
            log(f"Failed to set new_id for {driver_name}: {e}", "error")

    # 3. Perform Bind
    try:
        write_to_sysfs(bind_path, pci_id, newline=True)
        success = wait_for_condition(
            lambda: get_driver(pci_id) == driver_name,
            timeout=timeout,
            retries=retries,
            desc=f"bind {pci_id}"
        )
        if not success:
             log(f"Timeout waiting for {pci_id} to bind to {driver_name}.", "warning")
    except OSError as e:
        log(f"Failed to bind {pci_id} to {driver_name}: {e}", "error")
    
    # 4. Cleanup Override
    if use_override:
        try:
            write_to_sysfs(override_path, "\n", must_exist=True, newline=False)
        except OSError as e:
            log(f"Warning: Failed to clear driver_override: {e}", "warning")


# ==============================================================================
# SECTION 8: CORE OPERATIONS (VM & HOST)
# ==============================================================================

def preflight_checks(devices, vm_driver):
    """
    FIX #1: Enhanced preflight checks with modprobe dry-run and sysfs validation.
    """
    log("Running pre-flight checks...")
    
    if os.geteuid() != 0:
        raise PermissionError("Script must be run as root.")

    # 1. IOMMU Check
    iommu_path = SYSFS_PATHS['iommu_groups']
    if not iommu_path.exists() or not any(iommu_path.iterdir()):
        log("CRITICAL: IOMMU does not appear to be enabled.", "error")
        raise EnvironmentError("IOMMU not detected.")

    # 2. Verify Modules (Dry Run)
    log(f"Verifying availability of module: {vm_driver}...")
    try:
        subprocess.run(f"modprobe --dry-run {vm_driver}", shell=True, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        log(f"CRITICAL: Module '{vm_driver}' not found in kernel modules.", "error")
        raise EnvironmentError(f"Module {vm_driver} missing")

    # 3. Verify Sysfs Integrity for Devices
    for dev in devices:
        pci_id = dev['id']
        dev_path = SYSFS_PATHS['pci_devices'] / pci_id
        
        if not dev_path.exists():
            log(f"CRITICAL: Device {pci_id} defined in config not found on bus.", "error")
            raise FileNotFoundError(f"Device {pci_id} missing")
        
        # Check specific binding capabilities
        driver_override = dev_path / "driver_override"
        if not driver_override.exists():
            log(f"WARNING: 'driver_override' missing for {pci_id}. Binding will rely on legacy 'new_id' which is less reliable.", "warning")
        elif not os.access(driver_override, os.W_OK):
             log(f"CRITICAL: 'driver_override' for {pci_id} is not writable.", "error")
             raise PermissionError(f"Cannot write to driver_override for {pci_id}")

    log("Pre-flight checks passed.")

def unbind_vt_consoles():
    """Dynamic VT console detection."""
    log("Unbinding VT consoles...")
    
    vtconsole_path = SYSFS_PATHS['vtconsole']
    if not vtconsole_path.exists():
        log("VT console path not found, skipping.", "warning")
        return
    
    for vtcon in sorted(vtconsole_path.iterdir()):
        if vtcon.is_dir() and vtcon.name.startswith("vtcon"):
            bind_path = vtcon / "bind"
            if bind_path.exists():
                try:
                    current_state = bind_path.read_text().strip()
                    if current_state == "1":
                        log(f"Unbinding {vtcon.name}...")
                        write_to_sysfs(bind_path, "0", must_exist=False, newline=True)
                except (OSError, ValueError):
                    log(f"Could not unbind {vtcon.name}", "warning")

def bind_vt_consoles():
    """Dynamic VT console detection."""
    log("Rebinding VT consoles...")
    
    vtconsole_path = SYSFS_PATHS['vtconsole']
    if not vtconsole_path.exists():
        log("VT console path not found, skipping.", "warning")
        return
    
    for vtcon in sorted(vtconsole_path.iterdir(), reverse=True):
        if vtcon.is_dir() and vtcon.name.startswith("vtcon"):
            bind_path = vtcon / "bind"
            if bind_path.exists():
                try:
                    current_state = bind_path.read_text().strip()
                    if current_state == "0":
                        log(f"Binding {vtcon.name}...")
                        write_to_sysfs(bind_path, "1", must_exist=False, newline=True)
                except (OSError, ValueError):
                    log(f"Could not bind {vtcon.name}", "warning")

def switch_to_vm(args, devices, config):
    """
    Switch to VM mode with new features:
    FIX #4: Implements single_gpu_passthrough check.
    FIX #2: Removes fragile udev blocking.
    """
    vm_driver = config.get("vm_driver", "vfio-pci")
    log(f">>> Switching to VM Mode ({vm_driver}) <<<")

    preflight_checks(devices, vm_driver)

    if ROLLBACK_FILE.exists():
        ROLLBACK_FILE.unlink()

    all_vm_bound = True
    for dev in devices:
        if get_driver(dev['id']) != vm_driver:
            all_vm_bound = False
            break

    if all_vm_bound:
        log(f"All devices already bound to {vm_driver}. Skipping setup.")
        # If already bound, we assume we are good.
        # But we still check single_gpu logic below.
    else:
        unique_drivers = list(set([d['driver'] for d in devices if d.get('driver')]))

        match_strategy = config.get("module_match_strategy", "prefix")
        log(f"Analyzing module tree for roots: {unique_drivers} (strategy: {match_strategy})...")
        modules_to_unload = get_modules_to_unload(unique_drivers, match_strategy)
        log(f"Modules targeted for removal (in dependency order): {modules_to_unload}")

        try:
            log("Isolating multi-user.target (stopping graphical session)...")
            run_command("systemctl isolate multi-user.target", ignore_errors=True)
            time.sleep(5)

            initial_state = {
                "devices": [{"id": d['id'], "driver": get_driver(d['id'])} for d in devices],
                "modules": list(get_loaded_modules_sysfs())
            }
            save_rollback_checkpoint("initial_state", initial_state)

            killed_procs = terminate_gpu_processes(unique_drivers, devices)

            # Wait for all graphical processes to fully release DRM master before
            # unbinding the GPU. This prevents stale DRM state from poisoning
            # the next host switch.
            log("Waiting for DRM devices to be released...")
            for _ in range(20):
                holders = run_command("fuser /dev/dri/card* 2>/dev/null || true", ignore_errors=True)
                if not holders or not holders.strip():
                    break
                time.sleep(0.5)

            unbind_vt_consoles()
            detach_efi_framebuffer()

            pre_unbind_state = {
                "devices": [{"id": d['id'], "driver": get_driver(d['id'])} for d in devices]
            }
            save_rollback_checkpoint("pre_unbind", pre_unbind_state)

            for dev in devices:
                unbind_device(dev['id'], args.timeout, args.retries)

            for dev in devices:
                trigger_device_reset(dev['id'])

            save_rollback_checkpoint("pre_module_unload", {"modules": modules_to_unload})

            module_params = {}
            if modules_to_unload:
                # FIX #2: Removed block_module_autoload (fragile udev rules removed)
                module_params = unload_modules_safe(modules_to_unload, managed_devices=devices, retries=args.retries)

            save_driver_state(devices, modules_to_unload, killed_procs, module_params)

            log(f"Loading {vm_driver} modules...")
            if vm_driver == "vfio-pci":
                run_command("modprobe vfio")
                run_command("modprobe vfio_iommu_type1")
            run_command(f"modprobe {vm_driver}")

            for dev in devices:
                bind_device(dev['id'], vm_driver, args.timeout, args.retries)

            # Wait for kernel device topology to stabilize after binding to VFIO.
            # This ensures the VFIO device nodes are fully created and permissioned
            # before the VM manager (libvirt/QEMU) attempts to open them.
            log("Waiting for device subsystem to settle...")
            run_command("udevadm settle --timeout=30", ignore_errors=True)

            log(f">>> Devices ready for VM passthrough ({vm_driver}).")

            if ROLLBACK_FILE.exists():
                ROLLBACK_FILE.unlink()

        except Exception as e:
            log(f"CRITICAL ERROR during VM switch: {e}", "error")
            log("Attempting rollback...", "warning")

            if execute_rollback():
                log("Rollback completed. System should be in previous state.", "warning")
            else:
                log("Rollback failed. Manual recovery required.", "error")
                bind_vt_consoles()
                run_command("systemctl isolate graphical.target", ignore_errors=True)

            sys.exit(1)

    # FIX #4: Single GPU Passthrough Logic
    if config.get("single_gpu_passthrough", False):
        log("Single GPU Passthrough enabled: Staying in isolated state (graphical target not started).")
    else:
        run_command("systemctl isolate graphical.target", ignore_errors=True)

def switch_to_host(args, devices, config):
    """
    Switch to Host Mode.
    FIX #2: Removed fragile udev unblocking.
    """
    log(">>> Switching to Host Mode <<<")

    # We pass 'true' as placeholder for vm_driver in preflight because we are switching AWAY from it,
    # but we still want to verify device paths.
    vm_driver = config.get("vm_driver", "vfio-pci")
    preflight_checks(devices, vm_driver)

    if ROLLBACK_FILE.exists():
        ROLLBACK_FILE.unlink()

    saved_state = load_driver_state()
    modules_to_restore = []
    module_params = {}
    target_map = {}

    if saved_state:
        log(f"Found saved state from {saved_state.get('timestamp')}")

        if saved_state.get("unloaded_modules"):
            modules_to_restore = saved_state["unloaded_modules"]
            modules_to_restore.reverse()

        if saved_state.get("module_parameters"):
            module_params = saved_state["module_parameters"]

        if saved_state.get("devices"):
            for dev_state in saved_state["devices"]:
                if dev_state.get("previous_driver") and dev_state["previous_driver"] != vm_driver:
                    target_map[dev_state["id"]] = dev_state["previous_driver"]
    else:
        log("No saved state found (or unreadable). Using config defaults.", "warning")

    for dev in devices:
        if dev['id'] not in target_map:
            target_map[dev['id']] = dev['driver']

    if not modules_to_restore:
        for dev in devices:
            if dev['driver'] not in modules_to_restore:
                modules_to_restore.append(dev['driver'])

    try:
        # Stop display manager cleanly before mutating hardware (DE-agnostic systemd alias)
        log("Stopping display manager...")
        run_command("systemctl stop display-manager", ignore_errors=True)
        time.sleep(2)

        log("Isolating multi-user.target...")
        run_command("systemctl isolate multi-user.target", ignore_errors=True)
        time.sleep(2)

        # Wait for any graphical sessions to fully release DRM master.
        # This is hardware-agnostic: it waits until no userspace process holds
        # any DRM device, ensuring KWin/Xorg/Mutter have fully exited before
        # the GPU is unbound. Prevents compositor crashes on re-login.
        log("Waiting for DRM devices to be released...")
        for _ in range(20):
            holders = run_command("fuser /dev/dri/card* 2>/dev/null || true", ignore_errors=True)
            if not holders or not holders.strip():
                break
            time.sleep(0.5)

        initial_state = {
            "devices": [{"id": d['id'], "driver": get_driver(d['id'])} for d in devices]
        }
        save_rollback_checkpoint("initial_state", initial_state)

        for dev in devices:
            unbind_device(dev['id'], args.timeout, args.retries)

        for dev in devices:
            trigger_device_reset(dev['id'])

        # Allow hardware to settle after reset before driver reload
        time.sleep(2)

        # FIX #2: Removed unblock_module_autoload call

        if modules_to_restore:
            log(f"Restoring modules: {modules_to_restore}")
            for mod in modules_to_restore:
                try:
                    params = module_params.get(mod, {})
                    if params:
                        param_parts = []
                        for k, v in params.items():
                            if is_parameter_restorable(k, v):
                                if " " in v or "=" in v:
                                    param_parts.append(f"{k}={shlex.quote(v)}")
                                else:
                                    param_parts.append(f"{k}={v}")

                        if param_parts:
                            param_str = " ".join(param_parts)
                            log(f"Restoring {mod} with parameters: {param_str}", "debug")
                            run_command(f"modprobe {mod} {param_str}")
                        else:
                            log(f"Restoring {mod} (no restorable parameters)", "debug")
                            run_command(f"modprobe {mod}")
                    else:
                        run_command(f"modprobe {mod}")
                except Exception as e:
                    log(f"Warning: Failed to restore module {mod}: {e}", "warning")
                    try:
                        log(f"Retrying {mod} without parameters...", "debug")
                        run_command(f"modprobe {mod}")
                    except Exception as e2:
                        log(f"Failed to load {mod} even without parameters: {e2}", "warning")

        for dev in devices:
            pci_id = dev['id']
            target_drv = target_map.get(pci_id)
            if target_drv:
                bind_device(pci_id, target_drv, args.timeout, args.retries)
            else:
                log(f"No target driver determined for {pci_id}, skipping bind.", "warning")

        # Wait for kernel device topology to stabilize and device nodes to be created.
        # This is hardware-agnostic and ensures the display manager does not start
        # before DRM nodes, permissions, and seat assignments are fully established.
        log("Waiting for device subsystem to settle...")
        run_command("udevadm settle --timeout=30", ignore_errors=True)

        bind_vt_consoles()
        time.sleep(1)

        # Trigger platform bus rescan to reattach any orphaned framebuffer drivers
        # that were detached during VM preparation (efifb, simpledrm, etc.).
        # This is hardware-agnostic: the kernel decides which drivers to reattach.
        platform_rescan = Path("/sys/bus/platform/rescan")
        if platform_rescan.exists():
            try:
                write_to_sysfs(platform_rescan, "1", must_exist=False, newline=True)
                time.sleep(1)
            except (OSError, FileNotFoundError):
                pass

        if any("nvidia" in str(d).lower() for d in target_map.values()):
            log("Enabling Nvidia Persistence Mode...")
            run_command("nvidia-smi -pm 1", ignore_errors=True)

        # Reset any failed state and start graphical target.
        # Using isolate ensures the entire graphical stack (not just the DM) is started.
        log("Starting graphical target...")
        run_command("systemctl reset-failed display-manager", ignore_errors=True)
        run_command("systemctl isolate graphical.target")
        time.sleep(3)

        # Detect the display manager's VT via systemd-logind and switch to it.
        # This fixes the black-screen-with-cursor issue where the DM starts on a
        # VT that is not currently active. Works for SDDM, GDM, PLM, LightDM, etc.
        log("Activating display manager console...")
        vt_switched = False
        for _ in range(20):  # Poll for up to 10 seconds
            try:
                sessions = run_command("loginctl list-sessions --no-legend", ignore_errors=True)
                if not sessions:
                    time.sleep(0.5)
                    continue

                for line in sessions.strip().split('\n'):
                    parts = line.split()
                    if not parts:
                        continue
                    session_id = parts[0]

                    session_info = run_command(
                        f"loginctl show-session {session_id} -p Type -p VTNr -p Class -p Service",
                        ignore_errors=True
                    )
                    if not session_info:
                        continue

                    is_dm = False
                    vt_num = None
                    for info_line in session_info.split('\n'):
                        if info_line.startswith("Class=greeter"):
                            is_dm = True
                        elif info_line.startswith("Service="):
                            svc = info_line.split("=")[-1].lower()
                            if any(x in svc for x in ["sddm", "gdm", "lightdm", "plasma", "display-manager", "login"]):
                                is_dm = True
                        elif info_line.startswith("VTNr="):
                            vt_num = info_line.split("=")[-1].strip()

                    if is_dm and vt_num and vt_num.isdigit():
                        current_vt_path = Path("/sys/class/tty/tty0/active")
                        current_vt = None
                        if current_vt_path.exists():
                            current_vt = current_vt_path.read_text().strip()

                        if current_vt != vt_num:
                            log(f"Switching to VT{vt_num} where display manager is active...")
                            run_command(f"chvt {vt_num}", ignore_errors=True)
                        vt_switched = True
                        break

                if vt_switched:
                    break

            except Exception as e:
                log(f"Error detecting DM VT: {e}", "debug")

            time.sleep(0.5)

        if not vt_switched:
            log("Could not detect display manager VT automatically.", "warning")

        if ROLLBACK_FILE.exists():
            ROLLBACK_FILE.unlink()

        log(">>> Host environment restored.")

    except Exception as e:
        log(f"CRITICAL ERROR during Host switch: {e}", "error")

        if execute_rollback():
            log("Rollback completed.", "warning")
        else:
            log("Rollback failed. Attempting basic recovery...", "error")
            bind_vt_consoles()

        sys.exit(1)

def show_status(devices, config):
    """Display current device status."""
    vm_driver = config.get("vm_driver", "vfio-pci")
    
    print(f"{'Device':<15} {'PCI ID':<15} {'Driver':<15} {'Name'}")
    print("-" * 80)
    
    for dev in devices:
        pci_id = dev['id']
        driver = get_driver(pci_id) or "Unbound"
        name = get_device_name(pci_id)
        print(f"{'Device':<15} {pci_id:<15} {driver:<15} {name}")
    
    print(f"\nConfigured VM Driver: {vm_driver}")
    print(f"Module Match Strategy: {config.get('module_match_strategy', 'prefix')}")
    print(f"Single GPU Passthrough: {config.get('single_gpu_passthrough', False)}")
    
    
def open_settings(config):
    """
    Opens the configuration file in the specified text editor.
    """
    editor = config.get("editor", "/usr/bin/nano")
    
    # Check if the configured editor exists
    if not shutil.which(editor):
        log(f"Configured editor '{editor}' not found. Falling back to nano.", "warning")
        editor = "nano"
        # Fallback for systems without nano
        if not shutil.which(editor):
             log("Nano not found. Falling back to vi.", "warning")
             editor = "vi"

    log(f"Opening config file with {editor}...", "info")
    
    try:
        # We use check=False because the editor exiting is normal behavior,
        # but we want to know if launching it failed completely.
        subprocess.run([editor, str(CONFIG_FILE)], check=True)
        print(f"\n[INFO] Editing complete. Run '{sys.argv[0]} status' to verify changes.")
    except subprocess.CalledProcessError as e:
        log(f"Editor exited with error: {e}", "error")
    except FileNotFoundError:
        log(f"Editor executable '{editor}' could not be launched.", "error")
    except Exception as e:
        log(f"Unexpected error launching editor: {e}", "error")


# ==============================================================================
# SECTION 9: ENTRY POINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Dynamically bind PCI devices to vfio-pci or host drivers using /etc/devicebinder.json.")
    parser.add_argument("mode", choices=["vm", "host", "status", "logs", "settings"], help="Target mode, status check, view logs, or change settings")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Timeout for bind/unbind operations in seconds.")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Number of retries for operations.")
    
    args = parser.parse_args()

    setup_logging()
    config = load_config()
    devices = config["devices"]

    if args.mode == "status":
        show_status(devices, config)
        sys.exit(0)
        
    if args.mode == "logs":
        if LOG_FILE.exists():
            try:
                print(f"--- Displaying contents of {LOG_FILE} ---")
                print(LOG_FILE.read_text(encoding='utf-8', errors='replace'))
                print("--- End of Log ---")
            except PermissionError:
                print(f"Error: Permission denied. You may need root privileges to read {LOG_FILE}", file=sys.stderr)
            except Exception as e:
                print(f"Error reading log file: {e}", file=sys.stderr)
        else:
            print(f"Log file not found at: {LOG_FILE}")
        sys.exit(0)
        
    if args.mode == "settings":
        open_settings(config)
        sys.exit(0)

    log(f"------------------------------------ Script started with mode: {args.mode} (Retries: {args.retries}, Timeout: {args.timeout}s) ------------------------------------")
    log(f"Loaded {len(devices)} devices from configuration.", "debug")

    if args.mode == "vm":
        switch_to_vm(args, devices, config)
    elif args.mode == "host":
        switch_to_host(args, devices, config)

if __name__ == "__main__":
    main()
