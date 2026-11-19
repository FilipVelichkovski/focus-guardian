"""Configuration and constants for Focus Guardian."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "focus-guardian"
DATA_DIR = Path.home() / ".local" / "share" / "focus-guardian"
ACTIVITY_DB_PATH = Path.home() / ".local" / "share" / "activity-tracker" / "activity.db"

CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
TOKEN_PATH = CONFIG_DIR / "token.json"
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_PATH = DATA_DIR / "cache.json"

DEFAULT_ALLOWED_TOPICS = [
    # Analysis (Real Analysis / Calculus)
    "analysis",
    "real analysis",
    "calculus",
    "limit",
    "continuity",
    "derivative",
    "integral",
    "sequence",
    "series",
    "convergence",
    "supremum",
    "infimum",
    "cauchy",
    "riemann",
    "metric space",
    "topology",
    "bolzano",
    "weierstrass",
    "taylor",
    # Discrete Mathematics
    "discrete",
    "discrete math",
    "discrete mathematics",
    "combinatorics",
    "graph theory",
    "induction",
    "recurrence",
    "number theory",
    "modular arithmetic",
    "boolean",
    "predicate logic",
    "propositional",
    "set theory",
    "relation",
    "bijection",
    "pigeonhole",
    "permutation",
    "combination",
    "probability",
    "tree",
    "planar graph",
]


@dataclass
class GuardianConfig:
    check_interval_sec: int = 100
    quiet_start_hour: int = 21
    quiet_end_hour: int = 8
    min_active_sec_to_evaluate: int = 45
    min_focused_ratio: float = 0.40
    allowed_topics: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOPICS))
    extra_ical_urls: list[str] = field(default_factory=list)
    activity_db_path: Path = ACTIVITY_DB_PATH
    activity_api_url: str = "http://127.0.0.1:7878"

    @classmethod
    def load(cls) -> "GuardianConfig":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            raw = json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(
            check_interval_sec=int(raw.get("check_interval_sec", 300)),
            quiet_start_hour=int(raw.get("quiet_start_hour", 21)),
            quiet_end_hour=int(raw.get("quiet_end_hour", 8)),
            min_active_sec_to_evaluate=int(raw.get("min_active_sec_to_evaluate", 45)),
            min_focused_ratio=float(raw.get("min_focused_ratio", 0.40)),
            allowed_topics=list(raw.get("allowed_topics", DEFAULT_ALLOWED_TOPICS)),
            extra_ical_urls=list(raw.get("extra_ical_urls", [])),
            activity_db_path=Path(raw.get("activity_db_path", ACTIVITY_DB_PATH)).expanduser(),
            activity_api_url=str(raw.get("activity_api_url", "http://127.0.0.1:7878")).rstrip("/"),
        )
