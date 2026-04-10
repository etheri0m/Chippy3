import json
from valkey import Valkey


def run_kinematics():
    r = Valkey(host='localhost', port=6379, decode_responses=True)
    pubsub = r.pubsub()
    pubsub.subscribe('chippy:cmd:velocity')

    print("Kinematics Node Active. Listening for velocity vectors...")

    for message in pubsub.listen():
        if message['type'] == 'message':
            try:
                data = json.loads(message['data'])
                v = max(-1.0, min(1.0, data.get('v', 0.0)))
                w = max(-1.0, min(1.0, data.get('w', 0.0)))

                # Leg Logic (Standard PWM)
                leg_pwm = int(abs(v) * 255)
                leg_dir = 0 if v >= 0 else 1

                # Head Logic (Coupled Steering)
                if abs(w) < 0.05:
                    # If joystick is centered or in DRIVE mode,
                    # send a special flag to hardware to RE-HOME/CENTER
                    head_pwm = 0
                    head_dir = 0
                    auto_center = True
                else:
                    head_pwm = int(abs(w) * 255)
                    head_dir = 0 if w >= 0 else 1
                    auto_center = False

                cmd = {
                    "leg_pwm": leg_pwm,
                    "leg_dir": leg_dir,
                    "head_pwm": head_pwm,
                    "head_dir": head_dir,
                    "auto_center": auto_center
                }
                r.set('chippy:cmd:motors', json.dumps(cmd))

            except Exception:
                pass


if __name__ == "__main__":
    run_kinematics()