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
CROWD_WINDOW_FRAMES  = 40
CROWD_HIGH_THRESHOLD = 8.0
CROWD_LOW_THRESHOLD  = 3.0

# --- MAZE mode ---
MAZE_FRONT_OBSTACLE_DIST = 0.35
MAZE_REAR_OBSTACLE_DIST  = 0.25
MAZE_SEQUENCE = [
    {"v": 0.7, "w": 0.0,  "duration": 1.5},
    {"v": 0.0, "w": 0.8,  "duration": 0.4},
    {"v": 0.7, "w": 0.0,  "duration": 1.2},
    {"v": 0.0, "w": -0.8, "duration": 0.4},
    {"v": 0.7, "w": 0.0,  "duration": 1.0},
    {"v": 0.0, "w": 0.0,  "duration": 0.3},
]

# --- Valkey keys ---
KEY_MODE       = 'chippy:mode'
KEY_VELOCITY   = 'chippy:cmd:velocity'
KEY_RADAR_REAR = 'chippy:state:radar:rear'
KEY_RADAR_FR   = 'chippy:state:radar:front'
KEY_CROWD      = 'chippy:state:crowd'
KEY_MAZE       = 'chippy:state:maze'

MODE_FOLLOW = "FOLLOW"
MODE_CROWD  = "CROWD"
MODE_MAZE   = "MAZE"
VALID_MODES = {MODE_FOLLOW, MODE_CROWD, MODE_MAZE}


async def publish_velocity(r, v: float, w: float):
    await r.publish(KEY_VELOCITY, orjson.dumps({"v": round(v, 3), "w": round(w, 3)}).decode())


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
# Head sweep-and-lock
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
        self.hlog         = get_logger("Head")

    async def reset(self):
        await self._send_w(0.0)
        self.state       = self.STOPPED
        self.sweep_start = None
        self.sweep_total = None
        self.last_w      = None

    async def _send_w(self, w: float, v: float = 0.0):
        if w != self.last_w:
            await publish_velocity(self.r, v, w)
            self.last_w = w

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

    async def reset(self):
        await publish_velocity(self.r, 0.0, 0.0)
        self.inter_window.clear()
        self.intra_window.clear()

    def _classify(self, avg_inter: float) -> str:
        if avg_inter >= CROWD_HIGH_THRESHOLD:
            return "BUSY"
        elif avg_inter >= CROWD_LOW_THRESHOLD:
            return "LOW"
        return "EMPTY"

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
# MAZE mode
# ---------------------------------------------------------------------------

class MazeMode:
    def __init__(self, r):
        self.r             = r
        self.step          = 0
        self.step_start    = None
        self.active        = False
        self.obstacle_hold = False
        self.mlog          = get_logger("Maze")

    async def reset(self):
        await publish_velocity(self.r, 0.0, 0.0)
        self.step          = 0
        self.step_start    = None
        self.active        = False
        self.obstacle_hold = False

    async def start(self):
        self.step          = 0
        self.step_start    = time.time()
        self.active        = True
        self.obstacle_hold = False
        await self._dispatch_step()

    async def _dispatch_step(self):
        s = MAZE_SEQUENCE[self.step]
        await publish_velocity(self.r, s["v"], s["w"])
        self.mlog.info(
            "Step {}/{} — v:{:+.1f} w:{:+.1f} for {}s",
            self.step + 1, len(MAZE_SEQUENCE), s['v'], s['w'], s['duration']
        )
        await self._publish_state("RUNNING")

    async def _publish_state(self, status: str):
        await self.r.set(KEY_MAZE, orjson.dumps({
            "status":      status,
            "step":        self.step + 1,
            "total_steps": len(MAZE_SEQUENCE),
            "obstacle":    self.obstacle_hold,
            "ts":          round(time.time(), 4),
        }).decode())

    async def update(self):
        if not self.active:
            await self.start()
            return

        s     = MAZE_SEQUENCE[self.step]
        front = await read_front_radar(self.r)
        rear  = await read_rear_radar(self.r)

        front_blocked = (
            s["v"] > 0 and
            front["detected"] and
            front["dist"] is not None and
            front["dist"] < MAZE_FRONT_OBSTACLE_DIST
        )

        rear_blocked = (
            s["v"] < 0 and
            rear["detected"] and
            rear["dist"] is not None and
            rear["dist"] < MAZE_REAR_OBSTACLE_DIST
        )

        obstacle_now = front_blocked or rear_blocked

        if obstacle_now and not self.obstacle_hold:
            self.obstacle_hold = True
            await publish_velocity(self.r, 0.0, s["w"])
            direction = "front" if front_blocked else "rear"
            dist = front["dist"] if front_blocked else rear["dist"]
            self.mlog.warning("{} obstacle at {:.2f}m — legs paused", direction, dist)
            await self._publish_state("OBSTACLE_HOLD")
        elif not obstacle_now and self.obstacle_hold:
            self.obstacle_hold = False
            await publish_velocity(self.r, s["v"], s["w"])
            self.mlog.info("Obstacle cleared — resuming")

        if not self.obstacle_hold:
            if time.time() - self.step_start >= s["duration"]:
                self.step += 1
                if self.step >= len(MAZE_SEQUENCE):
                    await publish_velocity(self.r, 0.0, 0.0)
                    await self._publish_state("COMPLETE")
                    self.mlog.success("Sequence complete")
                    self.active = False
                    return
                self.step_start = time.time()
                await self._dispatch_step()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_controller():
    r = avalkey.Valkey(host='localhost', port=6379, decode_responses=True)

    if not await r.exists(KEY_MODE):
        await r.set(KEY_MODE, MODE_FOLLOW)

    # Rear radar is optional
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

    active_mode = None

    if rear_detector:
        log.info("Running — rear radar active")
    else:
        log.info("Running — front radar only")
    log.info("Modes: redis-cli set {} FOLLOW|CROWD|MAZE", KEY_MODE)
    log.info("FOLLOW zones: <{}m back | {}-{}m hold | >{}m forward",
             NEAR_ZONE, NEAR_ZONE, FAR_ZONE, FAR_ZONE)

    try:
        while True:
            if rear_detector:
                result = await asyncio.to_thread(rear_detector.get_next)
                detected = result.presence_detected
                raw_dist = result.presence_distance if detected else None
                intra    = result.intra_presence_score
                inter    = result.inter_presence_score

                await r.set(KEY_RADAR_REAR, orjson.dumps({
                    "detected": detected,
                    "dist":     raw_dist,
                    "intra":    round(intra, 2),
                    "inter":    round(inter, 2),
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
                active_mode = mode

            if mode == MODE_FOLLOW:
                await follow.update()
            elif mode == MODE_CROWD:
                await crowd.update()
            elif mode == MODE_MAZE:
                await maze.update()

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