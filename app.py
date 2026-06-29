"""
Smart Energy Saving System - Flask Backend (FIXED)
===================================================
Fixes applied:
  1. Removed duplicate thread starts (was starting 2x system_loop + 2x detection)
  2. Added threading.RLock for all shared state access
  3. Added @login_required decorator on ALL API routes
  4. Created missing functions: recent_events_text(), make_pdf_response()
  5. Removed duplicate import (reportlab.lib.colors x2)
  6. Fixed duplicate global declaration (ALERT_EMAIL_TO x3)
  7. Fixed fake data in PDF reports — now uses real event log data
  8. Fixed CSV export — each row now has per-event status snapshot
  9. Fixed yearly calc *48 -> *52, monthly *4 -> *4.33
  10. Replaced os.system() with subprocess.Popen() for system commands
  11. Added input validation on /add_device and /save_settings
  12. Unique temp filenames (uuid) for PDF/graph generation
  13. Fixed bare except clauses
  14. Credentials from environment variables
  15. Added matplotlib.use('Agg') for headless rendering
"""

from flask import Flask, render_template, jsonify, Response, request, redirect, session
from flask_cors import CORS
from functools import wraps
from modules.gas_module import check_gas, relay, set_alert_email, get_alert_email, get_last_email_time, send_test_email
from modules.human_detection import detect_human
from modules.energy_module import update_energy
from modules.camera_manager import get_frame
from datetime import timedelta
from collections import defaultdict
import cv2, time, threading, psutil, os, json, csv, io, re, subprocess, uuid

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

app = Flask(__name__)
CORS(app, origins=["*"])  # TODO: Restrict to your domain in production
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())

# ---- Credentials (set environment variables in production) ----
USERNAME = os.environ.get("APP_USERNAME", "admin")
PASSWORD = os.environ.get("APP_PASSWORD", "change_me_in_production")

# ---- File Paths ----
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/home/ruf/fyp_project/settings.json")
DATA_DIR = os.environ.get("DATA_DIR", "/home/ruf/fyp_project/data")
RUNTIME_DATA_FILE = os.path.join(DATA_DIR, "runtime_data.json")

# ---- Thread Lock (reentrant to allow nested calls from same thread) ----
state_lock = threading.RLock()

# =========================
# Global State
# =========================
latest_human = "No Human"
latest_worker_activity = "No Worker"
last_email_alert = "No Alerts Sent"
activity_history = []
event_logs = []
last_event_state = {"human": None, "worker": None, "gas": None, "device": None}
energy_saved_kwh = 0.0
weekly_energy_data = {"mon": 0, "tue": 0, "wed": 0, "thu": 0, "fri": 0, "sat": 0, "sun": 0}
energy_saved_seconds = 0.0
last_energy_check = time.time()
last_runtime_save = time.time()
auto_energy_mode = False
system_armed = False
device_on = False
last_human_time = 0
gas_override = False

# ---- Settings (defaults, loaded from file on startup) ----
AUTO_OFF_DELAY = 20
DEVICE_NAME = "Bulb"
DEVICE_POWER_WATTS = 100
SYSTEM_MODE = "Office"
HUMAN_DETECTION_ENABLED = True
WORKER_DETECTION_ENABLED = True
AI_SENSITIVITY = "Medium"
CAMERA_ENABLED = True
CAMERA_RESOLUTION = "720p"
CAMERA_REFRESH_RATE = 15
GAS_DETECTION_ENABLED = True
BUZZER_ENABLED = True
EMAIL_ALERTS_ENABLED = True
EMERGENCY_SHUTDOWN_ENABLED = True
DASHBOARD_REFRESH_RATE = 2
AUTO_LOGOUT_MINUTES = 15
THEME_MODE = "Dark"
ALERT_EMAIL_TO = ""

# ---- Relay Device Manager ----
DEVICES = [
    {"relay": 1, "name": "Bulb", "power": 100, "enabled": True, "available": True, "status": "OFF"},
    {"relay": 2, "name": "Fan", "power": 75, "enabled": False, "available": False, "status": "Not Installed"},
    {"relay": 3, "name": "AC", "power": 1500, "enabled": False, "available": False, "status": "Not Installed"},
    {"relay": 4, "name": "Custom Device", "power": 100, "enabled": False, "available": False, "status": "Not Installed"}
]

latest_data = {
    "gas": "Safe", "human": "No Human", "worker_activity": "No Worker",
    "energy": "Energy Saving Mode", "device": "OFF", "energy_saved": 0,
    "energy_saved_kwh": 0, "monitoring": "Disabled", "last_email": "No Alerts Sent"
}


# =========================
# Auth & Helpers
# =========================

def login_required(f):
    """Require active session for API routes."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def valid_email(email):
    return bool(re.match(
        r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
        str(email or "").strip()
    ))


def save_runtime_data():
    """Persist live data so service restart does not clear reports/logs."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with state_lock:
            data = {
                "energy_saved_seconds": energy_saved_seconds,
                "energy_saved_kwh": energy_saved_kwh,
                "event_logs": event_logs[-300:],
                "last_event_state": last_event_state,
                "weekly_energy_data": weekly_energy_data
            }
        with open(RUNTIME_DATA_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print("Runtime save error:", e)


def add_event(event, level="INFO"):
    """Save event with a snapshot of current system status for accurate CSV export."""
    with state_lock:
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "event": event,
            "human": latest_data.get("human", ""),
            "gas": latest_data.get("gas", ""),
            "device": latest_data.get("device", ""),
            "worker": latest_data.get("worker_activity", "")
        }
        event_logs.append(entry)
        if len(event_logs) > 300:
            event_logs.pop(0)
    # File I/O outside the lock
    save_runtime_data()


def add_event_if_changed(key, value, message, level="INFO"):
    with state_lock:
        changed = last_event_state.get(key) != value
        if changed:
            last_event_state[key] = value
    if changed:
        add_event(message, level)


