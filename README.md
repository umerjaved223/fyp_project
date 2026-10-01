# Smart Energy Saving System — Raspberry Pi Backend

AI-powered IoT backend on Raspberry Pi 5 that detects human presence, monitors worker activity, detects gas/smoke, and controls electrical devices via relay.

🔗 **Frontend Dashboard:** [fyp_fronted](https://github.com/umerjaved223/fyp_fronted)

---

## Overview

This is the backend of the Smart Energy Saving System. Runs on Raspberry Pi 5 and handles:
- AI-based human detection using YOLOv8 + OpenCV
- Worker activity monitoring (Active / Idle / Phone usage)
- Gas and smoke detection via MQ-2 sensor
- Relay control for lights, fans, and machines
- Flask REST API for the frontend dashboard
- Email alerts with snapshot on gas detection

System automatically turns OFF devices when no human is detected for 20 seconds — reducing energy waste by an estimated 30-40%.

---

## Tech Stack

- **Language:** Python 3
- **AI/ML:** YOLOv8 (CPU-optimized), OpenCV
- **Backend:** Flask, REST APIs
- **Hardware:** Raspberry Pi 5, Pi Camera v2, MQ-2 sensor, 4-channel relay, buzzer
- **Libraries:** gpiozero, Picamera2, smtplib, numpy

---

## Hardware Wiring

| Component | GPIO Pin | Purpose |
|-----------|----------|---------|
| MQ-2 Digital Out | GPIO 22 | Gas/smoke detection |
| Relay IN | GPIO 27 | Device switching |
| Buzzer | GPIO 24 | Local alarm |
| Pi Camera | CSI | Video feed |

---

## Installation

```bash
git clone https://github.com/umerjaved223/fyp_project.git
cd fyp_project
python -m venv venv --system-site-packages
source venv/bin/activate  # On Windows: .\venv\Scripts\activate
pip install -r requirements.txt
wget https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n.pt
python app.py
```
