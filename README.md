<h1 align="center">
  🤖 Chippy
</h1>

<p align="center">
  <strong>Custom controller for the KOSMOS Chipz hexapod robot</strong><br>
  Dual radar sensing ·Three operating modes · Live web dashboard
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/platform-Raspberry%20Pi%203-c51a4a?logo=raspberrypi&logoColor=white" alt="Raspberry Pi 3">
  <img src="https://img.shields.io/badge/comms-Valkey%20(Redis)-dc382d?logo=redis&logoColor=white" alt="Valkey">
  <img src="https://img.shields.io/badge/ui-NiceGUI-00d4aa" alt="NiceGUI">
  <img src="https://img.shields.io/badge/container-Docker-2496ED?logo=docker&logoColor=white" alt="Docker">
</p>

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Hardware](#hardware)
  - [Bill of Materials](#bill-of-materials)
  - [Pin Mapping](#pin-mapping)
  - [Wiring Notes](#wiring-notes)
- [Software Stack](#software-stack)
- [Project Structure](#project-structure)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Setup](#setup)
  - [Docker (Alternative)](#docker-alternative)
- [Usage](#usage)
- [Operating Modes](#operating-modes)
  - [FOLLOW Mode](#follow-mode)
  - [CROWD Mode](#crowd-mode)
  - [MAZE Mode](#maze-mode)
  - [IDLE Mode](#idle-mode)
- [Web Dashboard](#web-dashboard)
- [Valkey Schema](#valkey-schema)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Known Issues](#known-issues)
- [License](#license)

---

## Overview

ChippyPi replaces the original electronics of a **KOSMOS Chipz** 6-legged hexapod toy robot with a Raspberry Pi 3, dual Acconeer XM125 radar sensors, and a TB6612FNG motor driver. The robot operates in four modes — hand-following, crowd density measurement, right-hand-rule maze navigation, and idle — all controllable through a real-time web dashboard with an emergency stop button.

---

## Architecture

```
┌─────────────┐       Valkey pub/sub        ┌──────────────┐
│  core_radar  │──── chippy:state:radar ────▶│              │
│  (front XM125)│     :front                 │              │
│  Presence OR │                             │   core_      │
│  Distance det│                             │   joystick   │
└─────────────┘                              │  (controller)│
                                             │              │
┌─────────────┐                              │  FOLLOW /    │
│  rear radar  │──── chippy:state:radar ────▶│  CROWD /     │
│  (in core_   │      :rear                  │  MAZE /      │
│   joystick)  │                             │  IDLE logic  │
└─────────────┘                              └──────┬───────┘
                                                    │
                                          chippy:cmd:velocity
                                                    │
                                                    ▼
                                          ┌──────────────────┐
                                          │  core_kinematics  │
                                          │  v,w → motor cmds │
                                          └────────┬─────────┘
                                                   │
                                          chippy:cmd:motors
                                                   │
                                                   ▼
                                          ┌──────────────────┐
                                          │  core_hardware    │
                                          │  pigpio HW PWM    │
                                          │  → TB6612FNG      │
                                          │  + Hall monitor   │
                                          │  + Smart recal    │
                                          └──────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  main.py                                                     │
│  NiceGUI dashboard (port 8080) + subprocess launcher         │
│  Reads all chippy:state:* keys · Publishes manual commands   │
│  Emergency STOP · Demo mode (no Valkey)                      │
└─────────────────────────────────────────────────────────────┘
```

**Data flow:** Radar → Controller → Kinematics → Hardware → Motors

All inter-process communication runs through **Valkey** (Redis-compatible) using pub/sub for commands and key-value for state.

---

## Hardware

### Bill of Materials

| Component | Specification | Quantity |
|---|---|---|
| KOSMOS Chipz hexapod | Stock chassis, gearbox, legs | 1 |
| Raspberry Pi 3 | Running Pi OS Lite (64-bit), headless | 1 |
| TB6612FNG | Dual H-bridge motor driver (breakout board) | 1 |
| Acconeer XM125 | 1D presence/distance radar sensor (USB) | 2 |
| Hall effect sensor | Latching or unipolar, mounted on right hip | 1 |
| DC toy motors | Original KOSMOS motors (legs + head rotation) | 2 |
| Battery pack | 4× AAA (6V nominal) | 1 |
| 10kΩ resistor | Pull-down on STBY pin | 1 |

### Pin Mapping

#### TB6612FNG → Raspberry Pi 3

| TB6612FNG Pin | Pi GPIO | Function |
|---|---|---|
| STBY | GPIO 21 | Standby enable (10kΩ pull-down to GND) |
| AIN1 | GPIO 16 | Leg motor direction (digital HIGH/LOW) |
| AIN2 | GPIO 20 | Leg motor direction (digital HIGH/LOW) |
| BIN1 | GPIO 19 | Head motor direction (digital HIGH/LOW) |
| BIN2 | GPIO 26 | Head motor direction (digital HIGH/LOW) |
| PWMA | GPIO 13 | Leg motor speed (hardware PWM, 25 kHz) |
| PWMB | GPIO 12 | Head motor speed (hardware PWM, 25 kHz) |
| VM | Battery 6V+ | Motor power supply |
| VCC | Pi 3.3V | Logic power supply |
| GND | Common GND | Pi GND + Battery GND |

**Motor outputs:** A01/A02 → Legs motor · B01/B02 → Head motor

#### Hall Effect Sensor → Raspberry Pi 3

| Hall Pin | Pi GPIO | Notes |
|---|---|---|
| VCC | Pi 3.3V | |
| GND | Pi GND | |
| OUT | GPIO 17 | Internal pull-up enabled · LOW = magnet detected |

The sensor is mounted on Chippy's **right hip**. A magnet is attached to the head/upper-body belt. Position 0 (magnet over sensor) corresponds to the head facing **right**. The hall monitor task runs at 100 Hz, publishing real-time state and crossing direction for smart recalibration.

#### Radar Sensors (USB)

| Sensor | Serial Number | Facing | Port Path |
|---|---|---|---|
| Front | R1DNL25061800337 | Forward | `/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0` |
| Rear | R1DNL25061800352 | Backward | `/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800352-if00-port0` |

Ports use `/dev/serial/by-id/` paths (identified by serial number, not physical USB port). Radars can be plugged into any USB port.

### Wiring Notes

- **PWM method:** Speed is controlled using the Pi's **hardware PWM** on GPIO 12 (PWMB, head) and GPIO 13 (PWMA, legs) at **25 kHz** — above human hearing, which eliminates motor whine. The IN1/IN2 pins are plain digital direction outputs (HIGH/LOW). Duty cycle range: 0–1,000,000 (pigpio hardware_PWM scale).
- **STBY pin** requires a 10kΩ pull-down resistor to GND to ensure the driver stays off during Pi boot.
- **Common ground** is essential — Pi GND, battery GND, and TB6612FNG GND must all be connected.
- **Motor wire resistance** should measure 5–30Ω across terminals. 0Ω indicates a short that will destroy the driver.

---

## Software Stack

| Component | Purpose |
|---|---|
| **Python 3.11+** | Application language |
| **uv** | Package management and script runner |
| **pigpio** | GPIO control (hardware PWM on PWMA/PWMB, digital on IN pins) |
| **Valkey** | Redis-compatible pub/sub and key-value store |
| **NiceGUI** | Web dashboard framework (port 8080) |
| **Acconeer Exploration Tool** | XM125 radar sensor driver (presence + distance detectors) |
| **NumPy** | Radar sweep analysis (maze distance processing) |
| **Loguru** | Structured, colour-coded logging with file rotation |
| **orjson** | High-performance JSON serialisation |
| **asyncio** | Asynchronous I/O across all core scripts |
| **Docker** | Optional containerised deployment |

---

## Project Structure

```
chippy/
├── main.py              # Entry point: subprocess launcher + NiceGUI dashboard
├── core_hardware.py     # Motor driver (HW PWM → TB6612FNG) + hall monitor + smart recal
├── core_radar.py        # Front radar (presence ↔ distance detector, auto-switching)
├── core_kinematics.py   # Velocity vector → motor command translation
├── core_joystick.py     # Controller: FOLLOW / CROWD / MAZE / IDLE mode logic
├── log_config.py        # Shared Loguru configuration
├── pyproject.toml       # Dependencies and project metadata
├── Dockerfile           # Container image definition
├── docker-compose.yml   # Multi-service orchestration (app + Valkey)
├── compose_redis.yaml   # Optional RedisInsight container (port 5540)
└── .dockerignore        # Docker build exclusions
```

---

## Installation

### Prerequisites

1. **Raspberry Pi 3** running Pi OS Lite (64-bit, Bookworm)
2. **pigpiod** daemon installed and running
3. **Valkey** (Redis-compatible) server on localhost:6379
4. **uv** package manager

### Setup

```bash
# Install system dependencies
sudo apt update && sudo apt install -y python3 python3-pip git valkey-server

# Install pigpio from source
cd /tmp
wget https://github.com/joan2937/pigpio/archive/master.zip
unzip master.zip
cd pigpio-master
make
sudo make install

# Enable pigpiod on boot
sudo systemctl enable pigpiod
sudo systemctl start pigpiod

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# Clone and install
git clone https://github.com/etheri0m/Chippy3.git chippy
cd chippy
uv sync
```

### Docker (Alternative)

```bash
cd chippy
docker compose up --build
```

> **Note:** `pigpiod` must be running on the **host** Pi. The container connects to it via `PIGPIO_ADDR=host.docker.internal`. Radar USB devices are passed through via `/dev/serial/by-id`. The container joins the `dialout` group for serial access.

**Optional:** Launch RedisInsight for Valkey debugging:

```bash
docker compose -f compose_redis.yaml up -d
# Open http://<pi-ip>:5540
```

---

## Usage

```bash
# Full system (motors + radars + dashboard)
uv run main.py

# Without motors (dashboard + radars only)
SKIP_HARDWARE=1 uv run main.py
```

The web dashboard is accessible at `http://<pi-ip>:8080` from any device on the same network.

`main.py` launches all core scripts as subprocesses, monitors them with a watchdog (2-second polling), and auto-restarts any that crash. On startup, if hardware is enabled, a 12-second calibration delay is applied before launching the remaining scripts. Press `Ctrl+C` to shut everything down cleanly.

---

## Operating Modes

All mode logic lives in `core_joystick.py`. Switch modes via the web dashboard or directly:

```bash
redis-cli set chippy:mode FOLLOW
redis-cli set chippy:mode CROWD
redis-cli set chippy:mode MAZE
```

### FOLLOW Mode

The robot tracks a hand using the **front radar** (controls both legs and head).

**Dual gate** — both conditions must pass for hand detection:
- Distance ≤ 0.50m (filters out bystanders)
- Intra score ≥ 4.0 (filters out stationary objects)

**Distance zones:**

| Zone | Distance | Action |
|---|---|---|
| NEAR | < 0.20m | Legs back away (v = −0.7) |
| HOLD | 0.20–0.30m | Legs hold still (v = 0.0) |
| FAR | > 0.30m | Legs walk forward (v = +0.7) |
| NONE | Gate fails | Legs stop |

**Head behaviour:** Locks on detected presence. When lost, sweeps back and forth (±0.8 speed, 0.8s per direction) for up to 3 seconds then stops.

Rear radar is **not used** in FOLLOW mode.

### CROWD Mode

Passive crowd density measurement using **both** front and rear radars. The head sweeps continuously (±0.6 speed, 2s per direction) to scan the surrounding area. Legs are stopped.

Combines the maximum inter/intra scores from both radars, applies a rolling average over 40 frames (~2 seconds), and classifies:

| Density | Avg Inter Score | Dashboard Colour |
|---|---|---|
| EMPTY | < 3.0 | 🟢 Green |
| LOW | 3.0–8.0 | 🟡 Yellow |
| BUSY | > 8.0 | 🔴 Red |

On exiting CROWD mode, a **recalibration** signal is published to `chippy:cmd:calibrate`, triggering the hardware node to re-home the head via the hall sensor.

> Thresholds are placeholders — require tuning with real crowd data.

### MAZE Mode

Right-hand wall-following algorithm using the **front radar** in **Distance Detector** mode (5–50 cm range, PROFILE_1) with `close_range_leakage_cancellation`. The rear radar provides an additional safety layer.

#### Startup Sequence

1. Mode is set to MAZE → `core_radar.py` switches from Presence Detector to Distance Detector
2. Radar calibration runs (`calibrate_detector()` — **hold robot in free space** during this step)
3. Head calibration runs simultaneously via `smart_calibrate_head` (hall sensor homing)
4. Dashboard shows **ARMED** — both `chippy:state:radar:dist_calibrated` and `chippy:state:head.calibrated` must be true
5. User places robot in the maze and clicks **START** (or `redis-cli set chippy:cmd:maze:start 1`)

#### Wall-Following Algorithm

The maze uses a state machine with the following states:

| State | Description |
|---|---|
| `ARMED` | Waiting for dual calibration + start signal |
| `DRIVE` | Head aligned (hall triggered), legs forward |
| `PEEK_R_TURN` | Rotating head ~90° right (timed) |
| `PEEK_R_READ` | Head pointing right, collecting radar samples |
| `PEEK_R_RETURN` | Returning head to center (hall-based) |
| `COMMIT_R` | Right is open — head right, legs forward, body follows |
| `PEEK_L_TURN` | Rotating head ~90° left (timed) |
| `PEEK_L_READ` | Head pointing left, collecting radar samples |
| `COMMIT_L` | Left is open — head left, legs forward, body follows |
| `SPIN_180_TURN` | Dead end — spinning 180° |
| `SPIN_180_DRIVE` | Driving out of dead end, waiting for body alignment |

**Decision logic during driving:**
- Every 3 seconds → peek right (right-hand rule priority)
- Front blocked at < 0.15m → stop and check left
- Right open (> 0.50m with multi-sample confirmation) → commit right
- Right blocked → return head to center, resume forward
- Left open (> 0.45m) → commit left
- Both blocked → spin 180°

**Multi-sample confirmation:** Peek readings collect samples over 0.30s (minimum 4 samples at 20 Hz). ALL samples must show open to commit — this rejects false positives at wall edges.

**Proximity emergency stop:** Inspired by the Acconeer Touchless Button reference app. Any reflection within 0.15m above signal threshold 40 triggers an immediate halt and left-check, regardless of current state.

**Sweep analysis:** Raw `abs_sweep` data from the distance detector is thresholded at signal level 45 (noise floor ~5–40, real wall ~80–140) to find the closest wall reflection.

#### Commit Behaviour

After committing to a turn, the head stays pointed in the new direction while legs drive forward. The body naturally rotates to follow the head. The hall sensor detects when the body has aligned with the head (magnet crosses sensor) — at that point, the robot is driving straight in the new corridor and resumes normal wall-following.

### IDLE Mode

All motors stopped. Set automatically by the **emergency STOP** button. No autonomous behaviour. Switch to any other mode to resume operation.

---

## Web Dashboard

The NiceGUI dashboard on port 8080 provides:

- **STOP button** — Emergency stop: immediately halts motors, clears maze state, switches to IDLE mode
- **Mode selector** — FOLLOW / CROWD / MAZE buttons (active mode highlighted in green)
- **Head status** — Calibration state (YES/NO) and position
- **Motor indicators** — Direction arrows (▲/▼/⏸) and v/w values for legs and head
- **Front radar** — Presence, distance, intra/inter score bars
- **Rear radar** — Presence, distance, intra/inter score bars
- **Crowd density** — Colour-coded badge (EMPTY/LOW/BUSY) with averaged intra/inter scores
- **Maze status** — Current state, peek/left distances, progress bar, START button (only enabled when ARMED)
- **D-pad** — Manual motor control (forward/backward/left/right/stop)
- **Connection indicator** — Green dot (live), yellow dot (demo), red dot (error)

**Demo mode** activates automatically when Valkey is unavailable (e.g., running on a laptop for UI development). Generates animated fake sensor data with sinusoidal presence scores, simulated zone transitions, and rotating density classifications — no code changes or hardware needed.

---

## Valkey Schema

### Key-Value (persistent, latest value always readable)

| Key | Type | Description |
|---|---|---|
| `chippy:mode` | string | Current mode: `FOLLOW` \| `CROWD` \| `MAZE` \| `IDLE` |
| `chippy:state:radar:front` | JSON | `{detected, dist, proximity, intra, inter, ts}` |
| `chippy:state:radar:rear` | JSON | `{detected, dist, intra, inter, ts}` |
| `chippy:state:kinematics` | JSON | `{v, w, leg_dir, head_dir, ts}` |
| `chippy:state:crowd` | JSON | `{density, avg_inter, avg_intra, detected, dist, ts}` |
| `chippy:state:maze` | JSON | `{status, peek_dist, left_dist, ts}` |
| `chippy:state:head` | JSON | `{calibrated, position, last_crossing_dir, ts}` |
| `chippy:state:hall` | string | `"1"` if magnet over sensor (head centered), `"0"` otherwise |
| `chippy:state:radar:dist_calibrated` | string | `"1"` when distance detector calibration is complete |
| `chippy:cmd:maze:start` | string | `"1"` to trigger maze run (cleared after read) |

### Pub/Sub (fire-and-forget)

| Channel | Payload | Publisher → Subscriber |
|---|---|---|
| `chippy:cmd:velocity` | `{v, w}` (−1.0 to 1.0) | Controller/Dashboard → Kinematics |
| `chippy:cmd:motors` | `{target, dir, speed}` | Kinematics → Hardware |
| `chippy:cmd:calibrate` | `"1"` | Controller → Hardware (triggers smart recal) |

### Motor Command Format

```json
{
  "target": "legs | head | home",
  "dir": "forward | backward | stop",
  "speed": 0.0
}
```

---

## Configuration

### Radar Parameters

**Presence Detector** (used by FOLLOW, CROWD):

| Parameter | Front Radar | Rear Radar |
|---|---|---|
| `start_m` | 0.10 | 0.10 |
| `end_m` | 2.0 | 0.5 |
| `frame_rate` | 20.0 Hz | 20.0 Hz |
| `intra_detection_threshold` | 3.0 | 5.0 |
| `inter_detection_threshold` | 2.0 | 4.0 |

**Distance Detector** (used by MAZE, front radar only):

| Parameter | Value |
|---|---|
| `start_m` | 0.10 |
| `end_m` | 0.50 |
| `max_profile` | PROFILE_1 |
| `close_range_leakage_cancellation` | True |
| Signal threshold | 45.0 (abs_sweep) |
| Proximity zone | 0.15m at threshold 40.0 |

### FOLLOW Tuning Constants

| Constant | Value | Location |
|---|---|---|
| `NEAR_ZONE` | 0.20m | `core_joystick.py` |
| `FAR_ZONE` | 0.30m | `core_joystick.py` |
| `MAX_HAND_DIST` | 0.50m | `core_joystick.py` |
| `FOLLOW_SPEED` | 0.7 | `core_joystick.py` |
| `MIN_HAND_INTRA` | 4.0 | `core_joystick.py` |
| `SWEEP_SPEED` | 0.8 | `core_joystick.py` |
| `SWEEP_DURATION` | 0.8s | `core_joystick.py` |
| `SWEEP_TIMEOUT` | 3.0s | `core_joystick.py` |

### MAZE Tuning Constants

| Constant | Value | Description |
|---|---|---|
| `MAZE_DRIVE_SPEED` | 0.7 | Forward driving speed |
| `MAZE_TURN_SPEED` | 0.7 | Head rotation speed |
| `MAZE_FRONT_BLOCK` | 0.15m | Front wall stop distance |
| `MAZE_RIGHT_OPENING` | 0.50m | Min distance to confirm right opening |
| `MAZE_LEFT_OPENING` | 0.45m | Min distance to confirm left opening |
| `MAZE_PEEK_INTERVAL` | 3.0s | Seconds between right peeks |
| `MAZE_PEEK_TURN_DUR` | 1.50s | Time for ~90° head rotation |
| `MAZE_RADAR_SETTLE` | 0.15s | Wait after head stop for fresh reading |
| `MAZE_PEEK_CONFIRM_DUR` | 0.30s | Sample collection window |
| `MAZE_PEEK_MIN_SAMPLES` | 4 | Minimum samples for decision |
| `MAZE_HALL_TIMEOUT` | 3.0s | Max wait for body alignment |
| `MAZE_RECENTER_TIMEOUT` | 1.5s | Max wait for head return to center |

### Head Calibration Constants

| Constant | Value | Description |
|---|---|---|
| `CALIB_SPEED` | 0.70 | Motor speed during recalibration |
| `CALIB_TIMEOUT` | 4.0s | Max search time for magnet |
| `BACKOFF_SECS` | 0.25s | Back-off time if already on magnet |

### Logging

Logs are written to both stderr (coloured) and rotating files at `/tmp/chippy_YYYY-MM-DD.log` (3-day retention, gzip compressed). Adjust the log level in `log_config.py` — set to `"INFO"` in production to suppress per-frame debug output.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `pigpiod is not running` | Daemon not started | `sudo pigpiod` or `sudo systemctl start pigpiod` |
| Motors don't spin | STBY pin floating LOW | Verify STBY has 10kΩ pull-down and code sets it HIGH |
| Motors don't spin | PWMA/PWMB not connected to GPIO 13/12 | Ensure PWMA → GPIO 13 and PWMB → GPIO 12 |
| Motor whine audible | PWM frequency too low | Verify `PWM_FREQ = 25_000` in `core_hardware.py` |
| Driver gets hot, no movement | Motor wires shorted or stall current too high | Measure motor resistance (expect 5–30Ω, not 0Ω) |
| Radar not detected | Wrong serial port | Check `/dev/serial/by-id/` for connected devices |
| Dashboard shows "demo" | Valkey not running | `sudo systemctl start valkey-server` |
| Dashboard unreachable | Firewall or wrong IP | Check `hostname -I` on Pi, ensure port 8080 is open |
| Maze won't start | Calibration incomplete | Hold robot in free space; wait for ARMED status |
| Head calibration fails | Magnet misaligned or too far | Check hall sensor reads LOW when magnet is over it |
| `close_range_leakage_cancellation` error | Older Acconeer SDK | Update SDK, or code falls back automatically |

---

## Known Issues

- **XM125 distance is quantised** into range bins — not continuous. Fine distance tracking is limited by the sensor's spatial resolution.
- **Intra score** drops to near-zero within 1–2 frames when motion stops. **Inter score** decays slowly over several seconds. FOLLOW mode gates on intra to avoid the lingering inter problem.
- **Left/right hand tracking** is physically impossible with a single 1D radar. Head sweep-and-lock is the correct approach for following behaviour.
- **Rear radar is optional.** `core_joystick.py` degrades gracefully without it. FOLLOW works fine with front radar only. CROWD and MAZE lose rear coverage but do not crash.
- **CROWD thresholds** are placeholders requiring real-world tuning.
- **MAZE peek timing** (`MAZE_PEEK_TURN_DUR = 1.50s`) must be calibrated to produce approximately 90° of head rotation at the configured speed. Use `test_turns.py` to measure.
- **Smart recalibration** depends on `last_crossing_dir` history. On first boot with no crossing data, the head defaults to a forward sweep, which may take longer if the magnet is behind.
- **Hall sensor position** (right hip) means position 0 is head-right, not head-forward. The maze algorithm accounts for this; FOLLOW mode does not use the hall sensor.

---

## License

This project is part of an educational robotics build. No formal license has been applied.