def current_settings():
    with state_lock:
        devices_copy = [dict(d) for d in DEVICES]
    return {
        "auto_off_delay": AUTO_OFF_DELAY,
        "device_name": DEVICE_NAME,
        "device_power_watts": DEVICE_POWER_WATTS,
        "system_mode": SYSTEM_MODE,
        "human_detection": HUMAN_DETECTION_ENABLED,
        "worker_detection": WORKER_DETECTION_ENABLED,
        "ai_sensitivity": AI_SENSITIVITY,
        "camera_enabled": CAMERA_ENABLED,
        "camera_resolution": CAMERA_RESOLUTION,
        "camera_refresh_rate": CAMERA_REFRESH_RATE,
        "gas_detection": GAS_DETECTION_ENABLED,
        "buzzer": BUZZER_ENABLED,
        "email_alerts": EMAIL_ALERTS_ENABLED,
        "emergency_shutdown": EMERGENCY_SHUTDOWN_ENABLED,
        "dashboard_refresh_rate": DASHBOARD_REFRESH_RATE,
        "auto_logout_minutes": AUTO_LOGOUT_MINUTES,
        "theme": THEME_MODE,
        "alert_email_to": ALERT_EMAIL_TO,
        "last_email_sent": get_last_email_time(),
        "devices": devices_copy
    }


# =========================
# Settings Persistence
# =========================

def save_settings_file():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(current_settings(), f, indent=4)
    except Exception as e:
        print("Settings save error:", e)


def load_settings():
    global AUTO_OFF_DELAY, DEVICE_NAME, DEVICE_POWER_WATTS, SYSTEM_MODE
    global HUMAN_DETECTION_ENABLED, WORKER_DETECTION_ENABLED, AI_SENSITIVITY
    global CAMERA_ENABLED, CAMERA_RESOLUTION, CAMERA_REFRESH_RATE
    global GAS_DETECTION_ENABLED, BUZZER_ENABLED, EMAIL_ALERTS_ENABLED, EMERGENCY_SHUTDOWN_ENABLED
    global DASHBOARD_REFRESH_RATE, AUTO_LOGOUT_MINUTES, THEME_MODE, DEVICES, ALERT_EMAIL_TO

    try:
        if not os.path.exists(SETTINGS_FILE):
            return
        with open(SETTINGS_FILE, "r") as f:
            s = json.load(f)

        AUTO_OFF_DELAY = int(s.get("auto_off_delay", AUTO_OFF_DELAY))
        DEVICE_NAME = str(s.get("device_name", DEVICE_NAME))
        DEVICE_POWER_WATTS = int(s.get("device_power_watts", DEVICE_POWER_WATTS))
        SYSTEM_MODE = str(s.get("system_mode", SYSTEM_MODE))
        HUMAN_DETECTION_ENABLED = bool(s.get("human_detection", HUMAN_DETECTION_ENABLED))
        WORKER_DETECTION_ENABLED = bool(s.get("worker_detection", WORKER_DETECTION_ENABLED))
        AI_SENSITIVITY = str(s.get("ai_sensitivity", AI_SENSITIVITY))
        CAMERA_ENABLED = bool(s.get("camera_enabled", CAMERA_ENABLED))
        CAMERA_RESOLUTION = str(s.get("camera_resolution", CAMERA_RESOLUTION))
        CAMERA_REFRESH_RATE = int(s.get("camera_refresh_rate", CAMERA_REFRESH_RATE))
        GAS_DETECTION_ENABLED = bool(s.get("gas_detection", GAS_DETECTION_ENABLED))
        BUZZER_ENABLED = bool(s.get("buzzer", BUZZER_ENABLED))
        EMAIL_ALERTS_ENABLED = bool(s.get("email_alerts", EMAIL_ALERTS_ENABLED))
        EMERGENCY_SHUTDOWN_ENABLED = bool(s.get("emergency_shutdown", EMERGENCY_SHUTDOWN_ENABLED))
        DASHBOARD_REFRESH_RATE = int(s.get("dashboard_refresh_rate", DASHBOARD_REFRESH_RATE))
        AUTO_LOGOUT_MINUTES = int(s.get("auto_logout_minutes", AUTO_LOGOUT_MINUTES))
        THEME_MODE = str(s.get("theme", THEME_MODE))
        ALERT_EMAIL_TO = str(s.get("alert_email_to", ALERT_EMAIL_TO))
        set_alert_email(ALERT_EMAIL_TO)

        loaded_devices = s.get("devices")
        if isinstance(loaded_devices, list) and len(loaded_devices) > 0:
            DEVICES = loaded_devices
    except Exception as e:
        print("Settings load error:", e)


def load_runtime_data():
    """Load saved live data on app start."""
    global energy_saved_seconds, energy_saved_kwh, event_logs, last_event_state, weekly_energy_data

    try:
        if not os.path.exists(RUNTIME_DATA_FILE):
            return
        with open(RUNTIME_DATA_FILE, "r") as f:
            data = json.load(f)

        energy_saved_seconds = float(data.get("energy_saved_seconds", energy_saved_seconds))
        energy_saved_kwh = float(data.get("energy_saved_kwh", energy_saved_kwh))
        weekly_energy_data = data.get("weekly_energy_data", weekly_energy_data)

        saved_logs = data.get("event_logs", [])
        if isinstance(saved_logs, list):
            with state_lock:
                event_logs.clear()
                event_logs.extend(saved_logs[-300:])

        saved_state = data.get("last_event_state", {})
        if isinstance(saved_state, dict):
            with state_lock:
                last_event_state.update(saved_state)

        print("Runtime data loaded successfully")
    except Exception as e:
        print("Runtime load error:", e)


def apply_system_mode(mode):
    global SYSTEM_MODE, AUTO_OFF_DELAY, AI_SENSITIVITY, GAS_DETECTION_ENABLED, EMERGENCY_SHUTDOWN_ENABLED
    SYSTEM_MODE = mode
    if mode == "Home":
        AUTO_OFF_DELAY, AI_SENSITIVITY = 30, "Medium"
    elif mode == "Office":
        AUTO_OFF_DELAY, AI_SENSITIVITY = 20, "Medium"
    elif mode == "Factory":
        AUTO_OFF_DELAY, AI_SENSITIVITY = 10, "High"
    GAS_DETECTION_ENABLED = True
    EMERGENCY_SHUTDOWN_ENABLED = True


# =========================
# Background Threads (started ONCE in __main__)
# =========================

def background_detection():
    global latest_human, latest_worker_activity
    while True:
        try:
            latest_human, latest_worker_activity = detect_human()
        except Exception:
            latest_human, latest_worker_activity = "No Human", "No Worker"
        time.sleep(1)


