from flask import Flask, render_template, jsonify
import logging
import subprocess
import threading
import time

try:
    from gpiozero import Button, LED, OutputDevice
except ImportError:
    # Entwicklungs-Stub, falls das Script ohne Raspberry-Pi-GPIOs läuft
    class _DummyGPIO:
        def __init__(self, *_, **__):
            self.state = False

        def on(self):
            self.state = True

        def off(self):
            self.state = False

        def blink(self, *_, **__):
            pass

    Button = LED = OutputDevice = _DummyGPIO

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
BUTTONPIN = 5
LEDPIN = 23
RELAYPIN = 26
BUZZERPIN = 19

macaddresses = [
    "0C:15:63:DF:61:2F",
    "80:04:5F:A2:66:57",
]

scaninterval = 7              # Sekunden zwischen zwei kompletten Abfragen
absenceinterval = 15          # Dauer, bis ein Gerät als abwesend gilt
relayclosetime = 0.5
presencebeepduration = 0.1
presencebeepcount = 2
absencebeepduration = 0.1
absencebeepcount = 2
buttonbouncetime = 0.2
presenceledblinkinterval = 0.7
absenceledblinkinterval = 1.2
bluetooth_probe_timeout = 6   # Zeitlimit für hcitool name

# --------------------------------------------------------------------------- #
# Initialisierung
# --------------------------------------------------------------------------- #
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
last_seen = {mac: 0.0 for mac in macaddresses}
devicepresent = False

# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def probe_device(mac: str) -> bool:
    """Liefert True, wenn das Gerät per klassischem Bluetooth erreichbar ist."""
    cmd = ["timeout", str(bluetooth_probe_timeout), "hcitool", "name", mac]
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if res.returncode == 124:
            logging.debug("hcitool Timeout für %s", mac)
            return False
        if res.stderr:
            logging.debug("hcitool stderr (%s): %s", mac, res.stderr.strip())
        return bool(res.stdout.strip())
    except FileNotFoundError:
        logging.error("Befehl '%s' nicht gefunden. Prüfe, ob coreutils/bluez installiert ist.", cmd[0])
        return False
    except Exception as exc:  # pragma: no cover
        logging.error("Fehler bei hcitool name %s: %s", mac, exc)
        return False


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
        try:
            with state_lock:
                present = devicepresent

            interval = presenceledblinkinterval if present else absenceledblinkinterval
            led.on()
            time.sleep(0.2)
            led.off()
            time.sleep(interval)
        except Exception as exc:  # pragma: no cover
            logging.exception("LED-Thread Fehler: %s", exc)
            time.sleep(1)


def presence_monitor() -> None:
    """Hauptschleife zur Erkennung von Presence/Absence."""
    global devicepresent
    previous_state = None

    while True:
        try:
            cycle_start = time.time()
            now = cycle_start
            detected = []

            for mac in macaddresses:
                if probe_device(mac):
                    with state_lock:
                        last_seen[mac] = now
                    detected.append(mac)

            with state_lock:
                for mac in macaddresses:
                    device_states[mac] = (now - last_seen[mac]) <= absenceinterval
                devicepresent = any(device_states.values())
                current_state = "present" if devicepresent else "absent"

            logging.info(
                "Scan abgeschlossen → anwesend: %s",
                ", ".join(detected) if detected else "keine Geräte",
            )

            if current_state != previous_state:
                if current_state == "present":
                    beep(presencebeepcount, presencebeepduration)
                    logging.info("Statuswechsel → Presence")
                else:
                    beep(absencebeepcount, absencebeepduration)
                    logging.info("Statuswechsel → Absence")
                previous_state = current_state

            elapsed = time.time() - cycle_start
            time.sleep(max(0.0, scaninterval - elapsed))
        except Exception as exc:  # pragma: no cover
            logging.exception("Presence-Monitor Fehler: %s", exc)
            time.sleep(2)

# --------------------------------------------------------------------------- #
# Flask-Routen
# --------------------------------------------------------------------------- #
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

# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #
button.when_pressed = button_pressed


def start_threads() -> None:
    threading.Thread(target=presence_monitor, daemon=True).start()
    threading.Thread(target=blink_led, daemon=True).start()


if __name__ == "__main__":
    start_threads()
    app.run(host="0.0.0.0", port=5000)
