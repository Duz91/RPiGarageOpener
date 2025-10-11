from flask import Flask, render_template, jsonify
import threading
import time
from gpiozero import Button, LED, OutputDevice
import subprocess
import logging
from contextlib import suppress

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

devicepresent = False
device_states = {mac: False for mac in macaddresses}
bluetooth_probe_timeout = 5
bluetooth_scan_duration = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

app = Flask(__name__)

led = LED(LEDPIN)
relay = OutputDevice(RELAYPIN, active_high=False)
buzzer = OutputDevice(BUZZERPIN, active_high=False)
button = Button(BUTTONPIN, pull_up=True, bounce_time=buttonbouncetime)

def probe_with_hcitool(macaddress):
    """Fragt gezielt nach einem einzelnen Gerät und erzwingt einen Timeout."""
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
        logging.warning("hcitool Timeout für %s – Prozess wird beendet.", macaddress)
        process.kill()
        with suppress(Exception):
            process.communicate()
        return False
    except FileNotFoundError:
        logging.error("hcitool nicht gefunden. Installiere bluez oder passe die Konfiguration an.")
        return False
    except Exception as exc:
        logging.error("hcitool Fehler für %s: %s", macaddress, exc)
        return False


def scan_with_bluetoothctl(timeout):
    """Führt einen kurzen Scan mit bluetoothctl aus und liefert gefundene MACs."""
    cmd = ["bluetoothctl", "--timeout", str(timeout), "scan", "on"]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        found = set()
        for line in result.stdout.splitlines():
            lower_line = line.lower()
            for mac in macaddresses:
                if mac.lower() in lower_line:
                    found.add(mac)
        if result.stderr:
            logging.debug("bluetoothctl stderr: %s", result.stderr.strip())
        return found
    except FileNotFoundError:
        logging.warning("bluetoothctl nicht verfügbar – wechsle auf hcitool-Fallback.")
        return None
    except Exception as exc:
        logging.error("bluetoothctl Scan fehlgeschlagen: %s", exc)
        return None


def scan_for_devices():
    """Ermittelt alle aktuell sichtbaren Ziel-MAC-Adressen."""
    found = scan_with_bluetoothctl(bluetooth_scan_duration)
    if found is not None:
        return found

    # Fallback: einzelne hcitool-Abfragen
    found = set()
    for mac in macaddresses:
        if probe_with_hcitool(mac):
            found.add(mac)
    return found

def beep(times, duration):
    for _ in range(times):
        buzzer.on()
        time.sleep(duration)
        buzzer.off()
        time.sleep(duration)

def button_pressed():
    if devicepresent:
        relay.on()
        time.sleep(relayclosetime)
        relay.off()

def blink_led():
    global devicepresent
    while True:
        if devicepresent:
            led.on()
            time.sleep(presenceledblinkinterval)
            led.off()
            time.sleep(presenceledblinkinterval)
        else:
            led.on()
            time.sleep(absenceledblinkinterval)
            led.off()
            time.sleep(absenceledblinkinterval)

def main_thread():
    global devicepresent
    lastseen = {}
    previousstate = None

    while True:
        cycle_start = time.time()
        logging.info("Starte Bluetooth-Scan …")
        found_devices = scan_for_devices()
        now = time.time()
        logging.info("Scan abgeschlossen: %s", ", ".join(found_devices) or "nichts gefunden")

        for mac in macaddresses:
            if mac in found_devices:
                device_states[mac] = True
                lastseen[mac] = now
            else:
                # Als abwesend markieren, wenn älter als absenceinterval
                last_time = lastseen.get(mac, 0)
                if now - last_time > absenceinterval:
                    device_states[mac] = False

        # Alte Einträge entfernen
        for mac, lasttime in list(lastseen.items()):
            if now - lasttime > absenceinterval:
                del lastseen[mac]
                device_states[mac] = False

        devicepresent = len(lastseen) > 0

        # Statuswechsel → akustisches Signal
        if devicepresent and previousstate != "present":
            beep(presencebeepcount, presencebeepduration)
            previousstate = "present"
            logging.info("Statuswechsel: PRESENCE")
        elif not devicepresent and previousstate != "absent":
            beep(absencebeepcount, absencebeepduration)
            previousstate = "absent"
            logging.info("Statuswechsel: ABSENCE")

        elapsed = time.time() - cycle_start
        sleep_time = max(0, scaninterval - elapsed)
        if sleep_time:
            time.sleep(sleep_time)

button.when_pressed = button_pressed

@app.route("/")
def index():
    return render_template("index.html", macaddresses=macaddresses)

@app.route("/status")
def status():
    """Gibt den aktuellen Status als JSON zurück"""
    return jsonify(device_states)

@app.route("/activaterelay")
def activaterelay():
    relay.on()
    time.sleep(relayclosetime)
    relay.off()
    return "Relay activated!"

def start_threads():
    threading.Thread(target=main_thread, daemon=True).start()
    threading.Thread(target=blink_led, daemon=True).start()

if __name__ == "__main__":
    start_threads()
    app.run(host="0.0.0.0", port=5000)
