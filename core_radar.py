import asyncio
import time
import multiprocessing
import numpy as np
import orjson
import valkey.asyncio as avalkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import (
    Detector as PresDetector,
    DetectorConfig as PresConfig,
)
from acconeer.exptool.a121.algo.distance import (
    Detector as DistDetector,
    DetectorConfig as DistConfig,
)
from log_config import get_logger

log = get_logger("Radar")

FRONT_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0"
KEY_FRONT            = "chippy:state:radar:front"
KEY_MODE             = "chippy:mode"
KEY_DIST_CALIBRATED  = "chippy:state:radar:dist_calibrated"  # set to "1" when ready

MODE_MAZE = "MAZE"

# ── Detector configs ──────────────────────────────────────────────────────────

PRES_CONFIG = PresConfig(
    frame_rate=20.0,
    start_m=0.10,
    end_m=2.0,
    intra_detection_threshold=3.0,
    inter_detection_threshold=2.0,
)

# close_range_leakage_cancellation cancels the sensor's own near-field
# leakage during calibrate_detector() — requires calibration in free space.
try:
    DIST_CONFIG = DistConfig(
        start_m=0.10,
        end_m=0.50,
        max_profile=a121.Profile.PROFILE_1,
        close_range_leakage_cancellation=True,
    )
except TypeError:
    # Older SDK versions may not have this parameter — fall back gracefully
    log.warning("close_range_leakage_cancellation not supported in this SDK version")
    DIST_CONFIG = DistConfig(
        start_m=0.10,
        end_m=0.50,
        max_profile=a121.Profile.PROFILE_1,
    )

# Minimum abs_sweep signal to count as a real wall reflection.
# Lower = more sensitive (picks up grazing/edge reflections, prevents
# false "opening" at wall corners). Too low = detects noise.
# Noise floor ~5-40, real wall ~80-140. 45 catches edge reflections.
MAZE_SIGNAL_THRESHOLD = 45.0

# Proximity zone — inspired by the Acconeer Touchless Button reference app.
# Any reflection closer than MAZE_PROXIMITY_ZONE_M with signal above
# MAZE_PROXIMITY_THRESHOLD triggers an emergency "about to collide" flag.
# Uses lower threshold because close-range signals are stronger (1/r⁴), so
# even grazing reflections should clear this bar when very close.
MAZE_PROXIMITY_ZONE_M    = 0.150
MAZE_PROXIMITY_THRESHOLD = 40.0


def _analyze_sweep(dist_result) -> tuple[float | None, bool]:
    """
    Returns (closest_wall_m, proximity_alert).
      closest_wall_m  — nearest reflection above MAZE_SIGNAL_THRESHOLD (or None)
      proximity_alert — True if ANY bin inside MAZE_PROXIMITY_ZONE_M has
                        signal > MAZE_PROXIMITY_THRESHOLD (emergency stop)
    """
    try:
        extra     = dist_result.processor_results[0].extra_result
        abs_sweep = extra.abs_sweep
        distances = extra.distances_m

        # Proximity check — look only at close bins, lower threshold
        close_mask    = distances < MAZE_PROXIMITY_ZONE_M
        close_signals = abs_sweep[close_mask]
        proximity     = bool((close_signals > MAZE_PROXIMITY_THRESHOLD).any())

        # Normal wall detection
        above = np.where(abs_sweep > MAZE_SIGNAL_THRESHOLD)[0]
        closest = float(distances[above[0]]) if len(above) > 0 else None

        return closest, proximity
    except Exception:
        return None, False


# ── Radar loop ────────────────────────────────────────────────────────────────

async def radar_loop(client, state_key: str, wlog):
    """
    Watches chippy:mode and switches between Presence and Distance detectors.

    When switching to MAZE:
      1. Creates distance detector with leakage cancellation config
      2. Calls calibrate_detector() — THIS is when you hold the robot in free space
      3. Sets chippy:state:radar:dist_calibrated = "1" to signal the maze controller
      4. Starts reading frames

    The ARMED state in core_joystick.py waits for dist_calibrated before
    accepting the start signal, giving the user time to hold robot in free space.
    """
    r = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)

    detector  = None
    maze_mode = None

    try:
        while True:
            raw       = await r.get(KEY_MODE)
            mode      = raw if raw else "FOLLOW"
            want_maze = (mode == MODE_MAZE)

            # ── Switch detector on mode category change ───────────────────
            if want_maze != maze_mode:
                if detector is not None:
                    try:
                        detector.stop()
                    except Exception:
                        pass
                    detector = None

                if want_maze:
                    # Clear stale calibration flag
                    await r.delete(KEY_DIST_CALIBRATED)

                    wlog.info("Switching to Distance Detector — calibrating...")
                    wlog.info(">>> HOLD ROBOT IN FREE SPACE NOW <<<")

                    detector = DistDetector(
                        client=client,
                        sensor_ids=[1],
                        detector_config=DIST_CONFIG,
                    )

                    # Blocking call — runs in the event loop but is fast (~1s)
                    # User must hold robot clear of obstacles during this call
                    await asyncio.to_thread(detector.calibrate_detector)

                    detector.start()
                    maze_mode = want_maze

                    # Signal maze controller that calibration is done
                    await r.set(KEY_DIST_CALIBRATED, "1")
                    wlog.info("Distance Detector calibrated and running (5–50 cm)")

                else:
                    # Clear calibration flag when leaving MAZE
                    await r.delete(KEY_DIST_CALIBRATED)

                    detector = PresDetector(
                        client=client,
                        sensor_id=1,
                        detector_config=PRES_CONFIG,
                    )
                    detector.start()
                    maze_mode = want_maze
                    wlog.info("→ Presence Detector ({} mode)", mode)

            # ── Read one frame ────────────────────────────────────────────
            result = await asyncio.to_thread(detector.get_next)

            if want_maze:
                closest, proximity = _analyze_sweep(result[1])
                await r.set(state_key, orjson.dumps({
                    "detected":  closest is not None,
                    "dist":      round(closest, 3) if closest is not None else None,
                    "proximity": proximity,
                    "intra":     0.0,
                    "inter":     0.0,
                    "ts":        round(time.time(), 4),
                }).decode())
            else:
                pres    = result
                detected = bool(pres.presence_detected)
                await r.set(state_key, orjson.dumps({
                    "detected":  detected,
                    "dist":      float(pres.presence_distance) if detected else None,
                    "proximity": False,  # not applicable for presence mode
                    "intra":     round(float(pres.intra_presence_score), 2),
                    "inter":     round(float(pres.inter_presence_score), 2),
                    "ts":        round(time.time(), 4),
                }).decode())

    except asyncio.CancelledError:
        pass
    finally:
        if detector is not None:
            try:
                detector.stop()
            except Exception:
                pass
        await r.delete(KEY_DIST_CALIBRATED)
        await r.aclose()


# ── Worker entry point ────────────────────────────────────────────────────────

def radar_worker(port: str, state_key: str):
    from log_config import get_logger as _get_logger
    wlog = _get_logger("Radar")
    client = a121.Client.open(serial_port=port)
    wlog.info("Radar client open — key: {}", state_key)
    try:
        asyncio.run(radar_loop(client, state_key, wlog))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass
        wlog.info("Radar worker stopped — {}", state_key)


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