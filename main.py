from flask import Flask, render_template, jsonify
import logging
import subprocess
import threading
import time
import os
import resource

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

        @property
        def value(self):
            return float(self.state)

        @property
        def is_pressed(self):
            return bool(self.state)

    Button = LED = OutputDevice = _DummyGPIO


BUTTONPIN = 5
LEDPIN = 23
RELAYPIN = 26
BUZZERPIN = 19

macaddresses = [
    "0C:15:63:DF:61:2F",
    "80:04:5F:A2:66:57",
    "58:73:D8:CA:D3:F4",
    "58:AD:12:A0:7F:36",
]

mac_labels = {
    "0C:15:63:DF:61:2F": "Mercedes iPhone",
    "80:04:5F:A2:66:57": "iPhoneSE",
    "58:73:D8:CA:D3:F4": "Apple Watch Ultra",
    "58:AD:12:A0:7F:36": "iPhone 14 Pro Max",
}

scaninterval = 20
relayclosetime = 0.5
presencebeepduration = 0.1
presencebeepcount = 2
absencebeepduration = 0.1
absencebeepcount = 2
buttonbouncetime = 0.2
presenceledblinkinterval = 0.7
absenceledblinkinterval = 1.2
active_probe_schedule = [
    (1.4, 1, 0.8),
]
max_absent_failures = 3
inter_device_pause = 3.0


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = Flask(__name__)

led = LED(LEDPIN)
relay = OutputDevice(RELAYPIN, active_high=False)
buzzer = OutputDevice(BUZZERPIN, active_high=False)
button = Button(BUTTONPIN, pull_up=True, bounce_time=buttonbouncetime)

GPIO_INFO = [
    {"name": "LED", "pin": LEDPIN, "role": "Statusanzeige", "device": led},
    {"name": "Relais", "pin": RELAYPIN, "role": "Garagentor", "device": relay},
    {"name": "Buzzer", "pin": BUZZERPIN, "role": "Signalton", "device": buzzer},
    {"name": "Taster", "pin": BUTTONPIN, "role": "Manuelle Öffnung", "device": button},
]

state_lock = threading.Lock()
device_states = {mac: False for mac in macaddresses}
devicepresent = False
device_last_success = {mac: 0.0 for mac in macaddresses}
device_failure_counts = {mac: 0 for mac in macaddresses}
device_last_result = {mac: "never" for mac in macaddresses}
current_probe_target = None


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
                "Aktive Probe Stufe %d Versuch %d via hcitool name für %s (Timeout %.1fs)",
                stage,
                attempt,
                mac,
                timeout,
            )
            res = _run_command(["hcitool", "name", mac], timeout=timeout)
            if res is not None:
                if res.stderr:
                    logging.debug("hcitool stderr (%s): %s", mac, res.stderr.strip())
                if res.stdout and res.returncode == 0:
                    logging.debug(
                        "Aktive Probe erfolgreich via hcitool für %s: %s",
                        mac,
                        res.stdout.strip(),
                    )
                    return True

            if attempt < attempts:
                time.sleep(pause)
        if stage < len(active_probe_schedule):
            time.sleep(pause)
    logging.debug("Aktive Probe endgültig fehlgeschlagen für %s", mac)
    return False


def log_hcitool_processes() -> None:
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid,stat,comm"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        logging.debug("ps Befehl nicht verfügbar – Prozessdiagnose übersprungen.")
        return
    except Exception as exc:
        logging.debug("Prozessdiagnose fehlgeschlagen: %s", exc)
        return

    total = 0
    running = 0
    zombies = 0
    lines = res.stdout.strip().splitlines()
    for line in lines[1:]:
        if "hcitool" not in line:
            continue
        total += 1
        parts = line.split()
        if len(parts) >= 2:
            stat = parts[1]
        else:
            stat = ""
        if "Z" in stat:
            zombies += 1
        elif stat.startswith("R") or stat.startswith("D"):
            running += 1

    if total:
        msg = f"hcitool Prozesse aktiv: {total} (running: {running}, zombies: {zombies})"
        if zombies or running > 1:
            logging.warning("%s", msg)
        else:
            logging.debug("%s", msg)


def collect_system_stats() -> dict:
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = 0.0
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss_mb = usage.ru_maxrss / 1024.0
    return {
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "cpu_utime": usage.ru_utime,
        "rss_mb": rss_mb,
        "threads": len(threading.enumerate()),
    }


