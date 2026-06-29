from picamera2 import Picamera2
from libcamera import Transform
import cv2

picam2 = Picamera2()

config = picam2.create_preview_configuration(
    main={"size": (800, 600)},   # clear + fast
    transform=Transform(hflip=False, vflip=False)
)

picam2.configure(config)
picam2.start()

def get_frame():
    frame = picam2.capture_array()
    frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    return frame