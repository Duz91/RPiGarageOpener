from flask import Flask, render_template, jsonify
import threading
import time
from gpiozero import Button, LED, OutputDevice
import subprocess
import logging
from contextlib import suppress
import atexit
import re

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
state_lock = threading.Lock()
device_last_seen = {mac: 0 for mac in macaddresses}

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


class BluetoothScanner(threading.Thread):
    """Beobachtet bluetoothctl-Ausgaben und merkt sich zuletzt gesehene Zielgeräte."""

    def __init__(self, targets, lock, last_seen):
        super().__init__(daemon=True)
        self.targets = {mac.lower(): mac for mac in targets}
        self.lock = lock
        self.last_seen = last_seen
        self.running = threading.Event()
        self.running.set()
        self.process = None

    def run(self):
        while self.running.is_set():
            try:
                self._start_process()
                self._send_command("power on")
                self._send_command("scan on")
                logging.info("bluetoothctl Scanner gestartet.")
                while self.running.is_set():
                    line = self.process.stdout.readline()
                    if not line:
                        break
                    mac = self._extract_target_mac(line)
                    if mac:
                        with self.lock:
                            self.last_seen[mac] = time.time()
                if self.running.is_set():
                    logging.warning("bluetoothctl Scanner unerwartet beendet – versuche Neustart.")
            except FileNotFoundError:
                logging.warning("bluetoothctl nicht gefunden – Scanner deaktiviert.")
                return
            except Exception as exc:
                logging.error("Fehler im bluetoothctl Scanner: %s", exc)
            finally:
                self._stop_process()
                if self.running.is_set():
                    time.sleep(2)

    def stop(self):
        self.running.clear()
        self._stop_process()

    def _start_process(self):
        # stdbuf sorgt für zeilenweises Flushen, damit readline() zeitnah Daten bekommt
        try:
            self.process = subprocess.Popen(
                ["stdbuf", "-oL", "bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
        except FileNotFoundError:
            # stdbuf nicht verfügbar → ohne starten
            self.process = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

    def _send_command(self, command):
        if self.process and self.process.stdin:
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
            except BrokenPipeError:
                pass

    def _extract_target_mac(self, line):
        lower_line = line.lower()
        for lower_mac, original_mac in self.targets.items():
            if lower_mac in lower_line:
                logging.debug("bluetoothctl Meldung für %s: %s", original_mac, line.strip())
                return original_mac

        match = re.search(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", lower_line)
        if match:
            candidate = match.group(1).upper()
            return self.targets.get(candidate.lower())
        return None

    def _stop_process(self):
        if self.process:
            try:
                self._send_command("scan off")
                self._send_command("quit")
            except Exception:
                pass
            with suppress(Exception):
                self.process.terminate()
            with suppress(Exception):
                self.process.wait(timeout=2)
            if self.process.poll() is None:
                with suppress(Exception):
                    self.process.kill()
            self.process = None


bluetooth_scanner = BluetoothScanner(macaddresses, state_lock, device_last_seen)
atexit.register(bluetooth_scanner.stop)


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
    previousstate = None

    while True:
        cycle_start = time.time()
        logging.info("Starte Präsenz-Zyklus …")
        found_devices = set()
        now = time.time()

        # Ergebnisse aus dem Scanner übernehmen
        with state_lock:
            for mac in macaddresses:
                last_time = device_last_seen.get(mac, 0)
                if now - last_time <= absenceinterval:
                    found_devices.add(mac)

        # Für fehlende Geräte aktiv mit hcitool nachfragen
        for mac in macaddresses:
            if mac in found_devices:
                continue
            if probe_with_hcitool(mac):
                with state_lock:
                    device_last_seen[mac] = time.time()
                found_devices.add(mac)
                logging.info("Direkte Abfrage erfolgreich für %s", mac)

        # Gerätestatus aktualisieren
        for mac in macaddresses:
            device_states[mac] = mac in found_devices
            logging.debug("Status %s: %s", mac, "anwesend" if device_states[mac] else "abwesend")

        logging.info("Zyklus abgeschlossen: %s", ", ".join(found_devices) or "nichts gefunden")

        devicepresent = bool(found_devices)

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
        if sleep_time > 0:
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
    try:
        bluetooth_scanner.start()
    except RuntimeError:
        logging.warning("bluetoothctl Scanner konnte nicht gestartet werden (läuft bereits?).")
    threading.Thread(target=main_thread, daemon=True).start()
    threading.Thread(target=blink_led, daemon=True).start()

if __name__ == "__main__":
    start_threads()
    app.run(host="0.0.0.0", port=5000)
