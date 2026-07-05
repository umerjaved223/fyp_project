from flask import Flask, render_template, jsonify, Response, request, redirect, session
from flask_cors import CORS
from modules.gas_module import check_gas, relay, set_alert_email, get_alert_email, get_last_email_time, send_test_email, clear_gas_alert
from modules.human_detection import detect_human
from modules.energy_module import update_energy
from modules.camera_manager import get_frame
from datetime import timedelta, date, datetime
import cv2, time, threading, psutil, os, json, csv, io, re, subprocess
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as pdfcanvas
import matplotlib.pyplot as plt

app = Flask(__name__)
CORS(app)
app.secret_key = "fyp_secret_key_2026"
USERNAME = "admin"
PASSWORD = "12345"
SETTINGS_FILE = "/home/ruf/fyp_project/settings.json"
DATA_DIR = "/home/ruf/fyp_project/data"
RUNTIME_DATA_FILE = os.path.join(DATA_DIR, "runtime_data.json")
runtime_save_lock = threading.Lock()

latest_human = "No Human"
latest_worker_activity = "No Worker"
last_email_alert = "No Alerts Sent"
activity_history = []
event_logs = []
last_event_state = {"human": None, "worker": None, "gas": None, "device": None}
energy_saved_kwh = 0
weekly_energy_data = {
    "mon": 0,
    "tue": 0,
    "wed": 0,
    "thu": 0,
    "fri": 0,
    "sat": 0,
    "sun": 0
}
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTH_WEEK_KEYS = ["week1", "week2", "week3", "week4", "week5"]
monthly_energy_data = {k: 0 for k in MONTH_WEEK_KEYS}
weekly_period_key = None
monthly_period_key = None
energy_saved_seconds = 0
last_energy_check = time.time()
last_runtime_save = time.time()
auto_energy_mode = False
system_armed = False
device_on = False
last_human_time = 0
gas_grace_until = 0

AUTO_OFF_DELAY = 20
ELECTRICITY_RATE_PKR_PER_KWH = 67
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
ALERT_EMAIL_TO = "umergujjarg391@gmail.com"

# Relay Device Manager
# Relay 1 is connected now. Relay 2/3/4 are future expansion until hardware is added.
DEVICES = [
    {"relay": 1, "name": "Bulb", "power": 100, "enabled": True, "available": True, "status": "OFF"},
    {"relay": 2, "name": "Fan", "power": 75, "enabled": False, "available": False, "status": "Not Installed"},
    {"relay": 3, "name": "AC", "power": 1500, "enabled": False, "available": False, "status": "Not Installed"},
    {"relay": 4, "name": "Custom Device", "power": 100, "enabled": False, "available": False, "status": "Not Installed"}
]

latest_data = {
    "gas":"Safe", "human":"No Human", "worker_activity":"No Worker",
    "energy":"Energy Saving Mode", "device":"OFF", "energy_saved":0,
    "energy_saved_kwh":0, "monitoring":"Disabled", "last_email":"No Alerts Sent"
}




def save_runtime_data():
    """Save live data so service restart does not clear reports/logs."""
    try:
        with runtime_save_lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            data = {
                "energy_saved_seconds": energy_saved_seconds,
                "energy_saved_kwh": energy_saved_kwh,
                "event_logs": event_logs[-300:],
                "last_event_state": last_event_state,
                "weekly_energy_data": weekly_energy_data,
                "monthly_energy_data": monthly_energy_data,
                "weekly_period_key": weekly_period_key,
                "monthly_period_key": monthly_period_key,
                "activity_history": activity_history[-50:]
            }
            tmp_file = RUNTIME_DATA_FILE + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(data, f, indent=4)
            os.replace(tmp_file, RUNTIME_DATA_FILE)
    except Exception as e:
        print("Runtime save error:", e)

def valid_email(email):
    return bool(re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", str(email or "").strip()))

def add_event(event, level="INFO"):
    """Save only important status changes, not every second."""
    event_logs.append({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "event": event
    })
    if len(event_logs) > 300:
        event_logs.pop(0)
    save_runtime_data()

def add_event_if_changed(key, value, message, level="INFO"):
    if last_event_state.get(key) != value:
        last_event_state[key] = value
        add_event(message, level)

def current_settings():
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
        "devices": DEVICES
    }

def save_settings_file():
    try:
        tmp_file = SETTINGS_FILE + ".tmp"
        with open(tmp_file, "w") as f:
            json.dump(current_settings(), f, indent=4)
        os.replace(tmp_file, SETTINGS_FILE)
    except Exception as e:
        print("Settings save error:", e)

def load_settings():
    global AUTO_OFF_DELAY, DEVICE_NAME, DEVICE_POWER_WATTS, SYSTEM_MODE
    global HUMAN_DETECTION_ENABLED, WORKER_DETECTION_ENABLED, AI_SENSITIVITY
    global CAMERA_ENABLED, CAMERA_RESOLUTION, CAMERA_REFRESH_RATE
    global GAS_DETECTION_ENABLED, BUZZER_ENABLED, EMAIL_ALERTS_ENABLED, EMERGENCY_SHUTDOWN_ENABLED
    global DASHBOARD_REFRESH_RATE, AUTO_LOGOUT_MINUTES, THEME_MODE, DEVICES, ALERT_EMAIL_TO, ALERT_EMAIL_TO, ALERT_EMAIL_TO
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
    global energy_saved_seconds, energy_saved_kwh, event_logs, last_event_state
    global weekly_energy_data, monthly_energy_data, weekly_period_key, monthly_period_key
    try:
        if os.path.exists(RUNTIME_DATA_FILE):
            with open(RUNTIME_DATA_FILE, "r") as f:
                data = json.load(f)

            energy_saved_seconds = float(data.get("energy_saved_seconds", energy_saved_seconds))
            energy_saved_kwh = float(data.get("energy_saved_kwh", energy_saved_kwh))

            loaded_weekly = data.get("weekly_energy_data")
            if isinstance(loaded_weekly, dict):
                weekly_energy_data.update(loaded_weekly)

            loaded_monthly = data.get("monthly_energy_data")
            if isinstance(loaded_monthly, dict):
                monthly_energy_data.update(loaded_monthly)

            weekly_period_key = data.get("weekly_period_key", weekly_period_key)
            monthly_period_key = data.get("monthly_period_key", monthly_period_key)

            saved_logs = data.get("event_logs", [])
            if isinstance(saved_logs, list):
                event_logs.clear()
                event_logs.extend(saved_logs[-300:])

            saved_state = data.get("last_event_state", {})
            if isinstance(saved_state, dict):
                last_event_state.update(saved_state)

            saved_history = data.get("activity_history", [])
            if isinstance(saved_history, list):
                activity_history.clear()
                activity_history.extend(saved_history[-50:])

            print("Runtime data loaded successfully")
    except Exception as e:
        print("Runtime load error:", e)

    check_period_rollover()

