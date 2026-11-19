#!/usr/bin/env python3
"""Focus Guardian: lightweight Google Calendar/Tasks focus monitor for KDE laptops."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import CONFIG_PATH, CREDENTIALS_PATH, DATA_DIR, GuardianConfig, TOKEN_PATH
from env_checks import is_on_ac_power, is_online, is_quiet_hours, send_notification
from evaluator import evaluate_recent_activity
from google_client import fetch_google_state_cached, run_oauth_flow, write_agenda_snapshot

SERVICE_NAME = "focus-guardian.service"
STATE_PATH = DATA_DIR / "state.json"
SNOOZE_PATH = DATA_DIR / "snooze.json"
_RUNNING = True


def _load_cached_tasks_events() -> tuple[list, list]:
    from config import CACHE_PATH
    from google_client import CalendarEvent, PendingTask

    if not CACHE_PATH.exists():
        return [], []
    try:
        raw = json.loads(CACHE_PATH.read_text())
        events = [CalendarEvent(**e) for e in raw.get("events", [])]
        tasks = [PendingTask(**t) for t in raw.get("tasks", [])]
        return events, tasks
    except (OSError, json.JSONDecodeError, TypeError):
        return [], []


def _write_state(verdict: str, reason: str, tasks: list | None = None, events: list | None = None) -> None:
    if tasks is None and events is None:
        events, tasks = _load_cached_tasks_events()
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(
                {
                    "verdict": verdict,
                    "reason": reason,
                    "tasks": [t.__dict__ for t in (tasks or [])],
                    "events": [e.__dict__ for e in (events or [])],
                    "updated_at": time.time(),
                },
                indent=2,
            )
        )
    except OSError:
        pass


def _handle_signal(_signum: int, _frame: object) -> None:
    global _RUNNING
    _RUNNING = False


def get_active_snooze(now: dt.datetime | None = None) -> tuple[dt.datetime, str] | None:
    """Return (until_dt, reason) if a snooze is currently active, else None."""
    if not SNOOZE_PATH.exists():
        return None
    try:
        raw = json.loads(SNOOZE_PATH.read_text())
        until_ts = raw.get("until")
        if until_ts is not None:
            until_dt = dt.datetime.fromtimestamp(float(until_ts))
        else:
            until_dt = dt.datetime.fromisoformat(str(raw["until_iso"]))
        reason = str(raw.get("reason") or "")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

    ref = now or dt.datetime.now()
    if ref < until_dt:
        return until_dt, reason

    try:
        SNOOZE_PATH.unlink()
    except OSError:
        pass
    return None


def set_snooze(until: dt.datetime, reason: str = "") -> Path:
    """Persist a snooze until the given local datetime and update state."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNOOZE_PATH.write_text(
        json.dumps(
            {
                "until": until.timestamp(),
                "until_iso": until.isoformat(timespec="seconds"),
                "reason": reason,
            },
            indent=2,
        )
    )
    until_str = until.strftime("%H:%M")
    label = f"Until {until_str}" + (f" ({reason})" if reason else "")
    _write_state("SNOOZED", label)
    return SNOOZE_PATH


def clear_snooze() -> bool:
    """Remove any active snooze file. Returns True if one existed."""
    if not SNOOZE_PATH.exists():
        return False
    SNOOZE_PATH.unlink()
    return True


def _parse_snooze_until(until_str: str, now: dt.datetime | None = None) -> dt.datetime:
    """Parse HH:MM or ISO datetime into a future local datetime."""
    ref = now or dt.datetime.now()
    text = until_str.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = dt.datetime.strptime(text, fmt).time()
            target = dt.datetime.combine(ref.date(), t)
            if target <= ref:
                target += dt.timedelta(days=1)
            return target
        except ValueError:
            pass
    return dt.datetime.fromisoformat(text)


