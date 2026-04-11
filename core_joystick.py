import time
import json
from valkey import Valkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import Detector, DetectorConfig

JOYSTICK_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0"

MIN_DIST = 0.1
MAX_DIST = 0.5
MAX_SWEEP_SEC = 0.6  # MAX SECONDS THE MOTOR CAN DRIVE BEFORE SOFTWARE CUTS POWER

KEY_MODE = 'chippy:mode'
KEY_MOTORS = 'chippy:cmd:motors'
KEY_RADAR = 'chippy:state:radar:joystick'

MODE_STEER = "STEER"


def publish_motor_cmd(v_client, target, direction, speed=0.8):
    payload = {"target": target, "dir": direction, "speed": speed}
    v_client.publish(KEY_MOTORS, json.dumps(payload))


def run_joystick():
    r = Valkey(host='localhost', port=6379, decode_responses=True)
    if not r.exists(KEY_MODE):
        r.set(KEY_MODE, MODE_STEER)

    client = a121.Client.open(serial_port=JOYSTICK_RADAR_PORT)
    config = DetectorConfig(
        start_m=0.15,  # Pushed out from 0.1m to clear chassis reflections
        end_m=0.6,
        frame_rate=20.0,
        intra_detection_threshold=4.0,  # Default is ~1.5. Kills fast ghosting.
        inter_detection_threshold=3.0  # Kills slow background ghosting.
    )
    detector = Detector(client=client, sensor_id=1, detector_config=config)
    detector.start()

    print("Virtual Encoder Joystick running.")

    # --- Time-Based Tracking Variables ---
    head_pos_sec = 0.0
    current_dir = "stop"
    is_homed = True
    last_loop_time = time.time()

    try:
        while True:
            now = time.time()
            dt = now - last_loop_time
            last_loop_time = now

            result = detector.get_next()
            raw_dist = result.presence_distance
            mode = r.get(KEY_MODE) or MODE_STEER

            # 1. Update virtual position based on what the motor is currently doing
            if current_dir == "forward":
                head_pos_sec += dt
            elif current_dir == "backward":
                head_pos_sec -= dt

            # Clamp the software tracker to limits
            head_pos_sec = max(-MAX_SWEEP_SEC, min(MAX_SWEEP_SEC, head_pos_sec))

            if mode == MODE_STEER:
                if raw_dist is None:
                    target_pos = 0.0  # Drive back to center

                    # If we are close to center, hit the real magnet to zero drift
                    if abs(head_pos_sec) < 0.1 and not is_homed:
                        publish_motor_cmd(r, "home", "stop", 0.0)
                        is_homed = True
                        head_pos_sec = 0.0
                        current_dir = "stop"
                        print("[STEER] Hand lost. Homing to magnet.")
                        continue
                else:
                    is_homed = False
                    # Map 0.1m -> +MAX_SWEEP (up), 0.5m -> -MAX_SWEEP (down)
                    ratio = (raw_dist - MIN_DIST) / (MAX_DIST - MIN_DIST)
                    ratio = max(0.0, min(1.0, ratio))
                    target_pos = MAX_SWEEP_SEC - (ratio * (MAX_SWEEP_SEC * 2))

                error = target_pos - head_pos_sec

                # 2. Deadband proportional controller
                if abs(error) < 0.08:
                    cmd_dir = "stop"
                elif error > 0:
                    cmd_dir = "forward"
                else:
                    cmd_dir = "backward"

                # 3. Virtual Hard Stops (Cut power before physical stall)
                if cmd_dir == "forward" and head_pos_sec >= MAX_SWEEP_SEC:
                    cmd_dir = "stop"
                if cmd_dir == "backward" and head_pos_sec <= -MAX_SWEEP_SEC:
                    cmd_dir = "stop"

                # 4. Dispatch only on state change
                if cmd_dir != current_dir and not is_homed:
                    current_dir = cmd_dir
                    speed = 0.8 if cmd_dir != "stop" else 0.0
                    publish_motor_cmd(r, "head", cmd_dir, speed)

                print(
                    f"Dist: {raw_dist or 'None':>7} | Virtual Pos: {head_pos_sec:+.2f}s | Target: {target_pos:+.2f}s | Motor: {current_dir}")

    except KeyboardInterrupt:
        pass
    finally:
        try:
            detector.stop()
            client.close()
        except Exception:
            pass
        publish_motor_cmd(r, "head", "stop", 0.0)
        print("Stopped. Motors zeroed.")


if __name__ == "__main__":
    run_joystick()