def current_weekly_period_key():
    iso_year, iso_week, _ = date.today().isocalendar()
    return f"{iso_year}-W{iso_week:02d}"

def current_monthly_period_key():
    today = date.today()
    return f"{today.year}-{today.month:02d}"

def current_month_week_bucket():
    day = date.today().day
    return MONTH_WEEK_KEYS[min((day - 1) // 7, 4)]

def check_period_rollover():
    """Reset ONLY the weekly/monthly graphs when the real calendar week/month
    changes. Never touches lifetime totals (energy_saved_seconds/kwh) or logs.
    Safe to call every tick and at startup - a rollover only happens when the
    stored period key was already set and genuinely differs from today's."""
    global weekly_period_key, monthly_period_key
    week_key = current_weekly_period_key()
    month_key = current_monthly_period_key()

    if weekly_period_key is not None and weekly_period_key != week_key:
        for k in WEEKDAY_KEYS:
            weekly_energy_data[k] = 0
        weekly_period_key = week_key
        add_event("New calendar week started - weekly energy graph reset", "INFO")
    else:
        weekly_period_key = week_key

    if monthly_period_key is not None and monthly_period_key != month_key:
        for k in MONTH_WEEK_KEYS:
            monthly_energy_data[k] = 0
        monthly_period_key = month_key
        add_event("New calendar month started - monthly energy graph reset", "INFO")
    else:
        monthly_period_key = month_key

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

load_settings()
load_runtime_data()

def background_detection():
    global latest_human, latest_worker_activity
    while True:
        loop_start = time.time()
        if HUMAN_DETECTION_ENABLED or WORKER_DETECTION_ENABLED:
            try:
                latest_human, latest_worker_activity = detect_human()
            except Exception:
                latest_human, latest_worker_activity = "No Human", "No Worker"
        time.sleep(max(0, 1 - (time.time() - loop_start)))

def system_controller():
    global system_armed, device_on, last_human_time, latest_data
    global last_email_alert, energy_saved_seconds, last_energy_check, energy_saved_kwh, auto_energy_mode, last_runtime_save
    in_gas_grace_period = time.time() < gas_grace_until
    gas_status = check_gas(suppress=in_gas_grace_period) if GAS_DETECTION_ENABLED else "Gas Disabled"
    human_status = latest_human if HUMAN_DETECTION_ENABLED else "Detection Disabled"
    worker_status = latest_worker_activity if WORKER_DETECTION_ENABLED else "Worker Detection Disabled"

    if gas_status == "Gas Detected" and EMERGENCY_SHUTDOWN_ENABLED:
        relay.off()
        system_armed = False
        device_on = False
        last_email_alert = get_last_email_time()
        add_event_if_changed("gas", gas_status, "Gas Detected", "DANGER")
        add_event("Emergency Shutdown Activated", "DANGER")
        latest_data = {"gas":gas_status,"human":human_status,"worker_activity":worker_status,"energy":"Emergency","device":"OFF (Gas)","energy_saved":round(energy_saved_seconds/60,1),"energy_saved_kwh":energy_saved_kwh,"monitoring":"Disabled","last_email":last_email_alert,"device_name":DEVICE_NAME,"device_power":DEVICE_POWER_WATTS,"settings":current_settings(), "devices": DEVICES}
        return

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
        add_event(AUTO_SHUTDOWN_EVENT_TEXT, "WARNING")

    now = time.time()
    check_period_rollover()
    if auto_energy_mode:
        elapsed = now - last_energy_check
        energy_saved_seconds += elapsed
        today = WEEKDAY_KEYS[time.localtime().tm_wday]
        weekly_energy_data[today] += elapsed / 3600
        monthly_energy_data[current_month_week_bucket()] += elapsed / 3600
    last_energy_check = now
    energy_saved_kwh = round((DEVICE_POWER_WATTS * (energy_saved_seconds / 3600)) / 1000, 3)
    # save runtime every 10 seconds so service restart does not lose energy/logs
    if time.time() - last_runtime_save >= 10:
        save_runtime_data()
        last_runtime_save = time.time()

    device_status = "ON" if device_on else "OFF"

    for d in DEVICES:
        if int(d.get("relay", 0)) == 1 and d.get("enabled", True):
            d["name"] = DEVICE_NAME
            d["power"] = DEVICE_POWER_WATTS
            d["available"] = True
            d["enabled"] = True
            d["status"] = device_status

    last_email_alert = get_last_email_time()
    add_event_if_changed("human", human_status, f"Human Status Changed: {human_status}", "INFO")
    add_event_if_changed("worker", worker_status, f"Worker Activity Changed: {worker_status}", "INFO")
    add_event_if_changed("gas", gas_status, f"Gas Status Changed: {gas_status}", "DANGER" if gas_status == "Gas Detected" else "INFO")
    add_event_if_changed("device", device_status, f"Device Status Changed: {device_status}", "SUCCESS" if device_status == "ON" else "WARNING")

    latest_data = {
        "gas": gas_status,
        "human": human_status,
        "worker_activity": worker_status,
        "energy": update_energy(device_on),
        "device": device_status,
        "energy_saved": round(energy_saved_seconds / 60, 1),
        "energy_saved_kwh": energy_saved_kwh,
        "monitoring": "Active" if system_armed else "Disabled",
        "last_email": last_email_alert,
        "device_name": DEVICE_NAME,
        "device_power": DEVICE_POWER_WATTS,
        "settings": current_settings(),
        "devices": DEVICES
    }
    activity_history.append({"time": time.strftime("%H:%M:%S"), "human": 1 if human_status == "Human Detected" else 0, "worker": 1 if worker_status == "Worker Working" else 0})
    if len(activity_history) > 50:
        activity_history.pop(0)

def system_loop():
    while True:
        loop_start = time.time()
        try:
            system_controller()
        except Exception as e:
            print("system_controller error:", e)
        time.sleep(max(0, 1 - (time.time() - loop_start)))

@app.route("/manual_on")
def manual_on():
    global system_armed, device_on, last_human_time, auto_energy_mode
    system_armed = True
    device_on = True
    auto_energy_mode = False
    last_human_time = time.time()
    relay.on()
    add_event("Device ON (Manual)", "SUCCESS")
    return "ON"

@app.route("/manual_off")
def manual_off():
    global system_armed, device_on, auto_energy_mode
    system_armed = False
    device_on = False
    auto_energy_mode = False
    relay.off()
    add_event("Device OFF (Manual)", "WARNING")
    return "OFF"

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

@app.route("/system_status")
def system_status():
    return jsonify(latest_data)

@app.route("/activity_chart")
def activity_chart():
    return jsonify(activity_history[-12:])



@app.route("/weekly_energy")
def weekly_energy():
    return jsonify({
        "days": [
            {"day":"Mon","hours":round(weekly_energy_data["mon"], 2)},
            {"day":"Tue","hours":round(weekly_energy_data["tue"], 2)},
            {"day":"Wed","hours":round(weekly_energy_data["wed"], 2)},
            {"day":"Thu","hours":round(weekly_energy_data["thu"], 2)},
            {"day":"Fri","hours":round(weekly_energy_data["fri"], 2)},
            {"day":"Sat","hours":round(weekly_energy_data["sat"], 2)},
            {"day":"Sun","hours":round(weekly_energy_data["sun"], 2)}
        ],
        "total_kwh": energy_saved_kwh,
        "total_hours": round(energy_saved_seconds / 3600, 2),
        "y_axis": "Hours Saved"
    })

@app.route("/camera_status")
def camera_status():
    if not CAMERA_ENABLED:
        return jsonify({"status":"Disabled"})
    try:
        return jsonify({"status":"Online" if get_frame() is not None else "Offline"})
    except Exception:
        return jsonify({"status":"Offline"})

@app.route("/reset_gas")
def reset_gas():
    global gas_grace_until
    clear_gas_alert()
    gas_grace_until = time.time() + 30
    add_event("Gas Alert Reset - 30 Second Grace Period Started", "WARNING")
    return "Gas Alert Reset For 30 Seconds"

@app.route("/pi_health")
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
        uptime = f"{int(uptime_seconds//3600)}h {int((uptime_seconds%3600)//60)}m"
        return jsonify({"cpu":cpu,"ram":ram,"disk":disk,"temp":temp,"uptime":uptime})
    except Exception as e:
        return jsonify({"cpu":"N/A","ram":"N/A","disk":"N/A","temp":"N/A","uptime":"N/A","error":str(e)})

@app.route("/get_settings")
def get_settings():
    return jsonify(current_settings())


@app.route("/save_settings", methods=["POST"])
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
            return jsonify({"status":"error", "message":"Invalid email address"}), 400

        SYSTEM_MODE = str(s.get("system_mode", SYSTEM_MODE))
        AUTO_OFF_DELAY = int(s.get("auto_off_delay", AUTO_OFF_DELAY))
        DEVICE_NAME = str(s.get("device_name", DEVICE_NAME))
        DEVICE_POWER_WATTS = int(s.get("device_power_watts", DEVICE_POWER_WATTS))
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

        ALERT_EMAIL_TO = incoming_email
        set_alert_email(ALERT_EMAIL_TO)

        loaded_devices = s.get("devices")
        if isinstance(loaded_devices, list) and len(loaded_devices) > 0:
            DEVICES = loaded_devices

        add_event("Settings Updated", "INFO")
        save_settings_file()
        save_runtime_data()

        return jsonify({
            "status":"saved",
            "settings": current_settings(),
            "devices": DEVICES,
            "alert_email_to": ALERT_EMAIL_TO
        })

    except Exception as e:
        return jsonify({"status":"error", "message":str(e)}), 500


@app.route("/test_email")
def test_email():
    global last_email_alert
    try:
        if not valid_email(ALERT_EMAIL_TO):
            return jsonify({"status":"Email failed","error":"Invalid recipient email address"}), 400
        send_test_email(ALERT_EMAIL_TO)
        last_email_alert = get_last_email_time()
        add_event("Test Email Sent", "SUCCESS")
        return jsonify({"status": "Test email sent successfully", "last_email": last_email_alert})
    except Exception as e:
        add_event("Test Email Failed", "DANGER")
        return jsonify({"status": "Email failed", "error": str(e)}), 500

@app.route("/devices")
def get_devices():
    return jsonify(DEVICES)

@app.route("/add_device", methods=["POST"])
def add_device():
    global DEVICES, DEVICE_NAME, DEVICE_POWER_WATTS

    data = request.json or {}
    relay_no = int(data.get("relay", 1))
    name = str(data.get("name", "Device"))
    power = int(data.get("power", 100))

    if relay_no < 1 or relay_no > 4:
        return jsonify({"status": "Invalid relay number"}), 400

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
            "relay": relay_no,
            "name": name,
            "power": power,
            "enabled": True,
            "available": True,
            "status": "OFF"
        })

    DEVICES = sorted(DEVICES, key=lambda x: int(x.get("relay", 0)))

    # Relay 1 is the current real relay; sync it with energy calculation.
    if relay_no == 1:
        DEVICE_NAME = name
        DEVICE_POWER_WATTS = power

    save_settings_file()
    save_runtime_data()
    return jsonify({"status": "device saved", "devices": DEVICES})

