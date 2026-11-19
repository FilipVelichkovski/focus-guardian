"""Evaluate whether the last 5 minutes of activity were focused on tasks or allowed math topics."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from config import GuardianConfig
from google_client import PendingTask

STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "your",
    "task", "todo", "work", "finish", "complete", "submit", "check", "review",
    "make", "write", "read", "study", "chapter", "part", "week", "due",
}


@dataclass
class FocusEvaluation:
    verdict: str  # "IDLE" | "FOCUSED" | "DISTRACTED"
    total_active_sec: float
    productive_sec: float
    matched_reason: str
    top_distraction: str


def extract_task_keywords(tasks: list[PendingTask]) -> list[tuple[str, list[str]]]:
    """Return (task_title, keywords) for each pending task."""
    out: list[tuple[str, list[str]]] = []
    for task in tasks:
        text = f"{task.title} {task.notes}".lower()
        tokens = re.findall(r"[a-z0-9_+#.-]{3,}", text)
        kws = [tok for tok in tokens if tok not in STOPWORDS]
        if kws:
            out.append((task.title, kws))
    return out


STUDY_PROCESS_NAMES = (
    "rnote",
    "xournalpp",
    "okular",
    "evince",
    "zotero",
    "kate",
    "kwrite",
    "zed",
)


def _scan_running_study_cmdlines() -> list[str]:
    """Return command lines of running study/note/editor apps from /proc."""
    results: list[str] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return results

    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
        lower = cmd.lower()
        if any(app in lower for app in STUDY_PROCESS_NAMES):
            results.append(cmd)
    return results


def _matches_any(haystack: str, keywords: list[str]) -> str | None:
    lower = haystack.lower()
    for kw in keywords:
        if kw in lower:
            return kw
    return None


def _load_recent_from_api(
    api_url: str,
    since_ts: float,
    now: float,
) -> tuple[list[dict], list[str], list[str]] | None:
    """Fetch recent sessions, files, and sites from activity-tracker's local HTTP API."""
    try:
        with urllib.request.urlopen(f"{api_url}/api/sessions?limit=50", timeout=3) as resp:
            raw_sessions = json.loads(resp.read().decode("utf-8"))
        with urllib.request.urlopen(f"{api_url}/api/report?days=0&file_limit=30", timeout=3) as resp:
            raw_report = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None

    sessions = [
        s
        for s in raw_sessions
        if float(s.get("ended_at", 0.0)) >= since_ts and float(s.get("started_at", 0.0)) <= now
    ]
    files = [
        f.get("file_path", "")
        for f in raw_report.get("files", [])
        if float(f.get("last_seen_at", 0.0)) >= since_ts
    ]
    sites = [
        f"{s.get('domain', '')} {s.get('page_title', '')} {s.get('url', '')}"
        for s in raw_report.get("sites", [])
        if float(s.get("last_seen_at", 0.0)) >= since_ts
    ]
    return sessions, files, sites


def evaluate_recent_activity(
    cfg: GuardianConfig,
    tasks: list[PendingTask],
    window_sec: int = 300,
    now_ts: float | None = None,
) -> FocusEvaluation:
    """Inspect the last `window_sec` of activity (SQLite or HTTP API) and return a FocusEvaluation."""
    now = now_ts if now_ts is not None else time.time()
    since_ts = now - window_sec

    sessions: list[dict] = []
    recent_files: list[str] = []
    recent_sites: list[str] = []
    loaded = False

    if cfg.activity_db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{cfg.activity_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                sessions = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT app_name, window_title, started_at, ended_at
                        FROM sessions
                        WHERE ended_at >= ? AND started_at <= ?
                        """,
                        (since_ts, now),
                    ).fetchall()
                ]
                recent_files = [
                    r["file_path"]
                    for r in conn.execute(
                        "SELECT file_path FROM opened_files WHERE last_seen_at >= ?",
                        (since_ts,),
                    ).fetchall()
                ]
                recent_sites = [
                    f"{r['domain']} {r['page_title']} {r['url']}"
                    for r in conn.execute(
                        "SELECT domain, page_title, url FROM visited_sites WHERE last_seen_at >= ?",
                        (since_ts,),
                    ).fetchall()
                ]
                loaded = True
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            # Encrypted SQLCipher DB: fall back to activity-tracker's local HTTP API
            loaded = False

    if not loaded:
        api_data = _load_recent_from_api(cfg.activity_api_url, since_ts, now)
        if api_data is None:
            return FocusEvaluation(
                verdict="IDLE",
                total_active_sec=0.0,
                productive_sec=0.0,
                matched_reason="activity-tracker API unavailable",
                top_distraction="",
            )
        sessions, recent_files, recent_sites = api_data

    task_kws = extract_task_keywords(tasks)
    running_cmds = _scan_running_study_cmdlines()
    for cmd in running_cmds:
        for task_title, kws in task_kws:
            if _matches_any(cmd, kws):
                return FocusEvaluation(
                    verdict="FOCUSED",
                    total_active_sec=float(window_sec),
                    productive_sec=float(window_sec),
                    matched_reason=f"Running app ({task_title})",
                    top_distraction="",
                )
        if kw := _matches_any(cmd, cfg.allowed_topics):
            return FocusEvaluation(
                verdict="FOCUSED",
                total_active_sec=float(window_sec),
                productive_sec=float(window_sec),
                matched_reason=f"Running study app ({kw})",
                top_distraction="",
            )

    extra_context = " ".join(recent_files + recent_sites)

    total_active = 0.0
    productive = 0.0
    matched_reasons: list[str] = []
    distractions: dict[str, float] = {}

    for row in sessions:
        overlap_start = max(float(row["started_at"]), since_ts)
        overlap_end = min(float(row["ended_at"]), now)
        dur = max(0.0, overlap_end - overlap_start)
        if dur <= 0.0:
            continue

        total_active += dur
        app = row["app_name"]
        title = row["window_title"]
        combined = f"{app} {title} {extra_context}"

        matched = None
        for task_title, kws in task_kws:
            if _matches_any(f"{app} {title}", kws) or (
                _matches_any(extra_context, kws) and ("zed" in app.lower() or "code" in app.lower() or "firefox" in app.lower() or "okular" in app.lower())
            ):
                matched = f"Task: {task_title}"
                break

        if matched is None:
            if kw := _matches_any(combined, cfg.allowed_topics):
                matched = f"Allowed study ({kw})"

        if matched is not None:
            productive += dur
            if matched not in matched_reasons:
                matched_reasons.append(matched)
        else:
            label = f"{app}: {title[:60]}" if title else app
            distractions[label] = distractions.get(label, 0.0) + dur

    if total_active < cfg.min_active_sec_to_evaluate:
        return FocusEvaluation(
            verdict="IDLE",
            total_active_sec=total_active,
            productive_sec=productive,
            matched_reason="Away / minimized / locked",
            top_distraction="",
        )

    ratio = productive / total_active if total_active > 0 else 0.0
    top_distraction = (
        max(distractions.items(), key=lambda kv: kv[1])[0] if distractions else ""
    )

    if ratio >= cfg.min_focused_ratio:
        return FocusEvaluation(
            verdict="FOCUSED",
            total_active_sec=total_active,
            productive_sec=productive,
            matched_reason=", ".join(matched_reasons[:3]),
            top_distraction=top_distraction,
        )

    return FocusEvaluation(
        verdict="DISTRACTED",
        total_active_sec=total_active,
        productive_sec=productive,
        matched_reason="No active task or Analysis/Discrete Math detected",
        top_distraction=top_distraction,
    )
