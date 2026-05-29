#!/usr/bin/env python
"""Codex lifecycle hook for Yeelight status updates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLIENT = PROJECT_ROOT / "assistant_light.py"
LOG_FILE = PROJECT_ROOT / "yeelight_hook.log"
PROMPT_LATCH_FILE = PROJECT_ROOT / "yeelight_prompt_latch.json"
LAST_STATE_FILE = PROJECT_ROOT / "yeelight_last_state.json"
PROMPT_LATCH_SECONDS = 8
PROMPT_WATCHDOG_SECONDS = 120.0
PROMPT_RELEASE_GRACE_SECONDS = 0.05
MIN_REPEAT_SECONDS = 2.5

EVENT_STATES = {
    "UserPromptSubmit": "thinking",
    "PreToolUse": "thinking",
    "PermissionRequest": None,
    "Stop": "idle",
}


def read_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def run_light(state: str, *, wait: bool = True) -> None:
    command = [
        sys.executable,
        str(CLIENT),
        state,
        "--quiet",
        "--ignore-errors",
        "--timeout",
        "3",
    ]
    if not wait:
        with open(os.devnull, "rb") as devnull_in, open(
            os.devnull, "wb"
        ) as devnull_out:
            subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdin=devnull_in,
                stdout=devnull_out,
                stderr=devnull_out,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        return

    subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )


def read_prompt_latch() -> float | None:
    try:
        payload = json.loads(PROMPT_LATCH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    try:
        return float(payload.get("created_at"))
    except (TypeError, ValueError, AttributeError):
        return None


def write_prompt_latch(*, source: str) -> str:
    token = f"{time.time():.6f}-{os.getpid()}"
    try:
        PROMPT_LATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROMPT_LATCH_FILE.write_text(
            json.dumps({"created_at": time.time(), "token": token, "source": source}),
            encoding="utf-8",
        )
    except OSError:
        pass
    return token


def clear_prompt_latch() -> None:
    try:
        PROMPT_LATCH_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def read_last_state() -> tuple[str | None, float | None]:
    try:
        payload = json.loads(LAST_STATE_FILE.read_text(encoding="utf-8"))
        return str(payload.get("state")), float(payload.get("changed_at"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None, None


def write_last_state(state: str) -> None:
    try:
        LAST_STATE_FILE.write_text(
            json.dumps({"state": state, "changed_at": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        pass


def should_skip_repeated_state(state: str) -> bool:
    last_state, changed_at = read_last_state()
    if last_state != state or changed_at is None:
        return False
    bridge_state = read_bridge_state()
    if bridge_state is not None and bridge_state != state:
        return False
    return time.time() - changed_at < MIN_REPEAT_SECONDS


def read_bridge_state() -> str | None:
    try:
        with urlopen("http://127.0.0.1:8765/state", timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, URLError, json.JSONDecodeError):
        return None

    state = payload.get("current_state")
    return state if isinstance(state, str) else None


def prompt_latch_active() -> bool:
    created_at = read_prompt_latch()
    if created_at is None:
        return False
    if time.time() - created_at <= PROMPT_LATCH_SECONDS:
        return True
    clear_prompt_latch()
    return False


def prompt_latch_token() -> str | None:
    try:
        payload = json.loads(PROMPT_LATCH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    token = payload.get("token")
    return token if isinstance(token, str) else None


def prompt_latch_age() -> float | None:
    created_at = read_prompt_latch()
    if created_at is None:
        return None
    return time.time() - created_at


def start_prompt_watchdog(token: str, *, seconds: float) -> None:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--release-prompt",
        token,
        str(seconds),
    ]
    with open(os.devnull, "rb") as devnull_in, open(os.devnull, "wb") as devnull_out:
        subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=devnull_in,
            stdout=devnull_out,
            stderr=devnull_out,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def release_prompt_latch(token: str, seconds: float) -> int:
    time.sleep(seconds)
    if prompt_latch_token() != token:
        return 0

    clear_prompt_latch()
    log_event("PromptWatchdog", "thinking", "released-stale-prompt")
    run_light("thinking")
    write_last_state("thinking")
    return 0


def payload_contains(value: Any, needle: str) -> bool:
    if isinstance(value, dict):
        return any(
            payload_contains(key, needle) or payload_contains(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(payload_contains(item, needle) for item in value)
    if isinstance(value, str):
        return needle in value
    return False


def is_escalated_request(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "sandbox_permissions" and item == "require_escalated":
                return True
            if is_escalated_request(item):
                return True
    if isinstance(value, list):
        return any(is_escalated_request(item) for item in value)
    return False


def is_local_web_access(payload: dict[str, Any]) -> bool:
    if any(
        payload_contains(payload, needle)
        for needle in (
            "http://127.0.0.1:8765",
            "https://127.0.0.1:8765",
            "http://localhost:8765",
            "https://localhost:8765",
        )
    ):
        return False

    return any(
        payload_contains(payload, needle)
        for needle in (
            "http://127.0.0.1",
            "https://127.0.0.1",
            "http://localhost",
            "https://localhost",
        )
    )


def is_permission_shaped_pretool(payload: dict[str, Any]) -> bool:
    return is_escalated_request(payload) or is_local_web_access(payload)


def begin_prompt_latch(*, source: str, watchdog_seconds: float) -> None:
    token = write_prompt_latch(source=source)
    start_prompt_watchdog(token, seconds=watchdog_seconds)


def resolve_state(event_name: str, payload: dict[str, Any]) -> tuple[str | None, str | None]:
    state = EVENT_STATES.get(event_name)
    note = None

    if event_name == "PermissionRequest":
        begin_prompt_latch(
            source="permission-request",
            watchdog_seconds=PROMPT_WATCHDOG_SECONDS,
        )
        return "prompt", "latched-from-permission-request"

    if event_name == "PreToolUse" and prompt_latch_active():
        age = prompt_latch_age()
        if age is not None and age < PROMPT_RELEASE_GRACE_SECONDS:
            return None, "holding-fresh-prompt-latch"
        clear_prompt_latch()
        return "thinking", "released-prompt-latch"

    if event_name in {"UserPromptSubmit", "Stop"}:
        clear_prompt_latch()

    return state, note


def log_event(event_name: str, state: str | None, note: str | None = None) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            timestamp = datetime.now().isoformat(timespec="seconds")
            suffix = f" note={note}" if note else ""
            handle.write(
                f"{timestamp} event={event_name or '-'} state={state or '-'}{suffix}\n"
            )
    except OSError:
        pass


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--release-prompt":
        try:
            seconds = (
                float(sys.argv[3]) if len(sys.argv) >= 4 else PROMPT_WATCHDOG_SECONDS
            )
        except ValueError:
            seconds = PROMPT_WATCHDOG_SECONDS
        return release_prompt_latch(sys.argv[2], seconds)

    payload = read_payload()
    event_name = str(payload.get("hook_event_name", ""))
    state, note = resolve_state(event_name, payload)
    if state and should_skip_repeated_state(state):
        note = "skipped-duplicate-state"
        log_event(event_name, None, note)
        return 0

    log_event(event_name, state, note)
    if state:
        run_light(state)
        write_last_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
