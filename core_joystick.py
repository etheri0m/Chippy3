import asyncio
import time
import collections
import orjson
import valkey.asyncio as avalkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import Detector, DetectorConfig
from log_config import get_logger

log = get_logger("Controller")

REAR_RADAR_PORT = os.environ.get("REAR_RADAR_PORT", "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800352-if00-port0")

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
CROWD_HEAD_SWEEP_SPEED    = 0.6
CROWD_HEAD_SWEEP_DURATION = 2.0

# --- MAZE mode (right-hand wall-following, reactive) ---
#
# Algorithm (true right-hand rule, no timed peeks):
#   DRIVE forward until the front radar sees a wall close enough to count as
#   a junction. Stop. Peek RIGHT. If right is open, take it. If right is
#   blocked, rotate the head through center to the LEFT and peek there. If
#   left is open, take it. If both blocked, spin 180°. After any commit, wait
#   for body alignment (hall sensor) before resuming forward drive.
#
# This solves serpentine mazes correctly, and would also solve any branching
# maze whose entrance and exit are on the boundary (the classical right-hand
# rule guarantee).
#
# All decisions are reactive — there are no timer-based events that could
# fire mid-corridor and confuse the state machine.

MAZE_DRIVE_SPEED       = 0.8
MAZE_TURN_SPEED        = 0.7

# Junction detection.  Front radar < this for MAZE_BLOCKED_CONFIRM consecutive
# frames -> we're at a wall; run the peek protocol.
MAZE_FRONT_BLOCK       = 0.20

# Peek classification (hysteresis band).  Median of valid samples:
#   > MAZE_OPEN_HI -> OPEN     (commit to that side)
#   < MAZE_OPEN_LO -> BLOCKED  (try the other side)
#   in between     -> UNCERTAIN (re-peek once with longer dwell)
# Side wall in a 40cm corridor reads ~0.15-0.20m; opening reads >=0.40m or
# undetected (corridor extends past sensor range).  Big margin either way.
MAZE_OPEN_HI           = 0.35
MAZE_OPEN_LO           = 0.25

# Head rotation timing.  90° from hall-aligned takes exactly MAZE_PEEK_TURN_DUR
# seconds at MAZE_TURN_SPEED — every state that depends on a timed turn
# re-anchors on the hall sensor first.
MAZE_PEEK_TURN_DUR     = 1.00

# Sample collection.  Settle longer than before (was 0.15) so the radar's
# first frame after head-stop isn't included.  Collect for 0.5s -> ~10 samples
# at 20Hz, plenty for a stable median.
MAZE_RADAR_SETTLE      = 0.30
MAZE_PEEK_CONFIRM_DUR  = 0.50
MAZE_PEEK_MIN_SAMPLES  = 6

# A peek that comes back UNCERTAIN gets one retry with longer dwell.
# Two uncertains in a row are treated as BLOCKED (conservative).
MAZE_REPEEK_DWELL_MULT = 2.0

# Commit alignment timeouts.  After a commit, body is rotating to align
# with head.  First exit condition wins:
#   1. Hall fires  (body aligned with head)
#   2. After MAZE_COMMIT_MIN_DRIVE secs, front sees clear corridor
#   3. MAZE_HALL_TIMEOUT — last-resort fallthrough
MAZE_COMMIT_MIN_DRIVE  = 1.0
MAZE_COMMIT_CLEAR_DIST = 0.30

# COMMIT duration: how long the legs drive forward with head at side-90°
# during a turn.  This is the only knob you adjust to tune body rotation,
# same way you tuned MAZE_PEEK_TURN_DUR for the 90° head peek.
#
# Tuning rule: if the body undershoots the turn (robot keeps hitting the
# same wall after turning) → INCREASE.  If the body overshoots (robot
# ends up turned past 90°, drives sideways into next wall) → DECREASE.
# Each 0.1s ≈ 6-9° of body rotation in your setup.
MAZE_COMMIT_DURATION   = 1.0

# Backwards-compat alias (not actually a hall-based timeout — it's a
# fixed timer because hall can't fire from body rotation alone).
MAZE_HALL_TIMEOUT      = MAZE_COMMIT_DURATION

# Recentering during the right-blocked -> left-peek transition.
# Head rotates left through the magnet; the hall pulse re-anchors timing.
# 2x for the safety case where the magnet is missed entirely.
MAZE_RECENTER_TIMEOUT  = 2.0