@app.route("/remove_device/<int:relay_no>")
def remove_device(relay_no):
    global DEVICES

    for d in DEVICES:
        if int(d.get("relay", 0)) == relay_no:
            d["enabled"] = False
            d["available"] = False
            d["status"] = "Not Installed"

    save_settings_file()
    save_runtime_data()
    return jsonify({"status": "device removed", "devices": DEVICES})

@app.route("/test_relay/<int:relay_no>")
def test_relay_number(relay_no):
    if relay_no != 1:
        return jsonify({"status": f"Relay {relay_no} hardware not connected yet"})
    return test_relay()


@app.route("/apply_mode/<mode>")
def apply_mode(mode):
    if mode not in ["Home", "Office", "Factory", "Custom"]:
        return jsonify({"status":"error","message":"Invalid mode"}), 400
    if mode != "Custom":
        apply_system_mode(mode)
    save_settings_file()
    return jsonify({"status":"applied","settings":current_settings(), "devices": DEVICES})

@app.route("/toggle_camera")
def toggle_camera():
    global CAMERA_ENABLED
    CAMERA_ENABLED = not CAMERA_ENABLED
    save_settings_file()
    return jsonify({"camera":"ON" if CAMERA_ENABLED else "OFF","settings":current_settings(), "devices": DEVICES})

