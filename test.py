#!/usr/bin/env python3
"""
Simplest legs test. Full speed forward. Ctrl+C to stop.
"""

import pigpio
import time

pi = pigpio.pi()

pi.set_mode(21, pigpio.OUTPUT)  # STBY
pi.write(21, 1)

pi.set_mode(16, pigpio.OUTPUT)  # AIN1
pi.set_mode(20, pigpio.OUTPUT)  # AIN2
pi.write(16, 1)
pi.write(20, 0)

pi.hardware_PWM(12, 1000, 1_000_000)  # full speed

print("Legs should be moving. Ctrl+C to stop.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass

pi.hardware_PWM(12, 0, 0)
pi.write(16, 0)
pi.write(20, 0)
pi.write(21, 0)
pi.stop()
print("Stopped.")