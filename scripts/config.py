"""User-owned archive path for the bundled collector."""

from __future__ import annotations

import os
from pathlib import Path


def archive_dir() -> Path:
    override = os.getenv("WEREAD_SKILL_OUTPUT_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.getenv("WEREAD_SKILL_DATA_DIR") or (Path.home() / ".wechat-article-collector")) / "archive"
