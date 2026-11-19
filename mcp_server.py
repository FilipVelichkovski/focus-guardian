#!/usr/bin/env python3
"""Stdio MCP server for Focus Guardian (zero external dependencies)."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

_REPO_DIR = Path(__file__).resolve().parent
if str(_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_DIR))

import cli
from config import CREDENTIALS_PATH, TOKEN_PATH, GuardianConfig
from env_checks import is_on_ac_power, is_online, is_quiet_hours
from evaluator import evaluate_recent_activity
from google_client import fetch_google_state_cached, write_agenda_snapshot

SERVER_INFO = {"name": "focus-guardian", "version": "1.0.0"}
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_status",
        "description": (
            "Get Focus Guardian status: active snooze, AC power, network, quiet hours, "
            "ongoing Google Calendar events, pending Google Tasks, and last 5m activity evaluation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "snooze",
        "description": (
            "Silence Focus Guardian distraction notifications until a given local time (HH:MM or ISO datetime) "
            "or for a number of minutes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "until": {
                    "type": "string",
                    "description": "Local time (HH:MM) or ISO datetime to snooze until.",
                },
                "minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Minutes to snooze for if 'until' is not provided (default: 60).",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason shown in status and desktop sticky note.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "unsnooze",
        "description": "Clear an active Focus Guardian snooze immediately.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "check_once",
        "description": "Run one Focus Guardian evaluation right now and return the verdict.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "notify": {
                    "type": "boolean",
                    "default": False,
                    "description": "Send a desktop notification if the verdict is DISTRACTED.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sync_agenda",
        "description": "Sync the 37-day Google Calendar & Tasks snapshot for the KDE widget.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def _tool_get_status() -> dict[str, Any]:
    cfg = GuardianConfig.load()
    snooze = cli.get_active_snooze()
    status: dict[str, Any] = {
        "snoozed": snooze is not None,
        "snooze_until": snooze[0].isoformat(timespec="seconds") if snooze else None,
        "snooze_reason": snooze[1] if snooze else None,
        "ac_power": is_on_ac_power(),
        "online": is_online(),
        "quiet_hours": is_quiet_hours(
            quiet_start_hour=cfg.quiet_start_hour,
            quiet_end_hour=cfg.quiet_end_hour,
        ),
        "quiet_window": f"{cfg.quiet_start_hour:02d}:00-{cfg.quiet_end_hour:02d}:00",
        "credentials_present": CREDENTIALS_PATH.exists(),
        "token_present": TOKEN_PATH.exists(),
        "activity_db_present": cfg.activity_db_path.exists(),
    }

    if TOKEN_PATH.exists() and status["online"]:
        try:
            events, tasks = fetch_google_state_cached(cache_ttl_sec=60)
            ev = evaluate_recent_activity(cfg, tasks, window_sec=cfg.check_interval_sec)
            status["active_events"] = [e.__dict__ for e in events]
            status["pending_tasks"] = [t.__dict__ for t in tasks[:15]]
            status["last_5m_eval"] = {
                "verdict": ev.verdict,
                "productive_sec": round(ev.productive_sec, 1),
                "total_active_sec": round(ev.total_active_sec, 1),
                "matched_reason": ev.matched_reason,
                "top_distraction": ev.top_distraction,
            }
        except Exception as exc:
            status["google_sync_error"] = str(exc)

    return status


def _tool_snooze(args: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now()
    until_arg = args.get("until")
    minutes_arg = args.get("minutes")
    reason = str(args.get("reason") or "").strip()

    if until_arg:
        until = cli._parse_snooze_until(str(until_arg), now=now)
    elif minutes_arg is not None:
        minutes = int(minutes_arg)
        if minutes <= 0:
            raise ValueError("'minutes' must be a positive integer")
        until = now + dt.timedelta(minutes=minutes)
    else:
        until = now + dt.timedelta(minutes=60)

    if until <= now:
        raise ValueError(f"Snooze time {until.isoformat(timespec='seconds')} is not in the future")

    cli.set_snooze(until, reason=reason)
    return {
        "snoozed": True,
        "until": until.isoformat(timespec="seconds"),
        "reason": reason,
    }


def _tool_unsnooze() -> dict[str, Any]:
    cleared = cli.clear_snooze()
    return {"cleared": cleared}


def _tool_check_once(args: dict[str, Any]) -> dict[str, Any]:
    cfg = GuardianConfig.load()
    notify = bool(args.get("notify", False))
    result = cli.run_single_check(cfg, notify=notify, verbose=False)
    return {"result": result}


def _tool_sync_agenda() -> dict[str, Any]:
    path = write_agenda_snapshot()
    return {"agenda_path": str(path)}


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> tuple[str, bool]:
    """Execute an MCP tool by name and return (text, is_error)."""
    args = arguments or {}
    try:
        if name == "get_status":
            payload = _tool_get_status()
        elif name == "snooze":
            payload = _tool_snooze(args)
        elif name == "unsnooze":
            payload = _tool_unsnooze()
        elif name == "check_once":
            payload = _tool_check_once(args)
        elif name == "sync_agenda":
            payload = _tool_sync_agenda()
        else:
            return f"Unknown tool: {name}", True
        return json.dumps(payload, indent=2, ensure_ascii=False), False
    except Exception as exc:
        return f"Error running {name}: {exc}", True


def handle_request(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Process one JSON-RPC 2.0 request/notification and return a response or None."""
    msg_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # Notifications have no id and must not receive a response.
    if msg_id is None:
        return None

    if method == "initialize":
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        text, is_error = call_tool(name, arguments)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_request(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
