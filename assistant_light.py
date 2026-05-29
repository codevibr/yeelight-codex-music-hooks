#!/usr/bin/env python
"""Small client for sending assistant status updates to the local bridge."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765"
VALID_STATES = ("idle", "thinking", "prompt", "error")


def post_state(
    bridge_url: str,
    state: str,
    *,
    count: int | None = None,
    on_ms: int | None = None,
    off_ms: int | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": state}
    if count is not None:
        payload["count"] = count
    if on_ms is not None:
        payload["on_ms"] = on_ms
    if off_ms is not None:
        payload["off_ms"] = off_ms

    request = Request(
        f"{bridge_url.rstrip('/')}/state",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def bridge_health(bridge_url: str, *, timeout: float) -> bool:
    try:
        with urlopen(f"{bridge_url.rstrip('/')}/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, URLError, TimeoutError):
        return False


def start_bridge() -> None:
    bridge_script = Path(__file__).with_name("yeelight_bridge.py")
    subprocess.Popen(
        [sys.executable, str(bridge_script)],
        cwd=bridge_script.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def ensure_bridge(bridge_url: str, *, timeout: float) -> bool:
    if bridge_health(bridge_url, timeout=min(timeout, 1.0)):
        return True

    start_bridge()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bridge_health(bridge_url, timeout=0.5):
            return True
        time.sleep(0.2)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tell the local Yeelight bridge about an assistant state."
    )
    parser.add_argument("state", choices=VALID_STATES)
    parser.add_argument(
        "--bridge-url",
        default=DEFAULT_BRIDGE_URL,
        help=f"bridge URL, defaults to {DEFAULT_BRIDGE_URL}",
    )
    parser.add_argument("--count", type=int, help="flash count for prompt/error")
    parser.add_argument("--on-ms", type=int, help="flash-on milliseconds")
    parser.add_argument("--off-ms", type=int, help="flash-dim milliseconds")
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="seconds to wait for the bridge",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print output when there is an error",
    )
    parser.add_argument(
        "--ignore-errors",
        action="store_true",
        help="return success even when the bridge is unavailable",
    )
    parser.add_argument(
        "--no-start-bridge",
        action="store_true",
        help="do not start the local bridge automatically when it is unavailable",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.no_start_bridge:
        ensure_bridge(args.bridge_url, timeout=args.timeout)

    try:
        response = post_state(
            args.bridge_url,
            args.state,
            count=args.count,
            on_ms=args.on_ms,
            off_ms=args.off_ms,
            timeout=args.timeout,
        )
        if not args.no_start_bridge and not bridge_health(
            args.bridge_url,
            timeout=min(args.timeout, 1.0),
        ):
            ensure_bridge(args.bridge_url, timeout=args.timeout)
            response = post_state(
                args.bridge_url,
                args.state,
                count=args.count,
                on_ms=args.on_ms,
                off_ms=args.off_ms,
                timeout=args.timeout,
            )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if not args.quiet:
            print(f"Bridge rejected the request ({exc.code}): {body}", file=sys.stderr)
        return 0 if args.ignore_errors else 1
    except URLError as exc:
        if not args.quiet:
            print(
                f"Could not reach bridge at {args.bridge_url}: {exc.reason}",
                file=sys.stderr,
            )
        return 0 if args.ignore_errors else 1
    except TimeoutError:
        if not args.quiet:
            print(f"Timed out waiting for bridge at {args.bridge_url}", file=sys.stderr)
        return 0 if args.ignore_errors else 1

    if not args.quiet:
        print(json.dumps(response, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
