"""Local-only settings for the bundled WeRead collection service."""

from __future__ import annotations

import os
from pathlib import Path


def _positive_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.getenv("WEREAD_SKILL_DATA_DIR") or (Path.home() / ".wechat-article-collector"))
WEREAD_BASE_URL = os.getenv("WEREAD_SKILL_BASE_URL", "https://weread.qq.com").rstrip("/")
WEREAD_SESSION_FILE = DATA_DIR / "weread_session.yaml"
WEREAD_BROWSER_STATE_FILE = DATA_DIR / "weread_browser_state.json"
WEREAD_BROWSER_MODE = os.getenv("WEREAD_SKILL_BROWSER_MODE", "persistent").strip().lower()
WEREAD_BROWSER_HEADLESS = _bool("WEREAD_SKILL_BROWSER_HEADLESS", True)
WEREAD_BROWSER_USER_DATA_DIR = DATA_DIR / "weread-profile" / "active"
WEREAD_BROWSER_CHANNEL = os.getenv("WEREAD_SKILL_BROWSER_CHANNEL", "").strip()
WEREAD_BROWSER_ADMIN_URL = os.getenv("WEREAD_SKILL_BROWSER_ADMIN_URL", "http://127.0.0.1:8765/verify").strip()
WEREAD_BROWSER_COMMAND_TIMEOUT_SECONDS = _positive_float("WEREAD_SKILL_BROWSER_TIMEOUT", 70.0)
WEREAD_QRCODE_FILE = DATA_DIR / "weread_qrcode.png"
WEREAD_REQUEST_MIN_INTERVAL_SECONDS = _positive_float("WEREAD_SKILL_REQUEST_INTERVAL", 4.0)
WEREAD_RATE_LIMIT_COOLDOWN_SECONDS = _positive_float("WEREAD_SKILL_RATE_LIMIT_COOLDOWN", 86400.0)
WEREAD_AUTH_POLL_INTERVAL_SECONDS = _positive_float("WEREAD_SKILL_AUTH_POLL_INTERVAL", 5.0)
WEREAD_MP_ARTICLES_PATH = os.getenv("WEREAD_SKILL_MP_ARTICLES_PATH", "/web/mp/articles")
WEREAD_MP_ARTICLES_ID_PARAM = os.getenv("WEREAD_SKILL_MP_ARTICLES_ID_PARAM", "bookId")
WEREAD_MP_ARTICLES_CURSOR_PARAM = os.getenv("WEREAD_SKILL_MP_ARTICLES_CURSOR_PARAM", "maxIdx")
WEREAD_MP_ARTICLES_COUNT_PARAM = os.getenv("WEREAD_SKILL_MP_ARTICLES_COUNT_PARAM", "count")
WEREAD_MP_ARTICLES_COUNT = _positive_int("WEREAD_SKILL_MP_ARTICLES_COUNT", 20)
WEREAD_CONTENT_MAX_PER_RUN = _positive_int("WEREAD_SKILL_CONTENT_MAX_PER_RUN", 10)