@app.route("/reset_energy")
def reset_energy():
    global energy_saved_seconds, energy_saved_kwh
    energy_saved_seconds = 0
    energy_saved_kwh = 0
    add_event("Energy Statistics Reset", "WARNING")
    save_runtime_data()
    return jsonify({"status":"Energy statistics reset"})

@app.route("/clear_activity")
def clear_activity():
    activity_history.clear()
    event_logs.clear()
    save_runtime_data()
    return jsonify({"status":"Activity and event logs cleared"})

@app.route("/test_relay")
def test_relay():
    try:
        relay.on(); time.sleep(1); relay.off()
        return jsonify({"status":"Relay test completed"})
    except Exception as e:
        return jsonify({"status":"Relay test failed","error":str(e)})

@app.route("/test_camera")
def test_camera():
    try:
        return jsonify({"status":"Camera test successful" if get_frame() is not None else "Camera test failed"})
    except Exception as e:
        return jsonify({"status":"Camera test failed","error":str(e)})

@app.route("/test_gas")
def test_gas():
    try:
        return jsonify({"status":"Gas sensor test completed","gas":check_gas()})
    except Exception as e:
        return jsonify({"status":"Gas sensor test failed","error":str(e)})

@app.route("/test_buzzer")
def test_buzzer():
    return jsonify({"status":"Buzzer test endpoint ready"})

@app.route("/hardware_status")
def hardware_status():
    return jsonify({"camera":"Enabled" if CAMERA_ENABLED else "Disabled","relay":"Configured","gas_sensor":"Enabled" if GAS_DETECTION_ENABLED else "Disabled","buzzer":"Enabled" if BUZZER_ENABLED else "Disabled","future_expansion":["Temperature Sensor","Humidity Sensor","Fire Sensor","Motion Sensor"]})




@app.route("/runtime_data")
def runtime_data():
    return jsonify({
        "energy_saved_seconds": energy_saved_seconds,
        "energy_saved_kwh": energy_saved_kwh,
        "event_logs_count": len(event_logs),
        "runtime_file": RUNTIME_DATA_FILE
    })

@app.route("/export_csv")
def export_csv():

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Date Time",
        "Level",
        "Event",
        "Human Status",
        "Worker Status",
        "Gas Status",
        "Device Status",
        "Energy Saved (kWh)",
        "System Mode",
        "Device Name",
        "Device Power (W)"
    ])

    for item in event_logs:

        writer.writerow([
            item.get("time", ""),
            item.get("level", ""),
            item.get("event", ""),
            latest_data.get("human", ""),
            latest_data.get("worker_activity", ""),
            latest_data.get("gas", ""),
            latest_data.get("device", ""),
            energy_saved_kwh,
            SYSTEM_MODE,
            DEVICE_NAME,
            DEVICE_POWER_WATTS
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
            "attachment; filename=smart_energy_report.csv"
        }
    )
def run_system_action(command, log_message, success_message):
    """Common, reliable pattern for OS-level system actions (service restart,
    Pi reboot, Pi shutdown): logs the request, launches the command via
    subprocess.Popen (no shell parsing, returns immediately without waiting
    for it to finish), and always returns a consistent JSON response.
    Requires sudo permissions already confirmed present in the sudoers
    config for each of these exact commands. Every executable in the command
    list must be an absolute path - this service's PATH is restricted to the
    venv's bin directory, so PATH-based lookup (e.g. a bare "sudo") fails."""
    try:
        add_event(log_message, "WARNING")
        subprocess.Popen(command)
        return jsonify({"status": success_message})
    except Exception as e:
        add_event(f"{log_message} - FAILED: {e}", "DANGER")
        return jsonify({"status": "Action failed", "error": str(e)}), 500

@app.route("/restart_service")
def restart_service():
    return run_system_action(
        ["/usr/bin/sudo", "/bin/systemctl", "restart", "fyp.service"],
        "Service Restart Requested From Dashboard",
        "Restarting fyp.service..."
    )

@app.route("/restart_pi")
def restart_pi():
    return run_system_action(
        ["/usr/bin/sudo", "/sbin/reboot"],
        "Raspberry Pi Restart Requested From Dashboard",
        "Restarting Raspberry Pi..."
    )

@app.route("/shutdown_pi")
def shutdown_pi():
    return run_system_action(
        ["/usr/bin/sudo", "/sbin/shutdown", "now"],
        "Raspberry Pi Shutdown Requested From Dashboard",
        "Shutting Down..."
    )

AUTO_SHUTDOWN_EVENT_TEXT = "Automatic Shutdown - No Human Detected"

