import asyncio
import time
import collections
import orjson
import valkey.asyncio as avalkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import Detector, DetectorConfig
from log_config import get_logger

log = get_logger("Controller")

REAR_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800352-if00-port0"

# --- FOLLOW mode ---
NEAR_ZONE      = 0.20
FAR_ZONE       = 0.30
MAX_HAND_DIST  = 0.50
FOLLOW_SPEED   = 0.7
MIN_HAND_INTRA = 4.0

SWEEP_SPEED    = 0.8
SWEEP_DURATION = 0.8
SWEEP_TIMEOUT  = 3.0

# --- CROWD mode ---
CROWD_WINDOW_FRAMES       = 40
CROWD_HIGH_THRESHOLD      = 8.0
CROWD_LOW_THRESHOLD       = 3.0
CROWD_HEAD_SWEEP_SPEED    = 0.5
CROWD_HEAD_SWEEP_DURATION = 1.2

# --- MAZE mode (right-hand wall-following) ---
# Algorithm:
#   - Drive forward, head/body aligned (hall sensor triggered)
#   - Front radar continuously monitors what's ahead
#   - Every PEEK_INTERVAL seconds: stop legs, rotate head right ~90°,
#     read radar (now pointing right), then either:
#       a) Right is OPEN (>OPENING dist or no detection) → COMMIT (don't
#          return head, just drive — body follows head into right corridor)
#       b) Right is BLOCKED → return head left until hall triggers, resume drive
#   - If front becomes BLOCKED while driving:
#       a) Rotate head left ~90°, read radar
#       b) Left is OPEN → COMMIT left
#       c) Left is BLOCKED → SPIN_180 (rotate further left ~90° more, drive out)
#
# Hall sensor (chippy:state:hall) triggers when head/body aligned (forward).
# After a commit, we wait for hall to trigger again — meaning the body has
# rotated to align with where the head is now pointing = ready to drive straight
# in the new direction.
MAZE_DRIVE_SPEED       = 0.7
MAZE_TURN_SPEED        = 0.7
MAZE_FRONT_BLOCK       = 0.150    # stop forward when wall this close (m)
MAZE_RIGHT_OPENING     = 0.45    # right reading > this = corridor opening
MAZE_LEFT_OPENING      = 0.45
MAZE_PEEK_INTERVAL     = 4.0     # seconds of driving between right peeks
MAZE_PEEK_TURN_DUR     = 1.50    # time to rotate head ~90° (TUNE in test_turns.py)
MAZE_RADAR_SETTLE      = 0.15    # wait after head stops for fresh radar reading
MAZE_PEEK_CONFIRM_DUR  = 0.30    # collect samples for this long — all must show open
MAZE_PEEK_MIN_SAMPLES  = 4       # require at least this many samples (20Hz → ~6/0.3s)
MAZE_HALL_TIMEOUT      = 3.0     # max wait for hall after a commit
MAZE_RECENTER_TIMEOUT  = 1.5     # max wait for hall during peek-return
MAZE_REAR_SAFE_DIST    = 0.20    # for any future reverse safety check

# --- Valkey keys ---
KEY_MODE             = 'chippy:mode'
KEY_VELOCITY         = 'chippy:cmd:velocity'
KEY_RADAR_REAR       = 'chippy:state:radar:rear'
KEY_RADAR_FR         = 'chippy:state:radar:front'
KEY_CROWD            = 'chippy:state:crowd'
KEY_MAZE             = 'chippy:state:maze'
KEY_HEAD_STATE       = 'chippy:state:head'
KEY_HALL             = 'chippy:state:hall'         # "1" if head centered
KEY_MAZE_START       = 'chippy:cmd:maze:start'
KEY_DIST_CALIBRATED  = 'chippy:state:radar:dist_calibrated'
CH_CALIBRATE         = 'chippy:cmd:calibrate'

