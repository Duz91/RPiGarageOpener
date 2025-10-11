import subprocess
import time
from gpiozero import Button, LED, OutputDevice
from flask import Flask, render_template, request, redirect, url_for
import threading
import json

# Settingfile definieren
SETTINGS_FILE = "settings.json"

# GPIO-Pins definieren!
BUTTON_PIN = 5
LED_PIN = 23
RELAY_PIN = 26
BUZZER_PIN = 19

# Standardwerte für die Parameter
mac_addresses = [
    "0C:15:63:DF:61:2F",
    "80:04:5F:A2:66:57"
]
scan_interval = 7
absence_interval = 15
relay_close_time = 0.5
presence_beep_duration = 0.1
presence_beep_count = 2
absence_beep_duration = 0.1
absence_beep_count = 2
button_bounce_time = 0.2  # Entprellzeit für den Button (in Sekunden)
presence_led_blink_interval = 0.7  # Blinkintervall der LED bei Anwesenheit (in Sekunden)
absence_led_blink_interval = 1.2   # Blinkintervall der LED bei Abwesenheit (in Sekunden)

# Flask-App initialisieren
app = Flask(__name__)
device_present = False

# GPIO-Komponenten initialisieren (Button wird später dynamisch erstellt)
led = LED(LED_PIN)                        # LED-Steuerung
relay = OutputDevice(RELAY_PIN, active_high=False)  # Relais invertiert (LOW aktiviert)
buzzer = OutputDevice(BUZZER_PIN, active_high=False)  # Buzzer invertiert (LOW aktiviert)
button = None


# Funktion zum auslesen der Settings im JSON File
def load_settings():
    global mac_addresses, scan_interval, absence_interval, relay_close_time
    global presence_beep_duration, presence_beep_count, absence_beep_duration, absence_beep_count
    global button_bounce_time, presence_led_blink_interval, absence_led_blink_interval

    try:
        with open(SETTINGS_FILE, 'r') as file:
            settings = json.load(file)
            mac_addresses = settings.get('mac_addresses', mac_addresses)
            scan_interval = settings.get('scan_interval', scan_interval)
            absence_interval = settings.get('absence_interval', absence_interval)
            relay_close_time = settings.get('relay_close_time', relay_close_time)
            presence_beep_duration = settings.get('presence_beep_duration', presence_beep_duration)
            presence_beep_count = settings.get('presence_beep_count', presence_beep_count)
            absence_beep_duration = settings.get('absence_beep_duration', absence_beep_duration)
            absence_beep_count = settings.get('absence_beep_count', absence_beep_count)
            button_bounce_time = settings.get('button_bounce_time', button_bounce_time)
            presence_led_blink_interval = settings.get('presence_led_blink_interval', presence_led_blink_interval)
            absence_led_blink_interval = settings.get('absence_led_blink_interval', absence_led_blink_interval)
    except FileNotFoundError:
        print("Settings file not found. Using default settings.")
    except json.JSONDecodeError:
        print("Error reading settings file. Using default settings.")


# Funktion zum auslesen der Settings im JSON File
def save_settings():
    settings = {
        'mac_addresses': mac_addresses,
        'scan_interval': scan_interval,
        'absence_interval': absence_interval,
        'relay_close_time': relay_close_time,
        'presence_beep_duration': presence_beep_duration,
        'presence_beep_count': presence_beep_count,
        'absence_beep_duration': absence_beep_duration,
        'absence_beep_count': absence_beep_count,
        'button_bounce_time': button_bounce_time,
        'presence_led_blink_interval': presence_led_blink_interval,
        'absence_led_blink_interval': absence_led_blink_interval
    }

    with open(SETTINGS_FILE, 'w') as file:
        json.dump(settings, file, indent=4)

