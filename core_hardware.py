import asyncio
import pigpio
import orjson
import valkey.asyncio as avalkey
from log_config import get_logger

log = get_logger("Hardware")

# PWM frequency for IN1/IN2 software PWM (pigpio set_PWM_dutycycle)
# Duty cycle range: 0–255 (pigpio default)
PWM_FREQ = 1000

# ── DEMO CAP ── remove after firm demo ─────────────────────────────────────
DEMO_SPEED_CAP = 0.30   # clamps all motors to 30% max
# ───────────────────────────────────────────────────────────────────────────


class HardwareNode:
    def __init__(self):
        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("pigpiod is not running. Run: sudo systemctl start pigpiod")

        # --- TB6612FNG PIN MAPPING ---
        # PWMA/PWMB are wired to 3.3V (permanently HIGH).
        # Speed control via PWM on IN1/IN2 pins instead.
        self.STBY_PIN = 21

        # Legs motor (channel A) — PWM on IN pins
        self.LEG_IN1  = 16
        self.LEG_IN2  = 20

        # Head motor (channel B) — PWM on IN pins
        self.HEAD_IN1 = 19
        self.HEAD_IN2 = 26

        self.HALL_PIN = 17
        # track head motor direction for smart recalibration
        self._current_head_dir: str = "stop"
        # -----------------------------

        # STBY as plain output
        self.pi.set_mode(self.STBY_PIN, pigpio.OUTPUT)
        self.pi.write(self.STBY_PIN, 0)

        # IN pins: set PWM frequency and initialise duty to 0
        in_pins = [self.LEG_IN1, self.LEG_IN2, self.HEAD_IN1, self.HEAD_IN2]
        for pin in in_pins:
            self.pi.set_mode(pin, pigpio.OUTPUT)
            self.pi.set_PWM_frequency(pin, PWM_FREQ)
            self.pi.set_PWM_dutycycle(pin, 0)

        # Hall effect sensor
        self.pi.set_mode(self.HALL_PIN, pigpio.INPUT)
        self.pi.set_pull_up_down(self.HALL_PIN, pigpio.PUD_UP)

        # Wake the driver
        self.pi.write(self.STBY_PIN, 1)
        log.info("TB6612FNG awake (STBY HIGH) — PWM on IN1/IN2")

    def _duty(self, speed: float) -> int:
        """Convert 0.0–1.0 speed to pigpio duty (0–255), capped at DEMO_SPEED_CAP."""
        capped = min(speed, DEMO_SPEED_CAP)
        return int(max(0.0, min(capped, 1.0)) * 255)

    def set_motor(self, target: str, direction: str, speed: float):
        if target == "legs":
            in1_pin, in2_pin = self.LEG_IN1, self.LEG_IN2
        elif target in ("head", "home"):
            in1_pin, in2_pin = self.HEAD_IN1, self.HEAD_IN2
            # keep direction tracker in sync for smart recalibration
            self._current_head_dir = direction if speed > 0.0 else "stop"
        else:
            log.warning("Unknown motor target: '{}' — ignored", target)
            return

        duty = self._duty(speed)

        if direction == "stop" or speed <= 0.0:
            self.pi.set_PWM_dutycycle(in1_pin, 0)
            self.pi.set_PWM_dutycycle(in2_pin, 0)
        elif direction == "forward":
            self.pi.set_PWM_dutycycle(in1_pin, duty)
            self.pi.set_PWM_dutycycle(in2_pin, 0)
        elif direction == "backward":
            self.pi.set_PWM_dutycycle(in1_pin, 0)
            self.pi.set_PWM_dutycycle(in2_pin, duty)
        else:
            log.warning("Unknown direction: '{}' — ignored", direction)
            return

    async def self_test(self):
        log.info("Self-test: pulsing LEG motor...")
        self.set_motor("legs", "forward", 0.6)
        await asyncio.sleep(0.3)
        self.set_motor("legs", "stop", 0.0)
        await asyncio.sleep(0.3)

        log.info("Self-test: pulsing HEAD motor...")
        self.set_motor("head", "forward", 0.6)
        await asyncio.sleep(0.3)
        self.set_motor("head", "stop", 0.0)
        await asyncio.sleep(0.3)

        log.info("Self-test complete")

    async def _hall_monitor_task(self, vk) -> None:
        """
        Background task: watch GPIO 17 at 100Hz.
        On HIGH→LOW transition (head arriving at magnet) while head is moving,
        record the current direction as last_crossing_dir in chippy:state:head.
        This is what makes smart recalibration safe — no full rotation needed.
        """
        prev_high = True  # True = sensor was HIGH (no magnet) last tick
        while True:
            on_magnet = (self.pi.read(self.HALL_PIN) == 0)
            if on_magnet and prev_high and self._current_head_dir != "stop":
                raw = await vk.get("chippy:state:head")
                state = orjson.loads(raw) if raw else {}
                state["last_crossing_dir"] = self._current_head_dir
                state["ts"] = asyncio.get_event_loop().time()
                await vk.set("chippy:state:head", orjson.dumps(state).decode())
                log.debug("Hall crossing — dir: {}", self._current_head_dir)
            prev_high = not on_magnet
            await asyncio.sleep(0.01)  # 100Hz

    async def smart_calibrate_head(self, vk) -> bool:
        """
        Approach the magnet from the closest side using last_crossing_dir.

        If last_crossing_dir is known:
          - The magnet is directly behind us in that direction
          - So we go OPPOSITE — short travel, no cable-wrap risk
        If on magnet already:
          - Back off briefly in last_crossing_dir, then approach from opposite
        If unknown:
          - Fall back to forward sweep (same as old calibrate_head)
        """
        CALIB_SPEED   = 0.55   # ~55% — slow enough to stop cleanly (capped to 30% by _duty anyway in demo)
        CALIB_TIMEOUT = 4.0
        BACKOFF_SECS  = 0.25

        raw = await vk.get("chippy:state:head")
        head_state = orjson.loads(raw) if raw else {}
        last_dir: str | None = head_state.get("last_crossing_dir")

        log.info("Smart recal — last_crossing_dir: {}", last_dir)

        # Already sitting on magnet — back off first
        if self.pi.read(self.HALL_PIN) == 0:
            backoff = last_dir if last_dir else "forward"
            log.info("On magnet — backing off via: {}", backoff)
            self.set_motor("head", backoff, CALIB_SPEED)
            await asyncio.sleep(BACKOFF_SECS)
            self.set_motor("head", "stop", 0.0)
            await asyncio.sleep(0.05)

        # Approach = opposite of last crossing (magnet is behind us in last_dir)
        if last_dir == "forward":
            approach = "backward"
        elif last_dir == "backward":
            approach = "forward"
        else:
            approach = "forward"  # unknown — safe default
            log.warning("No crossing history — defaulting to forward sweep")

        log.info("Approaching from: {}", approach)
        self.set_motor("head", approach, CALIB_SPEED)
        deadline = asyncio.get_event_loop().time() + CALIB_TIMEOUT

        while asyncio.get_event_loop().time() < deadline:
            if self.pi.read(self.HALL_PIN) == 0:
                self.set_motor("head", "stop", 0.0)
                await vk.set("chippy:state:head", orjson.dumps({
                    "calibrated":        True,
                    "position":          0,
                    "last_crossing_dir": approach,
                    "ts":                asyncio.get_event_loop().time(),
                }).decode())
                log.success("Smart recal complete — approach: {}", approach)
                return True
            await asyncio.sleep(0.01)

        self.set_motor("head", "stop", 0.0)
        await vk.set("chippy:state:head", orjson.dumps({
            "calibrated":        False,
            "position":          None,
            "last_crossing_dir": last_dir,
        }).decode())
        log.warning("Smart recal timeout — magnet not found. last_dir was: {}", last_dir)
        return False

    async def run(self):
        await self.self_test()

        vk = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)

        # Start hall monitor — tracks crossing direction for safe recal
        asyncio.create_task(self._hall_monitor_task(vk))

        await self.smart_calibrate_head(vk)

        pubsub = vk.pubsub()
        await pubsub.subscribe("chippy:cmd:motors", "chippy:cmd:calibrate")
        log.info("Listening on chippy:cmd:motors + chippy:cmd:calibrate")

        try:
            async for message in pubsub.listen():
                if message['type'] != 'message':
                    continue

                # Recalibration trigger from controller (e.g. after CROWD exits)
                if message['channel'] == 'chippy:cmd:calibrate':
                    log.info("Recalibration requested")
                    await self.smart_calibrate_head(vk)
                    continue

                try:
                    cmd = orjson.loads(message['data'])
                    self.set_motor(cmd['target'], cmd['dir'], cmd['speed'])
                    log.debug("{:5} | {:8} | spd {:.2f}", cmd['target'], cmd['dir'], cmd['speed'])
                except Exception as e:
                    log.error("Bad command: {} — raw: {}", e, message['data'])
        finally:
            await pubsub.unsubscribe()
            await vk.aclose()

    def shutdown(self):
        self.pi.set_PWM_dutycycle(self.LEG_IN1,  0)
        self.pi.set_PWM_dutycycle(self.LEG_IN2,  0)
        self.pi.set_PWM_dutycycle(self.HEAD_IN1, 0)
        self.pi.set_PWM_dutycycle(self.HEAD_IN2, 0)
        self.pi.write(self.STBY_PIN, 0)
        self.pi.stop()
        log.info("Shutdown complete. Driver asleep.")


if __name__ == "__main__":
    node = HardwareNode()
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()