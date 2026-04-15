#!/usr/bin/env python3
"""
Test both channels with PWM on IN1/IN2.
PWMA/PWMB must be wired to 3.3V.
Nothing connected to A01/A02 or B01/B02 — just measure with multimeter.

Expected: ~6V across A01-A02, then ~6V across B01-B02.
Ctrl+C to stop.
"""

import pigpio
import time

pi = pigpio.pi()
if not pi.connected:
    print("ERROR: pigpiod not running. Start it with: sudo pigpiod")
    exit(1)

STBY = 21
LEG_IN1  = 16  # AIN1
LEG_IN2  = 20  # AIN2
HEAD_IN1 = 19  # BIN1
HEAD_IN2 = 26  # BIN2

# Setup
pi.set_mode(STBY, pigpio.OUTPUT)
pi.write(STBY, 1)

for pin in [LEG_IN1, LEG_IN2, HEAD_IN1, HEAD_IN2]:
    pi.set_mode(pin, pigpio.OUTPUT)
    pi.set_PWM_frequency(pin, 1000)
    pi.set_PWM_dutycycle(pin, 0)

try:
    # --- Channel A (legs) ---
    print("=== CHANNEL A (legs) — full speed forward ===")
    print("Measure across A01 and A02. Expected: ~6V")
    pi.set_PWM_dutycycle(LEG_IN1, 255)  # full
    pi.set_PWM_dutycycle(LEG_IN2, 0)
    input("Press Enter when done measuring A...")

    pi.set_PWM_dutycycle(LEG_IN1, 0)
    pi.set_PWM_dutycycle(LEG_IN2, 0)
    time.sleep(0.5)

    # --- Channel B (head) ---
    print("\n=== CHANNEL B (head) — full speed forward ===")
    print("Measure across B01 and B02. Expected: ~6V")
    pi.set_PWM_dutycycle(HEAD_IN1, 255)  # full
    pi.set_PWM_dutycycle(HEAD_IN2, 0)
    input("Press Enter when done measuring B...")

    pi.set_PWM_dutycycle(HEAD_IN1, 0)
    pi.set_PWM_dutycycle(HEAD_IN2, 0)
    time.sleep(0.5)

    # --- Half speed test ---
    print("\n=== CHANNEL A — 50% speed ===")
    print("Measure across A01 and A02. Expected: ~3V (PWM average)")
    pi.set_PWM_dutycycle(LEG_IN1, 128)  # 50%
    pi.set_PWM_dutycycle(LEG_IN2, 0)
    input("Press Enter when done measuring...")

    print("\nDone!")

except KeyboardInterrupt:
    print("\nAborted!")

finally:
    for pin in [LEG_IN1, LEG_IN2, HEAD_IN1, HEAD_IN2]:
        pi.set_PWM_dutycycle(pin, 0)
    pi.write(STBY, 0)
    pi.stop()
    print("All pins off.")