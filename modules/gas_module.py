from gpiozero import DigitalInputDevice, DigitalOutputDevice
from modules.camera_manager import get_frame
import cv2
import threading
import smtplib
from email.message import EmailMessage
import datetime
import socket
import time
import os

# ================= GPIO CONFIG (BCM MODE) =================
# MQ2 DO -> BCM 22  (BOARD 15)
# Relay  -> BCM 27  (BOARD 13)
# Buzzer -> BCM 24  (BOARD 18)

GAS_PIN = 22
RELAY_PIN = 27
BUZZER_PIN = 24

gas_sensor = DigitalInputDevice(GAS_PIN, pull_up=True)
relay = DigitalOutputDevice(RELAY_PIN, active_high=False)
buzzer = DigitalOutputDevice(BUZZER_PIN)

# ================= EMAIL CONFIG =================
EMAIL_ADDRESS = "umerg403@gmail.com"
EMAIL_PASSWORD = "iyhz wtll ytwi nukt"
TO_EMAIL = "umergujjarg391@gmail.com"
LAST_EMAIL_SENT = "No Alerts Sent"

def set_alert_email(email):
    global TO_EMAIL
    if email:
        TO_EMAIL = str(email).strip()

def get_alert_email():
    return TO_EMAIL

def get_last_email_time():
    return LAST_EMAIL_SENT

LOCATION_NAME = "Factory Floor - Section A"

# ================= STATE FLAGS =================
email_sent = False
alarm_active = False


# ================= BUZZER PROFESSIONAL ALARM =================
def buzzer_alarm():
    global alarm_active
    while alarm_active:

        # 3 Fast Beeps
        for _ in range(3):
            buzzer.on()
            time.sleep(0.2)
            buzzer.off()
            time.sleep(0.2)

        time.sleep(0.5)

        # 1 Long Beep
        buzzer.on()
        time.sleep(0.8)
        buzzer.off()

        time.sleep(1)


def start_alarm():
    global alarm_active
    if not alarm_active:
        alarm_active = True
        threading.Thread(target=buzzer_alarm, daemon=True).start()


def stop_alarm():
    global alarm_active
    alarm_active = False
    buzzer.off()


# ================= EMAIL FUNCTION =================
def send_email(path=None, to_email=None, test_mode=False):
    global LAST_EMAIL_SENT, TO_EMAIL
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        hostname = socket.gethostname()
        ip_address = socket.gethostbyname(hostname)

        msg = EmailMessage()
        msg["Subject"] = "Test Email - Smart Energy System" if test_mode else "🚨 GAS LEAK ALERT - Immediate Action Required"
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = to_email or TO_EMAIL

        msg.set_content(f"""
SMART ENERGY, SAFETY & EFFICIENCY MONITORING SYSTEM

⚠ ALERT TYPE: Gas Leakage Detected
📍 Location: {LOCATION_NAME}
🕒 Time: {now}
🌐 Device IP: {ip_address}

Status: GAS DETECTED
Relay Status: ON
Buzzer Status: ACTIVE

Please inspect the area immediately.

-- Automated AI Monitoring System
""")

        # Attach image only if available
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                msg.add_attachment(
                    f.read(),
                    maintype="image",
                    subtype="jpeg",
                    filename="gas_alert.jpg"
                )

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.send_message(msg)

        LAST_EMAIL_SENT = now
        print("Professional Alert Email Sent")

    except Exception as e:
        print("Email error:", e)


def send_email_thread(path, to_email=None):
    threading.Thread(
        target=send_email,
        args=(path, to_email, False),
        daemon=True
    ).start()

def send_test_email(to_email=None):
    send_email(path=None, to_email=to_email, test_mode=True)
    return get_last_email_time()


# ================= MAIN FUNCTION (Flask calls this) =================
def check_gas(suppress=False):
    """Reads the real sensor state. When suppress=True (active grace period
    after a Reset), all side effects (buzzer, email) are skipped and the
    status is reported as Safe, regardless of the real sensor reading, so a
    still-present leak is treated as a fresh detection once suppression ends."""
    global email_sent

    if gas_sensor.value:   # GAS DETECTED
        if suppress:
            stop_alarm()
            email_sent = False
            return "Safe"

        start_alarm()

        if not email_sent:

            frame = get_frame()

            if frame is not None:
                image_path = "/home/ruf/fyp_project/static/gas.jpg"

                # Make sure directory exists
                os.makedirs(os.path.dirname(image_path), exist_ok=True)

                cv2.imwrite(image_path, frame)

                send_email_thread(image_path)

            email_sent = True

        return "Gas Detected"

    else:
        stop_alarm()
        email_sent = False
        return "Safe"

def clear_gas_alert():
    """Called immediately when Reset Gas is pressed: silences the buzzer now
    and clears the email latch so a still-present leak can trigger a fresh
    alert once the grace period ends (no already-sent email can be recalled,
    but no new one will be queued until suppression is lifted)."""
    global email_sent
    stop_alarm()
    email_sent = False