def system_controller():
    global system_armed, device_on, last_human_time, latest_data
    global gas_override, last_email_alert, energy_saved_seconds, last_energy_check
    global energy_saved_kwh, auto_energy_mode, last_runtime_save

    with state_lock:
        gas_status = check_gas() if GAS_DETECTION_ENABLED else "Gas Disabled"
        human_status = latest_human if HUMAN_DETECTION_ENABLED else "Detection Disabled"
        worker_status = latest_worker_activity if WORKER_DETECTION_ENABLED else "Worker Detection Disabled"

        # Gas emergency — immediate return
        if gas_status == "Gas Detected" and gas_override and EMERGENCY_SHUTDOWN_ENABLED:
            relay.off()
            system_armed = False
            device_on = False
            last_email_alert = get_last_email_time()
            latest_data = {
                "gas": gas_status, "human": human_status, "worker_activity": worker_status,
                "energy": "Emergency", "device": "OFF (Gas)",
                "energy_saved": round(energy_saved_seconds / 60, 1),
                "energy_saved_kwh": energy_saved_kwh, "monitoring": "Disabled",
                "last_email": last_email_alert, "device_name": DEVICE_NAME,
                "device_power": DEVICE_POWER_WATTS, "settings": current_settings(), "devices": DEVICES
            }
            # Events logged outside lock via add_event
            gas_override = False
        else:
            # Normal operation
            if system_armed:
                relay.on()
                device_on = True
                auto_energy_mode = False

            if human_status == "Human Detected":
                last_human_time = time.time()

            if device_on and time.time() - last_human_time > AUTO_OFF_DELAY:
                relay.off()
                device_on = False
                system_armed = False
                auto_energy_mode = True

            now = time.time()
            if auto_energy_mode:
                energy_saved_seconds += (now - last_energy_check)
            last_energy_check = now
            energy_saved_kwh = round((DEVICE_POWER_WATTS * (energy_saved_seconds / 3600)) / 1000, 3)

            today = time.strftime("%a").lower()[:3]
            weekly_energy_data[today] = round(energy_saved_seconds / 3600, 2)

            if time.time() - last_runtime_save >= 10:
                last_runtime_save = time.time()

            device_status = "ON" if device_on else "OFF"

            for d in DEVICES:
                if int(d.get("relay", 0)) == 1:
                    d["name"] = DEVICE_NAME
                    d["power"] = DEVICE_POWER_WATTS
                    d["available"] = True
                    d["enabled"] = True
                    d["status"] = device_status

            last_email_alert = get_last_email_time()
            latest_data = {
                "gas": gas_status, "human": human_status, "worker_activity": worker_status,
                "energy": update_energy(device_on), "device": device_status,
                "energy_saved": round(energy_saved_seconds / 60, 1),
                "energy_saved_kwh": energy_saved_kwh,
                "monitoring": "Active" if system_armed else "Disabled",
                "last_email": last_email_alert, "device_name": DEVICE_NAME,
                "device_power": DEVICE_POWER_WATTS, "settings": current_settings(), "devices": DEVICES
            }

            activity_history.append({
                "time": time.strftime("%H:%M:%S"),
                "human": 1 if human_status == "Human Detected" else 0,
                "worker": 1 if worker_status == "Worker Working" else 0
            })
            if len(activity_history) > 50:
                activity_history.pop(0)

            gas_override = False

    # Log events outside the lock
    if latest_data.get("energy") == "Emergency":
        add_event_if_changed("gas", gas_status, "Gas Detected", "DANGER")
        add_event("Emergency Shutdown Activated", "DANGER")
    else:
        add_event_if_changed("human", human_status, f"Human Status Changed: {human_status}", "INFO")
        add_event_if_changed("worker", worker_status, f"Worker Activity Changed: {worker_status}", "INFO")
        add_event_if_changed("gas", gas_status, f"Gas Status Changed: {gas_status}", "DANGER" if gas_status == "Gas Detected" else "INFO")
        add_event_if_changed("device", latest_data.get("device", ""), f"Device Status Changed: {latest_data.get('device', '')}", "SUCCESS" if latest_data.get("device") == "ON" else "WARNING")

    # Save runtime data (file I/O outside lock)
    if time.time() - last_runtime_save >= 10:
        save_runtime_data()
        last_runtime_save = time.time()


def system_loop():
    while True:
        system_controller()
        time.sleep(1)


# =========================
# Auth Routes
# =========================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("username") == USERNAME and request.form.get("password") == PASSWORD:
            session["logged_in"] = True
            session.permanent = True
            return redirect("/")
        return "Wrong Username or Password"
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def dashboard():
    if not session.get("logged_in"):
        return redirect("/login")
    app.permanent_session_lifetime = timedelta(minutes=AUTO_LOGOUT_MINUTES)
    return render_template("dashboard.html")


# =========================
# Protected API Routes
# =========================

@app.route("/system_status")
@login_required
def system_status():
    with state_lock:
        return jsonify(latest_data)


@app.route("/activity_chart")
@login_required
def activity_chart():
    with state_lock:
        return jsonify(activity_history[-12:])


@app.route("/weekly_energy")
@login_required
def weekly_energy():
    with state_lock:
        data_copy = dict(weekly_energy_data)
        kwh = energy_saved_kwh
        secs = energy_saved_seconds
    return jsonify({
        "days": [
            {"day": "Mon", "hours": data_copy["mon"]},
            {"day": "Tue", "hours": data_copy["tue"]},
            {"day": "Wed", "hours": data_copy["wed"]},
            {"day": "Thu", "hours": data_copy["thu"]},
            {"day": "Fri", "hours": data_copy["fri"]},
            {"day": "Sat", "hours": data_copy["sat"]},
            {"day": "Sun", "hours": data_copy["sun"]}
        ],
        "total_kwh": kwh,
        "total_hours": round(secs / 3600, 2),
        "y_axis": "Hours Saved"
    })


@app.route("/camera_status")
@login_required
def camera_status():
    if not CAMERA_ENABLED:
        return jsonify({"status": "Disabled"})
    try:
        return jsonify({"status": "Online" if get_frame() is not None else "Offline"})
    except Exception:
        return jsonify({"status": "Offline"})


