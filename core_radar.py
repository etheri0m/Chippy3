import asyncio
import time
import multiprocessing
import orjson
import valkey.asyncio as avalkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import Detector, DetectorConfig
from log_config import get_logger

log = get_logger("Radar")

FRONT_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0"
KEY_FRONT = "chippy:state:radar:front"


async def radar_loop(detector: Detector, state_key: str, wlog):
    """Async loop: blocking radar read via to_thread, async Valkey write."""
    r = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)

    try:
        while True:
            result = await asyncio.to_thread(detector.get_next)
            detected = result.presence_detected

            await r.set(state_key, orjson.dumps({
                "detected": detected,
                "dist":     result.presence_distance if detected else None,
                "intra":    round(result.intra_presence_score, 2),
                "inter":    round(result.inter_presence_score, 2),
                "ts":       round(time.time(), 4),
            }).decode())
    except asyncio.CancelledError:
        pass
    finally:
        await r.aclose()


def radar_worker(port: str, state_key: str):
    """Subprocess entry — sets up radar hardware then runs async loop."""
    from log_config import get_logger as _get_logger
    wlog = _get_logger("Radar")

    client = a121.Client.open(serial_port=port)
    config = DetectorConfig(
        frame_rate=20.0,
        start_m=0.10,
        end_m=2.0,
        intra_detection_threshold=4.0,
        inter_detection_threshold=3.0,
    )
    detector = Detector(client=client, sensor_id=1, detector_config=config)
    detector.start()
    wlog.info("Worker started — key: {}", state_key)

    try:
        asyncio.run(radar_loop(detector, state_key, wlog))
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