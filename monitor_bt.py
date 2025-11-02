#!/usr/bin/env python3
"""
Diagnose-Skript für Bluetooth-/Systemzustand.

Logs werden in logs/monitor.log rotierend geschrieben. Jede Minute werden:
 - Systemlast, CPU-Zeit, RSS und Threadzahl des Prozesses festgehalten
 - Aktive hcitool-Prozesse (inkl. Status/Zombies) gelistet
 - hci-Statistiken (hciconfig hci0 stats & hciconfig hci0) gesichert
 - Bluetoothcontroller-Status (bluetoothctl show) abgefragt
 - Aktuelle Verbindungen (hcitool con) protokolliert
 - Alle 5 Intervalle zusätzlich die letzten dmesg-Zeilen ergänzt
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import resource
import subprocess
import threading
import time
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "monitor.log"

INTERVAL_SECONDS = 60
DMESG_INTERVAL = 5  # jede fünfte Runde dmesg protokollieren


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("bt-monitor")
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


LOGGER = setup_logger()


def run_command(command: list[str] | str, *, timeout: float | None = None, shell: bool = False) -> str:
    cmd_display = command if isinstance(command, str) else " ".join(command)
    try:
        return subprocess.check_output(
            command,
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            shell=shell,
        )
    except subprocess.CalledProcessError as exc:
        return exc.output
    except FileNotFoundError:
        return f"Befehl nicht verfügbar: {cmd_display}"
    except Exception as exc:  # pragma: no cover
        return f"Fehler bei {cmd_display}: {exc}"


def collect_system_stats() -> dict[str, float]:
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
        "uptime": time.time(),
    }


def log_hcitool_processes() -> None:
    output = run_command(["ps", "-eo", "pid,stat,pcpu,pmem,comm"])
    lines = output.strip().splitlines()
    total = 0
    running = 0
    zombies = 0
    details: list[str] = []
    for line in lines[1:]:
        if "hcitool" not in line:
            continue
        total += 1
        parts = line.split(maxsplit=4)
        stat = parts[1] if len(parts) > 1 else ""
        cpu = parts[2] if len(parts) > 2 else "?"
        mem = parts[3] if len(parts) > 3 else "?"
        if "Z" in stat:
            zombies += 1
        elif stat.startswith("R") or stat.startswith("D"):
            running += 1
        details.append(f"{line.strip()}")

    if total:
        level = logging.WARNING if zombies or running > 1 else logging.INFO
        LOGGER.log(
            level,
            "hcitool Prozesse aktiv: %d (running: %d, zombies: %d)",
            total,
            running,
            zombies,
        )
        for entry in details:
            LOGGER.debug("    %s", entry)
    else:
        LOGGER.debug("Keine hcitool Prozesse gefunden")


def log_bluetooth_stats() -> None:
    LOGGER.info(
        "hciconfig hci0 stats:\n%s",
        run_command(["hciconfig", "hci0", "stats"], timeout=5).strip(),
    )
    LOGGER.debug(
        "hciconfig hci0:\n%s",
        run_command(["hciconfig", "hci0"], timeout=5).strip(),
    )
    LOGGER.debug(
        "hcitool con:\n%s",
        run_command(["hcitool", "con"], timeout=5).strip(),
    )
    LOGGER.debug(
        "bluetoothctl show:\n%s",
        run_command(["bluetoothctl", "show"], timeout=5).strip(),
    )


def log_dmesg_tail() -> None:
    LOGGER.warning(
        "dmesg (letzte 20 Zeilen):\n%s",
        run_command("dmesg -T | tail -n 20", timeout=5, shell=True),
    )


def log_system_stats() -> None:
    stats = collect_system_stats()
    LOGGER.info(
        "Systemstats: load=%.2f/%.2f/%.2f, cpu_utime=%.2fs, rss=%.1fMB, threads=%d",
        stats["load1"],
        stats["load5"],
        stats["load15"],
        stats["cpu_utime"],
        stats["rss_mb"],
        stats["threads"],
    )


def monitor_loop() -> None:
    counter = 0
    LOGGER.info("Starte Bluetooth/ System Monitor – Logdatei: %s", LOG_FILE)
    while True:
        counter += 1
        try:
            log_system_stats()
            log_hcitool_processes()
            log_bluetooth_stats()
            if counter % DMESG_INTERVAL == 0:
                log_dmesg_tail()
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("Monitor-Fehler: %s", exc)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        monitor_loop()
    except KeyboardInterrupt:
        LOGGER.info("Monitor beendet (KeyboardInterrupt).")
