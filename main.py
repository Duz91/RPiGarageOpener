from flask import Flask, render_template, jsonify
import threading
import time
import subprocess
import logging
from contextlib import suppress

try:
    from gpiozero import Button, LED, OutputDevice
except ImportError:
    # Fallbacks für Entwicklungsumgebungen ohne GPIO
    class _Dummy:
        def __init__(self, *_, **__):
            self.state = False

        def on(self):
            self.state = True

        def off(self):
            self.state = False

        def blink(self, *_, **__):
            pass

        def close(self):
            pass

    Button = LED = OutputDevice = _Dummy

BUTTONPIN = 5
LEDPIN = 23
RELAYPIN = 26
BUZZERPIN = 19

macaddresses = [
    "0C:15:63:DF:61:2F",
    "80:04:5F:A2:66:57"
]

scaninterval = 7
absenceinterval = 15
relayclosetime = 0.5
presencebeepduration = 0.1
presencebeepcount = 2
absencebeepduration = 0.1
absencebeepcount = 2
buttonbouncetime = 0.2
presenceledblinkinterval = 0.7
absenceledblinkinterval = 1.2
bluetooth_probe_timeout = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(__name__)

led = LED(LEDPIN)
relay = OutputDevice(RELAYPIN, active_high=False)
buzzer = OutputDevice(BUZZERPIN, active_high=False)
button = Button(BUTTONPIN, pull_up=True, bounce_time=buttonbouncetime)

state_lock = threading.Lock()
devicepresent = False
device_states = {mac: False for mac in macaddresses}
last_seen = {mac: 0 for mac in macaddresses}


def probe_device(macaddress):
    """Fragt per hcitool den Namen der MAC-Adresse ab und erzwingt einen Timeout."""
    try:
        process = subprocess.Popen(
            ["hcitool", "name", macaddress],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, _ = process.communicate(timeout=bluetooth_probe_timeout)
        return bool(stdout.strip())
    except subprocess.TimeoutExpired:
        logging.debug("hcitool Timeout für %s – beende den Prozess.", macaddress)
        process.kill()
        with suppress(Exception):
            process.communicate()
        return False
    except FileNotFoundError:
        logging.error("hcitool nicht gefunden. Stelle sicher, dass bluez installiert ist.")
        return False
    except Exception as exc:
        logging.error("Fehler bei hcitool name %s: %s", macaddress, exc)
        return False


def beep(times, duration):
    for _ in range(times):
        buzzer.on()
        time.sleep(duration)
        buzzer.off()
        time.sleep(duration)


def button_pressed():
    with state_lock:
        unlocked = devicepresent
    if unlocked:
        relay.on()
        time.sleep(relayclosetime)
        relay.off()


def blink_led():
    while True:
        with state_lock:
            present = devicepresent
        interval = presenceledblinkinterval if present else absenceledblinkinterval
        led.on()
        time.sleep(0.2)
        led.off()
        time.sleep(interval)


def presence_monitor():
    global devicepresent
    previous_state = None

    while True:
        cycle_start = time.time()
        now = cycle_start

        for mac in macaddresses:
            if probe_device(mac):
                with state_lock:
                    last_seen[mac] = now
                logging.debug("Gerät %s erkannt.", mac)

        with state_lock:
            for mac in macaddresses:
                device_states[mac] = (now - last_seen[mac]) <= absenceinterval
            devicepresent = any(device_states.values())
            current_state = "present" if devicepresent else "absent"
            current_states_copy = device_states.copy()

        logging.info(
            "Präsenz-Zyklus abgeschlossen: %s",
            ", ".join(mac for mac, present in current_states_copy.items() if present) or "keine Geräte"
        )

        if current_state != previous_state:
            if current_state == "present":
                beep(presencebeepcount, presencebeepduration)
                logging.info("Statuswechsel: PRESENCE")
            else:
                beep(absencebeepcount, absencebeepduration)
                logging.info("Statuswechsel: ABSENCE")
            previous_state = current_state

        elapsed = time.time() - cycle_start
        wait_time = max(0, scaninterval - elapsed)
        time.sleep(wait_time)


button.when_pressed = button_pressed


@app.route("/")
def index():
    return render_template("index.html", macaddresses=macaddresses)


@app.route("/status")
def status():
    with state_lock:
        snapshot = device_states.copy()
    return jsonify(snapshot)


@app.route("/activaterelay")
def activaterelay():
    relay.on()
    time.sleep(relayclosetime)
    relay.off()
    return "Relay activated!"


def start_threads():
    threading.Thread(target=presence_monitor, daemon=True).start()
    threading.Thread(target=blink_led, daemon=True).start()


if __name__ == "__main__":
    start_threads()
    app.run(host="0.0.0.0", port=5000)
