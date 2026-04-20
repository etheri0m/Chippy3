import asyncio
import time
import orjson
import valkey.asyncio as avalkey
from log_config import get_logger

log = get_logger("Kinematics")

DEADBAND = 0.05

KEY_VELOCITY = 'chippy:cmd:velocity'
KEY_MOTORS   = 'chippy:cmd:motors'
KEY_STATE    = 'chippy:state:kinematics'


def velocity_to_motor(value: float, target: str) -> dict:
    speed = abs(value)
    if speed < DEADBAND:
        return {"target": target, "dir": "stop", "speed": 0.0}
    elif value > 0:
        return {"target": target, "dir": "forward",  "speed": round(speed, 3)}
    else:
        return {"target": target, "dir": "backward", "speed": round(speed, 3)}


async def run_kinematics():
    r = avalkey.Valkey(host='localhost', port=6379, decode_responses=True,
                       socket_timeout=None)
    pubsub = r.pubsub()
    await pubsub.subscribe(KEY_VELOCITY)

    log.info("Active — listening on chippy:cmd:velocity")

    try:
        async for message in pubsub.listen():
            if message['type'] != 'message':
                continue

            try:
                data = orjson.loads(message['data'])

                v = max(-1.0, min(1.0, float(data.get('v', 0.0))))
                w = max(-1.0, min(1.0, float(data.get('w', 0.0))))

                leg_cmd  = velocity_to_motor(v, "legs")
                head_cmd = velocity_to_motor(w, "head")

                await r.publish(KEY_MOTORS, orjson.dumps(leg_cmd).decode())
                await r.publish(KEY_MOTORS, orjson.dumps(head_cmd).decode())

                await r.set(KEY_STATE, orjson.dumps({
                    "v":        v,
                    "w":        w,
                    "leg_dir":  leg_cmd["dir"],
                    "head_dir": head_cmd["dir"],
                    "ts":       round(time.time(), 4),
                }).decode())

            except Exception as e:
                log.error("Bad message: {}", e)
    finally:
        await pubsub.unsubscribe()
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(run_kinematics())