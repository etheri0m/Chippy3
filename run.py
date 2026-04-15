import subprocess
import sys
import time
import os
import signal
from log_config import get_logger

log = get_logger("Run")

# Set SKIP_HARDWARE=1 to run without core_hardware.py (no motors)
SKIP_HARDWARE = os.environ.get("SKIP_HARDWARE", "0") == "1"

SCRIPTS = []

if not SKIP_HARDWARE:
    SCRIPTS.append(("Hardware",    "core_hardware.py"))

SCRIPTS += [
    ("Radar",       "core_radar.py"),
    ("Kinematics",  "core_kinematics.py"),
    ("Controller",  "core_joystick.py"),
    ("Dashboard",   "main.py"),
]

processes = []

def shutdown(sig=None, frame=None):
    log.info("Shutting down all processes...")
    for name, proc in processes:
        proc.terminate()
        log.info("Stopped {}", name)
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

# Launch in order with a short delay between each
for name, script in SCRIPTS:
    path = os.path.join(os.path.dirname(__file__), script)
    proc = subprocess.Popen(
        [sys.executable, path],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    processes.append((name, proc))
    log.info("Started {} (pid {})", name, proc.pid)

    # Hardware needs time to calibrate before anything else starts
    if name == "Hardware":
        log.info("Waiting 12s for hardware calibration...")
        time.sleep(12)
    else:
        time.sleep(1)

if SKIP_HARDWARE:
    log.warning("Hardware SKIPPED (no motors)")

log.success("All systems up. Ctrl+C to stop everything.")

# Watch for any process dying unexpectedly
while True:
    for i, (name, proc) in enumerate(processes):
        if proc.poll() is not None:
            log.warning("{} died (exit {}) — restarting...", name, proc.returncode)
            path = os.path.join(os.path.dirname(__file__),
                                next(s for n, s in SCRIPTS if n == name))
            new_proc = subprocess.Popen(
                [sys.executable, path],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            processes[i] = (name, new_proc)
    time.sleep(2)