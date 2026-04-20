"""
Interactive turn tuner for Chippy.
Publishes velocity commands directly — no maze logic, no radar.
Run alongside the hardware stack (core_hardware + core_kinematics must be running):
    uv run test_turns.py

Keys:
    l  — turn LEFT  (w = -TURN_SPEED for TURN_DURATION)
    r  — turn RIGHT (w = +TURN_SPEED for TURN_DURATION)
    s  — scan left  (head only, legs stopped)
    d  — scan right (head only, legs stopped)
    f  — drive forward for DRIVE_DURATION then stop
    b  — drive backward for DRIVE_DURATION then stop
    +  — increase TURN_DURATION by 0.05s
    -  — decrease TURN_DURATION by 0.05s
    ]  — increase TURN_SPEED by 0.05
    [  — decrease TURN_SPEED by 0.05
    p  — print current settings
    q  — quit (sends stop first)
"""
import asyncio
import sys
import orjson
import valkey.asyncio as avalkey

KEY_VELOCITY = "chippy:cmd:velocity"

# ── Tunable settings ──────────────────────────────────────────────────────────
TURN_SPEED      = 0.80   # w for body turns
TURN_DURATION   = 0.50   # seconds — this is what you tune for 90°
SCAN_SPEED      = 0.70   # w for head scans
SCAN_DURATION   = 0.80   # seconds per scan sweep
DRIVE_SPEED     = 0.70   # v for forward/backward test
DRIVE_DURATION  = 0.50   # seconds of driving


async def pub(r, v: float, w: float):
    await r.publish(KEY_VELOCITY,
                    orjson.dumps({"v": round(v, 3), "w": round(w, 3)}).decode())


async def stop(r):
    await pub(r, 0.0, 0.0)


async def run_turn(r, direction: float, label: str):
    global TURN_SPEED, TURN_DURATION
    print(f"  {label} — speed={TURN_SPEED:.2f}  duration={TURN_DURATION:.2f}s")
    await pub(r, 0.0, direction * TURN_SPEED)
    await asyncio.sleep(TURN_DURATION)
    await stop(r)
    print(f"  done")


async def run_scan(r, direction: float, label: str):
    global SCAN_SPEED, SCAN_DURATION
    print(f"  {label} — speed={SCAN_SPEED:.2f}  duration={SCAN_DURATION:.2f}s")
    await pub(r, 0.0, direction * SCAN_SPEED)
    await asyncio.sleep(SCAN_DURATION)
    await stop(r)
    print(f"  done")


async def run_drive(r, direction: float, label: str):
    global DRIVE_SPEED, DRIVE_DURATION
    print(f"  {label} — speed={DRIVE_SPEED:.2f}  duration={DRIVE_DURATION:.2f}s")
    await pub(r, direction * DRIVE_SPEED, 0.0)
    await asyncio.sleep(DRIVE_DURATION)
    await stop(r)
    print(f"  done")


def print_settings():
    print(f"\n  ── Current settings ──────────────────")
    print(f"  TURN_SPEED     = {TURN_SPEED:.2f}")
    print(f"  TURN_DURATION  = {TURN_DURATION:.2f}s  ← main tuning knob for 90°")
    print(f"  SCAN_SPEED     = {SCAN_SPEED:.2f}")
    print(f"  SCAN_DURATION  = {SCAN_DURATION:.2f}s")
    print(f"  DRIVE_SPEED    = {DRIVE_SPEED:.2f}")
    print(f"  DRIVE_DURATION = {DRIVE_DURATION:.2f}s")
    print(f"  ──────────────────────────────────────\n")


async def main():
    global TURN_SPEED, TURN_DURATION, SCAN_SPEED, SCAN_DURATION

    r = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)
    await stop(r)

    print(__doc__)
    print_settings()

    # Put stdin in raw mode so we get keypresses without Enter
    import tty, termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)

            if ch == 'q':
                await stop(r)
                break
            elif ch == 'l':
                await run_turn(r, -1.0, "LEFT turn")
            elif ch == 'r':
                await run_turn(r, +1.0, "RIGHT turn")
            elif ch == 's':
                await run_scan(r, -1.0, "Scan LEFT")
            elif ch == 'd':
                await run_scan(r, +1.0, "Scan RIGHT")
            elif ch == 'f':
                await run_drive(r, +1.0, "Forward")
            elif ch == 'b':
                await run_drive(r, -1.0, "Backward")
            elif ch == '+':
                TURN_DURATION = round(TURN_DURATION + 0.05, 2)
                print(f"  TURN_DURATION → {TURN_DURATION:.2f}s")
            elif ch == '-':
                TURN_DURATION = round(max(0.05, TURN_DURATION - 0.05), 2)
                print(f"  TURN_DURATION → {TURN_DURATION:.2f}s")
            elif ch == ']':
                TURN_SPEED = round(min(1.0, TURN_SPEED + 0.05), 2)
                print(f"  TURN_SPEED → {TURN_SPEED:.2f}")
            elif ch == '[':
                TURN_SPEED = round(max(0.1, TURN_SPEED - 0.05), 2)
                print(f"  TURN_SPEED → {TURN_SPEED:.2f}")
            elif ch == 'p':
                print_settings()

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        await stop(r)
        await r.aclose()
        print("\nStopped.")


if __name__ == "__main__":
    asyncio.run(main())