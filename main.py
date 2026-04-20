"""
ChippyPi — Entry point
Launches all core scripts as subprocesses, then starts the NiceGUI dashboard.
Run:   uv run main.py              (full system)
Run:   SKIP_HARDWARE=1 uv run main.py  (without motors)
Open:  http://<pi-ip>:8080
"""

import asyncio
import math
import os
import random
import sys
import time
import orjson
from nicegui import app, ui
from log_config import get_logger

log = get_logger("Main")

# ── Subprocess launcher ──────────────────────────────────────────────────────

SKIP_HARDWARE = os.environ.get("SKIP_HARDWARE", "0") == "1"

SCRIPTS = []
if not SKIP_HARDWARE:
    SCRIPTS.append(("Hardware",   "core_hardware.py"))
SCRIPTS += [
    ("Radar",      "core_radar.py"),
    ("Kinematics", "core_kinematics.py"),
    ("Controller", "core_joystick.py"),
]

_processes: list[tuple[str, asyncio.subprocess.Process]] = []


async def _launch(name: str, script: str) -> asyncio.subprocess.Process:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, path,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    log.info("Started {} (pid {})", name, proc.pid)
    return proc


async def _watchdog():
    """Restart any crashed subprocess."""
    while True:
        for i, (name, proc) in enumerate(_processes):
            if proc.returncode is not None:
                log.warning("{} died (exit {}) — restarting...", name, proc.returncode)
                script = next(s for n, s in SCRIPTS if n == name)
                new_proc = await _launch(name, script)
                _processes[i] = (name, new_proc)
        await asyncio.sleep(2)


async def startup():
    """Called by NiceGUI on server start — launch core scripts."""
    for name, script in SCRIPTS:
        proc = await _launch(name, script)
        _processes.append((name, proc))

        if name == "Hardware":
            log.info("Waiting 12s for hardware calibration...")
            await asyncio.sleep(12)
        else:
            await asyncio.sleep(1)

    if SKIP_HARDWARE:
        log.warning("Hardware SKIPPED (no motors)")

    log.success("All systems up")

    # Start watchdog as background task
    asyncio.create_task(_watchdog())


async def shutdown():
    """Called by NiceGUI on server stop — terminate core scripts."""
    log.info("Shutting down all processes...")
    for name, proc in _processes:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            log.info("Stopped {}", name)


app.on_startup(startup)
app.on_shutdown(shutdown)


# ── Valkey keys ──────────────────────────────────────────────────────────────

KEYS = {
    "mode":        "chippy:mode",
    "head":        "chippy:state:head",
    "kinematics":  "chippy:state:kinematics",
    "radar_front": "chippy:state:radar:front",
    "radar_rear":  "chippy:state:radar:rear",
    "crowd":       "chippy:state:crowd",
    "maze":        "chippy:state:maze",
}

KEY_MAZE_START = "chippy:cmd:maze:start"

VALID_MODES = ["FOLLOW", "CROWD", "MAZE"]
POLL_INTERVAL = 0.2

# ── Try Valkey, fall back to demo mode ───────────────────────────────────────

DEMO_MODE = False
vk = None

try:
    import valkey.asyncio as avalkey
    import valkey as sync_valkey
    _test = sync_valkey.Valkey(host="localhost", port=6379, decode_responses=True)
    _test.ping()
    _test.close()
    del _test
    vk = avalkey.Valkey(host="localhost", port=6379, decode_responses=True)
    log.info("Connected to Valkey")
except Exception:
    DEMO_MODE = True
    log.warning("Valkey unavailable — running in DEMO mode with fake data")


# ── Demo state ───────────────────────────────────────────────────────────────

_demo_state = {"mode": "FOLLOW"}


