import time
from modules.gas_module import relay

NO_HUMAN_TIMEOUT = 20

last_human_time = time.time()

def update_energy(human_detected):
    global last_human_time

    current = time.time()

    if human_detected:
        last_human_time = current
        return "Device ON"

    else:
        if current - last_human_time > NO_HUMAN_TIMEOUT:
            return "Energy Saving Mode"
        else:
            return "Waiting..."