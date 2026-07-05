from ultralytics import YOLO
from modules.camera_manager import get_frame
import cv2
import time
import os
import traceback

MODEL_PATH = "yolov8n.pt"

try:
    model = YOLO(MODEL_PATH) if os.path.exists(MODEL_PATH) else None
    if model is None:
        print("YOLO model file not found, detection unavailable:", MODEL_PATH)
except Exception as e:
    print("YOLO model load error, detection unavailable:", e)
    model = None

previous_gray = None
idle_start_time = None
latest_display_frame = None

IDLE_SECONDS = 10
SLEEP_SECONDS = 20
MOVEMENT_THRESHOLD = 12


def draw_red_box(frame, box, label):
    x1, y1, x2, y2 = map(int, box.xyxy[0])

    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)

    cv2.putText(
        frame,
        label,
        (x1, y1 - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 0, 255),
        2
    )


def detect_human():
    global previous_gray, idle_start_time, latest_display_frame

    if model is None:
        return "Detection Unavailable", "Detection Unavailable"

    try:
        frame = get_frame()

        if frame is None:
            return "Camera Error", "Camera Error"

        display_frame = frame.copy()

        results = model(frame, imgsz=320, conf=0.25, verbose=False)

        person_detected = False
        phone_detected = False
        person_box = None
        phone_box = None

        for r in results:
            for box in r.boxes:
                cls = int(box.cls[0])
                name = model.names[cls]

                if name == "person":
                    person_detected = True
                    person_box = box

                if name == "cell phone":
                    phone_detected = True
                    phone_box = box

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (160, 120))

        movement = 0

        if previous_gray is not None:
            diff = cv2.absdiff(previous_gray, gray)
            movement = diff.mean()

        previous_gray = gray

        current_time = time.time()

        # No worker
        if not person_detected:
            idle_start_time = None
            latest_display_frame = display_frame
            return "No Human", "No Worker"

        # Mobile usage red box
        if person_detected and phone_detected:
            idle_start_time = None

            if person_box is not None:
                draw_red_box(display_frame, person_box, "MOBILE USAGE")

            if phone_box is not None:
                draw_red_box(display_frame, phone_box, "PHONE")

            latest_display_frame = display_frame
            return "Human Detected", "Mobile Usage"

        # Worker moving = working = no box
        if movement > MOVEMENT_THRESHOLD:
            idle_start_time = None
            latest_display_frame = display_frame
            return "Human Detected", "Worker Working"

        # Worker not moving
        if idle_start_time is None:
            idle_start_time = current_time

        idle_duration = current_time - idle_start_time

        if idle_duration >= SLEEP_SECONDS:
            if person_box is not None:
                draw_red_box(display_frame, person_box, "SLEEPING / INACTIVE")

            latest_display_frame = display_frame
            return "Human Detected", "Sleeping Worker"

        elif idle_duration >= IDLE_SECONDS:
            if person_box is not None:
                draw_red_box(display_frame, person_box, "IDLE WORKER")

            latest_display_frame = display_frame
            return "Human Detected", "Worker Idle"

        else:
            latest_display_frame = display_frame
            return "Human Detected", "Worker Working"

    except Exception as e:
        print("Detection Error:", e)
        traceback.print_exc()
        return "Error", "Error"


def get_display_frame():
    global latest_display_frame

    if latest_display_frame is not None:
        return latest_display_frame

    return get_frame()