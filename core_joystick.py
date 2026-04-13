import time
import json
import collections
from valkey import Valkey
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.presence import Detector, DetectorConfig

REAR_RADAR_PORT = "/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800352-if00-port0"

# --- FOLLOW mode ---
# Front radar hand distance zones:
#   < NEAR_ZONE  → robot backs away
#   > FAR_ZONE   → robot walks toward hand
#   between      → robot holds still (neutral dead zone)
# Hand detection requires BOTH:
#   - distance within MAX_HAND_DIST (filters out bystanders standing further away)
#   - intra score above MIN_HAND_INTRA (filters out still objects / people not moving)
NEAR_ZONE      = 0.20   # metres — hand closer than this = back up
FAR_ZONE       = 0.30   # metres — hand further than this = walk forward
MAX_HAND_DIST  = 0.50   # metres — anything beyond this is ignored (not a hand)
FOLLOW_SPEED   = 0.7    # leg motor speed (0.0–1.0)
MIN_HAND_INTRA = 4.0    # intra score below this = hand gone (cuts through lingering inter)

# Head sweep (front radar)
# When front radar loses presence, head sweeps back and forth until it finds something
SWEEP_SPEED    = 0.8   # slow sweep so it doesn't overshoot
SWEEP_DURATION = 0.8   # seconds in each sweep direction before reversing
SWEEP_TIMEOUT  = 3.0   # seconds total before sweep gives up and head stops

# --- CROWD mode ---
CROWD_WINDOW_FRAMES  = 40
CROWD_HIGH_THRESHOLD = 8.0
CROWD_LOW_THRESHOLD  = 3.0

# --- MAZE mode ---
MAZE_FRONT_OBSTACLE_DIST = 0.35   # forward obstacle guard
MAZE_REAR_OBSTACLE_DIST  = 0.25   # rear safety guard
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


def publish_velocity(r, v: float, w: float):
    r.publish(KEY_VELOCITY, json.dumps({"v": round(v, 3), "w": round(w, 3)}))


def read_front_radar(r) -> dict:
    raw = r.get(KEY_RADAR_FR)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"detected": False, "dist": None, "intra": 0.0, "inter": 0.0}


def read_rear_radar(r) -> dict:
    raw = r.get(KEY_RADAR_REAR)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"detected": False, "dist": None, "intra": 0.0, "inter": 0.0}


# ---------------------------------------------------------------------------
# Head sweep-and-lock
# Uses FRONT radar only.
# ---------------------------------------------------------------------------

class HeadSweep:
    """
    Continuously locks the head onto whatever the front radar sees.
    When front radar detects presence → stop head (locked).
    When front radar loses presence  → sweep head back and forth until
    something is found again.

    Publishes w via publish_velocity — caller controls v independently.
    """

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

    def reset(self):
        self._send_w(0.0)
        self.state       = self.STOPPED
        self.sweep_start = None
        self.sweep_total = None
        self.last_w      = None

    def _send_w(self, w: float, v: float = 0.0):
        """Only publish on change to avoid bus flooding."""
        if w != self.last_w:
            publish_velocity(self.r, v, w)
            self.last_w = w

    def update(self, front: dict, leg_v: float = 0.0):
        """
        Call every loop with front radar data and desired leg velocity.
        Head sweep piggybacks on the same velocity publish.
        """
        if front["detected"]:
            # Locked onto something — stop head
            if self.state != self.LOCKED:
                self.state       = self.LOCKED
                self.sweep_start = None
                self.sweep_total = None
                print("[HEAD ] Locked on target.")
            self._send_w(0.0, leg_v)

        else:
            if self.state == self.LOCKED:
                # Just lost target — start sweeping
                self.state       = self.SWEEPING
                self.sweep_dir   = 1.0
                self.sweep_start = time.time()
                self.sweep_total = time.time()
                print("[HEAD ] Target lost — sweeping.")

            if self.state == self.SWEEPING:
                elapsed_total = time.time() - self.sweep_total

                if elapsed_total >= SWEEP_TIMEOUT:
                    # Gave up — stop head and wait
                    self.state = self.STOPPED
                    self._send_w(0.0, leg_v)
                    print("[HEAD ] Sweep timeout — stopped.")
                else:
                    # Reverse direction each SWEEP_DURATION
                    if time.time() - self.sweep_start >= SWEEP_DURATION:
                        self.sweep_dir  = -self.sweep_dir
                        self.sweep_start = time.time()
                    self._send_w(self.sweep_dir * SWEEP_SPEED, leg_v)

            elif self.state == self.STOPPED:
                # Stopped and nothing detected — just hold still
                self._send_w(0.0, leg_v)

        return self.state