MODE_FOLLOW = "FOLLOW"
MODE_CROWD  = "CROWD"
MODE_MAZE   = "MAZE"
MODE_IDLE   = "IDLE"
VALID_MODES = {MODE_FOLLOW, MODE_CROWD, MODE_MAZE, MODE_IDLE}


async def publish_velocity(r, v: float, w: float):
    await r.publish(KEY_VELOCITY,
                    orjson.dumps({"v": round(v, 3), "w": round(w, 3)}).decode())


async def read_front_radar(r) -> dict:
    raw = await r.get(KEY_RADAR_FR)
    if raw:
        try:
            return orjson.loads(raw)
        except Exception:
            pass
    return {"detected": False, "dist": None, "intra": 0.0, "inter": 0.0}


async def read_rear_radar(r) -> dict:
    raw = await r.get(KEY_RADAR_REAR)
    if raw:
        try:
            return orjson.loads(raw)
        except Exception:
            pass
    return {"detected": False, "dist": None, "intra": 0.0, "inter": 0.0}


# ---------------------------------------------------------------------------
# Head sweep-and-lock (used by FOLLOW)
# ---------------------------------------------------------------------------

class HeadSweep:
    LOCKED   = "LOCKED"
    SWEEPING = "SWEEPING"
    STOPPED  = "STOPPED"

    def __init__(self, r):
        self.r            = r
        self.state        = self.STOPPED
        self.sweep_dir    = 1.0
        self.sweep_start  = None
        self.sweep_total  = None
        self.last_w       = None
        self.last_v       = None
        self.hlog         = get_logger("Head")

    async def reset(self):
        await self._send_w(0.0)
        self.state       = self.STOPPED
        self.sweep_start = None
        self.sweep_total = None
        self.last_w      = None
        self.last_v      = None

    async def _send_w(self, w: float, v: float = 0.0):
        if w != self.last_w or v != self.last_v:
            await publish_velocity(self.r, v, w)
            self.last_w = w
            self.last_v = v

    async def update(self, front: dict, leg_v: float = 0.0):
        if front["detected"]:
            if self.state != self.LOCKED:
                self.state       = self.LOCKED
                self.sweep_start = None
                self.sweep_total = None
                self.hlog.info("Locked on target")
            await self._send_w(0.0, leg_v)
        else:
            if self.state == self.LOCKED:
                self.state       = self.SWEEPING
                self.sweep_dir   = 1.0
                self.sweep_start = time.time()
                self.sweep_total = time.time()
                self.hlog.info("Target lost — sweeping")

            if self.state == self.SWEEPING:
                elapsed_total = time.time() - self.sweep_total
                if elapsed_total >= SWEEP_TIMEOUT:
                    self.state = self.STOPPED
                    await self._send_w(0.0, leg_v)
                    self.hlog.info("Sweep timeout — stopped")
                else:
                    if time.time() - self.sweep_start >= SWEEP_DURATION:
                        self.sweep_dir  = -self.sweep_dir
                        self.sweep_start = time.time()
                    await self._send_w(self.sweep_dir * SWEEP_SPEED, leg_v)

            elif self.state == self.STOPPED:
                await self._send_w(0.0, leg_v)

        return self.state


# ---------------------------------------------------------------------------
# FOLLOW mode
# ---------------------------------------------------------------------------