def run_single_check(cfg: GuardianConfig, notify: bool = True, verbose: bool = False) -> str:
    """Run one lightweight check cycle and return a human-readable status string."""
    now = dt.datetime.now()

    snooze = get_active_snooze(now)
    if snooze is not None:
        until_dt, reason = snooze
        until_str = until_dt.strftime("%H:%M")
        label = f"Until {until_str}" + (f" ({reason})" if reason else "")
        msg = f"SKIP: Snoozed ({label})"
        _write_state("SNOOZED", label)
        if verbose:
            print(msg)
        return msg

    if is_quiet_hours(now, cfg.quiet_start_hour, cfg.quiet_end_hour):
        msg = f"SKIP: Quiet hours ({cfg.quiet_start_hour:02d}:00-{cfg.quiet_end_hour:02d}:00)"
        _write_state("QUIET_HOURS", f"{cfg.quiet_start_hour:02d}:00-{cfg.quiet_end_hour:02d}:00")
        if verbose:
            print(msg)
        return msg

    if not is_on_ac_power():
        msg = "SKIP: On battery power (saving battery)"
        _write_state("ON_BATTERY", "Plug in charger to resume checks")
        if verbose:
            print(msg)
        return msg

    if not is_online():
        msg = "SKIP: Offline"
        _write_state("OFFLINE", "No internet connection")
        if verbose:
            print(msg)
        return msg

    try:
        events, tasks = fetch_google_state_cached(cache_ttl_sec=cfg.check_interval_sec)
    except Exception as exc:
        msg = f"SKIP: Google API unavailable ({exc})"
        _write_state("UNAUTHENTICATED", str(exc))
        if verbose:
            print(msg, file=sys.stderr)
        return msg

    if events:
        names = ", ".join(ev.summary for ev in events[:2])
        msg = f"SKIP: In scheduled event ({names})"
        _write_state("IN_EVENT", names, tasks=tasks, events=events)
        if verbose:
            print(msg)
        return msg

    ev = evaluate_recent_activity(cfg, tasks, window_sec=cfg.check_interval_sec)
    if ev.verdict == "IDLE":
        msg = f"IDLE: {ev.matched_reason} (active={ev.total_active_sec:.0f}s)"
        _write_state("IDLE", ev.matched_reason, tasks=tasks, events=events)
        if verbose:
            print(msg)
        return msg

    if ev.verdict == "FOCUSED":
        msg = f"FOCUSED: {ev.matched_reason} ({ev.productive_sec:.0f}s / {ev.total_active_sec:.0f}s)"
        _write_state("FOCUSED", ev.matched_reason, tasks=tasks, events=events)
        if verbose:
            print(msg)
        return msg

    # DISTRACTED
    _write_state("DISTRACTED", ev.top_distraction or "Unproductive activity", tasks=tasks, events=events)
    if tasks:
        top_tasks = "; ".join(t.title for t in tasks[:2])
        body = (
            f"Drifted to: {ev.top_distraction or 'unproductive activity'}\n"
            f"Pending tasks: {top_tasks}\n"
            f"(Allowed: Analysis or Discrete Mathematics)"
        )
    else:
        body = (
            f"Drifted to: {ev.top_distraction or 'unproductive activity'}\n"
            f"Allowed: Learning Analysis or Discrete Mathematics"
        )

    if notify:
        send_notification("Focus Check: Attention Drifted", body)

    msg = f"DISTRACTED: {ev.top_distraction} -> notified={notify}"
    if verbose:
        print(msg)
    return msg


def cmd_run(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    cfg = GuardianConfig.load()

    note_proc: subprocess.Popen | None = None
    has_display = bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
    if not args.no_note and has_display:
        note_script = Path(__file__).resolve().parent / "sticky_note.py"
        if note_script.exists():
            note_proc = subprocess.Popen([sys.executable, str(note_script)])

    if args.verbose:
        print(f"Focus Guardian started (interval={cfg.check_interval_sec}s)")

    try:
        tick = 0
        while _RUNNING:
            run_single_check(cfg, notify=True, verbose=args.verbose)
            if tick % 3 == 0 and is_online():
                try:
                    write_agenda_snapshot()
                except Exception:
                    pass
            tick += 1
            # Sleep in 1s chunks for responsive SIGINT/SIGTERM exit
            for _ in range(cfg.check_interval_sec):
                if not _RUNNING:
                    break
                time.sleep(1)
    finally:
        if note_proc is not None and note_proc.poll() is None:
            note_proc.terminate()
            try:
                note_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                note_proc.kill()

    return 0


def cmd_sticky_note(_args: argparse.Namespace) -> int:
    from sticky_note import main as note_main

    return note_main()


def cmd_status(_args: argparse.Namespace) -> int:
    cfg = GuardianConfig.load()
    snooze = get_active_snooze()
    if snooze is not None:
        until_dt, reason = snooze
        snooze_str = f"Yes (until {until_dt.strftime('%Y-%m-%d %H:%M')}" + (f" - {reason}" if reason else "") + ")"
    else:
        snooze_str = "No"
    print(f"Snoozed:      {snooze_str}")
    print(f"AC Power:     {'Yes' if is_on_ac_power() else 'No (on battery)'}")
    print(f"Online:       {'Yes' if is_online() else 'No'}")
    print(f"Quiet Hours:  {'Yes' if is_quiet_hours(quiet_start_hour=cfg.quiet_start_hour, quiet_end_hour=cfg.quiet_end_hour) else 'No'} ({cfg.quiet_start_hour:02d}:00-{cfg.quiet_end_hour:02d}:00)")
    print(f"Credentials:  {'Present' if CREDENTIALS_PATH.exists() else f'Missing ({CREDENTIALS_PATH})'}")
    print(f"OAuth Token:  {'Present' if TOKEN_PATH.exists() else f'Missing ({TOKEN_PATH})'}")
    print(f"Activity DB:  {'Present' if cfg.activity_db_path.exists() else 'Missing'} ({cfg.activity_db_path})")

    if TOKEN_PATH.exists() and is_online():
        try:
            events, tasks = fetch_google_state_cached(cache_ttl_sec=60)
            print(f"Active Events ({len(events)}):")
            for ev in events:
                print(f"  - {ev.summary} ({ev.calendar})")
            print(f"Pending Tasks ({len(tasks)}):")
            for t in tasks[:10]:
                print(f"  - [{t.tasklist}] {t.title}")
            ev = evaluate_recent_activity(cfg, tasks, window_sec=cfg.check_interval_sec)
            print(f"Last 5m Eval: {ev.verdict} ({ev.productive_sec:.0f}s / {ev.total_active_sec:.0f}s) - {ev.matched_reason}")
        except Exception as exc:
            print(f"Google Sync Error: {exc}")
    return 0


def cmd_check_once(args: argparse.Namespace) -> int:
    cfg = GuardianConfig.load()
    res = run_single_check(cfg, notify=args.notify, verbose=True)
    return 0 if res else 1


def cmd_auth(_args: argparse.Namespace) -> int:
    run_oauth_flow()
    return 0


def cmd_sync_agenda(_args: argparse.Namespace) -> int:
    path = write_agenda_snapshot()
    print(f"Synced agenda snapshot to {path}")
    return 0


def cmd_snooze(args: argparse.Namespace) -> int:
    now = dt.datetime.now()
    if args.until:
        until = _parse_snooze_until(args.until, now=now)
    elif args.minutes is not None:
        if args.minutes <= 0:
            print("Error: --minutes must be positive", file=sys.stderr)
            return 2
        until = now + dt.timedelta(minutes=args.minutes)
    else:
        until = now + dt.timedelta(minutes=60)

    if until <= now:
        print(f"Error: snooze time {until.isoformat(timespec='seconds')} is not in the future", file=sys.stderr)
        return 2

    reason = (args.reason or "").strip()
    set_snooze(until, reason=reason)
    suffix = f" ({reason})" if reason else ""
    print(f"Snoozed Focus Guardian until {until.strftime('%Y-%m-%d %H:%M')}{suffix}")
    return 0


def cmd_unsnooze(_args: argparse.Namespace) -> int:
    if clear_snooze():
        print("Focus Guardian snooze cleared")
    else:
        print("Focus Guardian was not snoozed")
    return 0


def cmd_install_service(_args: argparse.Namespace) -> int:
    cli_path = Path(__file__).resolve()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / SERVICE_NAME

    unit = f"""[Unit]
Description=Focus Guardian (Google Calendar/Tasks & Focus Notifier)
After=graphical-session.target network-online.target

[Service]
Type=simple
ExecStart={sys.executable} {cli_path} run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
"""
    unit_path.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], check=True)
    print(f"Installed and started {unit_path}")
    return 0


