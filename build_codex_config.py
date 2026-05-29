#!/usr/bin/env python
"""Render local Codex config files from .env-backed examples."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"
HOOKS_TEMPLATE = PROJECT_ROOT / ".codex" / "hooks.json.example"
HOOKS_OUTPUT = PROJECT_ROOT / ".codex" / "hooks.json"
AGENTS_TEMPLATE = PROJECT_ROOT / "AGENTS.md.example"
AGENTS_OUTPUT = PROJECT_ROOT / "AGENTS.md"

REQUIRED_ENV = ("YEELIGHT_CODEX_ROOT", "YEELIGHT_CODEX_PYTHON")
PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def require_env(values: dict[str, str]) -> None:
    missing = [key for key in REQUIRED_ENV if not values.get(key)]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Missing required .env value(s): {joined}")


def render_template(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise SystemExit(f"Template references missing .env value: {key}")
        return values[key]

    return PLACEHOLDER_PATTERN.sub(replace, template)


def build(*, check: bool) -> int:
    if not ENV_FILE.exists():
        print(
            f"Missing {ENV_FILE.name}. Copy {ENV_EXAMPLE_FILE.name} to .env and edit it.",
            file=sys.stderr,
        )
        return 1

    values = parse_env(ENV_FILE)
    require_env(values)

    outputs = {
        HOOKS_OUTPUT: render_template(HOOKS_TEMPLATE.read_text(encoding="utf-8"), values),
        AGENTS_OUTPUT: render_template(AGENTS_TEMPLATE.read_text(encoding="utf-8"), values),
    }
    if check:
        for output, rendered in outputs.items():
            current = output.read_text(encoding="utf-8") if output.exists() else ""
            if current != rendered:
                print(f"{output} is out of date. Run build_codex_config.py.")
                return 1
        print("Codex config is up to date.")
        return 0

    for output, rendered in outputs.items():
        output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render local Codex files from examples and .env "
            "(.codex/hooks.json and AGENTS.md)."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify generated files are current without writing",
    )
    args = parser.parse_args()
    return build(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