class FollowMode:
    def __init__(self, r):
        self.r    = r
        self.head = HeadSweep(r)
        self.last_v = None
        self.flog = get_logger("Follow")

    async def reset(self):
        await self.head.reset()
        await publish_velocity(self.r, 0.0, 0.0)
        self.last_v = None

    async def update(self):
        front = await read_front_radar(self.r)

        hand_present = (
            front["detected"] and
            front["dist"] is not None and
            front["dist"] <= MAX_HAND_DIST and
            front.get("intra", 0.0) >= MIN_HAND_INTRA
        )
        raw_dist = front["dist"] if hand_present else None

        if raw_dist is None:
            leg_v = 0.0
            zone  = "NONE"
        elif raw_dist < NEAR_ZONE:
            leg_v = -FOLLOW_SPEED
            zone  = "NEAR"
        elif raw_dist > FAR_ZONE:
            leg_v = FOLLOW_SPEED
            zone  = "FAR"
        else:
            leg_v = 0.0
            zone  = "HOLD"

        head_state = await self.head.update(front, leg_v)
        dist_str = f"{raw_dist:.3f}m" if raw_dist is not None else "None   "
        intra_str = f"{front.get('intra', 0.0):.1f}"
        self.flog.debug(
            "Hand: {} | Zone: {:4} | Legs: {:+.1f} | Head: {} | Intra: {}",
            dist_str, zone, leg_v, head_state, intra_str
        )


# ---------------------------------------------------------------------------
# CROWD mode
# ---------------------------------------------------------------------------

class CrowdMode:
    def __init__(self, r):
        self.r            = r
        self.inter_window = collections.deque(maxlen=CROWD_WINDOW_FRAMES)
        self.intra_window = collections.deque(maxlen=CROWD_WINDOW_FRAMES)
        self.clog         = get_logger("Crowd")
        self._sweep_dir:        float = 1.0
        self._sweep_step_start: float = time.time()

    async def reset(self):
        await publish_velocity(self.r, 0.0, 0.0)
        self.inter_window.clear()
        self.intra_window.clear()
        self._sweep_dir        = 1.0
        self._sweep_step_start = time.time()
        await asyncio.sleep(0.2)
        await self.r.publish(CH_CALIBRATE, "1")
        self.clog.info("CROWD exit — recalibration triggered")

    def _classify(self, avg_inter: float) -> str:
        if avg_inter >= CROWD_HIGH_THRESHOLD:
            return "BUSY"
        elif avg_inter >= CROWD_LOW_THRESHOLD:
            return "LOW"
        return "EMPTY"

    def _sweep_w(self) -> float:
        if time.time() - self._sweep_step_start >= CROWD_HEAD_SWEEP_DURATION:
            self._sweep_dir        = -self._sweep_dir
            self._sweep_step_start = time.time()
        return self._sweep_dir * CROWD_HEAD_SWEEP_SPEED

    async def update(self):
        front = await read_front_radar(self.r)
        rear  = await read_rear_radar(self.r)

        combined_inter = max(front["inter"], rear["inter"])
        combined_intra = max(front["intra"], rear["intra"])

        self.inter_window.append(combined_inter)
        self.intra_window.append(combined_intra)

        avg_inter = sum(self.inter_window) / len(self.inter_window)
        avg_intra = sum(self.intra_window) / len(self.intra_window)
        density   = self._classify(avg_inter)

        await publish_velocity(self.r, 0.0, self._sweep_w())

        await self.r.set(KEY_CROWD, orjson.dumps({
            "density":   density,
            "avg_inter": round(avg_inter, 2),
            "avg_intra": round(avg_intra, 2),
            "detected":  front["detected"] or rear["detected"],
            "dist":      front["dist"],
            "ts":        round(time.time(), 4),
        }).decode())

        self.clog.debug("{:5} | Inter: {:5.1f} | Intra: {:5.1f}", density, avg_inter, avg_intra)


# ---------------------------------------------------------------------------
# MAZE mode — right-hand wall following
# ---------------------------------------------------------------------------