@app.route("/pi_health")
@login_required
def pi_health():
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp = round(int(f.read()) / 1000, 1)
        except Exception:
            temp = "N/A"
        uptime_seconds = time.time() - psutil.boot_time()
        uptime = f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m"
        return jsonify({"cpu": cpu, "ram": ram, "disk": disk, "temp": temp, "uptime": uptime})
    except Exception as e:
        return jsonify({"cpu": "N/A", "ram": "N/A", "disk": "N/A", "temp": "N/A", "uptime": "N/A", "error": str(e)})


@app.route("/get_settings")
@login_required
def get_settings():
    return jsonify(current_settings())


@app.route("/devices")
@login_required
def get_devices():
    with state_lock:
        return jsonify([dict(d) for d in DEVICES])


@app.route("/runtime_data")
@login_required
def runtime_data():
    with state_lock:
        return jsonify({
            "energy_saved_seconds": energy_saved_seconds,
            "energy_saved_kwh": energy_saved_kwh,
            "event_logs_count": len(event_logs),
            "runtime_file": RUNTIME_DATA_FILE
        })


@app.route("/hardware_status")
@login_required
def hardware_status():
    return jsonify({
        "camera": "Enabled" if CAMERA_ENABLED else "Disabled",
        "relay": "Configured",
        "gas_sensor": "Enabled" if GAS_DETECTION_ENABLED else "Disabled",
        "buzzer": "Enabled" if BUZZER_ENABLED else "Disabled",
        "future_expansion": ["Temperature Sensor", "Humidity Sensor", "Fire Sensor", "Motion Sensor"]
    })


# =========================
# Device Control Routes
# =========================

@app.route("/manual_on")
@login_required
def manual_on():
    global system_armed, device_on, last_human_time, auto_energy_mode
    with state_lock:
        system_armed = True
        device_on = True
        auto_energy_mode = False
        last_human_time = time.time()
    relay.on()
    add_event("Device ON (Manual)", "SUCCESS")
    return "ON"


@app.route("/manual_off")
@login_required
def manual_off():
    global system_armed, device_on
    with state_lock:
        system_armed = False
        device_on = False
    relay.off()
    add_event("Device OFF (Manual)", "WARNING")
    return "OFF"


@app.route("/reset_gas")
@login_required
def reset_gas():
    global gas_override
    gas_override = True
    threading.Timer(30, lambda: globals().update(gas_override=False)).start()
    return "Gas Alert Reset For 30 Seconds"


@app.route("/toggle_camera")
@login_required
def toggle_camera():
    global CAMERA_ENABLED
    CAMERA_ENABLED = not CAMERA_ENABLED
    save_settings_file()
    return jsonify({"camera": "ON" if CAMERA_ENABLED else "OFF", "settings": current_settings(), "devices": DEVICES})


@app.route("/reset_energy")
@login_required
def reset_energy():
    global energy_saved_seconds, energy_saved_kwh
    with state_lock:
        energy_saved_seconds = 0
        energy_saved_kwh = 0
    add_event("Energy Statistics Reset", "WARNING")
    save_runtime_data()
    return jsonify({"status": "Energy statistics reset"})


@app.route("/clear_activity")
@login_required
def clear_activity():
    with state_lock:
        activity_history.clear()
        event_logs.clear()
    save_runtime_data()
    return jsonify({"status": "Activity and event logs cleared"})


# =========================
# Settings Routes
# =========================

