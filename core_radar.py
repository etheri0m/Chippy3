import json
import time
import multiprocessing
from valkey import Valkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import Detector, DetectorConfig
from log_config import get_logger

log = get_logger("Radar")

# Two separate physical radars, two separate serial IDs.
# The rear radar (337) is exclusively owned by core_joystick.py.
# This file only runs the front-facing radar.
FRONT_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0"

KEY_FRONT = "chippy:state:radar:front"


def radar_worker(port: str, state_key: str):
    # Re-init logger inside subprocess (loguru state doesn't fork cleanly)
    from log_config import get_logger as _get_logger
    wlog = _get_logger("Radar")

    r = Valkey(host='localhost', port=6379, decode_responses=True)
    client = a121.Client.open(serial_port=port)

    config = DetectorConfig(
        frame_rate=20.0,
        start_m=0.10,                  # Close range for hand tracking in FOLLOW
        end_m=2.0,                     # Front radar scans further for obstacle/crowd use
        intra_detection_threshold=4.0,
        inter_detection_threshold=3.0,
    )
    detector = Detector(client=client, sensor_id=1, detector_config=config)
    detector.start()

    wlog.info("Worker started — key: {}", state_key)

    try:
        while True:
            result = detector.get_next()
            detected = result.presence_detected

            r.set(state_key, json.dumps({
                "detected": detected,
                "dist":     result.presence_distance if detected else None,
                "intra":    round(result.intra_presence_score, 2),
                "inter":    round(result.inter_presence_score, 2),
                "ts":       round(time.time(), 4),
            }))

    except KeyboardInterrupt:
        pass
    finally:
        detector.stop()
        client.close()
        wlog.info("Worker stopped — {}", state_key)


if __name__ == "__main__":
    front = multiprocessing.Process(
        target=radar_worker,
        args=(FRONT_RADAR_PORT, KEY_FRONT),
        daemon=True,
    )
    front.start()
    log.info("Front radar process started")

    try:
        front.join()
    except KeyboardInterrupt:
        front.terminate()
        log.info("Shutdown")