class MazeMode:
    S_ARMED          = "ARMED"
    S_DRIVE          = "DRIVE"
    S_PEEK_R_TURN    = "PEEK_R_TURN"
    S_PEEK_R_READ    = "PEEK_R_READ"
    S_PEEK_R_RETURN  = "PEEK_R_RETURN"
    S_COMMIT_R       = "COMMIT_R"
    S_PEEK_L_TURN    = "PEEK_L_TURN"
    S_PEEK_L_READ    = "PEEK_L_READ"
    S_COMMIT_L       = "COMMIT_L"
    S_SPIN_180_TURN  = "SPIN_180_TURN"
    S_SPIN_180_DRIVE = "SPIN_180_DRIVE"

    def __init__(self, r):
        self.r              = r
        self.state          = None
        self.state_start    = 0.0
        self.active         = False
        self.mlog           = get_logger("Maze")
        self.last_peek_time = 0.0
        self.peek_dist      = None
        self.left_dist      = None
        self._peek_samples: list = []  # collected radar readings during peek-read

    async def reset(self):
        await publish_velocity(self.r, 0.0, 0.0)
        self.state          = None
        self.active         = False
        self.last_peek_time = 0.0
        self.peek_dist      = None
        self.left_dist      = None
        self._peek_samples  = []
        try:
            await self.r.delete(KEY_MAZE_START)
        except Exception:
            pass

    async def start(self):
        await publish_velocity(self.r, 0.0, 0.0)
        try:
            await self.r.delete(KEY_MAZE_START)
            # Clear any stale head calibration state so we wait for a fresh one
            await self.r.delete(KEY_HEAD_STATE)
        except Exception:
            pass
        # Kick off head calibration immediately — hardware will run smart_calibrate_head
        # which ends with head centered (magnet over sensor, hall="1").
        # Radar calibration happens in parallel (core_radar.py does it on mode switch).
        await self.r.publish(CH_CALIBRATE, "1")
        await self._enter(self.S_ARMED)
        self.active = True
        self.mlog.info("Armed — calibrating head + radar (hold robot in free space)")

    # ------------------------------------------------------------------
    async def _enter(self, new_state: str):
        self.state       = new_state
        self.state_start = time.time()
        self.mlog.info("→ {}", new_state)
        await self._publish_state(new_state)

    async def _publish_state(self, status: str):
        await self.r.set(KEY_MAZE, orjson.dumps({
            "status":    status,
            "peek_dist": self.peek_dist,
            "left_dist": self.left_dist,
            "ts":        round(time.time(), 4),
        }).decode())

    def _elapsed(self) -> float:
        return time.time() - self.state_start

    async def _hall_centered(self) -> bool:
        v = await self.r.get(KEY_HALL)
        return v == "1"

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------
    async def update(self):
        if not self.active:
            await self.start()
            return

        front = await read_front_radar(self.r)

        # ---- ARMED ----------------------------------------------------
        # Wait for two calibrations to complete before accepting start:
        #   1. Radar distance detector calibrated (core_radar.py sets KEY_DIST_CALIBRATED)
        #   2. Head calibrated (core_hardware.py's smart_calibrate_head sets
        #      KEY_HEAD_STATE with calibrated=True, head ends up centered)
        # Both run in parallel — so the wait is just "whichever finishes last".
        if self.state == self.S_ARMED:
            radar_ok = bool(await self.r.get(KEY_DIST_CALIBRATED))

            head_ok = False
            head_raw = await self.r.get(KEY_HEAD_STATE)
            if head_raw:
                try:
                    head_ok = bool(orjson.loads(head_raw).get("calibrated", False))
                except Exception:
                    head_ok = False

            if not (radar_ok and head_ok):
                # Log a status line roughly every 2 seconds
                if int(self._elapsed()) % 2 == 0 and self._elapsed() % 1.0 < 0.1:
                    self.mlog.info(
                        "Waiting — radar:{} head:{} (hold robot in free space)",
                        "OK" if radar_ok else "...",
                        "OK" if head_ok else "..."
                    )
                return

            flag = await self.r.get(KEY_MAZE_START)
            if flag:
                await self.r.delete(KEY_MAZE_START)
                self.mlog.info("Start received — driving forward")
                self.last_peek_time = time.time()
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                await self._enter(self.S_DRIVE)
            elif int(self._elapsed()) % 2 == 0 and self._elapsed() % 1.0 < 0.1:
                self.mlog.info(
                    "All calibrated — place robot then: redis-cli set {} 1", KEY_MAZE_START
                )
            return

        # ---- DRIVE ----------------------------------------------------
        # Head and body aligned (hall triggered), legs forward.
        # Two reactive triggers: front blocked → check left; peek timer → check right
        if self.state == self.S_DRIVE:
            blocked = (
                front["detected"] and
                front["dist"] is not None and
                front["dist"] < MAZE_FRONT_BLOCK
            )
            if blocked:
                self.mlog.warning("Front blocked at {:.2f}m — checking left", front["dist"])
                await publish_velocity(self.r, 0.0, 0.0)
                # Begin left rotation
                await publish_velocity(self.r, 0.0, -MAZE_TURN_SPEED)
                await self._enter(self.S_PEEK_L_TURN)
                return

            if time.time() - self.last_peek_time >= MAZE_PEEK_INTERVAL:
                f_str = f"{front['dist']:.2f}m" if front.get('dist') is not None else "open"
                self.mlog.info("Peek-right time (front: {})", f_str)
                await publish_velocity(self.r, 0.0, 0.0)
                # Begin right rotation
                await publish_velocity(self.r, 0.0, MAZE_TURN_SPEED)
                await self._enter(self.S_PEEK_R_TURN)
                return
            return

        # ---- PEEK_R_TURN ----------------------------------------------
        # Rotating head ~90° right (timed). Hall will go off shortly after start.
        if self.state == self.S_PEEK_R_TURN:
            if self._elapsed() >= MAZE_PEEK_TURN_DUR:
                await publish_velocity(self.r, 0.0, 0.0)
                await self._enter(self.S_PEEK_R_READ)
            return

        # ---- PEEK_R_READ ----------------------------------------------
        # Head pointing right (legs stopped). Collect samples over
        # MAZE_PEEK_CONFIRM_DUR — ALL samples must show open to commit.
        # This rejects one-off false "open" readings at wall edges.
        if self.state == self.S_PEEK_R_READ:
            if self._elapsed() < MAZE_RADAR_SETTLE:
                self._peek_samples = []
                return

            # Accumulate the current reading
            d = front.get("dist")
            self._peek_samples.append(d)

            # Still collecting
            if self._elapsed() < MAZE_RADAR_SETTLE + MAZE_PEEK_CONFIRM_DUR:
                return

            # Need a minimum number of samples to make a decision
            if len(self._peek_samples) < MAZE_PEEK_MIN_SAMPLES:
                self.mlog.warning(
                    "Only {} samples collected, waiting...", len(self._peek_samples)
                )
                return

            # Analyse the samples — all must be open for a commit
            all_open   = all(s is None or s > MAZE_RIGHT_OPENING
                             for s in self._peek_samples)
            valid      = [s for s in self._peek_samples if s is not None]
            closest    = min(valid) if valid else None
            n_detected = len(valid)

            self.peek_dist = round(closest, 3) if closest is not None else None
            c_str = f"{closest:.2f}m" if closest is not None else "all-OPEN"
            self.mlog.info(
                "Right peek: {} samples, {} detected, closest={}, all_open={}",
                len(self._peek_samples), n_detected, c_str, all_open
            )

            if all_open:
                self.mlog.success("Right opening confirmed → committing right")
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                await self._enter(self.S_COMMIT_R)
            else:
                self.mlog.info("Right blocked (or flickering) — returning to center")
                await publish_velocity(self.r, 0.0, -MAZE_TURN_SPEED)
                await self._enter(self.S_PEEK_R_RETURN)
            return

        # ---- PEEK_R_RETURN --------------------------------------------
        # Rotating head left until hall triggers (head centered = forward)
        if self.state == self.S_PEEK_R_RETURN:
            centered = await self._hall_centered()
            if centered or self._elapsed() >= MAZE_RECENTER_TIMEOUT:
                if not centered:
                    self.mlog.warning("Recenter timeout — proceeding anyway")
                await publish_velocity(self.r, 0.0, 0.0)
                await asyncio.sleep(0.05)
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                self.last_peek_time = time.time()
                await self._enter(self.S_DRIVE)
            return

        # ---- COMMIT_R -------------------------------------------------
        # Head pointing right, legs forward. Body follows head.
        # Wait for hall to trigger again — meaning body has rotated to align
        # with where the head is pointing → ready to drive straight.
        if self.state == self.S_COMMIT_R:
            centered = await self._hall_centered()
            if centered:
                self.mlog.success("Body aligned with head — straight drive")
                self.last_peek_time = time.time()
                await self._enter(self.S_DRIVE)
            elif self._elapsed() >= MAZE_HALL_TIMEOUT:
                self.mlog.warning("Commit hall timeout — proceeding to DRIVE anyway")
                self.last_peek_time = time.time()
                await self._enter(self.S_DRIVE)
            return

        # ---- PEEK_L_TURN ----------------------------------------------
        if self.state == self.S_PEEK_L_TURN:
            if self._elapsed() >= MAZE_PEEK_TURN_DUR:
                await publish_velocity(self.r, 0.0, 0.0)
                await self._enter(self.S_PEEK_L_READ)
            return

        # ---- PEEK_L_READ ----------------------------------------------
        # Same multi-sample confirmation pattern as PEEK_R_READ
        if self.state == self.S_PEEK_L_READ:
            if self._elapsed() < MAZE_RADAR_SETTLE:
                self._peek_samples = []
                return

            d = front.get("dist")
            self._peek_samples.append(d)

            if self._elapsed() < MAZE_RADAR_SETTLE + MAZE_PEEK_CONFIRM_DUR:
                return

            if len(self._peek_samples) < MAZE_PEEK_MIN_SAMPLES:
                self.mlog.warning(
                    "Only {} samples collected, waiting...", len(self._peek_samples)
                )
                return

            all_open = all(s is None or s > MAZE_LEFT_OPENING
                           for s in self._peek_samples)
            valid    = [s for s in self._peek_samples if s is not None]
            closest  = min(valid) if valid else None

            self.left_dist = round(closest, 3) if closest is not None else None
            c_str = f"{closest:.2f}m" if closest is not None else "all-OPEN"
            self.mlog.info(
                "Left peek: {} samples, closest={}, all_open={}",
                len(self._peek_samples), c_str, all_open
            )

            if all_open:
                self.mlog.success("Left opening confirmed → committing left")
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                await self._enter(self.S_COMMIT_L)
            else:
                self.mlog.warning("Dead end (front + left blocked) — spinning 180")
                await publish_velocity(self.r, 0.0, -MAZE_TURN_SPEED)
                await self._enter(self.S_SPIN_180_TURN)
            return

        # ---- COMMIT_L -------------------------------------------------
        if self.state == self.S_COMMIT_L:
            centered = await self._hall_centered()
            if centered:
                self.mlog.success("Body aligned with head — straight drive")
                self.last_peek_time = time.time()
                await self._enter(self.S_DRIVE)
            elif self._elapsed() >= MAZE_HALL_TIMEOUT:
                self.mlog.warning("Commit hall timeout — proceeding to DRIVE anyway")
                self.last_peek_time = time.time()
                await self._enter(self.S_DRIVE)
            return

        # ---- SPIN_180_TURN --------------------------------------------
        # Already rotated ~90° left (during PEEK_L), now another ~90° to hit 180°
        if self.state == self.S_SPIN_180_TURN:
            if self._elapsed() >= MAZE_PEEK_TURN_DUR:
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                await self._enter(self.S_SPIN_180_DRIVE)
            return

        # ---- SPIN_180_DRIVE -------------------------------------------
        # Head at 180° from original, legs driving. Body follows.
        if self.state == self.S_SPIN_180_DRIVE:
            centered = await self._hall_centered()
            if centered:
                self.mlog.success("Body realigned after 180 — straight drive")
                self.last_peek_time = time.time()
                await self._enter(self.S_DRIVE)
            elif self._elapsed() >= MAZE_HALL_TIMEOUT:
                self.mlog.warning("Spin-180 hall timeout — proceeding to DRIVE anyway")
                self.last_peek_time = time.time()
                await self._enter(self.S_DRIVE)
            return