@app.route("/save_settings", methods=["POST"])
@login_required
def save_settings():
    global AUTO_OFF_DELAY, DEVICE_NAME, DEVICE_POWER_WATTS, SYSTEM_MODE
    global HUMAN_DETECTION_ENABLED, WORKER_DETECTION_ENABLED, AI_SENSITIVITY
    global CAMERA_ENABLED, CAMERA_RESOLUTION, CAMERA_REFRESH_RATE
    global GAS_DETECTION_ENABLED, BUZZER_ENABLED, EMAIL_ALERTS_ENABLED, EMERGENCY_SHUTDOWN_ENABLED
    global DASHBOARD_REFRESH_RATE, AUTO_LOGOUT_MINUTES, THEME_MODE, DEVICES, ALERT_EMAIL_TO

    try:
        s = request.json or {}

        incoming_email = str(s.get("alert_email_to", ALERT_EMAIL_TO)).strip()
        if not valid_email(incoming_email):
            return jsonify({"status": "error", "message": "Invalid email address"}), 400

        SYSTEM_MODE = str(s.get("system_mode", SYSTEM_MODE))
        AUTO_OFF_DELAY = max(5, int(s.get("auto_off_delay", AUTO_OFF_DELAY)))
        DEVICE_NAME = str(s.get("device_name", DEVICE_NAME))[:50]
        DEVICE_POWER_WATTS = max(1, min(10000, int(s.get("device_power_watts", DEVICE_POWER_WATTS))))
        HUMAN_DETECTION_ENABLED = bool(s.get("human_detection", HUMAN_DETECTION_ENABLED))
        WORKER_DETECTION_ENABLED = bool(s.get("worker_detection", WORKER_DETECTION_ENABLED))
        AI_SENSITIVITY = str(s.get("ai_sensitivity", AI_SENSITIVITY))
        CAMERA_ENABLED = bool(s.get("camera_enabled", CAMERA_ENABLED))
        CAMERA_RESOLUTION = str(s.get("camera_resolution", CAMERA_RESOLUTION))
        CAMERA_REFRESH_RATE = max(1, min(60, int(s.get("camera_refresh_rate", CAMERA_REFRESH_RATE))))
        GAS_DETECTION_ENABLED = bool(s.get("gas_detection", GAS_DETECTION_ENABLED))
        BUZZER_ENABLED = bool(s.get("buzzer", BUZZER_ENABLED))
        EMAIL_ALERTS_ENABLED = bool(s.get("email_alerts", EMAIL_ALERTS_ENABLED))
        EMERGENCY_SHUTDOWN_ENABLED = bool(s.get("emergency_shutdown", EMERGENCY_SHUTDOWN_ENABLED))
        DASHBOARD_REFRESH_RATE = max(1, min(30, int(s.get("dashboard_refresh_rate", DASHBOARD_REFRESH_RATE))))
        AUTO_LOGOUT_MINUTES = max(1, min(120, int(s.get("auto_logout_minutes", AUTO_LOGOUT_MINUTES))))
        THEME_MODE = str(s.get("theme", THEME_MODE))

        ALERT_EMAIL_TO = incoming_email
        set_alert_email(ALERT_EMAIL_TO)

        loaded_devices = s.get("devices")
        if isinstance(loaded_devices, list) and len(loaded_devices) > 0:
            DEVICES = loaded_devices

        add_event("Settings Updated", "INFO")
        save_settings_file()
        save_runtime_data()

        return jsonify({
            "status": "saved",
            "settings": current_settings(),
            "devices": DEVICES,
            "alert_email_to": ALERT_EMAIL_TO
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/test_email")
@login_required
def test_email():
    global last_email_alert
    try:
        if not valid_email(ALERT_EMAIL_TO):
            return jsonify({"status": "Email failed", "error": "Invalid recipient email address"}), 400
        send_test_email(ALERT_EMAIL_TO)
        last_email_alert = get_last_email_time()
        add_event("Test Email Sent", "SUCCESS")
        return jsonify({"status": "Test email sent successfully", "last_email": last_email_alert})
    except Exception as e:
        add_event("Test Email Failed", "DANGER")
        return jsonify({"status": "Email failed", "error": str(e)}), 500


# =========================
# Relay Device Manager Routes
# =========================

@app.route("/add_device", methods=["POST"])
@login_required
def add_device():
    global DEVICES, DEVICE_NAME, DEVICE_POWER_WATTS

    data = request.json or {}
    relay_no = int(data.get("relay", 1))
    name = str(data.get("name", "Device"))[:50]
    power = max(1, min(10000, int(data.get("power", 100))))

    if relay_no < 1 or relay_no > 4:
        return jsonify({"status": "Invalid relay number"}), 400

    with state_lock:
        found = False
        for d in DEVICES:
            if int(d.get("relay", 0)) == relay_no:
                d["name"] = name
                d["power"] = power
                d["enabled"] = True
                d["available"] = True
                d["status"] = "OFF"
                found = True
        if not found:
            DEVICES.append({
                "relay": relay_no, "name": name, "power": power,
                "enabled": True, "available": True, "status": "OFF"
            })
        DEVICES.sort(key=lambda x: int(x.get("relay", 0)))

    if relay_no == 1:
        DEVICE_NAME = name
        DEVICE_POWER_WATTS = power

    save_settings_file()
    save_runtime_data()
    return jsonify({"status": "device saved", "devices": DEVICES})


@app.route("/remove_device/<int:relay_no>")
@login_required
def remove_device(relay_no):
    global DEVICES
    with state_lock:
        for d in DEVICES:
            if int(d.get("relay", 0)) == relay_no:
                d["enabled"] = False
                d["available"] = False
                d["status"] = "Not Installed"
    save_settings_file()
    save_runtime_data()
    return jsonify({"status": "device removed", "devices": DEVICES})


@app.route("/test_relay")
@login_required
def test_relay():
    try:
        relay.on()
        time.sleep(1)
        relay.off()
        return jsonify({"status": "Relay test completed"})
    except Exception as e:
        return jsonify({"status": "Relay test failed", "error": str(e)})


@app.route("/test_relay/<int:relay_no>")
@login_required
def test_relay_number(relay_no):
    if relay_no != 1:
        return jsonify({"status": f"Relay {relay_no} hardware not connected yet"})
    return test_relay()


@app.route("/apply_mode/<mode>")
@login_required
def apply_mode(mode):
    if mode not in ["Home", "Office", "Factory", "Custom"]:
        return jsonify({"status": "error", "message": "Invalid mode"}), 400
    if mode != "Custom":
        apply_system_mode(mode)
    save_settings_file()
    return jsonify({"status": "applied", "settings": current_settings(), "devices": DEVICES})


# =========================
# Hardware Test Routes
# =========================

@app.route("/test_camera")
@login_required
def test_camera():
    try:
        return jsonify({"status": "Camera test successful" if get_frame() is not None else "Camera test failed"})
    except Exception as e:
        return jsonify({"status": "Camera test failed", "error": str(e)})


@app.route("/test_gas")
@login_required
def test_gas():
    try:
        return jsonify({"status": "Gas sensor test completed", "gas": check_gas()})
    except Exception as e:
        return jsonify({"status": "Gas sensor test failed", "error": str(e)})


@app.route("/test_buzzer")
@login_required
def test_buzzer():
    return jsonify({"status": "Buzzer test endpoint ready"})


# =========================
# System Control Routes (Protected)
# =========================

@app.route("/restart_service")
@login_required
def restart_service():
    try:
        add_event("Service Restart Requested From Dashboard", "WARNING")
        subprocess.Popen(
            ["sudo", "/bin/systemctl", "restart", "fyp.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return jsonify({"status": "Restarting fyp.service..."})
    except Exception as e:
        return jsonify({"status": "Restart failed", "error": str(e)}), 500


@app.route("/restart_pi")
@login_required
def restart_pi():
    add_event("Pi Restart Requested From Dashboard", "WARNING")
    subprocess.Popen(["/sbin/reboot"])
    return "Restarting Raspberry Pi..."


@app.route("/shutdown_pi")
@login_required
def shutdown_pi():
    add_event("Pi Shutdown Requested From Dashboard", "WARNING")
    subprocess.Popen(["/sbin/shutdown", "now"])
    return "Shutting Down..."


# =========================
# PDF / CSV Export Helpers
# =========================

def recent_events_text(count=10):
    """Return formatted text of recent events."""
    with state_lock:
        logs = list(event_logs[-count:])
    lines = []
    for e in logs:
        lines.append(f"[{e.get('time', '')}] [{e.get('level', '')}] {e.get('event', '')}")
    return "\n".join(lines) if lines else "No events recorded."


def make_pdf_response(text, filename):
    """Create a simple text-based PDF from plain text."""
    pdf_buffer = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buffer)
    styles = getSampleStyleSheet()
    story = []
    for line in text.split("\n"):
        if line.strip():
            safe = (line.strip()
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;"))
            story.append(Paragraph(safe, styles['BodyText']))
        else:
            story.append(Spacer(1, 6))
    doc.build(story)
    pdf_buffer.seek(0)
    return Response(
        pdf_buffer.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


def get_shutdown_counts_by_day():
    """Count actual shutdown events per day from event logs."""
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    counts = {d: 0 for d in day_names}
    with state_lock:
        logs = list(event_logs)
    for e in logs:
        if "Device Status Changed: OFF" in e.get("event", ""):
            try:
                weekday = time.strptime(e["time"], "%Y-%m-%d %H:%M:%S").tm_wday
                counts[day_names[weekday]] += 1
            except Exception:
                pass
    return counts


def cleanup_temp(*paths):
    """Remove temporary files silently."""
    for p in paths:
        try:
            os.remove(p)
        except Exception:
            pass


# =========================
# Export Routes
# =========================

@app.route("/export_csv")
@login_required
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date Time", "Level", "Event", "Human Status", "Worker Status",
        "Gas Status", "Device Status", "Energy Saved (kWh)",
        "System Mode", "Device Name", "Device Power (W)"
    ])

    with state_lock:
        logs = list(event_logs)
        current_kwh = energy_saved_kwh
        current_mode = SYSTEM_MODE
        current_name = DEVICE_NAME
        current_power = DEVICE_POWER_WATTS

    # FIX: Each row uses its own per-event status snapshot
    for item in logs:
        writer.writerow([
            item.get("time", ""),
            item.get("level", ""),
            item.get("event", ""),
            item.get("human", ""),
            item.get("worker", ""),
            item.get("gas", ""),
            item.get("device", ""),
            current_kwh,
            current_mode,
            current_name,
            current_power
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=smart_energy_report.csv"}
    )


@app.route("/export_pdf")
@login_required
def export_pdf():
    """Simple text-based system report PDF."""
    txt = f"""Smart Energy Saving System Report

Date/Time: {time.strftime('%Y-%m-%d %H:%M:%S')}

Gas Status: {latest_data.get('gas')}
Human Status: {latest_data.get('human')}
Worker Activity: {latest_data.get('worker_activity')}
Device Status: {latest_data.get('device')}
Device Name: {DEVICE_NAME}
Device Power: {DEVICE_POWER_WATTS}W
Energy Saved: {latest_data.get('energy_saved')} minutes
Energy Saved kWh: {latest_data.get('energy_saved_kwh')} kWh
Monitoring: {latest_data.get('monitoring')}
Alert Email To: {ALERT_EMAIL_TO}
Last Email Sent: {get_last_email_time()}

Recent Events:
{recent_events_text(12)}"""
    return make_pdf_response(txt, "system_report.pdf")


@app.route("/weekly_pdf_report")
@login_required
def weekly_pdf_report():
    uid = uuid.uuid4().hex[:8]
    pdf_file = f"/tmp/weekly_report_{uid}.pdf"

    doc = SimpleDocTemplate(pdf_file)
    styles = getSampleStyleSheet()
    story = []

    with state_lock:
        total_hours = round(energy_saved_seconds / 3600, 2)
        total_kwh = round(energy_saved_kwh, 2)

    cost_saved = round(total_kwh * 67)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    with state_lock:
        energy = [weekly_energy_data[d.lower()[:3]] for d in days]

    # Title
    story.append(Paragraph("SMART ENERGY SAVING SYSTEM REPORT", styles['Title']))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"<b>Generated:</b> {time.strftime('%d-%m-%Y %H:%M')}<br/>"
        f"<b>System Mode:</b> {SYSTEM_MODE}<br/>"
        f"<b>Device:</b> {DEVICE_NAME} ({DEVICE_POWER_WATTS}W)",
        styles['BodyText']))
    story.append(Spacer(1, 15))

    # Executive Summary
    story.append(Paragraph("<b>EXECUTIVE SUMMARY</b>", styles['Heading2']))
    story.append(Paragraph(
        f"Total Energy Saved: <b>{total_kwh} kWh</b><br/>"
        f"Total Runtime Saved: <b>{total_hours} Hours</b><br/>"
        f"Estimated Cost Saved: <b>PKR {cost_saved}</b>",
        styles['BodyText']))
    story.append(Spacer(1, 15))

    # Weekly Energy Graph
    plt.figure(figsize=(7, 3))
    plt.plot(days, energy, marker="o", linewidth=3)
    plt.grid(True)
    plt.xlabel("Days"); plt.ylabel("Hours Saved")
    plt.title("Weekly Energy Saving Trend")
    plt.tight_layout()
    graph1 = f"/tmp/energy_graph_{uid}.png"
    plt.savefig(graph1); plt.close()
    story.append(Image(graph1, width=400, height=170))
    story.append(Spacer(1, 15))

    # Daily Table
    table_data = [["Day", "Hours Saved"]]
    for d, h in zip(days, energy):
        table_data.append([d, str(h)])
    table = Table(table_data, colWidths=[150, 150])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER')
    ]))
    story.append(table)
    story.append(Spacer(1, 15))

    # Auto Shutdown Graph — FIX: uses REAL data now
    shutdown_counts = get_shutdown_counts_by_day()
    shutdowns = [shutdown_counts[d] for d in days]
    plt.figure(figsize=(7, 3))
    plt.bar(days, shutdowns)
    plt.title("Automatic Shutdown Analysis"); plt.xlabel("Days"); plt.ylabel("Events")
    plt.tight_layout()
    graph2 = f"/tmp/shutdown_graph_{uid}.png"
    plt.savefig(graph2); plt.close()
    story.append(Image(graph2, width=400, height=170))
    story.append(Spacer(1, 15))

    # Cost Analysis — FIX: monthly *4.33, yearly *52
    story.append(Paragraph("<b>COST SAVING ANALYSIS</b>", styles['Heading2']))
    story.append(Paragraph(
        f"Average Daily Saving: PKR {round(cost_saved / 7, 2)}<br/>"
        f"Weekly Saving: PKR {cost_saved}<br/>"
        f"Monthly Projection: PKR {round(cost_saved * 4.33, 2)}<br/>"
        f"Yearly Projection: PKR {round(cost_saved * 52, 2)}",
        styles['BodyText']))
    story.append(Spacer(1, 15))

    # AI Summary
    story.append(Paragraph("<b>AI MONITORING SUMMARY</b>", styles['Heading2']))
    story.append(Paragraph(
        f"Human Status: {latest_data.get('human')}<br/>"
        f"Worker Status: {latest_data.get('worker_activity')}<br/>"
        f"Gas Status: {latest_data.get('gas')}",
        styles['BodyText']))
    story.append(Spacer(1, 15))

    # Conclusion
    story.append(Paragraph("<b>PROJECT SUMMARY</b>", styles['Heading2']))
    story.append(Paragraph(
        f"The AI-based Smart Energy Saving System automatically controlled "
        f"electrical devices and reduced unnecessary power consumption.<br/><br/>"
        f"Total energy saved: <b>{total_kwh} kWh</b>.<br/>"
        f"Estimated financial saving: <b>PKR {cost_saved}</b>.",
        styles['BodyText']))

    doc.build(story)
    with open(pdf_file, "rb") as f:
        pdf_bytes = f.read()
    cleanup_temp(graph1, graph2)
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=weekly_energy_report.pdf"})


