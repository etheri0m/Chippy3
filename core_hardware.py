import time
import json
import pigpio
import valkey


class HardwareNode:
    def __init__(self):
        self.pi = pigpio.pi()
        if not self.pi.connected:
            raise RuntimeError("pigpiod is not running. Run: sudo systemctl start pigpiod")

        self.vk = valkey.Valkey(host='localhost', port=6379, decode_responses=True)
        self.pubsub = self.vk.pubsub()

        # --- UPDATE THESE PINS TO MATCH YOUR WIRING ---
        self.LEG_PWM = 12
        self.LEG_DIR = 16

        self.HEAD_PWM = 13
        self.HEAD_DIR = 19

        self.HALL_PIN = 17
        # ----------------------------------------------

        # Initialize Motor Pins
        for pin in [self.LEG_PWM, self.LEG_DIR, self.HEAD_PWM, self.HEAD_DIR]:
            self.pi.set_mode(pin, pigpio.OUTPUT)
            self.pi.write(pin, 0)

        # Initialize Hall Sensor Pin
        self.pi.set_mode(self.HALL_PIN, pigpio.INPUT)
        self.pi.set_pull_up_down(self.HALL_PIN, pigpio.PUD_UP)

    def set_motor(self, target, direction, speed):
        pwm_pin = self.LEG_PWM if target == "legs" else self.HEAD_PWM
        dir_pin = self.LEG_DIR if target == "legs" else self.HEAD_DIR

        if direction == "stop" or speed == 0.0:
            self.pi.set_PWM_dutycycle(pwm_pin, 0)
            return

        dir_val = 1 if direction == "forward" else 0
        self.pi.write(dir_pin, dir_val)

        duty = int(max(0.0, min(speed, 1.0)) * 255)
        self.pi.set_PWM_dutycycle(pwm_pin, duty)

    def calibrate_head(self):
        print("[Hardware] Starting head calibration...")

        # If already on the magnet, back off first
        if self.pi.read(self.HALL_PIN) == 0:
            print("[Hardware] Head on magnet. Backing off...")
            self.set_motor("head", "backward", 0.8)

            # Spin backward until the sensor reads 1 (off magnet)
            while self.pi.read(self.HALL_PIN) == 0:
                time.sleep(0.01)

            # Add a tiny delay to ensure it fully clears the magnetic field
            time.sleep(0.2)
            self.set_motor("head", "stop", 0.0)
            time.sleep(0.5)

        # Sweep forward to find the exact center
        print("[Hardware] Sweeping forward to find center...")
        self.set_motor("head", "forward", 0.8)

        timeout = time.time() + 10.0
        while time.time() < timeout:
            if self.pi.read(self.HALL_PIN) == 0:
                self.set_motor("head", "stop", 0.0)
                print("[Hardware] Head centered.")
                self.vk.set("chippy:state:head", json.dumps({"calibrated": True, "position": 0}))
                return
            time.sleep(0.01)

        self.set_motor("head", "stop", 0.0)
        print("[Hardware] Calibration timeout. Magnet not found.")

    def run(self):
        self.calibrate_head()

        self.pubsub.subscribe("chippy:cmd:motors")
        print("[Hardware] Listening for motor commands on Valkey...")

        for message in self.pubsub.listen():
            if message['type'] == 'message':
                try:
                    cmd = json.loads(message['data'])
                    self.set_motor(cmd['target'], cmd['dir'], cmd['speed'])
                except Exception as e:
                    print(f"[Hardware] Error parsing command: {e}")


if __name__ == "__main__":
    node = HardwareNode()
    try:
        node.run()
    except KeyboardInterrupt:
        node.pi.stop()