def _demo_data() -> dict:
    t = time.time()
    mode = _demo_state["mode"]

    intra_f = abs(math.sin(t * 0.7)) * 8.0 + random.uniform(0, 2)
    inter_f = abs(math.cos(t * 0.4)) * 6.0 + random.uniform(0, 1.5)
    intra_j = abs(math.sin(t * 1.1)) * 10.0 + random.uniform(0, 1)
    inter_j = abs(math.cos(t * 0.8)) * 4.0 + random.uniform(0, 1)
    dist_f  = 0.3 + math.sin(t * 0.5) * 0.15
    dist_j  = 0.25 + math.sin(t * 0.3) * 0.1

    det_f = intra_f > 4.0
    det_j = intra_j > 5.0

    avg_inter = inter_f
    if avg_inter >= 8.0:
        density = "BUSY"
    elif avg_inter >= 3.0:
        density = "LOW"
    else:
        density = "EMPTY"

    if det_j and dist_j < 0.20:
        leg_dir, v = "backward", -0.7
    elif det_j and dist_j > 0.30:
        leg_dir, v = "forward", 0.7
    else:
        leg_dir, v = "stop", 0.0

    head_dir = "forward" if det_f else "stop"
    w = 0.5 if det_f else 0.0
    maze_step = int((t % 12) / 2) + 1

    return {
        "mode": mode,
        "head": {"calibrated": True, "position": 0},
        "kinematics": {
            "v": round(v, 3), "w": round(w, 3),
            "leg_dir": leg_dir, "head_dir": head_dir,
            "ts": round(t, 4),
        },
        "radar_front": {
            "detected": det_f, "dist": round(dist_f, 3) if det_f else None,
            "intra": round(intra_f, 2), "inter": round(inter_f, 2), "ts": round(t, 4),
        },
        "radar_rear": {
            "detected": det_j, "dist": round(dist_j, 3) if det_j else None,
            "intra": round(intra_j, 2), "inter": round(inter_j, 2), "ts": round(t, 4),
        },
        "crowd": {
            "density": density, "avg_inter": round(avg_inter, 2),
            "avg_intra": round(intra_f, 2), "detected": det_f,
            "dist": round(dist_f, 3) if det_f else None, "ts": round(t, 4),
        },
        "maze": {
            "status": "RUNNING", "step": min(maze_step, 6),
            "total_steps": 6, "obstacle": random.random() < 0.1, "ts": round(t, 4),
        },
    }


# ── Helpers ──────────────────────────────────────────────────────────────────

async def vk_get(key: str) -> dict | None:
    if DEMO_MODE:
        return None
    raw = await vk.get(key)
    if raw:
        try:
            return orjson.loads(raw)
        except Exception:
            pass
    return None


async def publish_velocity(v: float, w: float):
    if DEMO_MODE:
        return
    await vk.publish("chippy:cmd:velocity", orjson.dumps({"v": v, "w": w}).decode())


async def set_mode(mode: str):
    if DEMO_MODE:
        _demo_state["mode"] = mode
        log.info("Demo mode set to {}", mode)
        return
    await vk.set(KEYS["mode"], mode)
    log.info("Mode set to {}", mode)


async def maze_start():
    """Signal MazeMode to begin its run (after user places Chipz)."""
    if DEMO_MODE:
        log.info("Demo: maze start clicked")
        return
    await vk.set(KEY_MAZE_START, "1")
    log.info("Maze start signal sent")


async def emergency_stop():
    """Halt everything — send zero velocity and switch to IDLE mode."""
    if DEMO_MODE:
        _demo_state["mode"] = "IDLE"
        log.warning("Demo: STOP clicked")
        return
    # 1. Kill motors now
    await vk.publish("chippy:cmd:velocity", orjson.dumps({"v": 0.0, "w": 0.0}).decode())
    # 2. Switch to IDLE so nothing re-publishes a non-zero velocity
    await vk.set(KEYS["mode"], "IDLE")
    # 3. Clear any pending maze start flag so switching back to MAZE is safe
    try:
        await vk.delete(KEY_MAZE_START)
    except Exception:
        pass
    log.warning("EMERGENCY STOP")


