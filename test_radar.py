"""
Verify the raw sweep wall detection before deploying core_radar.py.
Point the radar at a wall at various distances and run:
    uv run test_radar.py
"""
import time
import numpy as np
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.distance import Detector, DetectorConfig

PORT    = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0"
THRESH  = 60.0

config = DetectorConfig(
    start_m=0.05,
    end_m=1.0,
    max_profile=a121.Profile.PROFILE_1,
    close_range_leakage_cancellation=True,
)
client   = a121.Client.open(serial_port=PORT)
detector = Detector(client=client, sensor_ids=[1], detector_config=config)
detector.calibrate_detector()
detector.start()
print(f"Signal threshold = {THRESH}. Point at a wall. Ctrl+C to stop.\n")

try:
    while True:
        result    = detector.get_next()
        extra     = result[1].processor_results[0].extra_result
        abs_sweep = extra.abs_sweep
        distances = extra.distances_m

        above   = np.where(abs_sweep > THRESH)[0]
        closest = float(distances[above[0]]) if len(above) > 0 else None
        peak_i  = int(abs_sweep.argmax())

        print(f"  closest={closest}m  |  peak={distances[peak_i]:.3f}m @ {abs_sweep[peak_i]:.1f}  |  detected={closest is not None}")
        time.sleep(0.1)

except KeyboardInterrupt:
    pass
finally:
    detector.stop()
    client.close()