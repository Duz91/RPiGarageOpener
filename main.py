import bluetooth
import RPi.GPIO as GPIO
import time
import subprocess
import threading
from flask import Flask, render_template, request

app = Flask(__name__)

# Konfiguration (wie zuvor)
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

# GPIO Setup (wie zuvor)
GPIO.setmode(GPIO.BCM)
GPIO.setup(BUTTONPIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(LEDPIN, GPIO.OUT)
GPIO.setup(RELAYPIN, GPIO.OUT)
GPIO.setup(BUZZERPIN, GPIO.OUT)

# Globale Variablen (wie zuvor)
presence = False
last_presence_time = 0

# Funktionen (check_bluetooth_device, beep, open_garage - wie zuvor)
def check_bluetooth_device(mac_address):
    try:
        # Verwende 'l2ping' für zuverlässigere Ergebnisse (benötigt root-Rechte)
        result = subprocess.run(['sudo', 'l2ping', '-c', '1', mac_address], capture_output=True, text=True)
        return "bytes from" in result.stdout
    except Exception as e:
        print(f"Fehler beim Pingen von {mac_address}: {e}")
        return False

# Funktion zum Abspielen eines Signaltons
def beep(duration, count):
    for _ in range(count):
        GPIO.output(BUZZERPIN, GPIO.HIGH)
        time.sleep(duration)
        GPIO.output(BUZZERPIN, GPIO.LOW)
        time.sleep(duration)

# Funktion zum Schalten des Relais
def open_garage():
    print("Garage wird geöffnet!")
    GPIO.output(RELAYPIN, GPIO.HIGH)
    time.sleep(relayclosetime)
    GPIO.output(RELAYPIN, GPIO.LOW)

# Flask Routen
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/open_garage", methods=["POST"])
def open_garage_route():
    open_garage()
    return "Garage geöffnet!", 200

# Button-Überwachungsfunktion (Hardware Button)
def button_monitor():
    while True:
        if GPIO.input(BUTTONPIN) == GPIO.LOW:
            time.sleep(buttonbouncetime)  # Entprellen
            if GPIO.input(BUTTONPIN) == GPIO.LOW:
                open_garage()
                while GPIO.input(BUTTONPIN) == GPIO.LOW:  # Warte bis der Button losgelassen wird
                    time.sleep(0.1)
        time.sleep(0.1)  # Kurze Pause, um CPU-Last zu reduzieren

# Hauptschleife (Bluetooth-Scan)
def main_loop():
    global presence, last_presence_time
    try:
        while True:
            # Bluetooth-Scan (wie zuvor)
            nearby_devices = []
            for mac in macaddresses:
                if check_bluetooth_device(mac):
                    nearby_devices.append(mac)

            if nearby_devices:
                if not presence:
                    print("Presence erkannt!")
                    presence = True
                    beep(presencebeepduration, presencebeepcount)
                last_presence_time = time.time()
            else:
                if presence and time.time() - last_presence_time > absenceinterval:
                    print("Absence erkannt!")
                    presence = False
                    beep(absencebeepduration, absencebeepcount)

            # Wartezeit
            time.sleep(scaninterval)

    except KeyboardInterrupt:
        print("Programm beendet.")
    finally:
        GPIO.cleanup()

# Starte die Threads
if __name__ == "__main__":
    threading.Thread(target=main_loop, daemon=True).start()
    threading.Thread(target=button_monitor, daemon=True).start()  # Starte den Button-Monitor-Thread
    app.run(debug=True, host="0.0.0.0", port=5000)