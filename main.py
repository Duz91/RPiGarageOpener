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
relayclosetime = 0.5
presencebeepduration = 0.1
presencebeepcount = 2
absencebeepduration = 0.1
absencebeepcount = 2
buttonbouncetime = 0.2
presenceledblinkinterval = 0.7
absenceledblinkinterval = 1.2
bluetooth_probe_timeout = 4
presence_grace_period = 25
present_reprobe_interval = 20
absent_retry_interval = 6
probe_pause = 0.2
bluetooth_adapter = "hci0"
scanner_restart_delay = 4


logging.basicConfig(
    level=logging.DEBUG,
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
device_last_seen = {mac: 0.0 for mac in macaddresses}
device_next_probe = {mac: 0.0 for mac in macaddresses}


class BluetoothScanner(threading.Thread):
    def __init__(self, targets, lock):
        super().__init__(daemon=True)
        self.targets = {mac.lower(): mac for mac in targets}
        self.lock = lock
        self.running = threading.Event()
        self.running.set()
        self.process = None
        self.mac_regex = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)

    def run(self):
        while self.running.is_set():
            try:
                self._start_process()
                if bluetooth_adapter:
                    self._send_command(f"select {bluetooth_adapter}")
                self._send_command("power on")
                self._send_command("scan on")
                logging.info("Bluetooth-Scanner gestartet.")

                while self.running.is_set():
                    line = self.process.stdout.readline()
                    if not line:
                        break
                    mac = self._extract_mac(line)
                    if not mac:
                        continue
                    now = time.time()
                    with self.lock:
                        device_last_seen[mac] = now
                    logging.debug("Scanner meldet %s @ %.3f", mac, now)
            except FileNotFoundError:
                logging.error("bluetoothctl nicht gefunden – Scanner deaktiviert.")
                return
            except Exception as exc:
                logging.warning("Bluetooth-Scanner Fehler: %s", exc)
            finally:
                self._stop_process()
                if self.running.is_set():
                    time.sleep(scanner_restart_delay)

    def stop(self):
        self.running.clear()
        self._stop_process()

    def _start_process(self):
        self.process = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def _send_command(self, command: str) -> None:
        if not self.process or not self.process.stdin:
            return
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except BrokenPipeError:
            pass

    def _extract_mac(self, line: str):
        lower_line = line.lower()
        for mac, original in self.targets.items():
            if mac in lower_line:
                return original
        match = self.mac_regex.search(lower_line)
        if not match:
            return None
        mac = match.group(1).upper()
        return self.targets.get(mac.lower())

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


bluetooth_scanner = BluetoothScanner(macaddresses, state_lock)
atexit.register(bluetooth_scanner.stop)


def probe_device(mac: str) -> bool:
    logging.debug("Starte Probe für %s", mac)
    try:
        res = subprocess.run(
            ["timeout", str(bluetooth_probe_timeout), "bluetoothctl", "info", mac],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        missing = exc.filename or "timeout/bluetoothctl"
        logging.error("Befehl %s nicht gefunden. Prüfe bluez/coreutils Installation.", missing)
        return False
    except Exception as exc:  # pragma: no cover
        logging.error("Fehler bei bluetoothctl info %s: %s", mac, exc)
        return False

    if res.stderr:
        logging.debug("bluetoothctl stderr (%s): %s", mac, res.stderr.strip())
    if res.returncode == 124:
        logging.debug("bluetoothctl Timeout für %s", mac)
        return False
    return "Device" in res.stdout or "Name:" in res.stdout


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
        now = cycle_start
        logging.debug("Presence-Zyklus gestartet (now=%.3f)", now)
        current_states = {}
        present_macs = []
        last_seen_updates = {}
        next_probe_updates = {}

        with state_lock:
            last_seen_snapshot = {mac: device_last_seen.get(mac, 0.0) for mac in macaddresses}
            next_probe_snapshot = {mac: device_next_probe.get(mac, 0.0) for mac in macaddresses}

        for mac in macaddresses:
            last_seen = last_seen_snapshot.get(mac, 0.0)
            next_allowed_probe = next_probe_snapshot.get(mac, 0.0)
            seen_recently = (now - last_seen) <= presence_grace_period

            if seen_recently:
                current_states[mac] = True
                present_macs.append(mac)
                next_probe_updates[mac] = now + present_reprobe_interval
                logging.debug(
                    "Markiere %s als präsent (last_seen=%.3f, next_probe=%.3f)",
                    mac,
                    last_seen,
                    next_probe_updates[mac],
                )
                continue

            if now < next_allowed_probe:
                current_states[mac] = False
                next_probe_updates[mac] = next_allowed_probe
                logging.debug(
                    "Überspringe Probe für %s (next_probe=%.3f, last_seen=%.3f)",
                    mac,
                    next_allowed_probe,
                    last_seen,
                )
                continue

            is_present = probe_device(mac)
            if is_present:
                current_states[mac] = True
                present_macs.append(mac)
                last_seen_updates[mac] = now
                next_probe_updates[mac] = now + present_reprobe_interval
                logging.debug(
                    "Probe erfolgreich für %s (next_probe=%.3f)",
                    mac,
                    next_probe_updates[mac],
                )
            else:
                current_states[mac] = False
                next_probe_updates[mac] = now + absent_retry_interval
                logging.debug(
                    "Probe fehlgeschlagen für %s (nächster Versuch=%.3f)",
                    mac,
                    next_probe_updates[mac],
                )
            time.sleep(probe_pause)

        with state_lock:
            device_states.update(current_states)
            devicepresent = bool(present_macs)
            current_presence = devicepresent
            for mac, ts in last_seen_updates.items():
                device_last_seen[mac] = ts
            for mac in macaddresses:
                device_next_probe[mac] = next_probe_updates.get(
                    mac, device_next_probe.get(mac, now + absent_retry_interval)
                )

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
        sleep_time = scaninterval - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            logging.warning(
                "Presence-Zyklus überschreitet scaninterval (elapsed=%.3f, interval=%.3f)",
                elapsed,
                scaninterval,
            )


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
