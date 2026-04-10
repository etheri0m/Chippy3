import time
import json
from valkey import Valkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import Detector, DetectorConfig

JOYSTICK_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0"

# --- TUNING PARAMETERS ---
INTRA_CLUTCH_THRESHOLD = 20.0
# 2. Adjust for smoothness vs lag. (0.1 = very smooth/laggy, 0.8 = jittery/responsive)
ALPHA = 0.3
DEBOUNCE_TIME = 1.0
MIN_DIST = 0.1
MAX_DIST = 0.5


def map_distance(dist):
    d = max(MIN_DIST, min(MAX_DIST, dist))
    mapped = ((d - MIN_DIST) / (MAX_DIST - MIN_DIST)) * 2.0 - 1.0
    return round(mapped, 2)


def run_joystick():
    r = Valkey(host='localhost', port=6379, decode_responses=True)

    client = a121.Client.open(serial_port=JOYSTICK_RADAR_PORT)
    config = DetectorConfig(start_m=0.1, end_m=0.6)
    detector = Detector(client=client, sensor_id=1, detector_config=config)
    detector.start()

    mode = "DRIVE"
    last_toggle_time = time.time()
    smoothed_dist = None

    try:
        while True:
            result = detector.get_next()

            raw_dist = result.presence_distance
            intra = result.intra_presence_score

            if raw_dist is None:
                # User removed hand. Reset filter and stop motors.
                smoothed_dist = None
                r.set('chippy:cmd:velocity', json.dumps({"v": 0.0, "w": 0.0}))
                time.sleep(0.05)
                continue

            # Apply Exponential Moving Average (EMA) Filter
            if smoothed_dist is None:
                smoothed_dist = raw_dist  # Initialize filter on first frame
            else:
                smoothed_dist = (ALPHA * raw_dist) + ((1.0 - ALPHA) * smoothed_dist)

            # Check for Clutch (Hand Shake)
            current_time = time.time()
            if intra > INTRA_CLUTCH_THRESHOLD and (current_time - last_toggle_time) > DEBOUNCE_TIME:
                mode = "STEER" if mode == "DRIVE" else "DRIVE"
                last_toggle_time = current_time
                print(f"\n--- MODE SWITCHED TO: {mode} ---\n")

            # Map the SMOOTHED distance to the motor command
            cmd_val = map_distance(smoothed_dist)

            if mode == "DRIVE":
                payload = {"v": cmd_val, "w": 0.0}
            else:
                payload = {"v": 0.0, "w": cmd_val}

            r.set('chippy:cmd:velocity', json.dumps(payload))

            print(
                f"Mode: {mode} | Raw: {raw_dist:.2f}m | Smooth: {smoothed_dist:.2f}m | Intra: {intra:.1f} | Cmd: {payload}")

            time.sleep(0.05)

    except KeyboardInterrupt:
        pass
    finally:
        detector.stop()
        client.close()
        r.set('chippy:cmd:velocity', json.dumps({"v": 0.0, "w": 0.0}))


if __name__ == "__main__":
    run_joystick()