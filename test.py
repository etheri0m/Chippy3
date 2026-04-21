"""
Test 90° head turn. Sets mode to MAZE first (ARMED state does nothing),
so the main loop won't fight our commands. Run WITH main.py running.
    uv run test_90.py
"""
import asyncio
import orjson
import valkey.asyncio as avalkey

# ─── TUNE THIS ────────────────────────────────────────
TURN_DURATION = 1.4    # seconds
TURN_SPEED    = 0.7     # 0.0 - 1.0
# ──────────────────────────────────────────────────────

KEY_MOTORS = "chippy:cmd:motors"


async def motor(r, target: str, direction: str, speed: float):
    await r.publish(KEY_MOTORS,
                    orjson.dumps({"target": target, "dir": direction,
                                  "speed": round(speed, 3)}).decode())


async def main():
    r = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)

    # Put controller in MAZE/ARMED — it publishes nothing while waiting
    print("Setting mode to MAZE (ARMED = silent)...")
    await r.set("chippy:mode", "MAZE")
    await asyncio.sleep(1.0)

    print(f"TURN_DURATION = {TURN_DURATION}s")
    print(f"TURN_SPEED    = {TURN_SPEED}")
    print("\nStarting in 2s...")
    await motor(r, "head", "stop", 0.0)
    await motor(r, "legs", "stop", 0.0)
    await asyncio.sleep(2.0)

    print("→ HEAD right (forward)")
    await motor(r, "head", "forward", TURN_SPEED)
    await asyncio.sleep(TURN_DURATION)
    await motor(r, "head", "stop", 0.0)

    print("Holding 2s — check the angle...")
    await asyncio.sleep(2.0)

    print("→ HEAD left (backward)")
    await motor(r, "head", "backward", TURN_SPEED)
    await asyncio.sleep(TURN_DURATION)
    await motor(r, "head", "stop", 0.0)

    print("\nDone. Setting mode back to IDLE.")
    await r.set("chippy:mode", "IDLE")
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())