def log_system_stats() -> None:
    stats = collect_system_stats()
    logging.debug(
        "Systemstats: load=%.2f/%.2f/%.2f, cpu_utime=%.2fs, rss=%.1fMB, threads=%d",
        stats["load1"],
        stats["load5"],
        stats["load15"],
        stats["cpu_utime"],
        stats["rss_mb"],
        stats["threads"],
    )

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
    global devicepresent, current_probe_target
    previous_presence = None
    order = list(macaddresses)
    start_index = 0
    monitored_mac = None

    while True:
        cycle_start = time.time()
        logging.debug("Presence-Zyklus gestartet (now=%.3f)", cycle_start)
        results = {mac: None for mac in order}

        if monitored_mac is None:
            logging.debug("Kein aktives Gerät – starte Suche")
            next_monitored = None
            for step in range(len(order)):
                mac = order[(start_index + step) % len(order)]
                logging.debug("Starte aktive Prüfung für %s", mac)
                with state_lock:
                    current_probe_target = mac
                success = active_probe(mac)
                results[mac] = success
                if success:
                    next_monitored = mac
                    logging.debug("%s als aktives Gerät übernommen", mac)
                    break
                if step < len(order) - 1:
                    time.sleep(inter_device_pause)
            start_index = (start_index + 1) % len(order)
        else:
            logging.debug("Prüfe ausschließlich aktives Gerät %s", monitored_mac)
            with state_lock:
                current_probe_target = monitored_mac
            success = active_probe(monitored_mac)
            results[monitored_mac] = success
            next_monitored = monitored_mac

        now = time.time()
        status_lines = []
        with state_lock:
            for mac in order:
                success = results.get(mac)
                if success:
                    device_states[mac] = True
                    device_failure_counts[mac] = 0
                    device_last_success[mac] = now
                    device_last_result[mac] = "hit"
                    next_monitored = mac
                else:
                    if success is False:
                        device_failure_counts[mac] += 1
                        device_failure_counts[mac] = min(
                            device_failure_counts[mac], max_absent_failures + 1
                        )
                        device_last_result[mac] = "miss"
                        if (
                            device_failure_counts[mac] > max_absent_failures
                            and mac == next_monitored
                        ):
                            device_states[mac] = False
                            next_monitored = None
                    else:
                        device_last_result[mac] = "skip"
                    if mac != next_monitored:
                        device_states[mac] = False
                state = device_states[mac]
                fails = device_failure_counts[mac]
                last_success = device_last_success[mac]
                if last_success:
                    delta = now - last_success
                    note = f"{delta:.1f}s seit Erfolg"
                else:
                    note = "keine Messung"
                result_label = device_last_result[mac]
                note = f"{note}, {result_label}, fails={fails}"
                status_lines.append(
                    f"{mac} → {'PRESENT' if state else 'ABSENT'} ({note})"
                )
                logging.debug(
                    "Bewertung %s → state=%s, result=%s, fails=%d, last_success=%.3f",
                    mac,
                    state,
                    result_label,
                    fails,
                    last_success,
                )
            devicepresent = any(device_states.values())
            current_presence = devicepresent
            present_macs = [mac for mac, state in device_states.items() if state]
            current_probe_target = next_monitored

        monitored_mac = next_monitored
        logging.info("Statusübersicht: %s", " | ".join(status_lines))
        logging.info(
            "Scan abgeschlossen → anwesend: %s",
            ", ".join(present_macs) if present_macs else "keine Geräte",
        )

        log_hcitool_processes()
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
    return render_template("index.html", macaddresses=macaddresses, mac_labels=mac_labels)


@app.route("/status")
def status():
    now = time.time()
    with state_lock:
        devices = {}
        for mac in macaddresses:
            last_success = device_last_success[mac]
            since = now - last_success if last_success else None
            devices[mac] = {
                "present": device_states[mac],
                "failures": device_failure_counts[mac],
                "last_success": last_success,
                "since_last_success": since,
                "last_result": device_last_result[mac],
                "probing": mac == current_probe_target,
            }
        gpio = []
        for info in GPIO_INFO:
            device = info["device"]
            if isinstance(device, Button):
                value = bool(device.is_pressed)
            else:
                try:
                    value = bool(device.value)
                except AttributeError:
                    value = False
            gpio.append(
                {
                    "name": info["name"],
                    "pin": info["pin"],
                    "role": info["role"],
                    "active": value,
                }
            )
        payload = {
            "devices": devices,
            "any_present": devicepresent,
            "current_probe": current_probe_target,
            "system": collect_system_stats(),
            "gpio": gpio,
            "timestamp": now,
        }
    return jsonify(payload)


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
