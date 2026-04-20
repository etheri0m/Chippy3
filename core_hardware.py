import asyncio
import pigpio
import orjson
import valkey.asyncio as avalkey
from log_config import get_logger

log = get_logger("Hardware")

# Hardware PWM frequency on PWMA/PWMB (GPIO 13 and 12 — RPi hardware PWM pins).
# 25 kHz is above human hearing — eliminates motor whine.
# IN1/IN2 are now plain digital direction outputs (no PWM on them).
# Duty cycle range for hardware_PWM: 0–1_000_000.
PWM_FREQ = 25_000


class HardwareNode:
    def __init__(self):
        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("pigpiod is not running. Run: sudo systemctl start pigpiod")

        # --- TB6612FNG PIN MAPPING ---
        # Standard mode: IN1/IN2 set direction (digital), PWMA/PWMB set speed (HW PWM).
        # GPIO 12 (PWMB) and GPIO 13 (PWMA) are the RPi's hardware PWM pins — no rewiring needed.
        self.STBY_PIN = 21
        self.PWMA     = 13   # legs speed (hardware PWM)
        self.PWMB     = 12   # head speed (hardware PWM)

        # Legs motor (channel A) — digital direction
        self.LEG_IN1  = 16
        self.LEG_IN2  = 20

        # Head motor (channel B) — digital direction
        self.HEAD_IN1 = 19
        self.HEAD_IN2 = 26

        # Hall sensor is mounted on Chippy's RIGHT HIP.
        # Position 0 = head turned to face right (magnet over sensor).
        self.HALL_PIN = 17
        # track head motor direction for smart recalibration
        self._current_head_dir: str = "stop"
        # -----------------------------

        # STBY: plain output, start LOW (standby)
        self.pi.set_mode(self.STBY_PIN, pigpio.OUTPUT)
        self.pi.write(self.STBY_PIN, 0)

        # IN pins: plain digital direction outputs, start LOW
        for pin in [self.LEG_IN1, self.LEG_IN2, self.HEAD_IN1, self.HEAD_IN2]:
            self.pi.set_mode(pin, pigpio.OUTPUT)
            self.pi.write(pin, 0)

        # PWMA/PWMB: hardware PWM for speed, start silent
        # hardware_PWM(pin, frequency, dutycycle_0_to_1000000)
        self.pi.hardware_PWM(self.PWMA, PWM_FREQ, 0)
        self.pi.hardware_PWM(self.PWMB, PWM_FREQ, 0)

        # Hall effect sensor
        self.pi.set_mode(self.HALL_PIN, pigpio.INPUT)
        self.pi.set_pull_up_down(self.HALL_PIN, pigpio.PUD_UP)

        # Wake the driver
        self.pi.write(self.STBY_PIN, 1)
        log.info("TB6612FNG awake (STBY HIGH) — HW PWM on PWMA/PWMB at {}Hz", PWM_FREQ)

    def _duty(self, speed: float) -> int:
        """Convert 0.0–1.0 speed to pigpio hardware_PWM duty (0–1_000_000)."""
        return int(max(0.0, min(speed, 1.0)) * 1_000_000)

    def set_motor(self, target: str, direction: str, speed: float):
        if target == "legs":
            in1_pin, in2_pin = self.LEG_IN1, self.LEG_IN2
            pwm_pin = self.PWMA
        elif target in ("head", "home"):
            in1_pin, in2_pin = self.HEAD_IN1, self.HEAD_IN2
            pwm_pin = self.PWMB
            # keep direction tracker in sync for smart recalibration
            self._current_head_dir = direction if speed > 0.0 else "stop"
        else:
            log.warning("Unknown motor target: '{}' — ignored", target)
            return

        duty = self._duty(speed)

        if direction == "stop" or speed <= 0.0:
            # Cut speed first, then clear direction — clean stop, no brake glitch
            self.pi.hardware_PWM(pwm_pin, PWM_FREQ, 0)
            self.pi.write(in1_pin, 0)
            self.pi.write(in2_pin, 0)
        elif direction == "forward":
            self.pi.write(in1_pin, 1)
            self.pi.write(in2_pin, 0)
            self.pi.hardware_PWM(pwm_pin, PWM_FREQ, duty)
        elif direction == "backward":
            self.pi.write(in1_pin, 0)
            self.pi.write(in2_pin, 1)
            self.pi.hardware_PWM(pwm_pin, PWM_FREQ, duty)
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
        Background task: watch GPIO 17 (right-hip hall sensor) at 100Hz.
        Publishes:
          chippy:state:hall   — "1" if magnet currently over sensor, "0" otherwise
                                (head/upper-body in centered/forward position)
          chippy:state:head   — last_crossing_dir on each fresh magnet crossing
        Magnet on belt, sensor on right rib. Hall triggers when head is
        FORWARD (magnet aligned with sensor).
        """
        prev_high = True  # True = sensor was HIGH (no magnet) last tick
        while True:
            on_magnet = (self.pi.read(self.HALL_PIN) == 0)
            # Continuous state — used by maze for "is head centered?" checks
            await vk.set("chippy:state:hall", "1" if on_magnet else "0")
            # Crossing detection — only on edges, while head is moving
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
        Find position 0 (head pointing RIGHT, magnet over right-hip sensor).

        Strategy:
          - If last_crossing_dir is known → magnet is just behind us in that
            direction, so approach from the OPPOSITE side (short travel).
          - If already on magnet → back off briefly, then approach from opposite.
          - If unknown → default forward sweep until magnet found.

        After this returns True the head is sitting exactly at position 0
        (right-hip). Caller is responsible for any centering move if needed.
        """
        CALIB_SPEED   = 0.70   # ~70% — slow enough to stop cleanly
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

        vk = avalkey.Valkey(host='localhost', port=6379, decode_responses=True,
                           socket_timeout=None)

        # Start hall monitor — tracks crossing direction for safe recal
        asyncio.create_task(self._hall_monitor_task(vk))

       # await self.smart_calibrate_head(vk)

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
        self.pi.hardware_PWM(self.PWMA, PWM_FREQ, 0)
        self.pi.hardware_PWM(self.PWMB, PWM_FREQ, 0)
        self.pi.write(self.LEG_IN1,  0)
        self.pi.write(self.LEG_IN2,  0)
        self.pi.write(self.HEAD_IN1, 0)
        self.pi.write(self.HEAD_IN2, 0)
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