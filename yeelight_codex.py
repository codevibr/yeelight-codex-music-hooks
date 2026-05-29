#!/usr/bin/env python
"""Small Yeelight D2 controller for Codex status lighting."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

try:
    from yeelight import (
        Bulb,
        BulbException,
        Flow,
        HSVTransition,
        RGBTransition,
        SceneClass,
        SleepTransition,
    )
except ImportError:  # pragma: no cover - only exercised before dependencies exist.
    Bulb = None  # type: ignore[assignment]
    BulbException = Exception  # type: ignore[assignment]
    Flow = None  # type: ignore[assignment]
    HSVTransition = None  # type: ignore[assignment]
    RGBTransition = None  # type: ignore[assignment]
    SceneClass = None  # type: ignore[assignment]
    SleepTransition = None  # type: ignore[assignment]


DEFAULT_IP = "192.168.1.52"


@dataclass(frozen=True)
class State:
    rgb: tuple[int, int, int]
    brightness: int
    description: str


STATES: dict[str, State] = {
    "idle": State((255, 184, 92), 12, "dim warm white idle light"),
    "prompt": State((255, 214, 36), 85, "yellow prompt pulse"),
    "error": State((255, 0, 0), 100, "red intervention flash"),
    "thinking": State((34, 94, 255), 70, "blue thinking light"),
}

STATE_COMMANDS = ("idle", "thinking", "prompt", "error")


def require_yeelight() -> None:
    if Bulb is None:
        print(
            "Missing dependency: install the Yeelight library with "
            "`python -m pip install -r requirements.txt`.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def build_bulb(ip: str) -> Bulb:
    require_yeelight()
    return Bulb(ip, auto_on=True)


class DryRunBulb:
    def __init__(self, ip: str) -> None:
        self.ip = ip

    def turn_on(self) -> None:
        print(f"[dry-run] {self.ip}: turn_on")

    def turn_off(self) -> None:
        print(f"[dry-run] {self.ip}: turn_off")

    def set_rgb(self, red: int, green: int, blue: int) -> None:
        print(f"[dry-run] {self.ip}: set_rgb({red}, {green}, {blue})")

    def set_brightness(self, brightness: int) -> None:
        print(f"[dry-run] {self.ip}: set_brightness({brightness})")

    def set_scene(self, scene_class: Any, *args: Any, **_kwargs: Any) -> None:
        print(f"[dry-run] {self.ip}: set_scene({scene_class}, {', '.join(map(str, args))})")

    def start_flow(self, flow: Any) -> None:
        print(f"[dry-run] {self.ip}: start_flow({flow})")

    def stop_flow(self) -> None:
        print(f"[dry-run] {self.ip}: stop_flow")

    def start_music(self, port: int = 0, ip: str | None = None) -> str:
        print(f"[dry-run] {self.ip}: start_music(port={port}, ip={ip})")
        return "ok"

    def stop_music(self) -> tuple[str, list[int], dict[str, Any]]:
        print(f"[dry-run] {self.ip}: stop_music")
        return ("set_music", [0], {})


def set_rgb_state(bulb: Bulb, state: State) -> None:
    red, green, blue = state.rgb
    bulb.turn_on()
    bulb.set_rgb(red, green, blue)
    bulb.set_brightness(state.brightness)


def flash(
    bulb: Bulb,
    color: tuple[int, int, int],
    brightness: int,
    count: int,
    on_ms: int,
    off_ms: int,
    final_state: State | None = None,
) -> None:
    red, green, blue = color
    bulb.turn_on()
    for index in range(count):
        bulb.set_rgb(red, green, blue)
        bulb.set_brightness(brightness)
        time.sleep(on_ms / 1000)

        bulb.set_rgb(1, 1, 1)
        bulb.set_brightness(1)
        time.sleep(off_ms / 1000)

    if final_state is not None:
        set_rgb_state(bulb, final_state)
    else:
        bulb.set_rgb(red, green, blue)
        bulb.set_brightness(brightness)


def blue_breathe(bulb: Bulb, count: int) -> None:
    if isinstance(bulb, DryRunBulb):
        bulb.turn_on()
        bulb.start_flow(f"blue_breathe count={count}")
        return

    if Flow is None or RGBTransition is None or SleepTransition is None:
        require_yeelight()

    transitions = [
        RGBTransition(8, 56, 180, duration=900, brightness=35),
        SleepTransition(duration=120),
        RGBTransition(28, 124, 255, duration=950, brightness=75),
        SleepTransition(duration=160),
        RGBTransition(90, 180, 255, duration=900, brightness=45),
        SleepTransition(duration=180),
    ]
    bulb.turn_on()
    bulb.start_flow(Flow(count=count, transitions=transitions))


def prompt_pulse(bulb: Bulb, count: int) -> None:
    state = STATES["prompt"]
    if isinstance(bulb, DryRunBulb):
        bulb.turn_on()
        bulb.start_flow(f"prompt_pulse count={count}")
        return

    if Flow is None or HSVTransition is None or SleepTransition is None:
        require_yeelight()

    transitions = [
        HSVTransition(44, 88, duration=900, brightness=12),
        SleepTransition(duration=120),
        HSVTransition(51, 72, duration=900, brightness=state.brightness),
        SleepTransition(duration=180),
    ]
    bulb.turn_on()
    bulb.start_flow(Flow(count=count, action=Flow.actions.stay, transitions=transitions))


def stop_effects(bulb: Bulb) -> None:
    try:
        bulb.stop_flow()
    except BulbException:
        pass


def command_on(bulb: Bulb, _args: argparse.Namespace) -> None:
    bulb.turn_on()


def command_off(bulb: Bulb, _args: argparse.Namespace) -> None:
    bulb.turn_off()


def command_idle(bulb: Bulb, _args: argparse.Namespace) -> None:
    stop_effects(bulb)
    set_rgb_state(bulb, STATES["idle"])


def command_thinking(bulb: Bulb, _args: argparse.Namespace) -> None:
    stop_effects(bulb)
    blue_breathe(bulb, count=0)


def command_blue_breathe(bulb: Bulb, args: argparse.Namespace) -> None:
    blue_breathe(bulb, count=args.count)


def command_prompt(bulb: Bulb, args: argparse.Namespace) -> None:
    stop_effects(bulb)
    prompt_pulse(bulb, count=args.count)


def command_error(bulb: Bulb, args: argparse.Namespace) -> None:
    stop_effects(bulb)
    flash(
        bulb,
        STATES["error"].rgb,
        STATES["error"].brightness,
        args.count,
        args.on_ms,
        args.off_ms,
        final_state=STATES["error"] if args.latch else STATES["idle"],
    )


def apply_named_state(
    bulb: Bulb,
    state_name: str,
    *,
    flash_count: int | None = None,
    on_ms: int = 260,
    off_ms: int = 180,
) -> str:
    if state_name == "idle":
        command_idle(bulb, argparse.Namespace())
        return "idle"
    elif state_name == "thinking":
        command_thinking(bulb, argparse.Namespace())
        return "thinking"
    elif state_name == "prompt":
        command_prompt(
            bulb,
            argparse.Namespace(
                count=flash_count if flash_count is not None else 0,
                on_ms=on_ms,
                off_ms=off_ms,
                latch=True,
            ),
        )
        return "prompt"
    elif state_name == "error":
        command_error(
            bulb,
            argparse.Namespace(
                count=flash_count if flash_count is not None else 6,
                on_ms=on_ms,
                off_ms=off_ms,
                latch=True,
            ),
        )
        return "error"
    else:
        raise ValueError(f"Unknown state: {state_name}")


def command_status(_bulb: Bulb, args: argparse.Namespace) -> None:
    print(f"Target bulb: {args.ip}")
    for name, state in STATES.items():
        red, green, blue = state.rgb
        print(
            f"{name:9} rgb=({red}, {green}, {blue}) "
            f"brightness={state.brightness:3} - {state.description}"
        )


def add_flash_args(parser: argparse.ArgumentParser, default_count: int) -> None:
    parser.add_argument("--count", type=int, default=default_count, help="flash count")
    parser.add_argument("--on-ms", type=int, default=360, help="milliseconds on")
    parser.add_argument("--off-ms", type=int, default=320, help="milliseconds dim")
    parser.add_argument(
        "--no-latch",
        dest="latch",
        action="store_false",
        default=True,
        help="restore idle after flashing instead of staying on the alert color",
    )
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control a Yeelight D2 for Codex status lighting."
    )
    parser.add_argument(
        "--ip",
        default=DEFAULT_IP,
        help=f"Yeelight LAN IP address, defaults to {DEFAULT_IP}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the commands that would be sent without touching the bulb",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    commands: dict[str, tuple[str, Callable[[Bulb, argparse.Namespace], None]]] = {
        "on": ("turn the bulb on", command_on),
        "off": ("turn the bulb off", command_off),
        "idle": ("set dim warm idle light", command_idle),
        "thinking": ("start an infinite blue breathing flow", command_thinking),
        "status": ("show configured states", command_status),
    }
    for name, (help_text, handler) in commands.items():
        command_parser = subparsers.add_parser(name, help=help_text)
        command_parser.set_defaults(handler=handler)

    prompt_parser = subparsers.add_parser("prompt", help="pulse yellow for input")
    prompt_parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="number of pulse cycles; 0 means repeat until another state stops it",
    )
    prompt_parser.add_argument("--on-ms", type=int, help=argparse.SUPPRESS)
    prompt_parser.add_argument("--off-ms", type=int, help=argparse.SUPPRESS)
    prompt_parser.add_argument(
        "--no-latch",
        dest="latch",
        action="store_false",
        default=True,
        help=argparse.SUPPRESS,
    )
    prompt_parser.set_defaults(handler=command_prompt)

    error_parser = subparsers.add_parser("error", help="flash red for intervention")
    add_flash_args(error_parser, default_count=6)
    error_parser.set_defaults(handler=command_error)

    breathe_parser = subparsers.add_parser(
        "blue-breathe", help="run blue breathing flow"
    )
    breathe_parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="number of cycles; 0 means repeat until another command stops it",
    )
    breathe_parser.set_defaults(handler=command_blue_breathe)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "status":
            args.handler(None, args)
            return 0

        bulb = DryRunBulb(args.ip) if args.dry_run else build_bulb(args.ip)
        args.handler(bulb, args)
    except BulbException as exc:
        print(f"Yeelight command failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Could not reach Yeelight at {args.ip}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
