import subprocess
import sys
import time
import os

SCRIPTS = [
    ("Hardware",    "core_hardware.py"),
    ("Radar",       "core_radar.py"),
    ("Kinematics",  "core_kinematics.py"),
    ("Joystick",    "core_joystick.py"),
]

processes = []

def shutdown(sig=None, frame=None):
    print("\n[Run] Shutting down all processes...")
    for name, proc in processes:
        proc.terminate()
        print(f"[Run] Stopped {name}")
    sys.exit(0)

import signal
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

print("\n[Run] All systems up. Ctrl+C to stop everything.\n")

# Watch for any process dying unexpectedly
while True:
    for name, proc in processes:
        if proc.poll() is not None:
            print(f"[Run] WARNING: {name} died (exit {proc.returncode}) — restarting...")
            path = next(s for n, s in SCRIPTS if n == name)
            new_proc = subprocess.Popen(
                [sys.executable, path],
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            processes[processes.index((name, proc))] = (name, new_proc)
    time.sleep(2)