# ---------------------------------------------------------------------------
# FOLLOW mode — front radar drives BOTH legs and head
# ---------------------------------------------------------------------------

class FollowMode:
    """
    Front radar controls everything:
      - Hand distance zones drive legs (forward / backward / hold / stop)
      - Presence detection drives head via HeadSweep (sweep-and-lock)

    Zones:
      hand < NEAR_ZONE  → back away  (v = -FOLLOW_SPEED)
      hand > FAR_ZONE   → approach   (v = +FOLLOW_SPEED)
      between           → hold still (v = 0.0)
      hand gone         → stop legs, head sweeps looking for target
    """

    def __init__(self, r):
        self.r    = r
        self.head = HeadSweep(r)
        self.last_v = None

    def reset(self):
        self.head.reset()
        publish_velocity(self.r, 0.0, 0.0)
        self.last_v = None

    def update(self):
        front = read_front_radar(self.r)

        # Gate on distance + intra score:
        #   - MAX_HAND_DIST filters out people standing further away
        #   - MIN_HAND_INTRA filters out still objects (inter lingers, intra drops fast)
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

        # Head sweep handles its own publish including leg_v
        head_state = self.head.update(front, leg_v)

        dist_str = f"{raw_dist:.3f}m" if raw_dist is not None else "None   "
        intra_str = f"{front.get('intra', 0.0):.1f}"
        print(
            f"[FOLLOW] Hand: {dist_str} | Zone: {zone:4} | "
            f"Legs: {leg_v:+.1f} | Head: {head_state} | Intra: {intra_str}"
        )


# ---------------------------------------------------------------------------
# CROWD mode — uses BOTH radars for wider coverage
# ---------------------------------------------------------------------------

class CrowdMode:
    def __init__(self, r):
        self.r            = r
        self.inter_window = collections.deque(maxlen=CROWD_WINDOW_FRAMES)
        self.intra_window = collections.deque(maxlen=CROWD_WINDOW_FRAMES)

    def reset(self):
        publish_velocity(self.r, 0.0, 0.0)
        self.inter_window.clear()
        self.intra_window.clear()

    def _classify(self, avg_inter: float) -> str:
        if avg_inter >= CROWD_HIGH_THRESHOLD:
            return "BUSY"
        elif avg_inter >= CROWD_LOW_THRESHOLD:
            return "LOW"
        return "EMPTY"

    def update(self):
        front = read_front_radar(self.r)
        rear  = read_rear_radar(self.r)

        # Combine both radars: take the max of each score for wider coverage
        combined_inter = max(front["inter"], rear["inter"])
        combined_intra = max(front["intra"], rear["intra"])

        self.inter_window.append(combined_inter)
        self.intra_window.append(combined_intra)

        avg_inter = sum(self.inter_window) / len(self.inter_window)
        avg_intra = sum(self.intra_window) / len(self.intra_window)
        density   = self._classify(avg_inter)

        # Report front radar for distance (forward-facing is more useful)
        self.r.set(KEY_CROWD, json.dumps({
            "density":   density,
            "avg_inter": round(avg_inter, 2),
            "avg_intra": round(avg_intra, 2),
            "detected":  front["detected"] or rear["detected"],
            "dist":      front["dist"],
            "ts":        round(time.time(), 4),
        }))

        print(f"[CROWD] {density:5} | Inter: {avg_inter:5.1f} | Intra: {avg_intra:5.1f}")


# ---------------------------------------------------------------------------
# MAZE mode — front for forward obstacles, rear for backward safety
# ---------------------------------------------------------------------------

