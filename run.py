import subprocess
import sys
import time
import os
import signal

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
    print("\n[Run] Shutting down all processes...")
    for name, proc in processes:
        proc.terminate()
        print(f"[Run] Stopped {name}")
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
    print(f"[Run] Started {name} (pid {proc.pid})")

    # Hardware needs time to calibrate before anything else starts
    if name == "Hardware":
        print("[Run] Waiting 12s for hardware calibration...")
        time.sleep(12)
    else:
        time.sleep(1)

if SKIP_HARDWARE:
    print("[Run] Hardware SKIPPED (no motors).")

print("\n[Run] All systems up. Ctrl+C to stop everything.\n")

# Watch for any process dying unexpectedly
while True:
    for i, (name, proc) in enumerate(processes):
        if proc.poll() is not None:
            print(f"[Run] WARNING: {name} died (exit {proc.returncode}) — restarting...")
            path = os.path.join(os.path.dirname(__file__),
                                next(s for n, s in SCRIPTS if n == name))
            new_proc = subprocess.Popen(
                [sys.executable, path],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            processes[i] = (name, new_proc)
    time.sleep(2)