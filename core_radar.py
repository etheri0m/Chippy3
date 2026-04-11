import time
import json
import multiprocessing
from valkey import Valkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import Detector, DetectorConfig

FRONT_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800352-if00-port0"
REAR_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0"


def radar_worker(port, state_key):
    # Connect Valkey
    r = Valkey(host='localhost', port=6379, decode_responses=True)

    # Setup A121 Client
    client = a121.Client.open(serial_port=port)

    # Setup Presence Detector (No update_rate parameter here)
    config = DetectorConfig(frame_rate=20.0)

    detector = Detector(client=client, sensor_id=1, detector_config=config)
    detector.start()

    try:
        while True:
            # Pull frame from sensor
            result = detector.get_next()

            # Extract metrics
            payload = {
                "distance": result.presence_distance,
                "intra": result.intra_presence_score
            }

            # Push to Valkey bus
            r.set(state_key, json.dumps(payload))

            # Anchor worker to 20Hz
            time.sleep(0.05)

    except KeyboardInterrupt:
        pass
    finally:
        detector.stop()
        client.close()


if __name__ == "__main__":
    front_process = multiprocessing.Process(
        target=radar_worker,
        args=(FRONT_RADAR_PORT, "chippy:state:radar:front")
    )
    rear_process = multiprocessing.Process(
        target=radar_worker,
        args=(REAR_RADAR_PORT, "chippy:state:radar:rear")
    )

    front_process.start()
    rear_process.start()

    front_process.join()
    rear_process.join()