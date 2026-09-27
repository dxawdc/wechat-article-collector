"""Install this portable skill into a SKILL.md-compatible AI tool."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent
FILES = (
    "SKILL.md", "README.md", "requirements.txt", "install.py",
    "agents/openai.yaml", "references/service.md",
    "scripts/__init__.py", "scripts/run.py", "scripts/config.py", "scripts/api.py",
    "scripts/exporter.py", "scripts/collector_client.py",
    "service/__init__.py", "service/config.py", "service/storage.py",
    "service/output.py", "service/main.py", "service/integrations/__init__.py",
    "service/integrations/weread.py", "service/integrations/weread_browser.py",
    "service/integrations/weread_captcha.py",
)


def target_root(tool: str, explicit: str) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if tool == "codex":
        return Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex")) / "skills"
    if tool == "claude":
        return Path.home() / ".claude" / "skills"
    raise ValueError("其他 AI 工具请提供 --target skills根目录")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", choices=("codex", "claude", "custom"), default="codex")
    parser.add_argument("--target", default="", help="AI 工具的 skills 根目录")
    args = parser.parse_args()
    destination = target_root(args.tool, args.target) / ROOT.name
    if destination.resolve() == ROOT:
        print(f"Skill already installed at: {destination}")
        return 0
    for name in FILES:
        source = ROOT / name
        if not source.is_file():
            raise FileNotFoundError(f"安装包缺少文件：{name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f"Installed {len(FILES)} files at: {destination}")
    print("Ready: ask your AI tool to collect articles; the bundled service starts automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
