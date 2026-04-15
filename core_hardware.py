import asyncio
import pigpio
import orjson
import valkey.asyncio as avalkey
from log_config import get_logger

log = get_logger("Hardware")

# PWM frequency for IN1/IN2 software PWM (pigpio set_PWM_dutycycle)
# Duty cycle range: 0–255 (pigpio default)
PWM_FREQ = 1000


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
        """Convert 0.0–1.0 speed to pigpio duty (0–255)."""
        return int(max(0.0, min(speed, 1.0)) * 255)

    def set_motor(self, target: str, direction: str, speed: float):
        if target == "legs":
            in1_pin, in2_pin = self.LEG_IN1, self.LEG_IN2
        elif target in ("head", "home"):
            in1_pin, in2_pin = self.HEAD_IN1, self.HEAD_IN2
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

    async def calibrate_head(self):
        vk = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)
        log.info("Starting head calibration...")

        if self.pi.read(self.HALL_PIN) == 0:
            log.info("Head on magnet — backing off...")
            self.set_motor("head", "backward", 0.8)
            deadline = asyncio.get_event_loop().time() + 5.0
            while self.pi.read(self.HALL_PIN) == 0 and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            self.set_motor("head", "stop", 0.0)
            await asyncio.sleep(0.5)

        log.info("Sweeping forward to find centre...")
        self.set_motor("head", "forward", 0.8)

        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            if self.pi.read(self.HALL_PIN) == 0:
                self.set_motor("head", "stop", 0.0)
                log.success("Head centred on magnet")
                await vk.set("chippy:state:head",
                             orjson.dumps({"calibrated": True, "position": 0}).decode())
                await vk.aclose()
                return
            await asyncio.sleep(0.01)

        self.set_motor("head", "stop", 0.0)
        await vk.set("chippy:state:head",
                      orjson.dumps({"calibrated": False, "position": None}).decode())
        await vk.aclose()
        log.warning("Calibration timeout — magnet not found. Check wiring.")

    async def run(self):
        await self.self_test()
        await self.calibrate_head()

        vk = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)
        pubsub = vk.pubsub()
        await pubsub.subscribe("chippy:cmd:motors")
        log.info("Listening on chippy:cmd:motors")

        try:
            async for message in pubsub.listen():
                if message['type'] != 'message':
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