def fmt(v, dp=2) -> str:
    if v is None:
        return "—"
    return f"{float(v):.{dp}f}"


def bar_pct(v, max_val=20.0) -> float:
    if v is None:
        return 0.0
    return min(1.0, abs(v) / max_val)


def dir_arrow(d: str) -> str:
    if d == "forward":
        return "▲"
    if d == "backward":
        return "▼"
    return "⏸"


DENSITY_COLORS = {
    "EMPTY": "#00d4aa",
    "LOW":   "#ffc44a",
    "BUSY":  "#ff4a6b",
}


# ── Page ─────────────────────────────────────────────────────────────────────

@ui.page("/")
def dashboard():

    ui.dark_mode().enable()
    ui.add_head_html("""
    <style>
        body { font-family: 'JetBrains Mono', monospace !important; }
        .nicegui-content { max-width: 1200px; margin: 0 auto; }
        .q-card { background: #16161a !important; border: 1px solid #2a2a30 !important; }
        .card-title {
            font-size: 11px; font-weight: 600; letter-spacing: 1.5px;
            text-transform: uppercase; color: #6b6b76;
        }
        .data-label { color: #6b6b76; font-size: 13px; }
        .data-value { font-weight: 600; font-size: 13px; }
        .density-badge {
            display: inline-block; padding: 4px 14px; border-radius: 4px;
            font-weight: 700; font-size: 14px; letter-spacing: 1px;
        }
        .dpad-btn {
            width: 56px !important; height: 56px !important; min-width: 56px !important;
            font-size: 20px !important;
            border: 1px solid #2a2a30 !important; border-radius: 6px !important;
        }
        .dpad-btn:hover { border-color: #00d4aa !important; }
    </style>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
    """)

    with ui.row().classes("w-full items-center justify-between pb-4"):
        ui.label("CHIPPYPI").style(
            "font-size:22px; font-weight:700; letter-spacing:3px; color:#00d4aa;"
        )
        with ui.row().classes("items-center gap-4"):
            ui.button("STOP", on_click=lambda: emergency_stop()).props(
                "flat dense"
            ).style(
                "padding:10px 24px; font-size:13px; font-weight:700; "
                "letter-spacing:2px; border:2px solid #ff4a6b; border-radius:6px; "
                "background:#ff4a6b !important; color:#0c0c0f !important;"
            )
            with ui.row().classes("items-center gap-2"):
                conn_dot = ui.icon("circle").style("font-size:10px; color:#ff4a6b;")
                conn_text = ui.label("connecting...").style("font-size:11px; color:#6b6b76;")

    ui.separator().style("background:#2a2a30;")

    with ui.grid(columns=3).classes("w-full gap-4 mt-4").style(
        "grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));"
    ):

        # ── MODE ──
        with ui.card().classes("p-4"):
            ui.label("Mode").classes("card-title")
            mode_label = ui.label("—").style(
                "font-size:28px; font-weight:700; letter-spacing:3px; color:#e0e0e4;"
            )
            with ui.row().classes("gap-2 mt-2"):
                mode_btns = {}
                for m in VALID_MODES:
                    btn = ui.button(m, on_click=lambda _, mode=m: set_mode(mode)).props(
                        "flat dense"
                    ).style(
                        "flex:1; padding:8px 0; font-size:12px; font-weight:600; "
                        "letter-spacing:1px; border:1px solid #2a2a30; border-radius:6px; "
                        "color:#6b6b76;"
                    )
                    mode_btns[m] = btn

        # ── HEAD ──
        with ui.card().classes("p-4"):
            ui.label("Head").classes("card-title")
            with ui.column().classes("gap-1"):
                with ui.row().classes("w-full justify-between"):
                    ui.label("Calibrated").classes("data-label")
                    head_cal = ui.label("—").classes("data-value")
                with ui.row().classes("w-full justify-between"):
                    ui.label("Position").classes("data-label")
                    head_pos = ui.label("—").classes("data-value")

        # ── MOTORS ──
        with ui.card().classes("p-4"):
            ui.label("Motors").classes("card-title")
            with ui.row().classes("w-full justify-around py-2"):
                with ui.column().classes("items-center"):
                    leg_icon = ui.label("⏸").style("font-size:28px;")
                    ui.label("LEGS").style(
                        "font-size:10px; color:#6b6b76; letter-spacing:1px;"
                    )
                    leg_dir_label = ui.label("stop").style("font-size:12px; font-weight:600;")
                with ui.column().classes("items-center"):
                    head_icon = ui.label("⏸").style("font-size:28px;")
                    ui.label("HEAD").style(
                        "font-size:10px; color:#6b6b76; letter-spacing:1px;"
                    )
                    head_dir_label = ui.label("stop").style("font-size:12px; font-weight:600;")
            with ui.column().classes("gap-1"):
                with ui.row().classes("w-full justify-between"):
                    ui.label("v (linear)").classes("data-label")
                    kin_v = ui.label("—").classes("data-value")
                with ui.row().classes("w-full justify-between"):
                    ui.label("w (angular)").classes("data-label")
                    kin_w = ui.label("—").classes("data-value")

        # ── FRONT RADAR ──
        with ui.card().classes("p-4"):
            ui.label("Front Radar").classes("card-title")
            with ui.column().classes("gap-1"):
                with ui.row().classes("w-full justify-between"):
                    ui.label("Presence").classes("data-label")
                    front_det = ui.label("—").classes("data-value")
                with ui.row().classes("w-full justify-between"):
                    ui.label("Distance").classes("data-label")
                    front_dist = ui.label("—").classes("data-value")
            ui.label("intra").style("font-size:11px; color:#6b6b76; margin-top:8px;")
            front_intra_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='light-blue'")
            front_intra_val = ui.label("0").style("font-size:11px; font-weight:600;")
            ui.label("inter").style("font-size:11px; color:#6b6b76; margin-top:4px;")
            front_inter_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='amber'")
            front_inter_val = ui.label("0").style("font-size:11px; font-weight:600;")

        # ── REAR RADAR ──
        with ui.card().classes("p-4"):
            ui.label("Rear Radar").classes("card-title")
            with ui.column().classes("gap-1"):
                with ui.row().classes("w-full justify-between"):
                    ui.label("Presence").classes("data-label")
                    rear_det = ui.label("—").classes("data-value")
                with ui.row().classes("w-full justify-between"):
                    ui.label("Distance").classes("data-label")
                    rear_dist = ui.label("—").classes("data-value")
            ui.label("intra").style("font-size:11px; color:#6b6b76; margin-top:8px;")
            rear_intra_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='light-blue'")
            rear_intra_val = ui.label("0").style("font-size:11px; font-weight:600;")
            ui.label("inter").style("font-size:11px; color:#6b6b76; margin-top:4px;")
            rear_inter_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='amber'")
            rear_inter_val = ui.label("0").style("font-size:11px; font-weight:600;")

        # ── CROWD ──
        with ui.card().classes("p-4"):
            ui.label("Crowd Density").classes("card-title")
            crowd_badge = ui.html('<span class="density-badge">—</span>', sanitize=False)
            ui.label("avg intra").style("font-size:11px; color:#6b6b76; margin-top:10px;")
            crowd_intra_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='light-blue'")
            crowd_intra_val = ui.label("0").style("font-size:11px; font-weight:600;")
            ui.label("avg inter").style("font-size:11px; color:#6b6b76; margin-top:4px;")
            crowd_inter_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='amber'")
            crowd_inter_val = ui.label("0").style("font-size:11px; font-weight:600;")
            with ui.column().classes("gap-1 mt-2"):
                with ui.row().classes("w-full justify-between"):
                    ui.label("Presence").classes("data-label")
                    crowd_det = ui.label("—").classes("data-value")
                with ui.row().classes("w-full justify-between"):
                    ui.label("Distance").classes("data-label")
                    crowd_dist = ui.label("—").classes("data-value")

        # ── MAZE ──
        with ui.card().classes("p-4"):
            ui.label("Maze").classes("card-title")
            with ui.column().classes("gap-1"):
                with ui.row().classes("w-full justify-between"):
                    ui.label("Status").classes("data-label")
                    maze_status = ui.label("—").classes("data-value")
                with ui.row().classes("w-full justify-between"):
                    ui.label("Step").classes("data-label")
                    maze_step = ui.label("—").classes("data-value")
                with ui.row().classes("w-full justify-between"):
                    ui.label("Obstacle").classes("data-label")
                    maze_obstacle = ui.label("—").classes("data-value")
            maze_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:8px; margin-top:8px;"
            ).props("color='light-blue'")
            maze_start_btn = ui.button(
                "START", on_click=lambda: maze_start()
            ).props("flat dense").style(
                "width:100%; margin-top:10px; padding:10px 0; font-size:12px; "
                "font-weight:700; letter-spacing:2px; border:1px solid #2a2a30; "
                "border-radius:6px; color:#6b6b76;"
            )
            maze_start_btn.disable()

        # ── MANUAL CONTROL ──
        with ui.card().classes("p-4"):
            ui.label("Manual Control").classes("card-title")
            with ui.grid(columns=3).classes("justify-center mt-2").style(
                "gap:4px; grid-template-columns: 56px 56px 56px;"
            ):
                ui.label("")
                ui.button("▲", on_click=lambda: publish_velocity(0.7, 0)).classes("dpad-btn")
                ui.label("")
                ui.button("◀", on_click=lambda: publish_velocity(0, 0.7)).classes("dpad-btn")
                ui.button("■", on_click=lambda: publish_velocity(0, 0)).classes("dpad-btn").style(
                    "font-size:12px !important;"
                )
                ui.button("▶", on_click=lambda: publish_velocity(0, -0.7)).classes("dpad-btn")
                ui.label("")
                ui.button("▼", on_click=lambda: publish_velocity(-0.7, 0)).classes("dpad-btn")
                ui.label("")

    # ── Poll loop ────────────────────────────────────────────────────────────

    async def poll():
        try:
            if DEMO_MODE:
                d = _demo_data()
                mode = d["mode"]
                head = d["head"]
                kin  = d["kinematics"]
                rf   = d["radar_front"]
                rj   = d["radar_rear"]
                cr   = d["crowd"]
                mz   = d["maze"]
            else:
                mode = await vk.get(KEYS["mode"]) or "—"
                head = await vk_get(KEYS["head"])
                kin  = await vk_get(KEYS["kinematics"])
                rf   = await vk_get(KEYS["radar_front"])
                rj   = await vk_get(KEYS["radar_rear"])
                cr   = await vk_get(KEYS["crowd"])
                mz   = await vk_get(KEYS["maze"])

            mode_label.text = mode
            # In IDLE (from STOP button) no mode button should look active
            for m, btn in mode_btns.items():
                if m == mode and mode != "IDLE":
                    btn.style(
                        "flex:1; padding:8px 0; font-size:12px; font-weight:600; "
                        "letter-spacing:1px; border:1px solid #00d4aa; border-radius:6px; "
                        "background:#00d4aa !important; color:#0c0c0f !important;"
                    )
                else:
                    btn.style(
                        "flex:1; padding:8px 0; font-size:12px; font-weight:600; "
                        "letter-spacing:1px; border:1px solid #2a2a30; border-radius:6px; "
                        "color:#6b6b76; background:transparent !important;"
                    )

            if head:
                cal = head.get("calibrated", False)
                head_cal.text = "YES" if cal else "NO"
                head_cal.style(f"color:{'#00d4aa' if cal else '#ff4a6b'}; font-weight:600;")
                pos = head.get("position")
                head_pos.text = str(pos) if pos is not None else "—"

            if kin:
                kin_v.text = fmt(kin.get("v"), 3)
                kin_w.text = fmt(kin.get("w"), 3)
                ld = kin.get("leg_dir", "stop")
                hd = kin.get("head_dir", "stop")
                leg_dir_label.text = ld
                head_dir_label.text = hd
                leg_icon.text = dir_arrow(ld)
                head_icon.text = dir_arrow(hd)

            if rf:
                front_det.text = "YES" if rf.get("detected") else "no"
                d = rf.get("dist")
                front_dist.text = f"{fmt(d, 3)} m" if d is not None else "—"
                front_intra_bar.value = bar_pct(rf.get("intra", 0))
                front_intra_val.text = fmt(rf.get("intra", 0), 1)
                front_inter_bar.value = bar_pct(rf.get("inter", 0))
                front_inter_val.text = fmt(rf.get("inter", 0), 1)

            if rj:
                rear_det.text = "YES" if rj.get("detected") else "no"
                d = rj.get("dist")
                rear_dist.text = f"{fmt(d, 3)} m" if d is not None else "—"
                rear_intra_bar.value = bar_pct(rj.get("intra", 0))
                rear_intra_val.text = fmt(rj.get("intra", 0), 1)
                rear_inter_bar.value = bar_pct(rj.get("inter", 0))
                rear_inter_val.text = fmt(rj.get("inter", 0), 1)

            if cr:
                density = cr.get("density", "—")
                color = DENSITY_COLORS.get(density, "#6b6b76")
                crowd_badge.content = (
                    f'<span class="density-badge" '
                    f'style="color:{color}; background:{color}22;">'
                    f'{density}</span>'
                )
                crowd_inter_bar.value = bar_pct(cr.get("avg_inter", 0))
                crowd_inter_val.text = fmt(cr.get("avg_inter", 0), 1)
                crowd_intra_bar.value = bar_pct(cr.get("avg_intra", 0))
                crowd_intra_val.text = fmt(cr.get("avg_intra", 0), 1)
                crowd_det.text = "YES" if cr.get("detected") else "no"
                d = cr.get("dist")
                crowd_dist.text = f"{fmt(d, 3)} m" if d is not None else "—"

            if mz:
                maze_status.text = mz.get("status", "—")
                step = mz.get("step", 0)
                total = mz.get("total_steps", 0)
                maze_step.text = f"{step} / {total}"
                maze_obstacle.text = "BLOCKED" if mz.get("obstacle") else "clear"
                maze_bar.value = (step / total) if total > 0 else 0

            # Enable START only when we're in MAZE mode and waiting for go
            maze_armed = (
                mode == "MAZE" and
                mz is not None and
                mz.get("status") == "ARMED"
            )
            if maze_armed:
                maze_start_btn.enable()
                maze_start_btn.style(
                    "width:100%; margin-top:10px; padding:10px 0; font-size:12px; "
                    "font-weight:700; letter-spacing:2px; border:1px solid #00d4aa; "
                    "border-radius:6px; background:#00d4aa !important; "
                    "color:#0c0c0f !important;"
                )
            else:
                maze_start_btn.disable()
                maze_start_btn.style(
                    "width:100%; margin-top:10px; padding:10px 0; font-size:12px; "
                    "font-weight:700; letter-spacing:2px; border:1px solid #2a2a30; "
                    "border-radius:6px; color:#6b6b76; background:transparent !important;"
                )

            if DEMO_MODE:
                conn_dot.style("font-size:10px; color:#ffc44a;")
                conn_text.text = "demo"
            else:
                conn_dot.style("font-size:10px; color:#00d4aa;")
                conn_text.text = "live"

        except Exception as e:
            conn_dot.style("font-size:10px; color:#ff4a6b;")
            conn_text.text = f"error: {e}"
            log.error("Poll error: {}", e)

    ui.timer(POLL_INTERVAL, poll)


# ── Entry point ──────────────────────────────────────────────────────────────

ui.run(
    title="ChippyPi",
    host="0.0.0.0",
    port=8080,
    reload=False,
    show=False,
)