<h1 align="center">
  🤖 ChippyPi
</h1>

<p align="center">
  <strong>Custom controller for the KOSMOS Chipz hexapod robot</strong><br>
  Dual radar sensing · Three autonomous modes · Live web dashboard
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
- [Web Dashboard](#web-dashboard)
- [Valkey Schema](#valkey-schema)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Known Issues](#known-issues)
- [License](#license)

---

## Overview

ChippyPi replaces the original electronics of a **KOSMOS Chipz** 6-legged hexapod toy robot with a Raspberry Pi 3, dual Acconeer XM125 radar sensors, and a TB6612FNG motor driver. The robot operates in three autonomous modes — hand-following, crowd density measurement, and maze navigation — all controllable through a real-time web dashboard.

---

## Architecture

```
┌─────────────┐       Valkey pub/sub        ┌──────────────┐
│  core_radar  │──── chippy:state:radar ────▶│              │
│  (front XM125)│                            │              │
└─────────────┘                              │   core_      │
                                             │   joystick   │
┌─────────────┐                              │  (controller)│
│  rear radar  │──── chippy:state:radar ────▶│              │
│  (in core_   │      :rear                  │  FOLLOW /    │
│   joystick)  │                             │  CROWD /     │
└─────────────┘                              │  MAZE logic  │
                                             └──────┬───────┘
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
                                          │  pigpio → GPIO    │
                                          │  → TB6612FNG      │
                                          └──────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  main.py                                                     │
│  NiceGUI dashboard (port 8080) + subprocess launcher         │
│  Reads all chippy:state:* keys · Publishes manual commands   │
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
| Hall effect sensor | Latching or unipolar, for head home position | 1 |
| DC toy motors | Original KOSMOS motors (legs + head rotation) | 2 |
| Battery pack | 4× AAA (6V nominal) | 1 |
| 10kΩ resistor | Pull-down on STBY pin | 1 |

### Pin Mapping

#### TB6612FNG → Raspberry Pi 3

| TB6612FNG Pin | Pi GPIO | Function |
|---|---|---|
| STBY | GPIO 21 | Standby enable (10kΩ pull-down to GND) |
| AIN1 | GPIO 16 | Leg motor direction / PWM |
| AIN2 | GPIO 20 | Leg motor direction / PWM |
| BIN1 | GPIO 19 | Head motor direction / PWM |
| BIN2 | GPIO 26 | Head motor direction / PWM |
| PWMA | Pi 3.3V | Wired permanently HIGH |
| PWMB | Pi 3.3V | Wired permanently HIGH |
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

#### Radar Sensors (USB)

| Sensor | Serial Number | Facing | Port Path |
|---|---|---|---|
| Front | R1DNL25061800337 | Forward | `/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800337-if00-port0` |
| Rear | R1DNL25061800352 | Backward | `/dev/serial/by-id/usb-Silicon_Labs_Acconeer_XE125_R1DNL25061800352-if00-port0` |

Ports use `/dev/serial/by-id/` paths (identified by serial number, not physical USB port). Radars can be plugged into any USB port.

### Wiring Notes

- **PWM method:** Speed control is achieved by PWMing the IN1/IN2 pins directly using `pigpio.set_PWM_dutycycle()` (0–255). The dedicated PWMA/PWMB pins on the breakout board are wired to 3.3V (permanently HIGH). This avoids the Pi's hardware PWM / audio DMA conflict on GPIO 12/13.
- **STBY pin** requires a 10kΩ pull-down resistor to GND to ensure the driver stays off during Pi boot.
- **Common ground** is essential — Pi GND, battery GND, and TB6612FNG GND must all be connected.
- **Motor wire resistance** should measure 5–30Ω across terminals. 0Ω indicates a short that will destroy the driver.

---

## Software Stack

| Component | Purpose |
|---|---|
| **Python 3.11+** | Application language |
| **uv** | Package management and script runner |
| **pigpio** | GPIO control (software PWM on IN1/IN2 pins) |
| **Valkey** | Redis-compatible pub/sub and key-value store |
| **NiceGUI** | Web dashboard framework (port 8080) |
| **Acconeer Exploration Tool** | XM125 radar sensor driver |
| **Loguru** | Structured, colour-coded logging with file rotation |
| **orjson** | High-performance JSON serialisation |
| **asyncio** | Asynchronous I/O across all core scripts |
| **Docker** | Optional containerised deployment |

---

## Project Structure

```
chippy/
├── main.py              # Entry point: subprocess launcher + NiceGUI dashboard
├── core_hardware.py     # Motor driver (pigpio → TB6612FNG)
├── core_radar.py        # Front radar sensor (XM125 presence detection)
├── core_kinematics.py   # Velocity vector → motor command translation
├── core_joystick.py     # Controller: FOLLOW / CROWD / MAZE mode logic
├── log_config.py        # Shared Loguru configuration
├── pyproject.toml       # Dependencies and project metadata
├── Dockerfile           # Container image definition
├── docker-compose.yml   # Multi-service orchestration (app + Valkey)
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
wget https://github.com/joan2937/pigpio/archive/master.zip
unzip master.zip && cd pigpio-master
make && sudo make install && sudo ldconfig
pip install pigpio --break-system-packages
cd ..

# Create pigpiod service
sudo tee /etc/systemd/system/pigpiod.service > /dev/null <<EOF
[Unit]
Description=pigpio daemon
After=network.target

[Service]
ExecStart=/usr/local/bin/pigpiod -l
Type=forking

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pigpiod && sudo systemctl start pigpiod
sudo systemctl enable valkey-server && sudo systemctl start valkey-server

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

> **Note:** `pigpiod` must be running on the **host** Pi. The container connects to it via `PIGPIO_ADDR=host.docker.internal`. Radar USB devices are passed through to the container.

---

## Usage

```bash
# Full system (motors + radars + dashboard)
uv run main.py

# Without motors (dashboard + radars only)
SKIP_HARDWARE=1 uv run main.py
```

The web dashboard is accessible at `http://<pi-ip>:8080` from any device on the same network.

`main.py` launches all core scripts as subprocesses, monitors them with a watchdog, and auto-restarts any that crash. Press `Ctrl+C` to shut everything down cleanly.

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

**Head behaviour:** Locks on detected presence. When lost, sweeps back and forth for up to 3 seconds then stops.

Rear radar is **not used** in FOLLOW mode.

### CROWD Mode

Passive crowd density measurement using **both** front and rear radars. Motors are stopped.

Combines the maximum inter/intra scores from both radars, applies a rolling average over 40 frames (2 seconds), and classifies:

| Density | Avg Inter Score | Dashboard Colour |
|---|---|---|
| EMPTY | < 3.0 | 🟢 Green |
| LOW | 3.0–8.0 | 🟡 Yellow |
| BUSY | > 8.0 | 🔴 Red |

> Thresholds are placeholders — require tuning with real crowd data.

### MAZE Mode

Timed movement sequence with obstacle safety using **both** radars.

- **Forward guard:** Front radar detects obstacle < 0.35m while moving forward → legs pause
- **Rear guard:** Rear radar detects obstacle < 0.25m while moving backward → legs pause
- Head movement continues regardless of obstacle status
- Obstacle checks are **directional** — only the relevant radar is checked

The sequence runs once then stops. Set mode to MAZE again to re-run.

> Maze timings are placeholders — require tuning to actual robot speed. Recommended corridor: 30–40cm wide, 20cm+ wall height, built from cardboard boxes or books.

---

## Web Dashboard

The NiceGUI dashboard on port 8080 provides:

- **Mode selector** — FOLLOW / CROWD / MAZE buttons
- **Head status** — Calibration state and position
- **Motor indicators** — Direction arrows and v/w values
- **Front radar** — Presence, distance, intra/inter score bars
- **Rear radar** — Presence, distance, intra/inter score bars
- **Crowd density** — Colour-coded badge with averaged scores
- **Maze progress** — Step counter, progress bar, obstacle status
- **D-pad** — Manual motor control (click-to-send)
- **Connection indicator** — Green (live), yellow (demo), red (error)

**Demo mode** activates automatically when Valkey is unavailable (e.g., running on a laptop for UI development). Generates animated fake sensor data — no code changes needed.

---

## Valkey Schema

### Key-Value (persistent, latest value always readable)

| Key | Type | Description |
|---|---|---|
| `chippy:mode` | string | Current mode: `FOLLOW` \| `CROWD` \| `MAZE` |
| `chippy:state:radar:front` | JSON | `{detected, dist, intra, inter, ts}` |
| `chippy:state:radar:rear` | JSON | `{detected, dist, intra, inter, ts}` |
| `chippy:state:kinematics` | JSON | `{v, w, leg_dir, head_dir, ts}` |
| `chippy:state:crowd` | JSON | `{density, avg_inter, avg_intra, detected, dist, ts}` |
| `chippy:state:maze` | JSON | `{status, step, total_steps, obstacle, ts}` |
| `chippy:state:head` | JSON | `{calibrated, position}` |

### Pub/Sub (fire-and-forget)

| Channel | Payload | Publisher → Subscriber |
|---|---|---|
| `chippy:cmd:velocity` | `{v, w}` (−1.0 to 1.0) | Controller/Dashboard → Kinematics |
| `chippy:cmd:motors` | `{target, dir, speed}` | Kinematics → Hardware |

### Motor Command Format

```json
{
  "target": "legs" | "head" | "home",
  "dir": "forward" | "backward" | "stop",
  "speed": 0.0
}
```

---

## Configuration

### Radar Parameters

| Parameter | Front Radar | Rear Radar |
|---|---|---|
| `start_m` | 0.10 | 0.10 |
| `end_m` | 2.0 | 0.5 |
| `frame_rate` | 20.0 Hz | 20.0 Hz |
| `intra_detection_threshold` | 4.0 | 5.0 |
| `inter_detection_threshold` | 3.0 | 4.0 |

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

### Logging

Logs are written to both stderr (coloured) and rotating files at `/tmp/chippy_YYYY-MM-DD.log` (3-day retention, gzip compressed). Adjust the log level in `log_config.py` — set to `"INFO"` in production to suppress per-frame debug output.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `pigpiod is not running` | Daemon not started | `sudo pigpiod` or `sudo systemctl start pigpiod` |
| Motors don't spin | PWMA/PWMB not wired to 3.3V | Bridge PWMA and PWMB pads to VCC/3.3V |
| Motors don't spin | STBY pin floating LOW | Verify STBY has 10kΩ pull-down and code sets it HIGH |
| Driver gets hot, no movement | Motor wires shorted or stall current too high | Measure motor resistance (expect 5–30Ω, not 0Ω) |
| Radar not detected | Wrong serial port | Check `/dev/serial/by-id/` for connected devices |
| Dashboard shows "demo" | Valkey not running | `sudo systemctl start valkey-server` |
| Dashboard unreachable | Firewall or wrong IP | Check `hostname -I` on Pi, ensure port 8080 is open |
| `set_PWM_dutycycle` has no effect | Pin not initialised | Ensure `set_PWM_frequency()` called before `set_PWM_dutycycle()` |

---

## Known Issues

- **XM125 distance is quantised** into range bins — not continuous. Gesture detection based on distance reversal counting does not work.
- **Intra score** drops to near-zero within 1–2 frames when motion stops. **Inter score** decays slowly over several seconds. FOLLOW mode gates on intra to avoid the lingering inter problem.
- **Left/right hand tracking** is physically impossible with a single 1D radar. Head sweep-and-lock is the correct approach for following behaviour.
- **Rear radar is optional.** `core_joystick.py` degrades gracefully without it. FOLLOW works fine with front radar only. CROWD and MAZE lose rear coverage but do not crash.
- **CROWD thresholds** and **MAZE sequence timings** are placeholders requiring real-world tuning.
- **Three TB6612FNG boards** had dead A channels — root cause was missing PWM signal (NC pads left floating). Resolved by wiring PWMA/PWMB to 3.3V and PWMing IN1/IN2 pins directly.

---

## License

This project is part of an educational robotics build. No formal license has been applied.