from flask import Flask, render_template, jsonify
import threading
import time
from gpiozero import Button, LED, OutputDevice
import subprocess

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

app = Flask(__name__)

led = LED(LEDPIN)
relay = OutputDevice(RELAYPIN, active_high=False)
buzzer = OutputDevice(BUZZERPIN, active_high=False)
button = Button(BUTTONPIN, pull_up=True, bounce_time=buttonbouncetime)

def check_device_name(macaddress):
    try:
        result = subprocess.run(["hcitool", "name", macaddress],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True)
        devicename = result.stdout.strip()
        return bool(devicename)
    except Exception:
        return False

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
            led.blink(on_time=presenceledblinkinterval,
                      off_time=presenceledblinkinterval)
        else:
            led.blink(on_time=absenceledblinkinterval,
                      off_time=absenceledblinkinterval)
        time.sleep(scaninterval)

def main_thread():
    global devicepresent
    lastseen = {}
    previousstate = None

    while True:
        now = time.time()
        found_any = False

        # Scan aller MAC-Adressen
        for mac in macaddresses:
            active = check_device_name(mac)
            device_states[mac] = active
            if active:
                lastseen[mac] = now
                found_any = True

        # Alte Einträge entfernen
        for mac, lasttime in list(lastseen.items()):
            if now - lasttime > absenceinterval:
                del lastseen[mac]
                device_states[mac] = False

        devicepresent = len(lastseen) > 0 or found_any

        # Statuswechsel → akustisches Signal
        if devicepresent and previousstate != "present":
            beep(presencebeepcount, presencebeepduration)
            previousstate = "present"
        elif not devicepresent and previousstate != "absent":
            beep(absencebeepcount, absencebeepduration)
            previousstate = "absent"

        time.sleep(scaninterval)

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