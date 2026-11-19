"""Lightweight laptop power, network, quiet-hours, and notification helpers."""

from __future__ import annotations

import datetime as dt
import glob
import socket
import subprocess


def is_on_ac_power() -> bool:
    """Return True if any AC / USB-C power supply reports online=1."""
    paths = glob.glob("/sys/class/power_supply/*/online")
    if not paths:
        return True
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                if f.read().strip() == "1":
                    return True
        except OSError:
            continue
    return False


def is_online(timeout: float = 1.5) -> bool:
    """Return True if internet connectivity is available."""
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def should_spare_oled(screens: list[str], verdict: str) -> bool:
    """Return True if eDP is the only active screen and the user is idle."""
    if len(screens) == 1 and screens[0].startswith("eDP"):
        return verdict == "IDLE"
    return False


def is_quiet_hours(
    now: dt.datetime | None = None,
    quiet_start_hour: int = 21,
    quiet_end_hour: int = 8,
) -> bool:
    """Return True during quiet hours (default 21:00 -> 08:00)."""
    current = now or dt.datetime.now()
    hour = current.hour
    if quiet_start_hour > quiet_end_hour:
        return hour >= quiet_start_hour or hour < quiet_end_hour
    return quiet_start_hour <= hour < quiet_end_hour


def send_notification(title: str, body: str, urgency: str = "normal") -> bool:
    """Send a persistent desktop notification via notify-send (stays until dismissed)."""
    try:
        subprocess.run(
            [
                "notify-send",
                "--app-name=Focus Guardian",
                f"--urgency={urgency}",
                "--expire-time=0",
                "--icon=appointment-soon",
                title,
                body,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False
