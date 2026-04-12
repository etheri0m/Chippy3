import time
import json
import pigpio
import valkey


# hardware_PWM duty cycle is 0–1,000,000 (millionths, not 0–255)
# GPIO 12 = PWM channel 0,  GPIO 13 = PWM channel 1  (separate channels, no conflict)
PWM_FREQ = 1000   # Hz — good for toy DC motors. Lower = more torque at low speeds.


class HardwareNode:
    def __init__(self):
        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("pigpiod is not running. Run: sudo systemctl start pigpiod")

        self.vk = valkey.Valkey(host='localhost', port=6379, decode_responses=True)
        self.pubsub = self.vk.pubsub()

        # --- TB6612FNG PIN MAPPING ---
        self.STBY_PIN = 21

        self.LEG_PWM  = 12   # hardware PWM channel 0
        self.LEG_IN1  = 16
        self.LEG_IN2  = 20

        self.HEAD_PWM = 13   # hardware PWM channel 1
        self.HEAD_IN1 = 19
        self.HEAD_IN2 = 26

        self.HALL_PIN = 17
        # -----------------------------

        # Direction/logic pins — safe to set as plain outputs
        dir_pins = [
            self.STBY_PIN,
            self.LEG_IN1, self.LEG_IN2,
            self.HEAD_IN1, self.HEAD_IN2,
        ]
        for pin in dir_pins:
            self.pi.set_mode(pin, pigpio.OUTPUT)
            self.pi.write(pin, 0)

        # PWM pins — initialise via hardware_PWM (do NOT pre-set as OUTPUT).
        # GPIO 12/13 are hardware PWM pins. Using set_PWM_dutycycle (DMA) on
        # these conflicts with the Pi audio subsystem and produces no output.
        self.pi.hardware_PWM(self.LEG_PWM,  PWM_FREQ, 0)
        self.pi.hardware_PWM(self.HEAD_PWM, PWM_FREQ, 0)

        # Hall effect sensor
        self.pi.set_mode(self.HALL_PIN, pigpio.INPUT)
        self.pi.set_pull_up_down(self.HALL_PIN, pigpio.PUD_UP)

        # Wake the driver
        self.pi.write(self.STBY_PIN, 1)
        print("[Hardware] TB6612FNG awake (STBY HIGH).")

    # -------------------------------------------------------------------------

    def _pwm_duty(self, speed: float) -> int:
        """Convert 0.0–1.0 speed to hardware_PWM duty (0–1,000,000)."""
        return int(max(0.0, min(speed, 1.0)) * 1_000_000)

    def set_motor(self, target: str, direction: str, speed: float):
        if target == "legs":
            pwm_pin, in1_pin, in2_pin = self.LEG_PWM,  self.LEG_IN1,  self.LEG_IN2
        elif target in ("head", "home"):
            # "home" is a logical command from the joystick — physically it's the head motor
            pwm_pin, in1_pin, in2_pin = self.HEAD_PWM, self.HEAD_IN1, self.HEAD_IN2
        else:
            print(f"[Hardware] Unknown motor target: '{target}' — ignored.")
            return

        if direction == "stop" or speed <= 0.0:
            # Coast: IN1=0, IN2=0, PWM=0
            self.pi.write(in1_pin, 0)
            self.pi.write(in2_pin, 0)
            self.pi.hardware_PWM(pwm_pin, PWM_FREQ, 0)
            return

        if direction == "forward":
            self.pi.write(in1_pin, 1)
            self.pi.write(in2_pin, 0)
        elif direction == "backward":
            self.pi.write(in1_pin, 0)
            self.pi.write(in2_pin, 1)
        else:
            print(f"[Hardware] Unknown direction: '{direction}' — ignored.")
            return

        self.pi.hardware_PWM(pwm_pin, PWM_FREQ, self._pwm_duty(speed))

    # -------------------------------------------------------------------------

    def self_test(self):
        """
        Brief motor pulse on startup to confirm wiring before calibration.
        Each motor pulses forward for 0.3s then stops.
        If you see no movement here the problem is hardware (wiring/power), not code.
        """
        print("[Hardware] Self-test: pulsing LEG motor...")
        self.set_motor("legs", "forward", 0.6)
        time.sleep(0.3)
        self.set_motor("legs", "stop", 0.0)
        time.sleep(0.3)

        print("[Hardware] Self-test: pulsing HEAD motor...")
        self.set_motor("head", "forward", 0.6)
        time.sleep(0.3)
        self.set_motor("head", "stop", 0.0)
        time.sleep(0.3)

        print("[Hardware] Self-test complete.")

    def calibrate_head(self):
        print("[Hardware] Starting head calibration...")

        # If already on the magnet, back off first
        if self.pi.read(self.HALL_PIN) == 0:
            print("[Hardware] Head on magnet — backing off...")
            self.set_motor("head", "backward", 0.8)
            timeout = time.time() + 5.0
            while self.pi.read(self.HALL_PIN) == 0 and time.time() < timeout:
                time.sleep(0.01)
            time.sleep(0.2)
            self.set_motor("head", "stop", 0.0)
            time.sleep(0.5)

        # Sweep forward to find the magnet
        print("[Hardware] Sweeping forward to find centre...")
        self.set_motor("head", "forward", 0.8)

        timeout = time.time() + 10.0
        while time.time() < timeout:
            if self.pi.read(self.HALL_PIN) == 0:
                self.set_motor("head", "stop", 0.0)
                print("[Hardware] Head centred on magnet.")
                self.vk.set("chippy:state:head", json.dumps({"calibrated": True, "position": 0}))
                return
            time.sleep(0.01)

        # Timeout — stop and flag it so the joystick knows not to trust position
        self.set_motor("head", "stop", 0.0)
        self.vk.set("chippy:state:head", json.dumps({"calibrated": False, "position": None}))
        print("[Hardware] WARNING: Calibration timeout — magnet not found. Check wiring.")

    # -------------------------------------------------------------------------

    def run(self):
        self.self_test()
        self.calibrate_head()

        self.pubsub.subscribe("chippy:cmd:motors")
        print("[Hardware] Listening on chippy:cmd:motors ...")

        for message in self.pubsub.listen():
            if message['type'] != 'message':
                continue
            try:
                cmd = json.loads(message['data'])
                self.set_motor(cmd['target'], cmd['dir'], cmd['speed'])
                print(f"[Hardware] {cmd['target']:5} | {cmd['dir']:8} | spd {cmd['speed']:.2f}")
            except Exception as e:
                print(f"[Hardware] Bad command: {e} — raw: {message['data']}")


# -------------------------------------------------------------------------

if __name__ == "__main__":
    node = HardwareNode()
    try:
        node.run()
    except KeyboardInterrupt:
        pass
    finally:
        # Safe shutdown: brake off, driver asleep
        node.pi.write(node.STBY_PIN, 0)
        node.pi.hardware_PWM(node.LEG_PWM,  PWM_FREQ, 0)
        node.pi.hardware_PWM(node.HEAD_PWM, PWM_FREQ, 0)
        node.pi.write(node.LEG_IN1,  0)
        node.pi.write(node.LEG_IN2,  0)
        node.pi.write(node.HEAD_IN1, 0)
        node.pi.write(node.HEAD_IN2, 0)
        node.pi.stop()
        print("\n[Hardware] Shutdown complete. Driver asleep.")