# Funktion zum direkten Abfragen eines Geräts mit hcitool name
def check_device_name(mac_address):
    try:
        result = subprocess.run(['hcitool', 'name', mac_address], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        device_name = result.stdout.strip()
        if device_name:
            print(f"Device {mac_address} is present with name: {device_name}")
            return True
        else:
            print(f"Device {mac_address} is absent.")
            return False
    except Exception as e:
        print(f"Error checking device {mac_address}: {e}")
        return False

# Funktion zum Piepsen (invertiert)
def beep(times, duration):
    for _ in range(times):
        buzzer.on()  # Buzzer AN (invertiert)
        time.sleep(duration)
        buzzer.off()  # Buzzer AUS (invertiert)
        time.sleep(duration)

# Callback-Funktion für den Button-Event
def button_pressed():
    global device_present
    print("Button pressed!")
    if device_present:  # Nur bei Anwesenheit eines Geräts aktivieren
        print("Activating relay.")
        relay.on()  # Relais AN (invertiert)
        time.sleep(relay_close_time)
        relay.off()  # Relais AUS (invertiert)
    else:
        print("Device absent. Relay will not activate.")

# Funktion zum Blinken der LED je nach Zustand (Presence/Absence)
def blink_led():
    global device_present
    
    while True:
        if device_present:
            led.blink(on_time=presence_led_blink_interval, off_time=presence_led_blink_interval)
            time.sleep(scan_interval)  # Pause während des Scan-Intervalls
        else:
            led.blink(on_time=absence_led_blink_interval, off_time=absence_led_blink_interval)
            time.sleep(scan_interval)

# Hauptprogramm zur Überwachung der Bluetooth-Geräte und Steuerung der LED/Buzzer-Logik
def main():
    global device_present
    
    last_seen = {mac: None for mac in mac_addresses}
    previous_state = None
    
    while True:
        print("Checking devices using hcitool name...")
        
        device_present = False
        for mac in mac_addresses:
            if check_device_name(mac):
                last_seen[mac] = time.time()
                device_present = True
        
        # Zustandsänderung erkennen und Buzzer auslösen
        if device_present and previous_state != "present":
            print("State changed to PRESENT.")
            beep(presence_beep_count, presence_beep_duration)
            previous_state = "present"
        elif not device_present and previous_state != "absent":
            print("State changed to ABSENT.")
            beep(absence_beep_count, absence_beep_duration)
            previous_state = "absent"

        # Abwesenheitsprüfung basierend auf Zeit seit letztem Kontakt
        for addr in mac_addresses:
            if last_seen[addr] is not None:
                time_since_seen = time.time() - last_seen[addr]
                if time_since_seen > absence_interval:
                    print(f"Device {addr} is absent due to timeout.")
                    device_present = False

        time.sleep(scan_interval)

# Flask-Routen und Funktionen für die Weboberfläche
@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    global mac_addresses, scan_interval, absence_interval, relay_close_time
    global presence_beep_duration, presence_beep_count, absence_beep_duration, absence_beep_count, button_bounce_time
    global presence_led_blink_interval, absence_led_blink_interval
    
    if request.method == 'POST':
        mac_addresses = request.form['mac_addresses'].split(',')
        scan_interval = int(request.form['scan_interval'])
        absence_interval = int(request.form['absence_interval'])
        relay_close_time = float(request.form['relay_close_time'])
        presence_beep_duration = float(request.form['presence_beep_duration'])
        presence_beep_count = int(request.form['presence_beep_count'])
        absence_beep_duration = float(request.form['absence_beep_duration'])
        absence_beep_count = int(request.form['absence_beep_count'])
        button_bounce_time = float(request.form['button_bounce_time'])
        presence_led_blink_interval = float(request.form['presence_led_blink_interval'])
        absence_led_blink_interval = float(request.form['absence_led_blink_interval'])

        # Reinitialize the button with updated debounce time
        global button
        button.close()  # Close the old button (if any)
        button = Button(BUTTON_PIN, pull_up=True, bounce_time=button_bounce_time)
        button.when_pressed = button_pressed

        save_settings()  # Save settings to the JSON file

        return redirect(url_for('settings'))
    
    return render_template('settings.html', 
                           mac_addresses=",".join(mac_addresses),
                           scan_interval=scan_interval,
                           absence_interval=absence_interval,
                           relay_close_time=relay_close_time,
                           presence_beep_duration=presence_beep_duration,
                           presence_beep_count=presence_beep_count,
                           absence_beep_duration=absence_beep_duration,
                           absence_beep_count=absence_beep_count,
                           button_bounce_time=button_bounce_time,
                           presence_led_blink_interval=presence_led_blink_interval,
                           absence_led_blink_interval=absence_led_blink_interval)

@app.route('/activate_relay')
def activate_relay():
    relay.on()  # Relais AN (invertiert)
    time.sleep(relay_close_time)
    relay.off()  # Relais AUS (invertiert)
    return "Relay activated!"

if __name__ == '__main__':
    try:
        load_settings()
        button = Button(BUTTON_PIN, pull_up=True, bounce_time=button_bounce_time)  # Initialize button with debounce time
        button.when_pressed = button_pressed  # Register button event

        threading.Thread(target=main).start()       # Start the main program in a separate thread
        threading.Thread(target=blink_led).start()  # Start LED blinking in a separate thread
        app.run(host='0.0.0.0', port=5000)          # Start the Flask web server
    finally:
        save_settings()
        led.off()