# Multi-frame confirmation.  PROFILE_3 produces occasional single-frame
# spurious returns; require N consecutive frames before reacting.
MAZE_PROXIMITY_CONFIRM = 3
MAZE_BLOCKED_CONFIRM   = 3

# Pure time-based finish: after this many seconds since start, the
# robot stops and declares finished.  No junction count, no radar
# check — just the clock.  Tune to be slightly longer than your maze
# actually takes.  Run faster than expected? Lower it.  Slower? Raise it.
MAZE_RUN_DURATION = 85.0

# Serpentine trust mode.  When True, if the right peek verdict is BLOCKED,
# skip the left peek entirely and commit left.  This is correct for any
# maze where a "front + right blocked" geometry guarantees left is the
# only path forward (i.e., serpentines, single-path mazes).  Set False to
# restore the full peek-left-before-committing behaviour, which is needed
# for branching mazes with possible dead-ends.
MAZE_TRUST_SERPENTINE = True

# Reserved for future use (rear-radar safety during reverse maneuvers).
MAZE_REAR_SAFE_DIST    = 0.20

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
    return {"detected": False, "dist": None, "proximity": False,
            "intra": 0.0, "inter": 0.0}


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
# MAZE mode — right-hand wall following (reactive)
# ---------------------------------------------------------------------------