class MazeMode:
    def __init__(self, r):
        self.r             = r
        self.step          = 0
        self.step_start    = None
        self.active        = False
        self.obstacle_hold = False

    def reset(self):
        publish_velocity(self.r, 0.0, 0.0)
        self.step          = 0
        self.step_start    = None
        self.active        = False
        self.obstacle_hold = False

    def start(self):
        self.step          = 0
        self.step_start    = time.time()
        self.active        = True
        self.obstacle_hold = False
        self._dispatch_step()

    def _dispatch_step(self):
        s = MAZE_SEQUENCE[self.step]
        publish_velocity(self.r, s["v"], s["w"])
        print(f"[MAZE ] Step {self.step+1}/{len(MAZE_SEQUENCE)} — "
              f"v:{s['v']:+.1f} w:{s['w']:+.1f} for {s['duration']}s")
        self._publish_state("RUNNING")

    def _publish_state(self, status: str):
        self.r.set(KEY_MAZE, json.dumps({
            "status":      status,
            "step":        self.step + 1,
            "total_steps": len(MAZE_SEQUENCE),
            "obstacle":    self.obstacle_hold,
            "ts":          round(time.time(), 4),
        }))

    def update(self):
        if not self.active:
            self.start()
            return

        s     = MAZE_SEQUENCE[self.step]
        front = read_front_radar(self.r)
        rear  = read_rear_radar(self.r)

        # Forward obstacle: only matters when moving forward (v > 0)
        front_blocked = (
            s["v"] > 0 and
            front["detected"] and
            front["dist"] is not None and
            front["dist"] < MAZE_FRONT_OBSTACLE_DIST
        )

        # Rear safety: only matters when moving backward (v < 0)
        rear_blocked = (
            s["v"] < 0 and
            rear["detected"] and
            rear["dist"] is not None and
            rear["dist"] < MAZE_REAR_OBSTACLE_DIST
        )

        obstacle_now = front_blocked or rear_blocked

        if obstacle_now and not self.obstacle_hold:
            self.obstacle_hold = True
            publish_velocity(self.r, 0.0, s["w"])
            direction = "front" if front_blocked else "rear"
            dist = front["dist"] if front_blocked else rear["dist"]
            print(f"[MAZE ] {direction} obstacle at {dist:.2f}m — legs paused.")
            self._publish_state("OBSTACLE_HOLD")
        elif not obstacle_now and self.obstacle_hold:
            self.obstacle_hold = False
            publish_velocity(self.r, s["v"], s["w"])
            print(f"[MAZE ] Obstacle cleared — resuming.")

        if not self.obstacle_hold:
            if time.time() - self.step_start >= s["duration"]:
                self.step += 1
                if self.step >= len(MAZE_SEQUENCE):
                    publish_velocity(self.r, 0.0, 0.0)
                    self._publish_state("COMPLETE")
                    print("[MAZE ] Sequence complete.")
                    self.active = False
                    return
                self.step_start = time.time()
                self._dispatch_step()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_controller():
    r = Valkey(host='localhost', port=6379, decode_responses=True)
    if not r.exists(KEY_MODE):
        r.set(KEY_MODE, MODE_FOLLOW)

    # Rear radar is optional — FOLLOW only needs front, CROWD/MAZE degrade gracefully
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
        print("[Controller] Rear radar connected.")
    except Exception as e:
        print(f"[Controller] Rear radar unavailable ({e}) — running without it.")
        rear_client   = None
        rear_detector = None

    follow = FollowMode(r)
    crowd  = CrowdMode(r)
    maze   = MazeMode(r)

    active_mode = None

    if rear_detector:
        print("[Controller] Running — rear radar active.")
    else:
        print("[Controller] Running — front radar only.")
    print(f"[Controller] Modes: redis-cli set {KEY_MODE} FOLLOW|CROWD|MAZE")
    print(f"[Controller] FOLLOW zones: <{NEAR_ZONE}m back | {NEAR_ZONE}-{FAR_ZONE}m hold | >{FAR_ZONE}m forward\n")

    try:
        while True:
            # If rear radar is connected, it drives the loop timing.
            # Otherwise, sleep at ~20Hz to match front radar frame rate.
            if rear_detector:
                result   = rear_detector.get_next()
                detected = result.presence_detected
                raw_dist = result.presence_distance if detected else None
                intra    = result.intra_presence_score
                inter    = result.inter_presence_score

                r.set(KEY_RADAR_REAR, json.dumps({
                    "detected": detected,
                    "dist":     raw_dist,
                    "intra":    round(intra, 2),
                    "inter":    round(inter, 2),
                    "ts":       round(time.time(), 4),
                }))
            else:
                time.sleep(0.05)  # ~20Hz

            raw_mode = r.get(KEY_MODE)
            mode     = raw_mode if raw_mode in VALID_MODES else MODE_FOLLOW

            # Mode transition
            if mode != active_mode:
                print(f"\n--- MODE: {active_mode} → {mode} ---\n")
                if active_mode == MODE_FOLLOW: follow.reset()
                elif active_mode == MODE_CROWD: crowd.reset()
                elif active_mode == MODE_MAZE:  maze.reset()
                active_mode = mode

            if mode == MODE_FOLLOW:
                follow.update()
            elif mode == MODE_CROWD:
                crowd.update()
            elif mode == MODE_MAZE:
                maze.update()

    except KeyboardInterrupt:
        pass
    finally:
        try:
            if rear_detector:
                rear_detector.stop()
            if rear_client:
                rear_client.close()
        except Exception:
            pass
        publish_velocity(r, 0.0, 0.0)
        print("[Controller] Stopped.")


if __name__ == "__main__":
    run_controller()