def count_auto_shutdowns_by_day():
    """Real automatic-shutdown events (never manual) that happened on each
    weekday of the CURRENT calendar week, derived from event_logs timestamps."""
    counts = {k: 0 for k in WEEKDAY_KEYS}
    today = date.today()
    iso_year, iso_week, _ = today.isocalendar()
    for e in event_logs:
        if AUTO_SHUTDOWN_EVENT_TEXT not in e.get("event", ""):
            continue
        try:
            dt = datetime.strptime(e["time"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        e_year, e_week, _ = dt.date().isocalendar()
        if (e_year, e_week) == (iso_year, iso_week):
            counts[WEEKDAY_KEYS[dt.weekday()]] += 1
    return counts

def count_auto_shutdowns_by_month_week():
    """Real automatic-shutdown events grouped by week-of-month for the
    CURRENT calendar month, derived from event_logs timestamps."""
    counts = {k: 0 for k in MONTH_WEEK_KEYS}
    today = date.today()
    for e in event_logs:
        if AUTO_SHUTDOWN_EVENT_TEXT not in e.get("event", ""):
            continue
        try:
            dt = datetime.strptime(e["time"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if dt.year == today.year and dt.month == today.month:
            bucket = MONTH_WEEK_KEYS[min((dt.day - 1) // 7, 4)]
            counts[bucket] += 1
    return counts

def count_auto_shutdowns_total():
    """Real automatic-shutdown event count currently held in event_logs."""
    return sum(1 for e in event_logs if AUTO_SHUTDOWN_EVENT_TEXT in e.get("event", ""))

class NumberedCanvas(pdfcanvas.Canvas):
    """Adds a consistent footer (system name, institution, generated date/time)
    and a 'Page X of Y' page number to every page of every report."""
    def __init__(self, *args, **kwargs):
        pdfcanvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_footer(total_pages)
            pdfcanvas.Canvas.showPage(self)
        pdfcanvas.Canvas.save(self)

    def _draw_footer(self, total_pages):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.grey)
        page_width = self._pagesize[0]
        self.drawString(30, 20, "Smart Energy Saving System | University of Sialkot")
        self.drawCentredString(page_width / 2, 20, f"Generated: {time.strftime('%d-%m-%Y %H:%M')}")
        self.drawRightString(page_width - 30, 20, f"Page {self._pageNumber} of {total_pages}")
        self.restoreState()

def create_weekly_pdf():

    pdf_file = "/tmp/weekly_report.pdf"

    doc = SimpleDocTemplate(pdf_file)
    styles = getSampleStyleSheet()

    story = []

    total_hours = round(sum(weekly_energy_data.values()), 2)
    total_kwh = round((DEVICE_POWER_WATTS * total_hours) / 1000, 3)
    auto_off = sum(count_auto_shutdowns_by_day().values())
    cost_saved = round(total_kwh * ELECTRICITY_RATE_PKR_PER_KWH)

    # =========================
    # Title
    # =========================

    story.append(
        Paragraph(
            "SMART ENERGY SAVING SYSTEM REPORT",
            styles['Title']
        )
    )

    story.append(Spacer(1, 10))

    story.append(
        Paragraph(
            f"""
            <b>Generated:</b> {time.strftime('%d-%m-%Y %H:%M')}<br/>
            <b>System Mode:</b> {SYSTEM_MODE}<br/>
            <b>Device:</b> {DEVICE_NAME} ({DEVICE_POWER_WATTS}W)
            """,
            styles['BodyText']
        )
    )

    story.append(Spacer(1, 15))

    # =========================
    # Executive Summary
    # =========================

    story.append(
        Paragraph(
            "<b>EXECUTIVE SUMMARY</b>",
            styles['Heading2']
        )
    )

    story.append(
        Paragraph(
            f"""
            Total Energy Saved: <b>{total_kwh} kWh</b><br/>
            Total Runtime Saved: <b>{total_hours} Hours</b><br/>
            Estimated Cost Saved: <b>PKR {cost_saved}</b><br/>
            Automatic Shutdown Events: <b>{auto_off}</b>
            """,
            styles['BodyText']
        )
    )

    story.append(Spacer(1, 15))

    # =========================
    # Line Graph
    # =========================

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    energy = [
        weekly_energy_data["mon"],
        weekly_energy_data["tue"],
        weekly_energy_data["wed"],
        weekly_energy_data["thu"],
        weekly_energy_data["fri"],
        weekly_energy_data["sat"],
        weekly_energy_data["sun"]
    ]

    plt.figure(figsize=(7, 3))
    plt.plot(days, energy, marker="o", linewidth=3)
    plt.grid(True)
    plt.xlabel("Days")
    plt.ylabel("Hours Saved")
    plt.title("Weekly Energy Saving Trend")
    plt.tight_layout()

    graph1 = "/tmp/energy_graph.png"
    plt.savefig(graph1)
    plt.close()

    story.append(Image(graph1, width=400, height=170))

    story.append(Spacer(1, 15))

    # =========================
    # Daily Performance Table
    # =========================

    table_data = [
        ["Day", "Hours Saved"],
        ["Mon", str(round(weekly_energy_data["mon"], 2))],
        ["Tue", str(round(weekly_energy_data["tue"], 2))],
        ["Wed", str(round(weekly_energy_data["wed"], 2))],
        ["Thu", str(round(weekly_energy_data["thu"], 2))],
        ["Fri", str(round(weekly_energy_data["fri"], 2))],
        ["Sat", str(round(weekly_energy_data["sat"], 2))],
        ["Sun", str(round(weekly_energy_data["sun"], 2))]
    ]

    table = Table(table_data, colWidths=[150, 150])

    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1F4E78')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('ALIGN', (0,0), (-1,-1), 'CENTER')
    ]))

    story.append(table)

    story.append(Spacer(1, 15))

    # =========================
    # Auto Shutdown Graph
    # =========================

    shutdown_counts = count_auto_shutdowns_by_day()
    shutdowns = [shutdown_counts[k] for k in WEEKDAY_KEYS]

    plt.figure(figsize=(7, 3))
    plt.bar(days, shutdowns)
    plt.title("Automatic Shutdown Analysis")
    plt.xlabel("Days")
    plt.ylabel("Events")
    plt.tight_layout()

    graph2 = "/tmp/shutdown_graph.png"
    plt.savefig(graph2)
    plt.close()

    story.append(Image(graph2, width=400, height=170))

    story.append(Spacer(1, 15))

    # =========================
    # Cost Analysis
    # =========================

    story.append(
        Paragraph(
            "<b>COST SAVING ANALYSIS</b>",
            styles['Heading2']
        )
    )

    story.append(
        Paragraph(
            f"""
            Average Daily Saving: PKR {round(cost_saved/7,2)}<br/>
            Weekly Saving: PKR {cost_saved}<br/>
            Monthly Projection: PKR {cost_saved*4}<br/>
            Yearly Projection: PKR {cost_saved*48}<br/>
            <br/>
            <i>Electricity Rate Assumption: {ELECTRICITY_RATE_PKR_PER_KWH} PKR/kWh</i>
            """,
            styles['BodyText']
        )
    )

    story.append(Spacer(1, 15))

    # =========================
    # AI Summary
    # =========================

    story.append(
        Paragraph(
            "<b>AI MONITORING SUMMARY</b>",
            styles['Heading2']
        )
    )

    story.append(
        Paragraph(
            f"""
            Human Status: {latest_data.get('human')}<br/>
            Worker Status: {latest_data.get('worker_activity')}<br/>
            Gas Status: {latest_data.get('gas')}
            """,
            styles['BodyText']
        )
    )

    story.append(Spacer(1, 15))

    # =========================
    # Conclusion
    # =========================

    story.append(
        Paragraph(
            "<b>PROJECT SUMMARY</b>",
            styles['Heading2']
        )
    )

    story.append(
        Paragraph(
            f"""
            The AI-based Smart Energy Saving System
            automatically controlled electrical devices
            and reduced unnecessary power consumption.

            Total energy saved during monitoring period:
            <b>{total_kwh} kWh</b>.

            Estimated financial saving:
            <b>PKR {cost_saved}</b>.
            """,
            styles['BodyText']
        )
    )

    doc.build(story, canvasmaker=NumberedCanvas)

    with open(pdf_file, "rb") as f:
        pdf = f.read()

    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            "Content-Disposition":
            "attachment; filename=weekly_energy_report.pdf"
        }
    )