# ---------------------------------------------------------------------------
# IDLE mode
# ---------------------------------------------------------------------------

class IdleMode:
    def __init__(self, r):
        self.r    = r
        self.ilog = get_logger("Idle")

    async def reset(self):
        await publish_velocity(self.r, 0.0, 0.0)

    async def update(self):
        await publish_velocity(self.r, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_controller():
    r = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)

    if not await r.exists(KEY_MODE):
        await r.set(KEY_MODE, MODE_FOLLOW)

    rear_detector = None
    rear_client   = None
    try:
        rear_client = a121.Client.open(serial_port=REAR_RADAR_PORT)
        config = DetectorConfig(
            start_m=0.10,
            end_m=0.5,
            frame_rate=20.0,
            intra_detection_threshold=5.0,
            inter_detection_threshold=4.0,
        )
        rear_detector = Detector(client=rear_client, sensor_id=1, detector_config=config)
        rear_detector.start()
        log.info("Rear radar connected")
    except Exception as e:
        log.warning("Rear radar unavailable ({}) — running without it", e)
        rear_client   = None
        rear_detector = None

    follow = FollowMode(r)
    crowd  = CrowdMode(r)
    maze   = MazeMode(r)
    idle   = IdleMode(r)

    active_mode = None

    if rear_detector:
        log.info("Running — rear radar active")
    else:
        log.info("Running — front radar only")
    log.info("Modes: redis-cli set {} FOLLOW|CROWD|MAZE", KEY_MODE)
    log.info("MAZE start: redis-cli set {} 1  (after placing Chipz)", KEY_MAZE_START)

    try:
        while True:
            if rear_detector:
                result   = await asyncio.to_thread(rear_detector.get_next)
                pres     = result
                detected = bool(pres.presence_detected)
                raw_dist = pres.presence_distance if detected else None
                intra    = pres.intra_presence_score
                inter    = pres.inter_presence_score

                await r.set(KEY_RADAR_REAR, orjson.dumps({
                    "detected": detected,
                    "dist":     float(raw_dist) if raw_dist is not None else None,
                    "intra":    round(float(intra), 2),
                    "inter":    round(float(inter), 2),
                    "ts":       round(time.time(), 4),
                }).decode())
            else:
                await asyncio.sleep(0.05)

            raw_mode = await r.get(KEY_MODE)
            mode     = raw_mode if raw_mode in VALID_MODES else MODE_FOLLOW

            if mode != active_mode:
                log.info("Mode: {} → {}", active_mode, mode)
                if active_mode == MODE_FOLLOW: await follow.reset()
                elif active_mode == MODE_CROWD: await crowd.reset()
                elif active_mode == MODE_MAZE:  await maze.reset()
                elif active_mode == MODE_IDLE:  await idle.reset()
                active_mode = mode

            if mode == MODE_FOLLOW:
                await follow.update()
            elif mode == MODE_CROWD:
                await crowd.update()
            elif mode == MODE_MAZE:
                await maze.update()
            elif mode == MODE_IDLE:
                await idle.update()

    except asyncio.CancelledError:
        pass
    finally:
        try:
            if rear_detector:
                rear_detector.stop()
            if rear_client:
                rear_client.close()
        except Exception:
            pass
        await publish_velocity(r, 0.0, 0.0)
        await r.aclose()
        log.info("Stopped")


if __name__ == "__main__":
    try:
        asyncio.run(run_controller())
    except KeyboardInterrupt:
        pass