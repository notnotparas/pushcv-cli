"""Local, per-workspace preferences for pushcv.

Stored as JSON in ``.pushcv.json`` next to the database, matching pushcv's
local-first model (settings travel with the workspace they describe). Kept
dependency-free and human-editable so it's easy to inspect and tweak.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_PATH = Path(".pushcv.json")

# Preference key: whether to use the local AI model for salary estimates.
# Three states — True (use AI), False (use web extraction), None (not yet asked).
AI_SALARY_KEY = "ai_salary_enabled"

# Preference key: whether to estimate salaries at all. Salary estimation is
# the one feature that sends job metadata (title/company/location) to an
# external service (DuckDuckGo); privacy-conscious users can switch it off
# entirely by setting this to false in .pushcv.json.
SALARY_ESTIMATES_KEY = "salary_estimates_enabled"


def load_config() -> Dict[str, Any]:
    """Return the workspace config, or an empty dict if absent/unreadable."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(config: Dict[str, Any]) -> None:
    """Persist the config dict to ``.pushcv.json`` (pretty-printed)."""
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def get_ai_salary_enabled() -> Optional[bool]:
    """Return the AI-salary preference: True, False, or None if never set."""
    value = load_config().get(AI_SALARY_KEY)
    return value if isinstance(value, bool) else None


def set_ai_salary_enabled(enabled: bool) -> None:
    """Persist the AI-salary preference."""
    config = load_config()
    config[AI_SALARY_KEY] = enabled
    save_config(config)


def get_salary_estimates_enabled() -> bool:
    """Whether salary estimation (and its web lookups) is enabled at all.

    Defaults to True; only an explicit ``"salary_estimates_enabled": false``
    in .pushcv.json turns it off.
    """
    value = load_config().get(SALARY_ESTIMATES_KEY)
    return value if isinstance(value, bool) else True
