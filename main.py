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

scaninterval = 12
relayclosetime = 0.5
presencebeepduration = 0.1
presencebeepcount = 2
absencebeepduration = 0.1
absencebeepcount = 2
buttonbouncetime = 0.2
presenceledblinkinterval = 0.7
absenceledblinkinterval = 1.2
presence_grace_period = 60
scanner_restart_delay = 4
scanner_command = ["stdbuf", "-oL", "bluetoothctl"]
active_probe_trigger = 30
active_probe_schedule = [
    (1.5, 1, 0.3),
    (3.0, 2, 0.6),
]
active_probe_cooldown = 15
use_hcitool_fallback = True


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
device_last_probe = {mac: 0.0 for mac in macaddresses}


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
                self._configure_controller()
                logging.info("Bluetooth-Scanner aktiv.")

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
                    logging.debug("Scanner sieht %s @ %.3f (%s)", mac, now, line.strip())
            except FileNotFoundError:
                logging.error("bluetoothctl nicht gefunden – Scanner gestoppt.")
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
        try:
            cmd = scanner_command
            self.process = subprocess.Popen(
                cmd,
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

    def _configure_controller(self):
        if not self.process or not self.process.stdin:
            return
        commands = [
            "set le on",
            "set duplicate-data true",
            "scan on",
        ]
        for cmd in commands:
            self._send_command(cmd)

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
        for mac_lower, original in self.targets.items():
            if mac_lower in lower_line:
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


bluetooth_scanner = BluetoothScanner(macaddresses, state_lock)
atexit.register(bluetooth_scanner.stop)


def _run_command(cmd, timeout=None):
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logging.debug("Timeout für Kommando: %s", " ".join(cmd))
        return None
    except FileNotFoundError as exc:
        logging.error("Befehl %s nicht gefunden.", exc.filename or cmd[0])
        return None
    except Exception as exc:  # pragma: no cover
        logging.error("Fehler bei Kommando %s: %s", " ".join(cmd), exc)
        return None


def active_probe(mac: str) -> bool:
    for stage, (timeout, attempts, pause) in enumerate(active_probe_schedule, start=1):
        for attempt in range(1, attempts + 1):
            logging.debug(
                "Aktive Probe Stufe %d Versuch %d via bluetoothctl für %s (Timeout %.1fs)",
                stage,
                attempt,
                mac,
                timeout,
            )
            res = _run_command(["timeout", str(timeout), "bluetoothctl", "info", mac])
            if res is not None:
                if res.stderr:
                    logging.debug("bluetoothctl stderr (%s): %s", mac, res.stderr.strip())
                stdout = res.stdout.strip()
                if stdout:
                    preview = "; ".join(stdout.splitlines()[:3])
                    logging.debug("bluetoothctl info (%s) → rc=%s, Daten=%s", mac, res.returncode, preview)
                if res.returncode == 0 and ("Connected: yes" in stdout or "RSSI:" in stdout):
                    logging.debug("Aktive Probe erfolgreich via bluetoothctl für %s", mac)
                    return True

            if use_hcitool_fallback:
                logging.debug(
                    "Aktive Probe Stufe %d Versuch %d via hcitool name für %s (Timeout %.1fs)",
                    stage,
                    attempt,
                    mac,
                    timeout,
                )
                res = _run_command(["timeout", str(timeout), "hcitool", "name", mac])
                if res is not None:
                    if res.stderr:
                        logging.debug("hcitool stderr (%s): %s", mac, res.stderr.strip())
                    if res.stdout and res.returncode == 0:
                        logging.debug("Aktive Probe erfolgreich via hcitool für %s: %s", mac, res.stdout.strip())
                        return True

            if attempt < attempts:
                time.sleep(pause)
        if stage < len(active_probe_schedule):
            time.sleep(pause)
    logging.debug("Aktive Probe endgültig fehlgeschlagen für %s", mac)
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
    previous_presence = None

    while True:
        cycle_start = time.time()
        now = cycle_start
        logging.debug("Presence-Zyklus gestartet (now=%.3f)", now)
        with state_lock:
            status_info = {}
            for mac in macaddresses:
                last_seen = device_last_seen.get(mac, 0.0)
                last_probe = device_last_probe.get(mac, 0.0)
                delta = now - last_seen if last_seen else float("inf")
                status_info[mac] = {
                    "last_seen": last_seen,
                    "last_probe": last_probe,
                    "delta": delta,
                    "present": bool(last_seen and delta <= presence_grace_period),
                    "probe": "skipped",
                }

        for mac, info in status_info.items():
            if info["present"]:
                continue
            time_since_probe = (
                now - info["last_probe"] if info["last_probe"] else float("inf")
            )
            trigger_probe = (
                (info["last_seen"] == 0.0 or info["delta"] >= active_probe_trigger)
                and time_since_probe >= active_probe_cooldown
            )
            if not trigger_probe:
                continue
            logging.debug(
                "Aktive Probe erforderlich für %s (delta=%.1fs, seit Probe %.1fs)",
                mac,
                info["delta"] if info["delta"] != float("inf") else -1.0,
                time_since_probe if time_since_probe != float("inf") else -1.0,
            )
            probe_start = time.time()
            with state_lock:
                device_last_probe[mac] = probe_start
            info["last_probe"] = probe_start

            success = active_probe(mac)
            info["probe"] = "success" if success else "fail"
            if success:
                probe_time = time.time()
                info["last_seen"] = probe_time
                info["delta"] = 0.0
                info["present"] = True
                with state_lock:
                    device_last_seen[mac] = probe_time
            else:
                logging.debug("Aktive Probe ergab keine Präsenz für %s", mac)

        with state_lock:
            present_macs = [mac for mac, info in status_info.items() if info["present"]]
            for mac, info in status_info.items():
                device_states[mac] = info["present"]
            devicepresent = bool(present_macs)
            current_presence = devicepresent

        status_lines = []
        for mac, info in status_info.items():
            if info["last_seen"]:
                delta = time.time() - info["last_seen"]
                note = f"{delta:.1f}s"
            else:
                note = "noch nie gesehen"
            if info["probe"] == "success":
                note += ", probe ok"
            elif info["probe"] == "fail":
                note += ", probe fail"
            else:
                note += ", passiv"
            status_lines.append(f"{mac} → {'PRESENT' if info['present'] else 'ABSENT'} ({note})")
            logging.debug(
                "Bewertung %s → last_seen=%.3f, delta=%.3f, präsent=%s, probe=%s",
                mac,
                info['last_seen'],
                info['delta'],
                info['present'],
                info['probe'],
            )
        logging.info("Statusübersicht: %s", " | ".join(status_lines))

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
