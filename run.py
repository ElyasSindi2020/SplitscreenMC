#!/usr/bin/env python3
"""
Splitscreen Minecraft Launcher
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Launches a nested KWin Wayland compositor, then watches for DualSense
controllers connecting over Bluetooth. Each controller's MAC address maps to
a Minecraft instance. Up to 4 instances can run simultaneously. When all
instances exit the program kills KWin and exits cleanly.

Dependencies:
    pip install pyudev

Usage:
    python3 splitscreen.py

    !! Before running, fill in CONTROLLER_MAP below with your real MAC
    !! addresses. Run `bluetoothctl devices` to list paired devices.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import sys
import time
import signal
import logging
import threading
import subprocess
from pathlib import Path

# ── Dependency check ─────────────────────────────────────────────────────────
try:
    import pyudev
except ImportError:
    print("ERROR: pyudev is not installed.")
    print("Fix:   pip install pyudev")
    sys.exit(1)

# ═════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  ← edit this section
# ═════════════════════════════════════════════════════════════════════════════

# The Wayland socket name KWin will create for the nested compositor.
# Minecraft instances will connect to this display.
WAYLAND_DISPLAY = "wayland-1"

# The parent compositor's socket — the display KWin itself renders into.
# Your main KDE session is almost always wayland-0. Change if yours differs
# (run: echo $WAYLAND_DISPLAY in a terminal to confirm).
PARENT_WAYLAND_DISPLAY = "wayland-0"

# Directory that contains the portablemc executable.
PORTABLEMC_DIR = Path("/home/elyas/Desktop/splitscreen")

# Parent directory for per-instance game data folders.
GAMES_DIR = Path("/home/elyas/Desktop/splitscreen/games")

# Maximum simultaneous instances (hardware limit / your preference).
MAX_INSTANCES = 4

# ── Controller → instance mapping ────────────────────────────────────────────
# Keys are Bluetooth MAC addresses in lowercase with colons.
# To find your controller's MAC:
#   1. Pair it normally via KDE / bluetoothctl
#   2. Run:  bluetoothctl devices
#   3. Or:   ls /sys/class/bluetooth/ and check the connected device
#
# !! REPLACE the placeholder MACs below with your real ones !!
CONTROLLER_MAP: dict[str, dict[str, str]] = {
    "50:ee:32:56:fd:8a": {"name": "MC-Elyas", "user": "Elyas"},   # Controller 1
    "14:3a:9a:8d:52:73": {"name": "MC-Iz",    "user": "Iz"},       # Controller 2
    "14:3a:9a:d7:0d:5a": {"name": "MC-Misk",  "user": "Misk"},     # Controller 3
    "90:b6:85:ba:b5:2e": {"name": "MC-Awab",  "user": "Awab"},     # Controller 4
    "aa:bb:cc:dd:ee:05": {"name": "MC-Five",  "user": "Player5"},  # Controller 5 ← update
}

# ═════════════════════════════════════════════════════════════════════════════
#  INTERNALS  ← no need to edit below here
# ═════════════════════════════════════════════════════════════════════════════

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path.home() / "Desktop" / "splitscreen" / "launcher.log"),
    ],
)
log = logging.getLogger("splitscreen")

# ── Shared state ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_running: dict[str, subprocess.Popen] = {}   # mac → Popen
_kwin: subprocess.Popen | None = None
_shutdown_called = False


# ═══════════════════════════════════════════════════════════════════════════════
#  KWin
# ═══════════════════════════════════════════════════════════════════════════════

def launch_kwin() -> subprocess.Popen:
    """Start KWin nested with the specified config home and display socket."""
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = "/home/elyas/Desktop/splitscreen"
    env["WAYLAND_DISPLAY"]  = PARENT_WAYLAND_DISPLAY  # parent compositor KWin renders into

    cmd = ["kwin_wayland"]

    log.info("Starting KWin nested: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, env=env)
    log.info("KWin started — PID %d", proc.pid)
    return proc


def _kwin_socket_name(timeout: float = 20.0) -> str | None:
    """
    Wait for KWin to create a new Wayland socket in XDG_RUNTIME_DIR and
    return its name (e.g. 'wayland-1'). Returns None on timeout or if KWin dies.

    Strategy: snapshot existing wayland-* sockets before KWin starts, then
    watch for a new one to appear — that's the nested compositor's socket.
    """
    runtime_dir = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    )

    # Sockets look like "wayland-N" with an accompanying "wayland-N.lock"
    def _existing() -> set[str]:
        return {p.stem for p in runtime_dir.glob("wayland-*.lock")}

    before = _existing()
    log.info("Existing Wayland sockets before KWin: %s", before or "(none)")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _kwin is not None and _kwin.poll() is not None:
            log.error("KWin exited prematurely (code %d).", _kwin.returncode)
            return None

        after = _existing()
        new = after - before
        if new:
            socket_name = sorted(new)[0]   # take the first new one
            log.info("KWin created socket: %s — waiting 1 s for full init...", socket_name)
            time.sleep(1.0)
            if _kwin is not None and _kwin.poll() is not None:
                log.error("KWin exited during settle wait (code %d).", _kwin.returncode)
                return None
            log.info("KWin is ready on %s.", socket_name)
            return socket_name

        time.sleep(0.25)

    log.error("Timed out waiting for KWin to create a Wayland socket.")
    return None


def _kill_kwin():
    global _kwin
    if _kwin is None:
        return
    if _kwin.poll() is None:
        log.info("Terminating KWin (PID %d)...", _kwin.pid)
        _kwin.terminate()
        try:
            _kwin.wait(timeout=6)
        except subprocess.TimeoutExpired:
            log.warning("KWin didn't respond to SIGTERM — sending SIGKILL.")
            _kwin.kill()
    _kwin = None


# ═══════════════════════════════════════════════════════════════════════════════
#  Minecraft instances
# ═══════════════════════════════════════════════════════════════════════════════

def launch_instance(mac: str) -> bool:
    """
    Launch the Minecraft instance mapped to *mac*.
    Returns True if a new process was started, False otherwise.
    """
    with _lock:
        if mac in _running:
            log.info("Instance for %s is already running — ignoring.", mac)
            return False

        if len(_running) >= MAX_INSTANCES:
            log.warning(
                "Already at the %d-instance limit — cannot start instance for %s.",
                MAX_INSTANCES, mac,
            )
            return False

        config = CONTROLLER_MAP.get(mac)
        if config is None:
            log.warning("No instance configured for MAC %s.", mac)
            return False

        name = config["name"]
        user = config["user"]
        game_dir = GAMES_DIR / name

        cmd = [
            "env",
            f"WAYLAND_DISPLAY={WAYLAND_DISPLAY}",
            "./portablemc",
            "start",
            "fabric:26.2",
            "--main-dir", str(game_dir),
            "-u", user,
        ]

        env = os.environ.copy()

        log.info("Launching %s (user=%s, dir=%s)", name, user, game_dir)
        log.info("Command: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=str(PORTABLEMC_DIR), env=env)
        _running[mac] = proc
        log.info("%s started — PID %d", name, proc.pid)

    # Watcher runs outside the lock
    threading.Thread(
        target=_watch_instance,
        args=(mac,),
        name=f"watch-{mac}",
        daemon=True,
    ).start()
    return True


def _watch_instance(mac: str):
    """Wait for a Minecraft instance to exit, then clean up."""
    with _lock:
        proc = _running.get(mac)
    if proc is None:
        return

    proc.wait()                          # blocks until the process dies

    config = CONTROLLER_MAP.get(mac, {})
    name = config.get("name", mac)
    log.info("%s exited (return code %d).", name, proc.returncode)

    with _lock:
        _running.pop(mac, None)
        remaining = len(_running)

    log.info("%d instance(s) still running.", remaining)

    if remaining == 0:
        log.info("All instances have exited — initiating shutdown.")
        _shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
#  Shutdown
# ═══════════════════════════════════════════════════════════════════════════════

def _shutdown():
    global _shutdown_called
    with _lock:
        if _shutdown_called:
            return
        _shutdown_called = True

    log.info("Shutting down launcher...")

    # Terminate any still-running Minecraft instances
    with _lock:
        procs = list(_running.values())
    for proc in procs:
        if proc.poll() is None:
            log.info("Terminating PID %d...", proc.pid)
            proc.terminate()

    # Give them a moment before we kill KWin
    time.sleep(1)
    _kill_kwin()

    log.info("Launcher exited cleanly.")
    os._exit(0)


def _signal_handler(sig, _frame):
    log.info("Received signal %s — shutting down.", signal.Signals(sig).name)
    _shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
#  Controller detection (udev)
# ═══════════════════════════════════════════════════════════════════════════════

# DualSense vendor/product IDs
_DUALSENSE_VID = "054c"
_DUALSENSE_PIDS = {"0ce6", "0df2"}   # standard + Edge


def _is_dualsense(device: "pyudev.Device") -> bool:
    """Return True if this udev device looks like a DualSense controller."""
    props = device.properties
    vid = props.get("ID_VENDOR_ID", "").lower()
    pid = props.get("ID_MODEL_ID", "").lower()
    if vid == _DUALSENSE_VID and pid in _DUALSENSE_PIDS:
        return True
    # Fallback: human-readable name set by hid-sony (covers some BT stacks)
    name = props.get("NAME", "").lower()
    return "dualsense" in name


def _get_mac(device: "pyudev.Device") -> str | None:
    """
    Extract the Bluetooth MAC address from a udev input device.

    For uhid (Bluetooth) DualSense devices the MAC lives in the sysfs
    *attribute* file (device.attributes['uniq']), not in udev properties.
    We try both, then walk up the parent chain doing the same.
    """
    def _extract(dev: "pyudev.Device") -> str | None:
        # 1. sysfs attribute file  (most reliable for uhid/BT devices)
        try:
            val = dev.attributes.get("uniq")
            if val:
                mac = val.decode().lower().strip() if isinstance(val, bytes) else val.lower().strip()
                if _valid_mac(mac):
                    log.debug("MAC from sysfs attr on %s: %s", dev.sys_path, mac)
                    return mac
        except Exception:
            pass

        # 2. udev UNIQ property
        try:
            val = dev.properties.get("UNIQ", "")
            if val:
                mac = val.lower().strip()
                if _valid_mac(mac):
                    log.debug("MAC from udev prop on %s: %s", dev.sys_path, mac)
                    return mac
        except Exception:
            pass

        return None

    mac = _extract(device)
    if mac:
        return mac

    # Walk up the parent chain
    current = device.parent
    while current is not None:
        mac = _extract(current)
        if mac:
            return mac
        current = current.parent

    return None


def _valid_mac(mac: str) -> bool:
    """Quick sanity check: 17 chars, 5 colons."""
    return len(mac) == 17 and mac.count(":") == 5


def _scan_existing_controllers():
    """Check controllers that are already connected when we start up."""
    log.info("Scanning for already-connected DualSense controllers...")
    context = pyudev.Context()
    seen: set[str] = set()

    for device in context.list_devices(subsystem="input"):
        props = device.properties

        # Log every input device that has any Sony/DualSense hint so we can
        # see exactly what the kernel reports for the connected controller.
        vid  = props.get("ID_VENDOR_ID", "").lower()
        pid  = props.get("ID_MODEL_ID",  "").lower()
        name = props.get("NAME", "").lower()
        uniq = props.get("UNIQ", "").lower().strip()

        if vid == "054c" or "dualsense" in name or "wireless controller" in name or "sony" in name:
            log.debug(
                "Candidate device — path=%s  VID=%s  PID=%s  NAME=%s  UNIQ=%s",
                device.sys_path, vid, pid, name, uniq or "(none)",
            )

        if not _is_dualsense(device):
            continue

        mac = _get_mac(device)
        if mac is None:
            log.warning(
                "DualSense device found but could not read MAC — path=%s  UNIQ=%s",
                device.sys_path, uniq or "(none)",
            )
            continue
        if mac in seen:
            continue
        seen.add(mac)
        if mac in CONTROLLER_MAP:
            log.info("Found connected controller: %s", mac)
            launch_instance(mac)
        else:
            log.warning("DualSense connected but MAC %s not in CONTROLLER_MAP.", mac)


def _monitor_controllers():
    """
    Run forever in a daemon thread, reacting to udev add events for input
    devices. When a DualSense appears, look up its MAC and launch an instance.
    """
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="input")
    monitor.start()

    log.info("Controller monitor started — waiting for connections.")
    for device in iter(monitor.poll, None):
        if device.action != "add":
            continue
        if not _is_dualsense(device):
            continue

        mac = _get_mac(device)
        if mac is None:
            log.debug("DualSense add event but couldn't read MAC — skipping.")
            continue

        if mac not in CONTROLLER_MAP:
            log.warning(
                "DualSense connected with unrecognised MAC %s. "
                "Add it to CONTROLLER_MAP if you want it to launch an instance.",
                mac,
            )
            continue

        log.info("Controller connected: %s", mac)
        launch_instance(mac)


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global _kwin, WAYLAND_DISPLAY

    # Graceful shutdown on Ctrl-C / kill
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    log.info("══════════════════════════════════════════")
    log.info("  Splitscreen Minecraft Launcher")
    log.info("══════════════════════════════════════════")

    # 1. Start KWin nested
    _kwin = launch_kwin()

    # 2. Auto-detect which socket KWin created
    detected = _kwin_socket_name()
    if detected is None:
        log.error("KWin never became ready. Exiting.")
        _kill_kwin()
        sys.exit(1)

    WAYLAND_DISPLAY = detected
    log.info("Minecraft instances will use WAYLAND_DISPLAY=%s", WAYLAND_DISPLAY)

    # 3. Handle controllers already plugged in at launch
    _scan_existing_controllers()

    # 4. Background thread: watch for new controllers
    threading.Thread(
        target=_monitor_controllers,
        name="controller-monitor",
        daemon=True,
    ).start()

    # 5. Main thread blocks on KWin itself.
    #    If KWin crashes or is closed externally we also shut down.
    log.info("Monitoring KWin (PID %d) — launcher is live.", _kwin.pid)
    _kwin.wait()
    log.info("KWin exited (code %d).", _kwin.returncode)
    _shutdown()


if __name__ == "__main__":
    main()
