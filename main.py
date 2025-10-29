from flask import Flask, render_template, jsonify
import logging
import subprocess
import threading
import time

try:
    from gpiozero import Button, LED, OutputDevice
except ImportError:
    class _DummyGPIO:
        def __init__(self, *_, **__):
            self.state = False
            self.when_pressed = None

        def on(self):
            self.state = True

        def off(self):
            self.state = False

        def blink(self, *_, **__):
            pass

    Button = LED = OutputDevice = _DummyGPIO


BUTTONPIN = 5
LEDPIN = 23
RELAYPIN = 26
BUZZERPIN = 19

macaddresses = [
    "0C:15:63:DF:61:2F",
    "80:04:5F:A2:66:57",
]

scaninterval = 7
relayclosetime = 0.5
presencebeepduration = 0.1
presencebeepcount = 2
absencebeepduration = 0.1
absencebeepcount = 2
buttonbouncetime = 0.2
presenceledblinkinterval = 0.7
absenceledblinkinterval = 1.2
bluetooth_probe_timeout = 4


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)

led = LED(LEDPIN)
relay = OutputDevice(RELAYPIN, active_high=False)
buzzer = OutputDevice(BUZZERPIN, active_high=False)
button = Button(BUTTONPIN, pull_up=True, bounce_time=buttonbouncetime)

state_lock = threading.Lock()
device_states = {mac: False for mac in macaddresses}
devicepresent = False


def probe_device(mac: str) -> bool:
    try:
        res = subprocess.run(
            ["hcitool", "name", mac],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=bluetooth_probe_timeout,
        )
    except subprocess.TimeoutExpired:
        logging.debug("hcitool Timeout für %s", mac)
        return False
    except FileNotFoundError:
        logging.error("Befehl hcitool nicht gefunden. Prüfe bluez Installation.")
        return False
    except Exception as exc:  # pragma: no cover
        logging.error("Fehler bei hcitool name %s: %s", mac, exc)
        return False

    if res.stderr:
        logging.debug("hcitool stderr (%s): %s", mac, res.stderr.strip())
    return bool(res.stdout.strip())


def beep(times: int, duration: float) -> None:
    for _ in range(times):
        buzzer.on()
        time.sleep(duration)
        buzzer.off()
        time.sleep(duration)


def button_pressed() -> None:
    with state_lock:
        open_allowed = devicepresent
    if open_allowed:
        relay.on()
        time.sleep(relayclosetime)
        relay.off()


def blink_led() -> None:
    while True:
        with state_lock:
            present = devicepresent
        interval = presenceledblinkinterval if present else absenceledblinkinterval
        led.on()
        time.sleep(0.2)
        led.off()
        time.sleep(interval)


def presence_monitor() -> None:
    global devicepresent
    previous_presence = None

    while True:
        cycle_start = time.time()
        current_states = {}

        for mac in macaddresses:
            current_states[mac] = probe_device(mac)
            time.sleep(0.1)

        present_macs = [mac for mac, is_present in current_states.items() if is_present]

        with state_lock:
            device_states.update(current_states)
            devicepresent = any(present_macs)
            current_presence = devicepresent

        logging.info(
            "Scan abgeschlossen → anwesend: %s",
            ", ".join(present_macs) if present_macs else "keine Geräte",
        )

        if current_presence != previous_presence:
            if current_presence:
                beep(presencebeepcount, presencebeepduration)
                logging.info("Statuswechsel → Presence")
            else:
                beep(absencebeepcount, absencebeepduration)
                logging.info("Statuswechsel → Absence")
            previous_presence = current_presence

        elapsed = time.time() - cycle_start
        time.sleep(max(0.0, scaninterval - elapsed))


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


button.when_pressed = button_pressed


def start_threads() -> None:
    threading.Thread(target=presence_monitor, daemon=True).start()
    threading.Thread(target=blink_led, daemon=True).start()


if __name__ == "__main__":
    start_threads()
    app.run(host="0.0.0.0", port=5000)