def recent_events_text(n=12):
    """Format the most recent n real event log entries as plain text lines."""
    recent = event_logs[-n:]
    if not recent:
        return "No recent events."
    return "\n".join(
        f"{e.get('time','')} [{e.get('level','')}] {e.get('event','')}"
        for e in recent
    )

def make_pdf_response(text, filename):
    """Render plain text lines into a simple PDF report and return it as a Flask response."""
    pdf_file = "/tmp/simple_report.pdf"
    doc = SimpleDocTemplate(pdf_file)
    styles = getSampleStyleSheet()
    story = []
    for line in text.split("\n"):
        if line.strip():
            story.append(Paragraph(line, styles["BodyText"]))
        else:
            story.append(Spacer(1, 8))
    doc.build(story, canvasmaker=NumberedCanvas)
    with open(pdf_file, "rb") as f:
        pdf = f.read()
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.route("/export_pdf")
def export_pdf():
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
{recent_events_text(12)}
"""
    return make_pdf_response(txt, "system_report.pdf")

@app.route("/weekly_pdf_report")
def weekly_pdf_report():
    return create_weekly_pdf()

@app.route("/monthly_pdf_report")
def monthly_pdf_report():

    pdf_file = "/tmp/monthly_report.pdf"

    doc = SimpleDocTemplate(pdf_file)
    styles = getSampleStyleSheet()

    story = []

    total_hours = round(energy_saved_seconds / 3600, 2)
    total_kwh = round(energy_saved_kwh, 2)

    monthly_hours = round(sum(monthly_energy_data.values()), 2)
    monthly_kwh = round((DEVICE_POWER_WATTS * monthly_hours) / 1000, 3)

    monthly_cost = round(monthly_kwh * ELECTRICITY_RATE_PKR_PER_KWH)

    human_count = sum(
        1 for e in event_logs
        if "Human Status Changed: Human Detected" in e["event"]
    )

    no_human_count = sum(
        1 for e in event_logs
        if "Human Status Changed: No Human" in e["event"]
    )

    gas_alerts = sum(
        1 for e in event_logs
        if "Gas" in e["event"]
    )

    monthly_shutdown_counts = count_auto_shutdowns_by_month_week()
    auto_shutdowns = sum(monthly_shutdown_counts.values())

    story.append(
        Paragraph(
            "SMART ENERGY SAVING SYSTEM - MONTHLY REPORT",
            styles['Title']
        )
    )

    story.append(Spacer(1,10))

    story.append(
        Paragraph(
            f"""
            <b>Generated:</b> {time.strftime('%d-%m-%Y %H:%M')}<br/>
            <b>System Mode:</b> {SYSTEM_MODE}<br/>
            <b>Device:</b> {DEVICE_NAME} ({DEVICE_POWER_WATTS}W)
            """,
            styles['BodyText']
        )
    )

    story.append(Spacer(1,15))

    story.append(
        Paragraph(
            "EXECUTIVE SUMMARY",
            styles['Heading2']
        )
    )

    story.append(
        Paragraph(
            f"""
            Total Energy Saved: <b>{monthly_kwh} kWh</b><br/>
            Total Runtime Saved: <b>{monthly_hours} Hours</b><br/>
            Estimated Cost Saved: <b>PKR {monthly_cost}</b><br/>
            Automatic Shutdown Events: <b>{auto_shutdowns}</b>
            """,
            styles['BodyText']
        )
    )

    story.append(Spacer(1,15))

    energy_weeks = ["Week 1","Week 2","Week 3","Week 4","Week 5"]

    monthly_energy = [
        round((DEVICE_POWER_WATTS * monthly_energy_data[k]) / 1000, 3)
        for k in MONTH_WEEK_KEYS
    ]

    plt.figure(figsize=(7,3))
    plt.plot(energy_weeks, monthly_energy, marker="o", linewidth=3)
    plt.grid(True)
    plt.title("Monthly Energy Saving Trend")
    plt.xlabel("Weeks")
    plt.ylabel("kWh Saved")
    plt.tight_layout()

    graph1 = "/tmp/monthly_energy_graph.png"
    plt.savefig(graph1)
    plt.close()

    story.append(Image(graph1, width=400, height=170))
    story.append(Spacer(1,15))

    table_data = [
        ["Week","Energy Saved (kWh)"],
        ["Week 1", monthly_energy[0]],
        ["Week 2", monthly_energy[1]],
        ["Week 3", monthly_energy[2]],
        ["Week 4", monthly_energy[3]],
        ["Week 5", monthly_energy[4]]
    ]

    table = Table(table_data, colWidths=[180,180])

    table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1F4E78')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('GRID',(0,0),(-1,-1),1,colors.black),
        ('ALIGN',(0,0),(-1,-1),'CENTER')
    ]))

    story.append(table)

    story.append(Spacer(1,15))

    weeks = ["Week 1","Week 2","Week 3","Week 4","Week 5"]

    shutdowns = [monthly_shutdown_counts[k] for k in MONTH_WEEK_KEYS]

    plt.figure(figsize=(7,3))
    plt.bar(weeks, shutdowns)
    plt.title("Monthly Auto Shutdown Analysis")
    plt.xlabel("Weeks")
    plt.ylabel("Events")
    plt.tight_layout()

    graph2 = "/tmp/monthly_shutdown_graph.png"
    plt.savefig(graph2)
    plt.close()

    story.append(Image(graph2, width=400, height=170))

    story.append(Spacer(1,15))

    story.append(
        Paragraph(
            "COST SAVING ANALYSIS",
            styles['Heading2']
        )
    )

    story.append(
        Paragraph(
            f"""
            Average Daily Saving: PKR {round(monthly_cost/30,2)}<br/>
            Monthly Saving: PKR {monthly_cost}<br/>
            Yearly Projection: PKR {monthly_cost*12}<br/>
            <br/>
            <i>Electricity Rate Assumption: {ELECTRICITY_RATE_PKR_PER_KWH} PKR/kWh</i>
            """,
            styles['BodyText']
        )
    )

    story.append(Spacer(1,15))

    story.append(
        Paragraph(
            "AI MONITORING SUMMARY",
            styles['Heading2']
        )
    )

    story.append(
        Paragraph(
            f"""
            Human Detections : {human_count}<br/>
            No Human Events : {no_human_count}<br/>
            Auto Shutdowns : {auto_shutdowns}<br/>
            Gas Alerts : {gas_alerts}
            """,
            styles['BodyText']
        )
    )

    story.append(Spacer(1,15))

    story.append(
        Paragraph(
            "PROJECT SUMMARY",
            styles['Heading2']
        )
    )

    story.append(
        Paragraph(
            f"""
            The Smart Energy Saving System automatically
            reduced unnecessary power consumption using
            AI-based human detection and relay control.

            <br/><br/>
            Total Energy Saved: <b>{monthly_kwh} kWh</b>

            <br/><br/>
            Runtime Saved: <b>{monthly_hours} Hours</b>

            <br/><br/>
            Estimated Cost Saving: <b>PKR {monthly_cost}</b>
            """,
            styles['BodyText']
        )
    )

    doc.build(story, canvasmaker=NumberedCanvas)

    with open(pdf_file, "rb") as f:
        pdf = f.read()

    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            "Content-Disposition":
            "attachment; filename=monthly_energy_report.pdf"
        }
    )
@app.route("/full_pdf_report")
def full_pdf_report():

    pdf_file = "/tmp/full_report.pdf"

    doc = SimpleDocTemplate(pdf_file)
    styles = getSampleStyleSheet()

    story = []

    total_hours = round(energy_saved_seconds / 3600, 2)
    total_kwh = round(energy_saved_kwh, 2)
    cost_saved = round(total_kwh * ELECTRICITY_RATE_PKR_PER_KWH)

    human_count = sum(
        1 for e in event_logs
        if "Human Status Changed: Human Detected" in e["event"]
    )

    no_human_count = sum(
        1 for e in event_logs
        if "Human Status Changed: No Human" in e["event"]
    )

    gas_alerts = sum(
        1 for e in event_logs
        if "Gas" in e["event"]
    )

    auto_shutdowns = count_auto_shutdowns_total()

    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent

    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = round(int(f.read()) / 1000, 1)
    except:
        temp = "N/A"
    
    # ==================================
    # PAGE 1
    # ==================================

    story.append(
        Paragraph(
            "SMART ENERGY SAVING SYSTEM PROFESSIONAL REPORT",
            styles["Title"]
        )
    )

    story.append(Spacer(1,20))


    story.append(
        Paragraph(
            f"""
            <b>Generated:</b> {time.strftime('%d-%m-%Y %H:%M')}<br/>
            <b>System Mode:</b> {SYSTEM_MODE}<br/>
            <b>Device:</b> {DEVICE_NAME}<br/>
            <b>Power Rating:</b> {DEVICE_POWER_WATTS}W
            """,
            styles["BodyText"]
        )
    )
    # ==========================
    # WEEKLY ENERGY GRAPH
    # ==========================

    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

    weekly_values = [
        weekly_energy_data["mon"],
        weekly_energy_data["tue"],
        weekly_energy_data["wed"],
        weekly_energy_data["thu"],
        weekly_energy_data["fri"],
        weekly_energy_data["sat"],
        weekly_energy_data["sun"]
    ]

    plt.figure(figsize=(6,2.5))
    plt.plot(days, weekly_values, marker="o")
    plt.title("Weekly Energy Saving")
    plt.xlabel("Days")
    plt.ylabel("Hours Saved")
    plt.tight_layout()

    weekly_graph = "/tmp/weekly_graph.png"
    plt.savefig(weekly_graph)
    plt.close()

    story.append(Spacer(1,10))
    story.append(Image(weekly_graph, width=380, height=150))

    # ==========================
    # MONTHLY GRAPH
    # ==========================

    weeks = ["W1","W2","W3","W4","W5"]

    monthly_values = [
        round((DEVICE_POWER_WATTS * monthly_energy_data[k]) / 1000, 3)
        for k in MONTH_WEEK_KEYS
    ]

    plt.figure(figsize=(6,2.5))
    plt.plot(weeks, monthly_values, marker="o")
    plt.title("Monthly Energy Saving")
    plt.xlabel("Weeks")
    plt.ylabel("kWh Saved")
    plt.tight_layout()

    monthly_graph = "/tmp/monthly_graph.png"
    plt.savefig(monthly_graph)
    plt.close()

    story.append(Spacer(1,10))
    story.append(Image(monthly_graph, width=380, height=150))

    story.append(PageBreak())

    # ==================================
    # PAGE 2
    # ==================================

    story.append(
        Paragraph("EXECUTIVE SUMMARY", styles["Heading1"])
    )

    story.append(
        Paragraph(
            f"""
            Total Energy Saved: <b>{total_kwh} kWh</b><br/>
            Runtime Saved: <b>{total_hours} Hours</b><br/>
            Estimated Cost Saving: <b>PKR {cost_saved}</b><br/>
            Auto Shutdown Events: <b>{auto_shutdowns}</b><br/>
            <br/>
            <i>Electricity Rate Assumption: {ELECTRICITY_RATE_PKR_PER_KWH} PKR/kWh</i>
            """,
            styles["BodyText"]
        )
    )

    story.append(Spacer(1,20))

    table_data = [
        ["Metric","Value"],
        ["Human Detections", human_count],
        ["No Human Events", no_human_count],
        ["Auto Shutdowns", auto_shutdowns],
        ["Gas Alerts", gas_alerts],
        ["Energy Saved (kWh)", total_kwh],
        ["Cost Saving", f"PKR {cost_saved}"]
    ]

    table = Table(table_data, colWidths=[220,180])

    table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1F4E78')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('GRID',(0,0),(-1,-1),1,colors.black),
        ('ALIGN',(0,0),(-1,-1),'CENTER')
    ]))

    story.append(table)

    story.append(Spacer(1,20))

    device_table_data = [["Device","Power","Status"]]
    for d in DEVICES:
        device_table_data.append([
            d.get("name", ""),
            f"{d.get('power', 0)}W",
            d.get("status", "Not Installed")
        ])

    device_table = Table(device_table_data)

    device_table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.grey),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('GRID',(0,0),(-1,-1),1,colors.black)
    ]))

    story.append(
        Paragraph("DEVICE CONFIGURATION", styles["Heading2"])
    )

    story.append(device_table)

    story.append(PageBreak())

    # ==================================
    # PAGE 3
    # ==================================

    story.append(
        Paragraph(
            "SYSTEM PERFORMANCE OVERVIEW",
            styles["Heading1"]
        )
    )

    story.append(
        Paragraph(
            f"""
            Human Detection Events: {human_count}<br/>
            No Human Events: {no_human_count}<br/>
            Auto Shutdown Events: {auto_shutdowns}<br/>
            Gas Alert Events: {gas_alerts}<br/>
            Current Status: {latest_data.get('human')}<br/>
            Worker Status: {latest_data.get('worker_activity')}<br/>
            Gas Status: {latest_data.get('gas')}
            """,
            styles["BodyText"]
        )
    )

    story.append(Spacer(1,20))

    health_table = Table([
        ["Parameter","Value"],
        ["CPU Usage",f"{cpu}%"],
        ["RAM Usage",f"{ram}%"],
        ["Temperature",str(temp)]
    ])

    health_table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.darkblue),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('GRID',(0,0),(-1,-1),1,colors.black)
    ]))

    story.append(
        Paragraph("RASPBERRY PI HEALTH", styles["Heading2"])
    )

    story.append(health_table)

    story.append(PageBreak())

    # ==================================
    # PAGE 4
    # ==================================

    story.append(
        Paragraph("LAST 10 EVENT LOGS", styles["Heading1"])
    )

    log_cell_style = ParagraphStyle("LogCell", parent=styles["BodyText"], fontSize=8, leading=10)

    log_data = [["Time","Level","Event"]]

    for e in event_logs[-10:]:
        log_data.append([
            str(e.get("time","")),
            str(e.get("level","")),
            Paragraph(str(e.get("event","")), log_cell_style)
        ])

    log_table = Table(
        log_data,
        colWidths=[110,70,260]
    )

    log_table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1F4E78')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('GRID',(0,0),(-1,-1),1,colors.black),
        ('FONTSIZE',(0,0),(-1,-1),8)
    ]))

    story.append(log_table)

    story.append(Spacer(1,20))

    story.append(
        Paragraph(
            "PROJECT SUMMARY",
            styles["Heading1"]
        )
    )

    story.append(
        Paragraph(
            f"""
            The Smart Energy Saving System automatically
            monitored human presence and controlled
            electrical devices through relay automation.

            The system reduced unnecessary energy usage,
            improved safety monitoring and provided
            intelligent control using AI based detection.

            <br/><br/>
            Total Energy Saved: <b>{total_kwh} kWh</b>

            <br/><br/>
            Runtime Saved: <b>{total_hours} Hours</b>

            <br/><br/>
            Estimated Cost Saving: <b>PKR {cost_saved}</b>
            """,
            styles["BodyText"]
        )
    )

    doc.build(story, canvasmaker=NumberedCanvas)

    with open(pdf_file, "rb") as f:
        pdf = f.read()

    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            "Content-Disposition":
            "attachment; filename=professional_report.pdf"
        }
    )

def generate_frames():
    while True:
        if not CAMERA_ENABLED:
            time.sleep(0.5); continue
        frame = get_frame()
        if frame is None:
            time.sleep(0.1); continue
        ret, buffer = cv2.imencode(".jpg", frame)
        if not ret: continue
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"

@app.route("/video_feed")
def video_feed():
    if not CAMERA_ENABLED:
        return Response("Camera Disabled", status=503)
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

if __name__ == "__main__":
    threading.Thread(target=system_loop, daemon=True).start()
    threading.Thread(target=background_detection, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
