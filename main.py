from flask import Flask, render_template, jsonify
import atexit
import logging
import re
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
bluetooth_adapter = "hci0"
max_probe_failures = 3


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
device_last_seen = {mac: 0.0 for mac in macaddresses}
devicepresent = False


class BluetoothScanner(threading.Thread):
    """Läuft dauerhaft mit bluetoothctl scan on und aktualisiert last_seen."""

    def __init__(self, targets, lock, last_seen, adapter=None):
        super().__init__(daemon=True)
        self.targets = {mac.lower(): mac for mac in targets}
        self.lock = lock
        self.last_seen = last_seen
        self.adapter = adapter
        self.running = threading.Event()
        self.running.set()
        self.process = None
        self.mac_regex = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)

    def run(self):
        while self.running.is_set():
            try:
                self._start_process()
                if self.adapter:
                    self._send_command(f"select {self.adapter}")
                self._send_command("power on")
                self._send_command("scan on")
                logging.info("Bluetooth-Scanner gestartet.")

                while self.running.is_set():
                    line = self.process.stdout.readline()
                    if not line:
                        break
                    mac = self._extract_target_mac(line)
                    if mac:
                        with self.lock:
                            self.last_seen[mac] = time.time()
                if self.running.is_set():
                    logging.warning("bluetoothctl Scanner unerwartet beendet – Neustart.")
            except FileNotFoundError:
                logging.warning("bluetoothctl nicht gefunden – Scanner deaktiviert.")
                return
            except Exception as exc:
                logging.error("Bluetooth-Scanner Fehler: %s", exc)
            finally:
                self._stop_process()
                if self.running.is_set():
                    time.sleep(3)

    def stop(self):
        self.running.clear()
        self._stop_process()

    def _start_process(self):
        try:
            self.process = subprocess.Popen(
                ["stdbuf", "-oL", "bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except FileNotFoundError:
            self.process = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
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
        for lower_mac, original in self.targets.items():
            if lower_mac in lower_line:
                logging.debug("bluetoothctl Meldung für %s: %s", original, line.strip())
                return original
        match = self.mac_regex.search(lower_line)
        if match:
            mac = match.group(1).upper()
            return self.targets.get(mac.lower())
        return None

    def _stop_process(self):
        if not self.process:
            return
        try:
            self._send_command("scan off")
            self._send_command("quit")
        except Exception:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=2)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None


bluetooth_scanner = BluetoothScanner(macaddresses, state_lock, device_last_seen, bluetooth_adapter)
atexit.register(bluetooth_scanner.stop)


def reset_bluetooth_adapter():
    if not bluetooth_adapter:
        return
    logging.warning("Setze Bluetooth-Adapter %s zurück …", bluetooth_adapter)
    subprocess.run(["hciconfig", bluetooth_adapter, "reset"], check=False)
    time.sleep(1)


def probe_device(mac: str) -> bool:
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
        logging.error("Befehl %s nicht gefunden. Prüfe bluez/coreutils Installation.", cmd[0])
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
        with state_lock:
            present = devicepresent
        interval = presenceledblinkinterval if present else absenceledblinkinterval
        led.on()
        time.sleep(0.2)
        led.off()
        time.sleep(interval)


def presence_monitor() -> None:
    global devicepresent
    previous_state = None
    probe_failures = {mac: 0 for mac in macaddresses}

    while True:
        cycle_start = time.time()
        now = cycle_start
        present_now = set()
        adapter_reset = False

        with state_lock:
            for mac in macaddresses:
                if now - device_last_seen[mac] <= absenceinterval:
                    present_now.add(mac)

        for mac in macaddresses:
            if mac in present_now:
                probe_failures[mac] = 0
                continue

            if probe_device(mac):
                with state_lock:
                    device_last_seen[mac] = time.time()
                present_now.add(mac)
                probe_failures[mac] = 0
                logging.debug("Direkte Abfrage erfolgreich für %s", mac)
            else:
                probe_failures[mac] += 1
                logging.debug("Fehlversuch %s (%d)", mac, probe_failures[mac])
                if probe_failures[mac] >= max_probe_failures:
                    logging.warning(
                        "Mehrere Fehlversuche für %s – setze Bluetooth-Adapter zurück.",
                        mac,
                    )
                    reset_bluetooth_adapter()
                    probe_failures[mac] = 0
                    adapter_reset = True
                    break

        if adapter_reset:
            time.sleep(3)
            continue

        with state_lock:
            for mac in macaddresses:
                device_states[mac] = mac in present_now
            devicepresent = any(device_states.values())
            current_state = "present" if devicepresent else "absent"

        logging.info(
            "Scan abgeschlossen → anwesend: %s",
            ", ".join(sorted(present_now)) if present_now else "keine Geräte",
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
    bluetooth_scanner.start()
    threading.Thread(target=presence_monitor, daemon=True).start()
    threading.Thread(target=blink_led, daemon=True).start()


if __name__ == "__main__":
    start_threads()
    app.run(host="0.0.0.0", port=5000)
