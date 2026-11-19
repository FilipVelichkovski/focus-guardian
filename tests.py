"""Unit tests for Focus Guardian evaluator and environment checks."""

from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import GuardianConfig
from env_checks import is_quiet_hours, should_spare_oled
from evaluator import evaluate_recent_activity
from google_client import PendingTask


def _create_test_db(db_path: Path, rows: list[tuple[str, str, float, float]]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT NOT NULL,
            window_title TEXT NOT NULL,
            pid INTEGER NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL NOT NULL,
            duration_sec REAL NOT NULL
        );
        CREATE TABLE opened_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            app_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            source TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        );
        CREATE TABLE visited_sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            app_name TEXT NOT NULL,
            domain TEXT NOT NULL,
            url TEXT NOT NULL,
            page_title TEXT NOT NULL,
            source TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        );
        """
    )
    for app, title, start, end in rows:
        conn.execute(
            "INSERT INTO sessions (app_name, window_title, pid, started_at, ended_at, duration_sec) VALUES (?, ?, 1, ?, ?, ?)",
            (app, title, start, end, end - start),
        )
    conn.commit()
    conn.close()


class TestFocusGuardian(unittest.TestCase):
    def test_quiet_hours(self) -> None:
        self.assertTrue(is_quiet_hours(dt.datetime(2026, 11, 15, 21, 0)))
        self.assertTrue(is_quiet_hours(dt.datetime(2026, 11, 15, 23, 30)))
        self.assertTrue(is_quiet_hours(dt.datetime(2026, 11, 15, 7, 59)))
        self.assertFalse(is_quiet_hours(dt.datetime(2026, 11, 15, 8, 0)))
        self.assertFalse(is_quiet_hours(dt.datetime(2026, 11, 15, 20, 59)))

    def test_focused_on_pending_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "activity.db"
            now = 1000.0
            _create_test_db(db, [("dev.zed.Zed", "hw03-variant — variant.hpp", 800.0, 1000.0)])
            cfg = GuardianConfig(activity_db_path=db, allowed_topics=["discrete mathematics"])
            tasks = [PendingTask(title="Finish variant homework")]
            ev = evaluate_recent_activity(cfg, tasks, window_sec=300, now_ts=now)
            self.assertEqual(ev.verdict, "FOCUSED")

    def test_focused_on_allowed_math(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "activity.db"
            now = 1000.0
            _create_test_db(
                db,
                [("org.mozilla.firefox", "Discrete Mathematics - Graph Theory — Mozilla Firefox", 800.0, 1000.0)],
            )
            cfg = GuardianConfig(activity_db_path=db, allowed_topics=["discrete mathematics"])
            ev = evaluate_recent_activity(cfg, [], window_sec=300, now_ts=now)
            self.assertEqual(ev.verdict, "FOCUSED")

    def test_distracted_when_not_on_task_or_math(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "activity.db"
            now = 1000.0
            _create_test_db(
                db,
                [("com.discordapp.Discord", "#random - Discord", 800.0, 1000.0)],
            )
            cfg = GuardianConfig(activity_db_path=db, allowed_topics=["discrete mathematics"])
            tasks = [PendingTask(title="Finish variant homework")]
            with patch("evaluator._scan_running_study_cmdlines", return_value=[]):
                ev = evaluate_recent_activity(cfg, tasks, window_sec=300, now_ts=now)
            self.assertEqual(ev.verdict, "DISTRACTED")
            self.assertIn("Discord", ev.top_distraction)

    def test_idle_when_away(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "activity.db"
            now = 1000.0
            _create_test_db(db, [("com.discordapp.Discord", "#random", 990.0, 1000.0)])
            cfg = GuardianConfig(activity_db_path=db, allowed_topics=["discrete mathematics"])
            with patch("evaluator._scan_running_study_cmdlines", return_value=[]):
                ev = evaluate_recent_activity(cfg, [], window_sec=300, now_ts=now)
            self.assertEqual(ev.verdict, "IDLE")

    def test_oled_protection_when_edp_only_and_idle(self) -> None:
        self.assertTrue(should_spare_oled(["eDP-1"], "IDLE"))
        self.assertFalse(should_spare_oled(["eDP-1"], "FOCUSED"))
        self.assertFalse(should_spare_oled(["HDMI-A-1"], "IDLE"))
        self.assertFalse(should_spare_oled(["eDP-1", "HDMI-A-1"], "IDLE"))

    def test_snooze_active_expired_and_check_skip(self) -> None:
        import datetime as dt
        import cli

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            snooze_file = tmp / "snooze.json"
            state_file = tmp / "state.json"
            with patch.object(cli, "DATA_DIR", tmp), \
                 patch.object(cli, "SNOOZE_PATH", snooze_file), \
                 patch.object(cli, "STATE_PATH", state_file):
                now = dt.datetime(2026, 11, 19, 14, 30)
                until = cli._parse_snooze_until("16:30", now=now)
                self.assertEqual(until, dt.datetime(2026, 11, 19, 16, 30))

                cli.set_snooze(until, reason="C++ defense")
                active = cli.get_active_snooze(now=now)
                self.assertIsNotNone(active)
                self.assertEqual(active[0], until)
                self.assertEqual(active[1], "C++ defense")

                res = cli.run_single_check(GuardianConfig(), notify=True)
                self.assertEqual(res, "SKIP: Snoozed (Until 16:30 (C++ defense))")

                expired = cli.get_active_snooze(now=dt.datetime(2026, 11, 19, 16, 30))
                self.assertIsNone(expired)
                self.assertFalse(snooze_file.exists())

    def test_mcp_server_initialize_list_and_call(self) -> None:
        import mcp_server

        init_resp = mcp_server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        )
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "focus-guardian")

        self.assertIsNone(mcp_server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))

        list_resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = {t["name"] for t in list_resp["result"]["tools"]}
        self.assertEqual(tool_names, {"get_status", "snooze", "unsnooze", "check_once", "sync_agenda"})

        with patch("mcp_server.cli.run_single_check", return_value="SKIP: Snoozed"):
            call_resp = mcp_server.handle_request(
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "check_once", "arguments": {}}}
            )
        self.assertFalse(call_resp["result"]["isError"])
        self.assertIn("SKIP: Snoozed", call_resp["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