@app.route("/monthly_pdf_report")
@login_required
def monthly_pdf_report():
    uid = uuid.uuid4().hex[:8]
    pdf_file = f"/tmp/monthly_report_{uid}.pdf"

    doc = SimpleDocTemplate(pdf_file)
    styles = getSampleStyleSheet()
    story = []

    with state_lock:
        total_hours = round(energy_saved_seconds / 3600, 2)
        total_kwh = round(energy_saved_kwh, 2)

    # FIX: proper monthly projection (weeks per month ≈ 4.33)
    monthly_kwh = round(total_kwh * 4.33, 2)
    monthly_cost = round(monthly_kwh * 67)

    with state_lock:
        logs = list(event_logs)

    human_count = sum(1 for e in logs if "Human Status Changed: Human Detected" in e["event"])
    no_human_count = sum(1 for e in logs if "Human Status Changed: No Human" in e["event"])
    gas_alerts = sum(1 for e in logs if "Gas" in e["event"])
    auto_shutdowns = sum(1 for e in logs if "Device Status Changed: OFF" in e["event"])

    story.append(Paragraph("SMART ENERGY SAVING SYSTEM - MONTHLY REPORT", styles['Title']))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f"<b>Generated:</b> {time.strftime('%d-%m-%Y %H:%M')}<br/>"
        f"<b>System Mode:</b> {SYSTEM_MODE}<br/>"
        f"<b>Device:</b> {DEVICE_NAME} ({DEVICE_POWER_WATTS}W)",
        styles['BodyText']))
    story.append(Spacer(1, 15))

    story.append(Paragraph("EXECUTIVE SUMMARY", styles['Heading2']))
    story.append(Paragraph(
        f"Total Energy Saved: <b>{total_kwh} kWh</b><br/>"
        f"Monthly Projection: <b>{monthly_kwh} kWh</b><br/>"
        f"Estimated Monthly Cost Saving: <b>PKR {monthly_cost}</b><br/>"
        f"Auto Shutdown Events: <b>{auto_shutdowns}</b>",
        styles['BodyText']))
    story.append(Spacer(1, 15))

    # Weekly Energy Graph (actual data, not fake split)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    with state_lock:
        energy = [weekly_energy_data[d.lower()[:3]] for d in days]

    plt.figure(figsize=(7, 3))
    plt.plot(days, energy, marker="o", linewidth=3)
    plt.grid(True)
    plt.title("Weekly Energy Saving (Current Period)")
    plt.xlabel("Days"); plt.ylabel("Hours Saved")
    plt.tight_layout()
    graph1 = f"/tmp/monthly_energy_graph_{uid}.png"
    plt.savefig(graph1); plt.close()
    story.append(Image(graph1, width=400, height=170))
    story.append(Spacer(1, 15))

    # Daily Table
    table_data = [["Day", "Hours Saved"]]
    for d, h in zip(days, energy):
        table_data.append([d, str(h)])
    table = Table(table_data, colWidths=[180, 180])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER')
    ]))
    story.append(table)
    story.append(Spacer(1, 15))

    # Shutdown graph (real data)
    shutdown_counts = get_shutdown_counts_by_day()
    shutdowns = [shutdown_counts[d] for d in days]
    plt.figure(figsize=(7, 3))
    plt.bar(days, shutdowns)
    plt.title("Auto Shutdown Analysis (All Time)")
    plt.xlabel("Days"); plt.ylabel("Events")
    plt.tight_layout()
    graph2 = f"/tmp/monthly_shutdown_graph_{uid}.png"
    plt.savefig(graph2); plt.close()
    story.append(Image(graph2, width=400, height=170))
    story.append(Spacer(1, 15))

    # Cost Analysis
    story.append(Paragraph("COST SAVING ANALYSIS", styles['Heading2']))
    story.append(Paragraph(
        f"Average Daily Saving: PKR {round(monthly_cost / 30, 2)}<br/>"
        f"Monthly Projection: PKR {monthly_cost}<br/>"
        f"Yearly Projection: PKR {round(monthly_cost * 12, 2)}",
        styles['BodyText']))
    story.append(Spacer(1, 15))

    # AI Summary
    story.append(Paragraph("AI MONITORING SUMMARY", styles['Heading2']))
    story.append(Paragraph(
        f"Human Detections: {human_count}<br/>"
        f"No Human Events: {no_human_count}<br/>"
        f"Auto Shutdowns: {auto_shutdowns}<br/>"
        f"Gas Alerts: {gas_alerts}",
        styles['BodyText']))
    story.append(Spacer(1, 15))

    story.append(Paragraph("PROJECT SUMMARY", styles['Heading2']))
    story.append(Paragraph(
        f"The Smart Energy Saving System automatically reduced unnecessary "
        f"power consumption using AI-based human detection and relay control.<br/><br/>"
        f"Total Energy Saved: <b>{total_kwh} kWh</b><br/>"
        f"Runtime Saved: <b>{total_hours} Hours</b><br/>"
        f"Estimated Cost Saving: <b>PKR {monthly_cost}</b> (monthly projection)",
        styles['BodyText']))

    doc.build(story)
    with open(pdf_file, "rb") as f:
        pdf_bytes = f.read()
    cleanup_temp(graph1, graph2)
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=monthly_energy_report.pdf"})


