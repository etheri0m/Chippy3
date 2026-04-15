import json
import time
from valkey import Valkey
from log_config import get_logger

log = get_logger("Kinematics")

# Minimum speed below which a motor is stopped rather than driven slowly.
# Prevents motor whine and jitter at near-zero commands.
DEADBAND = 0.05

KEY_VELOCITY = 'chippy:cmd:velocity'   # INPUT  — receives {"v": float, "w": float}
KEY_MOTORS   = 'chippy:cmd:motors'     # OUTPUT — sends to hardware node
KEY_STATE    = 'chippy:state:kinematics'  # Published every cycle for GUI


def velocity_to_motor(value: float, target: str) -> dict:
    """
    Convert a signed velocity (-1.0 to 1.0) to a motor command dict.
    Positive = forward, negative = backward, within deadband = stop.
    """
    speed = abs(value)

    if speed < DEADBAND:
        return {"target": target, "dir": "stop", "speed": 0.0}
    elif value > 0:
        return {"target": target, "dir": "forward",  "speed": round(speed, 3)}
    else:
        return {"target": target, "dir": "backward", "speed": round(speed, 3)}


def run_kinematics():
    r = Valkey(host='localhost', port=6379, decode_responses=True)
    pubsub = r.pubsub()
    pubsub.subscribe(KEY_VELOCITY)

    log.info("Active — listening on chippy:cmd:velocity")
    log.info('GUI/maze/manual: publish {{"v": float, "w": float}}')

    for message in pubsub.listen():
        if message['type'] != 'message':
            continue

        try:
            data = json.loads(message['data'])

            # Clamp both axes to [-1.0, 1.0]
            v = max(-1.0, min(1.0, float(data.get('v', 0.0))))
            w = max(-1.0, min(1.0, float(data.get('w', 0.0))))

            leg_cmd  = velocity_to_motor(v, "legs")
            head_cmd = velocity_to_motor(w, "head")

            r.publish(KEY_MOTORS, json.dumps(leg_cmd))
            r.publish(KEY_MOTORS, json.dumps(head_cmd))

            # Write current kinematic state for GUI to read
            r.set(KEY_STATE, json.dumps({
                "v":        v,
                "w":        w,
                "leg_dir":  leg_cmd["dir"],
                "head_dir": head_cmd["dir"],
                "ts":       round(time.time(), 4),
            }))

        except Exception as e:
            log.error("Bad message: {}", e)


if __name__ == "__main__":
    run_kinematics()