def cmd_uninstall_service(_args: argparse.Namespace) -> int:
    unit_path = Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME
    subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], stderr=subprocess.DEVNULL)
    if unit_path.exists():
        unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], stderr=subprocess.DEVNULL)
    print(f"Uninstalled {SERVICE_NAME}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="focus-guardian",
        description="Lightweight Google Calendar/Tasks focus monitor.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run the 5-minute focus monitor loop")
    p_run.add_argument("-v", "--verbose", action="store_true")
    p_run.add_argument("--no-note", action="store_true", help="Do not launch desktop sticky note")
    p_run.set_defaults(func=cmd_run)

    p_note = sub.add_parser("sticky-note", help="Launch the persistent desktop sticky note widget")
    p_note.set_defaults(func=cmd_sticky_note)

    p_chk = sub.add_parser("check-once", help="Run one evaluation right now")
    p_chk.add_argument("--notify", action="store_true", help="Send desktop notification if distracted")
    p_chk.set_defaults(func=cmd_check_once)

    p_st = sub.add_parser("status", help="Show power, network, calendar, tasks, and focus state")
    p_st.set_defaults(func=cmd_status)

    p_auth = sub.add_parser("auth", help="Authorize with Google Calendar & Tasks via OAuth2")
    p_auth.set_defaults(func=cmd_auth)

    p_sync = sub.add_parser("sync-agenda", help="Sync 37-day Google Calendar & Tasks snapshot for KDE widget")
    p_sync.set_defaults(func=cmd_sync_agenda)

    p_snooze = sub.add_parser("snooze", help="Silence notifications until a given time or duration")
    p_snooze_grp = p_snooze.add_mutually_exclusive_group()
    p_snooze_grp.add_argument("--until", help="Local time (HH:MM) or ISO datetime to snooze until")
    p_snooze_grp.add_argument("-m", "--minutes", type=int, help="Minutes to snooze for (default: 60)")
    p_snooze.add_argument("-r", "--reason", default="", help="Optional reason shown in status/sticky note")
    p_snooze.set_defaults(func=cmd_snooze)

    p_unsnooze = sub.add_parser("unsnooze", help="Clear an active snooze immediately")
    p_unsnooze.set_defaults(func=cmd_unsnooze)

    p_inst = sub.add_parser("install-service", help="Install & start systemd user service")
    p_inst.set_defaults(func=cmd_install_service)

    p_uninst = sub.add_parser("uninstall-service", help="Stop & remove systemd user service")
    p_uninst.set_defaults(func=cmd_uninstall_service)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