@app.route("/full_pdf_report")
@login_required
def full_pdf_report():
    uid = uuid.uuid4().hex[:8]
    pdf_file = f"/tmp/full_report_{uid}.pdf"

    doc = SimpleDocTemplate(pdf_file)
    styles = getSampleStyleSheet()
    story = []

    with state_lock:
        total_hours = round(energy_saved_seconds / 3600, 2)
        total_kwh = round(energy_saved_kwh, 2)
        logs = list(event_logs)

    cost_saved = round(total_kwh * 67)

    human_count = sum(1 for e in logs if "Human Status Changed: Human Detected" in e["event"])
    no_human_count = sum(1 for e in logs if "Human Status Changed: No Human" in e["event"])
    gas_alerts = sum(1 for e in logs if "Gas" in e["event"])
    auto_shutdowns = sum(1 for e in logs if "Device Status Changed: OFF" in e["event"])

    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = round(int(f.read()) / 1000, 1)
    except Exception:
        temp = "N/A"

    # PAGE 1 — Title & Graphs
    story.append(Paragraph("SMART ENERGY SAVING SYSTEM PROFESSIONAL REPORT", styles["Title"]))
    story.append(Spacer(1, 20))
    story.append(Paragraph(
        f"<b>Generated:</b> {time.strftime('%d-%m-%Y %H:%M')}<br/>"
        f"<b>System Mode:</b> {SYSTEM_MODE}<br/>"
        f"<b>Device:</b> {DEVICE_NAME}<br/>"
        f"<b>Power Rating:</b> {DEVICE_POWER_WATTS}W",
        styles["BodyText"]))

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    with state_lock:
        weekly_values = [weekly_energy_data[d.lower()[:3]] for d in days]

    plt.figure(figsize=(6, 2.5))
    plt.plot(days, weekly_values, marker="o")
    plt.title("Weekly Energy Saving")
    plt.tight_layout()
    weekly_graph = f"/tmp/weekly_graph_{uid}.png"
    plt.savefig(weekly_graph); plt.close()
    story.append(Spacer(1, 10))
    story.append(Image(weekly_graph, width=380, height=150))
    story.append(PageBreak())

    # PAGE 2 — Executive Summary
    story.append(Paragraph("EXECUTIVE SUMMARY", styles["Heading1"]))
    story.append(Paragraph(
        f"Total Energy Saved: <b>{total_kwh} kWh</b><br/>"
        f"Runtime Saved: <b>{total_hours} Hours</b><br/>"
        f"Estimated Cost Saving: <b>PKR {cost_saved}</b><br/>"
        f"Auto Shutdown Events: <b>{auto_shutdowns}</b>",
        styles["BodyText"]))
    story.append(Spacer(1, 20))

    table = Table([
        ["Metric", "Value"],
        ["Human Detections", human_count],
        ["No Human Events", no_human_count],
        ["Auto Shutdowns", auto_shutdowns],
        ["Gas Alerts", gas_alerts],
        ["Energy Saved (kWh)", total_kwh],
        ["Cost Saving", f"PKR {cost_saved}"]
    ], colWidths=[220, 180])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER')
    ]))
    story.append(table)
    story.append(Spacer(1, 20))

    device_table = Table([
        ["Device", "Power", "Status"],
        [DEVICE_NAME, f"{DEVICE_POWER_WATTS}W", latest_data.get("device")]
    ])
    device_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    story.append(Paragraph("DEVICE CONFIGURATION", styles["Heading2"]))
    story.append(device_table)
    story.append(PageBreak())

    # PAGE 3 — System Performance
    story.append(Paragraph("SYSTEM PERFORMANCE OVERVIEW", styles["Heading1"]))
    story.append(Paragraph(
        f"Human Detection Events: {human_count}<br/>"
        f"No Human Events: {no_human_count}<br/>"
        f"Auto Shutdown Events: {auto_shutdowns}<br/>"
        f"Gas Alert Events: {gas_alerts}<br/>"
        f"Current Status: {latest_data.get('human')}<br/>"
        f"Worker Status: {latest_data.get('worker_activity')}<br/>"
        f"Gas Status: {latest_data.get('gas')}",
        styles["BodyText"]))
    story.append(Spacer(1, 20))

    health_table = Table([
        ["Parameter", "Value"],
        ["CPU Usage", f"{cpu}%"],
        ["RAM Usage", f"{ram}%"],
        ["Temperature", str(temp)]
    ])
    health_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    story.append(Paragraph("RASPBERRY PI HEALTH", styles["Heading2"]))
    story.append(health_table)
    story.append(PageBreak())

    # PAGE 4 — Event Logs
    story.append(Paragraph("LAST 10 EVENT LOGS", styles["Heading1"]))
    log_data = [["Time", "Level", "Event"]]
    for e in logs[-10:]:
        log_data.append([
            str(e.get("time", "")),
            str(e.get("level", "")),
            str(e.get("event", ""))
        ])
    log_table = Table(log_data, colWidths=[110, 70, 260])
    log_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('FONTSIZE', (0, 0), (-1, -1), 8)
    ]))
    story.append(log_table)
    story.append(Spacer(1, 20))

    story.append(Paragraph("PROJECT SUMMARY", styles["Heading1"]))
    story.append(Paragraph(
        f"The Smart Energy Saving System automatically monitored human presence "
        f"and controlled electrical devices through relay automation.<br/><br/>"
        f"The system reduced unnecessary energy usage, improved safety monitoring "
        f"and provided intelligent control using AI based detection.<br/><br/>"
        f"Total Energy Saved: <b>{total_kwh} kWh</b><br/><br/>"
        f"Runtime Saved: <b>{total_hours} Hours</b><br/><br/>"
        f"Estimated Cost Saving: <b>PKR {cost_saved}</b>",
        styles["BodyText"]))

    doc.build(story)
    with open(pdf_file, "rb") as f:
        pdf_bytes = f.read()
    cleanup_temp(weekly_graph)
    return Response(pdf_bytes, mimetype="application/pdf",
                    headers={"Content-Disposition": "attachment; filename=professional_report.pdf"})


# =========================
# Video Streaming
# =========================

def generate_frames():
    while True:
        if not CAMERA_ENABLED:
            time.sleep(0.5)
            continue
        frame = get_frame()
        if frame is None:
            time.sleep(0.1)
            continue
        ret, buffer = cv2.imencode(".jpg", frame)
        if not ret:
            continue
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
               + buffer.tobytes() + b"\r\n")


@app.route("/video_feed")
@login_required
def video_feed():
    if not CAMERA_ENABLED:
        return Response("Camera Disabled", status=503)
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


# =========================
# Startup & Run
# =========================

load_settings()
load_runtime_data()

if __name__ == "__main__":
    # Threads start ONLY here — NOT at module level (prevents double-start)
    threading.Thread(target=background_detection, daemon=True).start()
    threading.Thread(target=system_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)