"""Write scheduled collection output without calling any model service."""

from __future__ import annotations

import os
from pathlib import Path

from markdownify import markdownify
from scripts.exporter import normalize_code_blocks, render_html, safe_name

from .config import DATA_DIR


FORMATS = {"md", "html", "body-html", "both", "reading"}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def write_article(article: dict, fmt: str) -> list[str]:
    if fmt not in FORMATS:
        raise ValueError("无效的导出格式")
    root = Path(os.getenv("WEREAD_SKILL_OUTPUT_DIR") or (DATA_DIR / "archive"))
    day = str(article.get("publish_time_text") or "")[:10] or "unknown-date"
    folder = root / safe_name(article["mp_name"]) / day
    stem = safe_name(article["title"]) + f"--article-{article['id']}"
    body = str(article.get("content") or "")
    if not body.strip():
        path = folder / (stem + ".partial.md")
        _write(path, f"# {article['title']}\n\n- 公众号：{article['mp_name']}\n- 日期：{day}\n"
                     f"- 原文：{article.get('url', '')}\n- 内容状态：仅摘要\n\n{article.get('digest', '')}\n")
        return [str(path)]
    saved = []
    if fmt in {"md", "both", "reading"}:
        path = folder / (stem + ".md")
        _write(path, f"# {article['title']}\n\n- 公众号：{article['mp_name']}\n- 日期：{day}\n"
                     f"- 原文：{article.get('url', '')}\n- 来源：微信读书采集\n\n"
                     f"{markdownify(normalize_code_blocks(body), heading_style='ATX').strip()}\n")
        saved.append(str(path))
    if fmt in {"html", "both", "reading"}:
        path = folder / (stem + ".html")
        rendered, _stats = render_html(article)
        _write(path, rendered)
        saved.append(str(path))
    if fmt in {"body-html", "both"}:
        path = folder / (stem + ".body.html")
        _write(path, body)
        saved.append(str(path))
    return saved