class MazeMode:
    """
    Reactive right-hand wall-following.

    Decision tree at every junction (front blocked):
      1. Peek RIGHT
      2. Right open    -> COMMIT_R
      3. Right blocked -> Peek LEFT
      4. Left open     -> COMMIT_L
      5. Left blocked  -> SPIN_180

    Hysteresis on peek classification:
      median > MAZE_OPEN_HI   -> OPEN
      median < MAZE_OPEN_LO   -> BLOCKED
      anywhere in between     -> UNCERTAIN (re-peek once with longer dwell)

    Body alignment after a turn is confirmed (in priority order) by:
      hall sensor fires  OR
      front-clear past MAZE_COMMIT_CLEAR_DIST after MAZE_COMMIT_MIN_DRIVE  OR
      MAZE_HALL_TIMEOUT (last resort)
    """

    S_ARMED          = "ARMED"
    S_DRIVE          = "DRIVE"
    S_PEEK_R_TURN    = "PEEK_R_TURN"     # rotating head right (timed)
    S_PEEK_R_READ    = "PEEK_R_READ"     # head right, sampling
    S_TO_LEFT        = "TO_LEFT"         # right blocked: rotating left through hall to left-90°
    S_PEEK_L_READ    = "PEEK_L_READ"     # head left, sampling
    S_COMMIT_R       = "COMMIT_R"        # head right, legs forward, body following
    S_COMMIT_L       = "COMMIT_L"        # head left,  legs forward, body following
    S_SPIN_180_TURN  = "SPIN_180_TURN"   # both sides blocked: another 90° left to reach 180°
    S_SPIN_180_DRIVE = "SPIN_180_DRIVE"  # head behind, legs forward, body 180°-ing
    S_FINISHED       = "FINISHED"

    def __init__(self, r):
        self.r              = r
        self.state          = None
        self.state_start    = 0.0
        self.active         = False
        self.mlog           = get_logger("Maze")

        # Last published peek distances (for dashboard)
        self.peek_dist      = None    # right peek
        self.left_dist      = None    # left peek

        # Sampling
        self._peek_samples: list = []
        self._uncertain_repeek: bool = False  # set true on first UNCERTAIN

        # Run-wide timers/counters
        self.run_start_time:    float | None = None
        self._front_open_count: int = 0
        self._proximity_count:  int = 0
        self._blocked_count:    int = 0
        self._junctions_done:   int = 0   # incremented on each COMMIT/SPIN → DRIVE

        # TO_LEFT uses a pure timer now (MAZE_PEEK_TURN_DUR * 2), no hall substate needed.

    # ------------------------------------------------------------------
    async def reset(self):
        await publish_velocity(self.r, 0.0, 0.0)
        self.state          = None
        self.active         = False
        self.peek_dist      = None
        self.left_dist      = None
        self._peek_samples  = []
        self._uncertain_repeek = False
        self.run_start_time    = None
        self._front_open_count = 0
        self._proximity_count  = 0
        self._blocked_count    = 0
        self._junctions_done   = 0
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
        if new_state == self.S_DRIVE:
            self._front_open_count = 0
            self._proximity_count  = 0
            self._blocked_count    = 0
        if new_state in (self.S_PEEK_R_READ, self.S_PEEK_L_READ):
            # Reset sampling at the start of a fresh read window
            self._peek_samples = []
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
    def _classify_peek(self, samples: list) -> tuple[str, float | None]:
        """
        Classify a peek as 'open' / 'blocked' / 'uncertain' from the
        collected samples.  Returns (verdict, median_or_None).

        Decision rules:
          - If >=60% of samples are None (no detection), the side is OPEN.
            (1D radar reports None when no return clears the signal threshold,
            which means the corridor extends past sensor range.)
          - Otherwise compute median of valid distances.
          - median >= MAZE_OPEN_HI -> OPEN
          - median <= MAZE_OPEN_LO -> BLOCKED
          - in-between             -> UNCERTAIN
        """
        if not samples:
            return "uncertain", None

        none_count = sum(1 for s in samples if s is None)
        if none_count >= len(samples) * 0.6:
            return "open", None

        valid = [s for s in samples if s is not None]
        if not valid:
            return "open", None

        valid_sorted = sorted(valid)
        median = valid_sorted[len(valid_sorted) // 2]

        if median >= MAZE_OPEN_HI:
            return "open", median
        if median <= MAZE_OPEN_LO:
            return "blocked", median
        return "uncertain", median

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------
    async def update(self):
        if not self.active:
            await self.start()
            return

        front = await read_front_radar(self.r)

        # ── PROXIMITY EMERGENCY ─────────────────────────────────────────
        # Only meaningful when the legs are driving forward.  In DRIVE we
        # treat it as a junction trigger (run the peek protocol).  In a
        # COMMIT we let the existing alignment logic finish — pulling out
        # of a commit mid-rotation creates worse problems than it solves.
        if self.state == self.S_DRIVE:
            if front.get("proximity", False):
                self._proximity_count += 1
            else:
                self._proximity_count = 0

            if self._proximity_count >= MAZE_PROXIMITY_CONFIRM:
                self._proximity_count = 0
                self._blocked_count   = 0
                self.mlog.warning(
                    "⚠ PROXIMITY ({} frames) — entering junction protocol",
                    MAZE_PROXIMITY_CONFIRM
                )
                await publish_velocity(self.r, 0.0, 0.0)
                await asyncio.sleep(0.05)
                await publish_velocity(self.r, 0.0, MAZE_TURN_SPEED)
                self._uncertain_repeek = False
                await self._enter(self.S_PEEK_R_TURN)
                return

        # ---- ARMED ----------------------------------------------------
        # Wait for two calibrations to complete before accepting start:
        #   1. Radar distance detector calibrated (core_radar.py sets KEY_DIST_CALIBRATED)
        #   2. Head calibrated (core_hardware.py's smart_calibrate_head sets
        #      KEY_HEAD_STATE with calibrated=True, head ends up centered)
        # Both run in parallel — wait is just "whichever finishes last".
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
                self.run_start_time = time.time()
                self._front_open_count = 0
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                await self._enter(self.S_DRIVE)
            elif int(self._elapsed()) % 2 == 0 and self._elapsed() % 1.0 < 0.1:
                self.mlog.info(
                    "All calibrated — place robot then: redis-cli set {} 1",
                    KEY_MAZE_START
                )
            return

        # ---- DRIVE ----------------------------------------------------
        # Forward, head/body aligned (hall=1).  Watch front for blockage
        # (junction) or sustained open space (finish line).
        if self.state == self.S_DRIVE:
            blocked = (
                front["detected"] and
                front["dist"] is not None and
                front["dist"] < MAZE_FRONT_BLOCK
            )
            if blocked:
                self._blocked_count += 1
            else:
                self._blocked_count = 0

            if self._blocked_count >= MAZE_BLOCKED_CONFIRM:
                self._blocked_count    = 0
                self._front_open_count = 0
                d_str = f"{front['dist']:.2f}m" if front.get('dist') is not None else "?"
                self.mlog.warning(
                    "Junction — front blocked at {} ({} frames). Peeking RIGHT first.",
                    d_str, MAZE_BLOCKED_CONFIRM
                )
                await publish_velocity(self.r, 0.0, 0.0)
                await asyncio.sleep(0.05)
                await publish_velocity(self.r, 0.0, MAZE_TURN_SPEED)
                self._uncertain_repeek = False
                await self._enter(self.S_PEEK_R_TURN)
                return

            # Finish detection — pure time-based.  After MAZE_RUN_DURATION
            # seconds since start, declare finished.  No junction count,
            # no radar checks, just the clock.
            if (self.run_start_time is not None and
                    time.time() - self.run_start_time >= MAZE_RUN_DURATION):
                elapsed = int(time.time() - self.run_start_time)
                self.mlog.success(
                    "🏁 FINISH — {}s elapsed (MAZE_RUN_DURATION = {}s)",
                    elapsed, MAZE_RUN_DURATION
                )
                await publish_velocity(self.r, 0.0, 0.0)
                await self._enter(self.S_FINISHED)
                return
            return

        # ---- PEEK_R_TURN ---------------------------------------------
        # Rotating head right at MAZE_TURN_SPEED.  Started from hall-aligned
        # so MAZE_PEEK_TURN_DUR is reliably 90°.
        if self.state == self.S_PEEK_R_TURN:
            if self._elapsed() >= MAZE_PEEK_TURN_DUR:
                await publish_velocity(self.r, 0.0, 0.0)
                await self._enter(self.S_PEEK_R_READ)
            return

        # ---- PEEK_R_READ ---------------------------------------------
        # Head stopped, pointing right.  Settle, then collect samples.
        if self.state == self.S_PEEK_R_READ:
            if self._elapsed() < MAZE_RADAR_SETTLE:
                # Pre-settle: discard whatever's in the buffer
                self._peek_samples = []
                return

            self._peek_samples.append(front.get("dist"))

            confirm_dur = MAZE_PEEK_CONFIRM_DUR
            if self._uncertain_repeek:
                confirm_dur *= MAZE_REPEEK_DWELL_MULT

            if self._elapsed() < MAZE_RADAR_SETTLE + confirm_dur:
                return

            if len(self._peek_samples) < MAZE_PEEK_MIN_SAMPLES:
                # Not enough data yet, keep collecting
                return

            verdict, median = self._classify_peek(self._peek_samples)
            self.peek_dist = round(median, 3) if median is not None else None
            m_str = f"{median:.2f}m" if median is not None else "open"
            self.mlog.info(
                "Right peek: n={} verdict={} median={} (repeek={})",
                len(self._peek_samples), verdict, m_str, self._uncertain_repeek
            )

            if verdict == "open":
                self.mlog.success("Right OPEN → COMMIT_R")
                self._uncertain_repeek = False
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                await self._enter(self.S_COMMIT_R)

            elif verdict == "blocked":
                self.mlog.info("Right BLOCKED → peeking LEFT")
                self._uncertain_repeek = False
                # Start head rotating LEFT.  TO_LEFT will re-anchor at
                # the hall pulse and time another 90° from there.
                await publish_velocity(self.r, 0.0, -MAZE_TURN_SPEED)
                await self._enter(self.S_TO_LEFT)

            else:  # uncertain
                if self._uncertain_repeek:
                    self.mlog.warning(
                        "Right UNCERTAIN twice — treating as BLOCKED, peeking LEFT"
                    )
                    self._uncertain_repeek = False
                    await publish_velocity(self.r, 0.0, -MAZE_TURN_SPEED)
                    await self._enter(self.S_TO_LEFT)
                else:
                    self.mlog.warning("Right UNCERTAIN — re-peeking with longer dwell")
                    self._uncertain_repeek = True
                    # Re-enter same state with fresh sample buffer + timer
                    await self._enter(self.S_PEEK_R_READ)
            return

        # ---- TO_LEFT --------------------------------------------------
        # Rotating head left from right-90° to left-90°.
        # That is exactly 180° = MAZE_PEEK_TURN_DUR * 2.
        # Pure timer — no hall sensor.  The hall detection during this
        # rotation was unreliable: sometimes it fires early (false positive
        # while head is still near right-90°, causing the +1s timed leg to
        # land at the wrong angle), sometimes it never fires at all (4s
        # timeout, then +1s = head has rotated 450° before COMMIT_L).
        # Both produced excessive maniac turning.  A fixed 2× timer is
        # consistent and requires no re-anchoring.
        if self.state == self.S_TO_LEFT:
            if self._elapsed() >= MAZE_PEEK_TURN_DUR * 2:
                if MAZE_TRUST_SERPENTINE:
                    self.mlog.success("Serpentine trust: right BLOCKED → COMMIT_L")
                    await publish_velocity(self.r, 0.0, 0.0)
                    await asyncio.sleep(0.1)
                    await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                    await self._enter(self.S_COMMIT_L)
                else:
                    await publish_velocity(self.r, 0.0, 0.0)
                    await self._enter(self.S_PEEK_L_READ)
            return

        # ---- PEEK_L_READ ---------------------------------------------
        # Head stopped, pointing left.  Same protocol as PEEK_R_READ.
        if self.state == self.S_PEEK_L_READ:
            if self._elapsed() < MAZE_RADAR_SETTLE:
                self._peek_samples = []
                return

            self._peek_samples.append(front.get("dist"))

            confirm_dur = MAZE_PEEK_CONFIRM_DUR
            if self._uncertain_repeek:
                confirm_dur *= MAZE_REPEEK_DWELL_MULT

            if self._elapsed() < MAZE_RADAR_SETTLE + confirm_dur:
                return

            if len(self._peek_samples) < MAZE_PEEK_MIN_SAMPLES:
                return

            verdict, median = self._classify_peek(self._peek_samples)
            self.left_dist = round(median, 3) if median is not None else None
            m_str = f"{median:.2f}m" if median is not None else "open"
            self.mlog.info(
                "Left peek: n={} verdict={} median={} (repeek={})",
                len(self._peek_samples), verdict, m_str, self._uncertain_repeek
            )

            if verdict == "open":
                self.mlog.success("Left OPEN → COMMIT_L")
                self._uncertain_repeek = False
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                await self._enter(self.S_COMMIT_L)

            elif verdict == "blocked":
                self.mlog.warning("Both sides BLOCKED → SPIN 180°")
                self._uncertain_repeek = False
                # Head is at left-90°.  Continue rotating left another
                # MAZE_PEEK_TURN_DUR to reach 180° from forward.
                await publish_velocity(self.r, 0.0, -MAZE_TURN_SPEED)
                await self._enter(self.S_SPIN_180_TURN)

            else:  # uncertain
                if self._uncertain_repeek:
                    self.mlog.warning(
                        "Left UNCERTAIN twice — treating as BLOCKED, SPIN 180°"
                    )
                    self._uncertain_repeek = False
                    await publish_velocity(self.r, 0.0, -MAZE_TURN_SPEED)
                    await self._enter(self.S_SPIN_180_TURN)
                else:
                    self.mlog.warning("Left UNCERTAIN — re-peeking with longer dwell")
                    self._uncertain_repeek = True
                    await self._enter(self.S_PEEK_L_READ)
            return

        # ---- COMMIT_R / COMMIT_L -------------------------------------
        # Head at side-90°, legs forward.  Body rotates due to leg drive
        # against head leverage.  Exit after MAZE_COMMIT_DURATION elapses.
        # Adjust MAZE_COMMIT_DURATION up/down to tune the body rotation
        # amount.  No recenter, no hall check, no radar fallback — just
        # a clean timer.
        if self.state in (self.S_COMMIT_R, self.S_COMMIT_L):
            await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)

            if self._elapsed() >= MAZE_COMMIT_DURATION:
                self._junctions_done += 1
                self.mlog.success(
                    "Commit {:.1f}s complete — DRIVE [junction #{}]",
                    MAZE_COMMIT_DURATION, self._junctions_done
                )
                await self._enter(self.S_DRIVE)
            return

        # ---- SPIN_180_TURN -------------------------------------------
        # Already rotated ~90° left (during PEEK_L), now another ~90° to hit 180°
        if self.state == self.S_SPIN_180_TURN:
            if self._elapsed() >= MAZE_PEEK_TURN_DUR:
                await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)
                await self._enter(self.S_SPIN_180_DRIVE)
            return

        # ---- SPIN_180_DRIVE ------------------------------------------
        # Unreachable while MAZE_TRUST_SERPENTINE = True (kept for the
        # branching-maze fallback only).  Same simple timer as COMMIT
        # but for a 180° body rotation.
        if self.state == self.S_SPIN_180_DRIVE:
            await publish_velocity(self.r, MAZE_DRIVE_SPEED, 0.0)

            if self._elapsed() >= MAZE_COMMIT_DURATION * 2:
                self._junctions_done += 1
                self.mlog.success(
                    "Spin-180 {:.1f}s complete — DRIVE [junction #{}]",
                    MAZE_COMMIT_DURATION * 2, self._junctions_done
                )
                await self._enter(self.S_DRIVE)
            return

        # ---- FINISHED ------------------------------------------------
        # Maze complete — motors stopped, waiting for mode change.
        if self.state == self.S_FINISHED:
            await publish_velocity(self.r, 0.0, 0.0)
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