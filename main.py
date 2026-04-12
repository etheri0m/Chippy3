"""
ChippyPi — NiceGUI Dashboard
Open:  http://<pi-ip>:8080

Reads all Valkey state keys, displays live data, allows mode switching.
No motors or radar — purely a display/control layer.
"""

import json
import time
from nicegui import ui
from valkey import Valkey

vk = Valkey(host="localhost", port=6379, decode_responses=True)

# ── Valkey keys ──────────────────────────────────────────────────────────────
KEYS = {
    "mode":        "chippy:mode",
    "head":        "chippy:state:head",
    "kinematics":  "chippy:state:kinematics",
    "radar_front": "chippy:state:radar:front",
    "radar_joy":   "chippy:state:radar:joystick",
    "crowd":       "chippy:state:crowd",
    "maze":        "chippy:state:maze",
}

VALID_MODES = ["FOLLOW", "CROWD", "MAZE"]
POLL_INTERVAL = 0.2  # seconds


# ── Helpers ──────────────────────────────────────────────────────────────────

def vk_get(key: str) -> dict | None:
    raw = vk.get(key)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return None


def publish_velocity(v: float, w: float):
    vk.publish("chippy:cmd:velocity", json.dumps({"v": v, "w": w}))


def set_mode(mode: str):
    vk.set(KEYS["mode"], mode)


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

    # -- dark theme & custom style --
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

    # ── Header ──
    with ui.row().classes("w-full items-center justify-between pb-4"):
        ui.label("CHIPPYPI").style(
            "font-size:22px; font-weight:700; letter-spacing:3px; color:#00d4aa;"
        )
        with ui.row().classes("items-center gap-2"):
            conn_dot = ui.icon("circle").style("font-size:10px; color:#ff4a6b;")
            conn_text = ui.label("connecting...").style("font-size:11px; color:#6b6b76;")

    ui.separator().style("background:#2a2a30;")

    # ── Grid ──
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

        # ── JOYSTICK RADAR ──
        with ui.card().classes("p-4"):
            ui.label("Joystick Radar").classes("card-title")
            with ui.column().classes("gap-1"):
                with ui.row().classes("w-full justify-between"):
                    ui.label("Presence").classes("data-label")
                    joy_det = ui.label("—").classes("data-value")
                with ui.row().classes("w-full justify-between"):
                    ui.label("Distance").classes("data-label")
                    joy_dist = ui.label("—").classes("data-value")
            ui.label("intra").style("font-size:11px; color:#6b6b76; margin-top:8px;")
            joy_intra_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='light-blue'")
            joy_intra_val = ui.label("0").style("font-size:11px; font-weight:600;")
            ui.label("inter").style("font-size:11px; color:#6b6b76; margin-top:4px;")
            joy_inter_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='amber'")
            joy_inter_val = ui.label("0").style("font-size:11px; font-weight:600;")

        # ── CROWD ──
        with ui.card().classes("p-4"):
            ui.label("Crowd Density").classes("card-title")
            crowd_badge = ui.html('<span class="density-badge">—</span>')
            ui.label("avg inter").style("font-size:11px; color:#6b6b76; margin-top:10px;")
            crowd_inter_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='amber'")
            crowd_inter_val = ui.label("0").style("font-size:11px; font-weight:600;")
            ui.label("avg intra").style("font-size:11px; color:#6b6b76; margin-top:4px;")
            crowd_intra_bar = ui.linear_progress(value=0, show_value=False).style(
                "height:14px;"
            ).props("color='light-blue'")
            crowd_intra_val = ui.label("0").style("font-size:11px; font-weight:600;")
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

    def poll():
        try:
            # Mode
            mode = vk.get(KEYS["mode"]) or "—"
            mode_label.text = mode
            for m, btn in mode_btns.items():
                if m == mode:
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

            # Head
            head = vk_get(KEYS["head"])
            if head:
                cal = head.get("calibrated", False)
                head_cal.text = "YES" if cal else "NO"
                head_cal.style(f"color:{'#00d4aa' if cal else '#ff4a6b'}; font-weight:600;")
                pos = head.get("position")
                head_pos.text = str(pos) if pos is not None else "—"

            # Kinematics
            kin = vk_get(KEYS["kinematics"])
            if kin:
                kin_v.text = fmt(kin.get("v"), 3)
                kin_w.text = fmt(kin.get("w"), 3)
                ld = kin.get("leg_dir", "stop")
                hd = kin.get("head_dir", "stop")
                leg_dir_label.text = ld
                head_dir_label.text = hd
                leg_icon.text = dir_arrow(ld)
                head_icon.text = dir_arrow(hd)

            # Front radar
            rf = vk_get(KEYS["radar_front"])
            if rf:
                front_det.text = "YES" if rf.get("detected") else "no"
                d = rf.get("dist")
                front_dist.text = f"{fmt(d, 3)} m" if d is not None else "—"
                front_intra_bar.value = bar_pct(rf.get("intra", 0))
                front_intra_val.text = fmt(rf.get("intra", 0), 1)
                front_inter_bar.value = bar_pct(rf.get("inter", 0))
                front_inter_val.text = fmt(rf.get("inter", 0), 1)

            # Joystick radar
            rj = vk_get(KEYS["radar_joy"])
            if rj:
                joy_det.text = "YES" if rj.get("detected") else "no"
                d = rj.get("dist")
                joy_dist.text = f"{fmt(d, 3)} m" if d is not None else "—"
                joy_intra_bar.value = bar_pct(rj.get("intra", 0))
                joy_intra_val.text = fmt(rj.get("intra", 0), 1)
                joy_inter_bar.value = bar_pct(rj.get("inter", 0))
                joy_inter_val.text = fmt(rj.get("inter", 0), 1)

            # Crowd
            cr = vk_get(KEYS["crowd"])
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

            # Maze
            mz = vk_get(KEYS["maze"])
            if mz:
                maze_status.text = mz.get("status", "—")
                step = mz.get("step", 0)
                total = mz.get("total_steps", 0)
                maze_step.text = f"{step} / {total}"
                maze_obstacle.text = "BLOCKED" if mz.get("obstacle") else "clear"
                maze_bar.value = (step / total) if total > 0 else 0

            # Connection indicator
            conn_dot.style("font-size:10px; color:#00d4aa;")
            conn_text.text = "live"

        except Exception as e:
            conn_dot.style("font-size:10px; color:#ff4a6b;")
            conn_text.text = f"error: {e}"

    ui.timer(POLL_INTERVAL, poll)


# ── Entry point ──────────────────────────────────────────────────────────────

ui.run(
    title="ChippyPi",
    host="0.0.0.0",
    port=8080,
    reload=False,
    show=False,
)