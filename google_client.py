"""Stdlib-only Google Calendar & Google Tasks API client (plus optional iCal support)."""

from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import CACHE_PATH, CONFIG_DIR, CREDENTIALS_PATH, DATA_DIR, TOKEN_PATH

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass
class CalendarEvent:
    summary: str
    start: str
    end: str
    calendar: str = "primary"


@dataclass
class PendingTask:
    title: str
    notes: str = ""
    due: str = ""
    tasklist: str = ""


def _load_client_config() -> dict[str, str]:
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Missing Google OAuth credentials at {CREDENTIALS_PATH}. "
            "Download a Desktop App OAuth2 JSON from Google Cloud Console and save it there."
        )
    raw = json.loads(CREDENTIALS_PATH.read_text())
    section = raw.get("installed") or raw.get("web")
    if not section:
        raise ValueError("Invalid credentials.json: expected 'installed' or 'web' key")
    return {
        "client_id": section["client_id"],
        "client_secret": section["client_secret"],
        "auth_uri": section.get("auth_uri", AUTH_URL),
        "token_uri": section.get("token_uri", TOKEN_URL),
    }


def run_oauth_flow() -> None:
    """Interactive browser OAuth2 Desktop flow saving token.json."""
    cfg = _load_client_config()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    auth_code: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs:
                auth_code["code"] = qs["code"][0]
                body = b"<html><body><h2>Focus Guardian authorized.</h2><p>You can close this tab.</p></body></html>"
                self.send_response(200)
            else:
                auth_code["error"] = qs.get("error", ["unknown"])[0]
                body = b"<html><body><h2>Authorization failed.</h2></body></html>"
                self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"

    params = urllib.parse.urlencode(
        {
            "client_id": cfg["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    url = f"{cfg['auth_uri']}?{params}"
    print(f"Opening browser for Google authorization:\n{url}")
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    while "code" not in auth_code and "error" not in auth_code:
        server.handle_request()
    server.server_close()

    if "error" in auth_code:
        raise RuntimeError(f"OAuth authorization failed: {auth_code['error']}")

    token_payload = urllib.parse.urlencode(
        {
            "code": auth_code["code"],
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        cfg["token_uri"],
        data=token_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        token_data = json.loads(resp.read().decode("utf-8"))

    token_data["expires_at"] = time.time() + float(token_data.get("expires_in", 3600)) - 60
    TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
    os.chmod(TOKEN_PATH, 0o600)
    print(f"Saved OAuth token to {TOKEN_PATH}")


def _get_access_token() -> str:
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Not authenticated yet ({TOKEN_PATH} missing). Run: focus-guardian auth"
        )
    token_data = json.loads(TOKEN_PATH.read_text())
    if time.time() < float(token_data.get("expires_at", 0)):
        return token_data["access_token"]

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh_token in token.json; re-run `focus-guardian auth`")

    cfg = _load_client_config()
    payload = urllib.parse.urlencode(
        {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        cfg["token_uri"],
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        refreshed = json.loads(resp.read().decode("utf-8"))

    token_data["access_token"] = refreshed["access_token"]
    token_data["expires_at"] = time.time() + float(refreshed.get("expires_in", 3600)) - 60
    TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
    os.chmod(TOKEN_PATH, 0o600)
    return token_data["access_token"]


def _api_get(url: str, access_token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_active_events(now: dt.datetime | None = None) -> list[CalendarEvent]:
    """Return all currently ongoing busy Google Calendar events across user's calendars."""
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    time_min = (current - dt.timedelta(minutes=1)).isoformat()
    time_max = (current + dt.timedelta(minutes=1)).isoformat()
    token = _get_access_token()

    cal_list = _api_get("https://www.googleapis.com/calendar/v3/users/me/calendarList", token)
    active: list[CalendarEvent] = []

    for cal in cal_list.get("items", []):
        cal_id = cal.get("id")
        if not cal_id:
            continue
        cal_name = cal.get("summary", cal_id)
        qs = urllib.parse.urlencode(
            {
                "singleEvents": "true",
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": "20",
            }
        )
        encoded_id = urllib.parse.quote(cal_id, safe="")
        data = _api_get(
            f"https://www.googleapis.com/calendar/v3/calendars/{encoded_id}/events?{qs}",
            token,
        )
        for ev in data.get("items", []):
            if ev.get("status") == "cancelled":
                continue
            if ev.get("transparency") == "transparent":
                continue
            start_obj = ev.get("start", {})
            end_obj = ev.get("end", {})
            # Only treat timed events (dateTime) as blocking classes/seminars/tutorials
            if "dateTime" not in start_obj or "dateTime" not in end_obj:
                continue
            start_dt = dt.datetime.fromisoformat(start_obj["dateTime"])
            end_dt = dt.datetime.fromisoformat(end_obj["dateTime"])
            if start_dt <= current <= end_dt:
                active.append(
                    CalendarEvent(
                        summary=ev.get("summary", "(Busy event)"),
                        start=start_obj["dateTime"],
                        end=end_obj["dateTime"],
                        calendar=cal_name,
                    )
                )
    return active


def fetch_agenda_events(days_back: int = 7, days_ahead: int = 30) -> list[dict[str, str]]:
    """Fetch timed and all-day Google Calendar events across a multi-week window."""
    now = dt.datetime.now(dt.timezone.utc)
    time_min = (now - dt.timedelta(days=days_back)).isoformat()
    time_max = (now + dt.timedelta(days=days_ahead)).isoformat()
    token = _get_access_token()

    cal_list = _api_get("https://www.googleapis.com/calendar/v3/users/me/calendarList", token)
    agenda: list[dict[str, str]] = []

    for cal in cal_list.get("items", []):
        cal_id = cal.get("id")
        if not cal_id:
            continue
        cal_name = cal.get("summary", cal_id)
        cal_color = cal.get("backgroundColor", "#4c9aff")
        qs = urllib.parse.urlencode(
            {
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": "250",
            }
        )
        encoded_id = urllib.parse.quote(cal_id, safe="")
        data = _api_get(
            f"https://www.googleapis.com/calendar/v3/calendars/{encoded_id}/events?{qs}",
            token,
        )
        for ev in data.get("items", []):
            if ev.get("status") == "cancelled":
                continue
            start_obj = ev.get("start", {})
            end_obj = ev.get("end", {})
            start_raw = start_obj.get("dateTime") or start_obj.get("date") or ""
            end_raw = end_obj.get("dateTime") or end_obj.get("date") or ""
            if not start_raw:
                continue
            is_all_day = "date" in start_obj and "dateTime" not in start_obj
            if "T" in start_raw:
                local_start = dt.datetime.fromisoformat(start_raw).astimezone()
                local_end = dt.datetime.fromisoformat(end_raw).astimezone() if end_raw else local_start
                date_str = local_start.strftime("%Y-%m-%d")
                time_str = f"{local_start.strftime('%H:%M')}–{local_end.strftime('%H:%M')}"
            else:
                date_str = start_raw[:10]
                time_str = "All day"

            agenda.append(
                {
                    "date": date_str,
                    "time": time_str,
                    "start": start_raw,
                    "end": end_raw,
                    "summary": ev.get("summary", "(Event)"),
                    "location": ev.get("location", ""),
                    "calendar": cal_name,
                    "color": cal_color,
                    "all_day": "true" if is_all_day else "false",
                }
            )

    agenda.sort(key=lambda e: (e["date"], e["start"]))
    return agenda


def write_agenda_snapshot() -> Path:
    """Write a 37-day agenda + pending tasks JSON snapshot for the KDE Plasma calendar widget."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    agenda_path = DATA_DIR / "agenda.json"
    events = fetch_agenda_events()
    tasks = fetch_pending_tasks()
    payload = {
        "updated_at": time.time(),
        "events": events,
        "tasks": [t.__dict__ for t in tasks],
    }
    agenda_path.write_text(json.dumps(payload, indent=2))
    return agenda_path


def fetch_pending_tasks() -> list[PendingTask]:
    """Return all not-done Google Tasks across all task lists."""
    token = _get_access_token()
    lists_resp = _api_get("https://tasks.googleapis.com/tasks/v1/users/@me/lists", token)
    pending: list[PendingTask] = []

    for tl in lists_resp.get("items", []):
        tl_id = tl.get("id")
        if not tl_id:
            continue
        tl_name = tl.get("title", "Tasks")
        qs = urllib.parse.urlencode(
            {
                "showCompleted": "false",
                "showHidden": "false",
                "maxResults": "100",
            }
        )
        data = _api_get(
            f"https://tasks.googleapis.com/tasks/v1/lists/{urllib.parse.quote(tl_id, safe='')}/tasks?{qs}",
            token,
        )
        for t in data.get("items", []):
            if t.get("status") != "needsAction":
                continue
            title = (t.get("title") or "").strip()
            if not title:
                continue
            pending.append(
                PendingTask(
                    title=title,
                    notes=(t.get("notes") or "").strip(),
                    due=t.get("due", ""),
                    tasklist=tl_name,
                )
            )
    return pending


def fetch_google_state_cached(cache_ttl_sec: int = 300) -> tuple[list[CalendarEvent], list[PendingTask]]:
    """Fetch active events and pending tasks, using a short-lived local cache to save battery/network."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_PATH.exists():
        try:
            raw = json.loads(CACHE_PATH.read_text())
            if time.time() - float(raw.get("fetched_at", 0)) < cache_ttl_sec:
                events = [CalendarEvent(**e) for e in raw.get("events", [])]
                tasks = [PendingTask(**t) for t in raw.get("tasks", [])]
                return events, tasks
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    events = fetch_active_events()
    tasks = fetch_pending_tasks()
    try:
        CACHE_PATH.write_text(
            json.dumps(
                {
                    "fetched_at": time.time(),
                    "events": [e.__dict__ for e in events],
                    "tasks": [t.__dict__ for t in tasks],
                },
                indent=2,
            )
        )
    except OSError:
        pass
    return events, tasks
