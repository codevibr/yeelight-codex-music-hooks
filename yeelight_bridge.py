#!/usr/bin/env python
"""Local HTTP bridge for assistant-to-Yeelight status updates."""

from __future__ import annotations

import argparse
import configparser
import json
import threading
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yeelight_codex import (
    DEFAULT_IP,
    BulbException,
    DryRunBulb,
    Flow,
    RGBTransition,
    SceneClass,
    build_bulb,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_CONFIG = Path(__file__).with_name("config.ini")
STATE_COMMANDS = ("idle", "thinking", "prompt", "error")
MUSIC_COMMANDS = ("music_start", "music_stop")
STATE_PRIORITY = {"idle": 0, "thinking": 1, "prompt": 2, "error": 3}
STATE_MIN_HOLD_SECONDS = {"idle": 0.0, "thinking": 0.8, "prompt": 0.6, "error": 2.5}
COALESCE_SECONDS = 0.15
MIN_APPLY_INTERVAL_SECONDS = 0.35
DEFAULT_IDLE_OFF_SECONDS = 120.0


@dataclass(frozen=True)
class EffectConfig:
    name: str
    effect: str
    rgb: tuple[int, int, int]
    brightness: int
    low_brightness: int
    on_ms: int
    off_ms: int
    count: int
    duration_ms: int
    after: str


@dataclass(frozen=True)
class StateRequest:
    state_name: str
    received_at: float
    sequence: int
    flash_count: int | None = None
    on_ms: int | None = None
    off_ms: int | None = None

    @property
    def priority(self) -> int:
        if self.state_name in MUSIC_COMMANDS:
            return STATE_PRIORITY["thinking"]
        return STATE_PRIORITY[self.state_name]

    @property
    def has_overrides(self) -> bool:
        return (
            self.flash_count is not None
            or self.on_ms is not None
            or self.off_ms is not None
        )


@dataclass(frozen=True)
class BridgeOptions:
    idle_off_seconds: float


DEFAULT_EFFECTS: dict[str, EffectConfig] = {
    "idle": EffectConfig("idle", "solid", (255, 184, 92), 12, 1, 0, 0, 0, 0, "hold"),
    "thinking": EffectConfig(
        "thinking", "pulse", (34, 94, 255), 70, 14, 800, 800, 0, 0, "hold"
    ),
    "prompt": EffectConfig(
        "prompt", "pulse", (255, 214, 36), 85, 8, 650, 650, 0, 0, "hold"
    ),
    "error": EffectConfig(
        "error", "flash", (255, 0, 0), 100, 1, 260, 180, 0, 0, "hold"
    ),
}


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def parse_rgb(value: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    try:
        parts = [int(part.strip()) for part in value.split(",")]
    except ValueError:
        return default
    if len(parts) != 3:
        return default
    return tuple(max(0, min(255, part)) for part in parts)  # type: ignore[return-value]


def load_effects(config_path: Path) -> dict[str, EffectConfig]:
    parser = configparser.ConfigParser()
    parser.read(config_path)
    effects: dict[str, EffectConfig] = {}

    for state_name, default in DEFAULT_EFFECTS.items():
        section = parser[state_name] if parser.has_section(state_name) else {}
        effect = str(section.get("effect", default.effect)).strip().lower()
        if effect not in {"solid", "pulse", "flash"}:
            effect = default.effect

        after = str(section.get("after", default.after)).strip().lower()
        if after not in {"hold", "idle", "off"}:
            after = default.after

        effects[state_name] = EffectConfig(
            name=state_name,
            effect=effect,
            rgb=parse_rgb(str(section.get("color", "")), default.rgb),
            brightness=max(1, min(100, int(section.get("brightness", default.brightness)))),
            low_brightness=max(
                1, min(100, int(section.get("low_brightness", default.low_brightness)))
            ),
            on_ms=max(50, int(section.get("on_ms", default.on_ms))),
            off_ms=max(50, int(section.get("off_ms", default.off_ms))),
            count=max(0, int(section.get("count", default.count))),
            duration_ms=max(0, int(section.get("duration_ms", default.duration_ms))),
            after=after,
        )

    return effects


def load_bridge_options(config_path: Path) -> BridgeOptions:
    parser = configparser.ConfigParser()
    parser.read(config_path)
    section = parser["bridge"] if parser.has_section("bridge") else {}
    try:
        idle_off_seconds = float(
            section.get("idle_off_seconds", DEFAULT_IDLE_OFF_SECONDS)
        )
    except (TypeError, ValueError):
        idle_off_seconds = DEFAULT_IDLE_OFF_SECONDS

    return BridgeOptions(idle_off_seconds=max(0.0, idle_off_seconds))


class BridgeState:
    def __init__(
        self,
        ip: str,
        dry_run: bool,
        *,
        music_mode: bool,
        music_host: str | None,
        music_port: int,
        config_path: Path,
    ) -> None:
        self.ip = ip
        self.dry_run = dry_run
        self.music_mode_requested = music_mode
        self.music_mode_active = False
        self.music_host = music_host
        self.music_port = music_port
        self.config_path = config_path
        self.current_state = "unknown"
        self.current_effect: str | None = None
        self.last_effect_error: str | None = None
        self.music_lighting_active = False
        self.music_override_active = False
        self.effects = load_effects(config_path)
        self.options = load_bridge_options(config_path)
        self.lock = threading.Lock()
        self.scheduler = threading.Condition()
        self.pending_request: StateRequest | None = None
        self.deferred_request: StateRequest | None = None
        self.request_sequence = 0
        self.last_apply_at = 0.0
        self.priority_hold_until = 0.0
        self.worker_stop = False
        self.stats = {
            "queued": 0,
            "coalesced": 0,
            "applied": 0,
            "skipped_duplicate": 0,
        }
        self.effect_stop = threading.Event()
        self.effect_thread: threading.Thread | None = None
        self.idle_off_timer: threading.Timer | None = None
        self.idle_off_generation = 0
        self.idle_off_due_at: float | None = None
        self.bulb = DryRunBulb(ip) if dry_run else build_bulb(ip)
        self.start_music_mode()
        self.worker_thread = threading.Thread(
            target=self.run_scheduler,
            name="yeelight-state-scheduler",
            daemon=True,
        )
        self.worker_thread.start()

    def start_music_mode(self) -> None:
        if not self.music_mode_requested:
            return

        try:
            self.bulb.start_music(port=self.music_port, ip=self.music_host)
        except (BulbException, OSError, TimeoutError) as exc:
            self.last_effect_error = f"Music mode unavailable; using normal commands: {exc}"
            self.music_mode_active = False
            return
        self.music_mode_active = True

    def close(self) -> None:
        self.cancel_idle_off()
        with self.scheduler:
            self.worker_stop = True
            self.scheduler.notify_all()
        self.worker_thread.join(timeout=2.0)
        self.stop_effect()
        if not self.music_mode_active:
            return

        try:
            self.bulb.stop_music()
        finally:
            self.music_mode_active = False

    def reload_config(self) -> None:
        self.effects = load_effects(self.config_path)
        self.options = load_bridge_options(self.config_path)

    def cancel_idle_off(self) -> None:
        self.idle_off_generation += 1
        self.idle_off_due_at = None
        if self.idle_off_timer is not None:
            self.idle_off_timer.cancel()
        self.idle_off_timer = None

    def schedule_idle_off(self) -> None:
        self.cancel_idle_off()
        if self.options.idle_off_seconds <= 0:
            return

        self.idle_off_generation += 1
        generation = self.idle_off_generation
        self.idle_off_due_at = time.monotonic() + self.options.idle_off_seconds
        self.idle_off_timer = threading.Timer(
            self.options.idle_off_seconds,
            self.apply_idle_timeout,
            args=(generation,),
        )
        self.idle_off_timer.daemon = True
        self.idle_off_timer.start()

    def apply_idle_timeout(self, generation: int) -> None:
        with self.lock:
            if generation != self.idle_off_generation or self.current_state != "idle":
                return
            try:
                self.bulb.stop_flow()
            except BulbException:
                pass
            self.bulb.turn_off()
            self.current_state = "off"
            self.current_effect = None
            self.idle_off_due_at = None

    def stop_effect(self) -> None:
        self.effect_stop.set()
        if self.effect_thread is not None and self.effect_thread.is_alive():
            self.effect_thread.join(timeout=2.0)
        self.effect_thread = None
        self.current_effect = None

    def apply(
        self,
        state_name: str,
        *,
        flash_count: int | None = None,
        on_ms: int | None = None,
        off_ms: int | None = None,
    ) -> None:
        if state_name not in STATE_COMMANDS and state_name not in MUSIC_COMMANDS:
            raise ValueError(f"Unknown state: {state_name}")

        self.enqueue(
            StateRequest(
                state_name=state_name,
                received_at=time.monotonic(),
                sequence=self.next_sequence(),
                flash_count=flash_count,
                on_ms=on_ms,
                off_ms=off_ms,
            )
        )

    def apply_color(self, rgb: tuple[int, int, int], brightness: int) -> None:
        if (
            self.music_lighting_active
            and (
                self.music_override_active
                or self.current_state in {"prompt", "error"}
            )
        ):
            return

        self.cancel_idle_off()
        self.music_lighting_active = True
        red, green, blue = rgb
        brightness = max(1, min(100, brightness))
        self.stop_effect()
        self.current_state = "music"
        self.last_effect_error = None
        with self.lock:
            try:
                self.bulb.stop_flow()
            except BulbException:
                pass
            self.set_color_brightness(red, green, blue, brightness)

    def next_sequence(self) -> int:
        with self.scheduler:
            self.request_sequence += 1
            return self.request_sequence

    def enqueue(self, request: StateRequest) -> None:
        with self.scheduler:
            existing = self.pending_request
            if existing is None:
                self.pending_request = request
            elif request.priority > existing.priority:
                self.pending_request = request
                self.defer_request(existing)
                self.stats["coalesced"] += 1
            elif request.priority == existing.priority:
                self.pending_request = request
                self.stats["coalesced"] += 1
            else:
                self.defer_request(request)
                self.stats["coalesced"] += 1
            self.stats["queued"] += 1
            self.scheduler.notify_all()

    def defer_request(self, request: StateRequest) -> None:
        existing = self.deferred_request
        if existing is None or request.received_at >= existing.received_at:
            self.deferred_request = request

    def run_scheduler(self) -> None:
        while True:
            request = self.next_ready_request()
            if request is None:
                return
            self.apply_now(request)

    def next_ready_request(self) -> StateRequest | None:
        with self.scheduler:
            while not self.worker_stop:
                if self.pending_request is None:
                    self.scheduler.wait()
                    continue

                request = self.pending_request
                now = time.monotonic()
                delay = max(0.0, request.received_at + COALESCE_SECONDS - now)

                current_priority = STATE_PRIORITY.get(self.current_state, -1)
                if request.priority < current_priority:
                    delay = max(delay, self.priority_hold_until - now)

                delay = max(delay, self.last_apply_at + MIN_APPLY_INTERVAL_SECONDS - now)
                if delay > 0:
                    self.scheduler.wait(delay)
                    continue

                self.pending_request = self.deferred_request
                self.deferred_request = None
                return request

            return None

    def apply_now(self, request: StateRequest) -> None:
        if request.state_name == "music_start":
            self.cancel_idle_off()
            self.stop_effect()
            self.music_lighting_active = True
            self.music_override_active = False
            self.current_state = "music"
            self.current_effect = None
            self.last_apply_at = time.monotonic()
            with self.scheduler:
                self.stats["applied"] += 1
            return

        if request.state_name == "music_stop":
            self.music_lighting_active = False
            self.music_override_active = False
            self.current_state = "idle"
            self.current_effect = None
            self.last_apply_at = time.monotonic()
            with self.scheduler:
                self.stats["applied"] += 1
            self.reload_config()
            self.apply_solid(self.effects["idle"])
            self.schedule_idle_off()
            return

        if self.music_lighting_active and request.state_name in {"thinking", "idle"}:
            if self.current_state in {"prompt", "error"}:
                self.stop_effect()
                self.current_state = "music"
                self.current_effect = None
                self.music_override_active = False
                self.last_apply_at = time.monotonic()
                with self.scheduler:
                    self.stats["applied"] += 1
                return

            with self.scheduler:
                self.stats["skipped_duplicate"] += 1
            return

        self.reload_config()
        effect = self.effects[request.state_name]
        if request.has_overrides:
            effect = EffectConfig(
                **{
                    **asdict(effect),
                    "count": request.flash_count
                    if request.flash_count is not None
                    else effect.count,
                    "on_ms": request.on_ms if request.on_ms is not None else effect.on_ms,
                    "off_ms": request.off_ms if request.off_ms is not None else effect.off_ms,
                }
            )

        if self.is_duplicate_request(request, effect):
            with self.scheduler:
                self.stats["skipped_duplicate"] += 1
            return

        self.stop_effect()
        self.current_state = request.state_name
        if not (
            self.music_lighting_active and request.state_name in {"prompt", "error"}
        ):
            self.music_lighting_active = False
        elif request.state_name in {"prompt", "error"}:
            self.music_override_active = True
        if request.state_name != "idle":
            self.cancel_idle_off()
        self.last_effect_error = None
        self.last_apply_at = time.monotonic()
        self.priority_hold_until = self.last_apply_at + STATE_MIN_HOLD_SECONDS[
            request.state_name
        ]
        with self.scheduler:
            self.stats["applied"] += 1

        if effect.effect == "solid" and effect.duration_ms == 0:
            self.apply_solid(effect)
            if request.state_name == "idle":
                self.schedule_idle_off()
            return

        self.effect_stop.clear()
        self.current_effect = request.state_name
        self.effect_thread = threading.Thread(
            target=self.run_effect,
            args=(effect,),
            name=f"yeelight-{request.state_name}-{effect.effect}",
            daemon=True,
        )
        self.effect_thread.start()

    def is_duplicate_request(self, request: StateRequest, effect: EffectConfig) -> bool:
        if request.has_overrides or request.state_name != self.current_state:
            return False
        if effect.effect == "solid":
            return self.current_effect is None
        return self.effect_thread is not None and self.effect_thread.is_alive()

    def apply_solid(self, effect: EffectConfig) -> None:
        red, green, blue = effect.rgb
        with self.lock:
            try:
                self.bulb.stop_flow()
            except BulbException:
                pass
            self.set_color_brightness(red, green, blue, effect.brightness)

    def set_level(self, effect: EffectConfig, brightness: int) -> None:
        red, green, blue = effect.rgb
        with self.lock:
            self.set_color_brightness(red, green, blue, brightness)

    def set_color_brightness(
        self, red: int, green: int, blue: int, brightness: int
    ) -> None:
        if SceneClass is not None and hasattr(self.bulb, "set_scene"):
            self.bulb.set_scene(SceneClass.COLOR, red, green, blue, brightness)
            return

        self.bulb.turn_on()
        self.bulb.set_rgb(red, green, blue)
        self.bulb.set_brightness(brightness)

    def wait_or_stop(self, milliseconds: int, started_at: float, duration_ms: int) -> bool:
        if duration_ms and (time.monotonic() - started_at) * 1000 >= duration_ms:
            return True
        return self.effect_stop.wait(milliseconds / 1000)

    def run_effect(self, effect: EffectConfig) -> None:
        started_at = time.monotonic()
        try:
            self.apply_solid(effect)
            if effect.effect == "solid":
                self.effect_stop.wait(effect.duration_ms / 1000)
            elif effect.effect == "pulse":
                self.run_pulse(effect, started_at)
            elif effect.effect == "flash":
                if not self.start_flash_flow(effect):
                    self.run_flash(effect, started_at)
            self.apply_after(effect)
        except (BulbException, OSError) as exc:
            self.last_effect_error = str(exc)
        finally:
            self.current_effect = None

    def start_flash_flow(self, effect: EffectConfig) -> bool:
        if Flow is None or RGBTransition is None:
            return False

        red, green, blue = effect.rgb
        flow = Flow(
            count=effect.count,
            action=Flow.actions.stay,
            transitions=[
                RGBTransition(
                    red,
                    green,
                    blue,
                    duration=effect.on_ms,
                    brightness=effect.brightness,
                ),
                RGBTransition(
                    red,
                    green,
                    blue,
                    duration=effect.off_ms,
                    brightness=effect.low_brightness,
                ),
            ],
        )
        with self.lock:
            self.bulb.start_flow(flow)

        if effect.count:
            total_seconds = effect.count * (effect.on_ms + effect.off_ms) / 1000
            self.effect_stop.wait(total_seconds)
            return True

        self.effect_stop.wait()
        return True

    def run_pulse(self, effect: EffectConfig, started_at: float) -> None:
        while not self.effect_stop.is_set():
            self.set_level(effect, effect.brightness)
            if self.wait_or_stop(effect.on_ms, started_at, effect.duration_ms):
                break
            self.set_level(effect, effect.low_brightness)
            if self.wait_or_stop(effect.off_ms, started_at, effect.duration_ms):
                break

    def run_flash(self, effect: EffectConfig, started_at: float) -> None:
        cycles = 0
        while not self.effect_stop.is_set():
            if effect.count and cycles >= effect.count:
                break
            self.set_level(effect, effect.brightness)
            if self.wait_or_stop(effect.on_ms, started_at, effect.duration_ms):
                break
            self.set_level(effect, effect.low_brightness)
            if self.wait_or_stop(effect.off_ms, started_at, effect.duration_ms):
                break
            cycles += 1

    def apply_after(self, effect: EffectConfig) -> None:
        if self.effect_stop.is_set():
            return
        if effect.after == "idle":
            self.current_state = "idle"
            self.apply_solid(self.effects["idle"])
            self.schedule_idle_off()
        elif effect.after == "off":
            self.cancel_idle_off()
            with self.lock:
                self.bulb.turn_off()
        else:
            self.apply_solid(effect)

    def snapshot(self) -> dict[str, Any]:
        return {
            "bulb_ip": self.ip,
            "current_state": self.current_state,
            "dry_run": self.dry_run,
            "music_mode_requested": self.music_mode_requested,
            "music_mode_active": self.music_mode_active,
            "music_lighting_active": self.music_lighting_active,
            "music_override_active": self.music_override_active,
            "music_host": self.music_host,
            "music_port": self.music_port,
            "config_path": str(self.config_path),
            "effect_active": (
                self.effect_thread is not None and self.effect_thread.is_alive()
            ),
            "current_effect": self.current_effect,
            "last_effect_error": self.last_effect_error,
            "idle_off_seconds": self.options.idle_off_seconds,
            "idle_off_remaining_seconds": (
                max(0.0, self.idle_off_due_at - time.monotonic())
                if self.idle_off_due_at is not None
                else None
            ),
            "pending_state": (
                self.pending_request.state_name if self.pending_request else None
            ),
            "deferred_state": (
                self.deferred_request.state_name if self.deferred_request else None
            ),
            "scheduler": {
                **self.stats,
                "coalesce_seconds": COALESCE_SECONDS,
                "min_apply_interval_seconds": MIN_APPLY_INTERVAL_SECONDS,
                "priority_hold_remaining_seconds": max(
                    0.0, self.priority_hold_until - time.monotonic()
                ),
            },
            "states": {name: asdict(effect) for name, effect in self.effects.items()},
        }


class YeelightBridgeHandler(BaseHTTPRequestHandler):
    server_version = "YeelightCodexBridge/0.2"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    @property
    def bridge(self) -> BridgeState:
        return self.server.bridge_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self.respond_json({"ok": True, "service": "yeelight-codex-bridge"})
        elif path == "/state":
            self.respond_json(
                {
                    "current_state": self.bridge.current_state,
                    "current_effect": self.bridge.current_effect,
                    "bulb_ip": self.bridge.ip,
                    "dry_run": self.bridge.dry_run,
                    "music_mode_active": self.bridge.music_mode_active,
                }
            )
        elif path == "/states":
            self.respond_json(self.bridge.snapshot())
        else:
            self.respond_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")

        if path == "/color":
            try:
                payload = self.read_json_body(required=True)
                rgb = self.required_rgb(payload)
                brightness = self.required_int(payload, "brightness")
                self.bridge.apply_color(rgb, brightness)
            except RequestError as exc:
                self.respond_error(exc.status, exc.message)
            except BulbException as exc:
                self.respond_error(
                    HTTPStatus.BAD_GATEWAY, f"Yeelight command failed: {exc}"
                )
            except OSError as exc:
                self.respond_error(
                    HTTPStatus.BAD_GATEWAY,
                    f"Could not reach Yeelight at {self.bridge.ip}: {exc}",
                )
            else:
                self.respond_json(
                    {
                        "ok": True,
                        "current_state": self.bridge.current_state,
                        "current_effect": self.bridge.current_effect,
                        "bulb_ip": self.bridge.ip,
                    }
                )
            return

        if path.startswith("/state/"):
            state_name = path.removeprefix("/state/")
            try:
                payload = self.read_json_body(required=False)
            except RequestError as exc:
                self.respond_error(exc.status, exc.message)
                return
        elif path == "/state":
            try:
                payload = self.read_json_body(required=True)
            except RequestError as exc:
                self.respond_error(exc.status, exc.message)
                return
            state_name = str(payload.get("state", ""))
        else:
            self.respond_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return

        if not state_name:
            self.respond_error(HTTPStatus.BAD_REQUEST, "Missing state name")
            return

        try:
            self.bridge.apply(
                state_name,
                flash_count=self.optional_int(payload, "count"),
                on_ms=self.optional_int(payload, "on_ms"),
                off_ms=self.optional_int(payload, "off_ms"),
            )
        except RequestError as exc:
            self.respond_error(exc.status, exc.message)
        except ValueError as exc:
            self.respond_error(HTTPStatus.BAD_REQUEST, str(exc))
        except BulbException as exc:
            self.respond_error(HTTPStatus.BAD_GATEWAY, f"Yeelight command failed: {exc}")
        except OSError as exc:
            self.respond_error(
                HTTPStatus.BAD_GATEWAY,
                f"Could not reach Yeelight at {self.bridge.ip}: {exc}",
            )
        else:
            self.respond_json(
                {
                    "ok": True,
                    "queued": True,
                    "current_state": self.bridge.current_state,
                    "current_effect": self.bridge.current_effect,
                    "pending_state": (
                        self.bridge.pending_request.state_name
                        if self.bridge.pending_request
                        else None
                    ),
                    "bulb_ip": self.bridge.ip,
                }
            )

    def read_json_body(self, *, required: bool) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            if required:
                raise RequestError(HTTPStatus.BAD_REQUEST, "Missing JSON body")
            return {}

        try:
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid JSON body")

        if not isinstance(payload, dict):
            raise RequestError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")

        return payload

    def optional_int(self, payload: dict[str, Any], key: str) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise RequestError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer")

    def required_int(self, payload: dict[str, Any], key: str) -> int:
        value = self.optional_int(payload, key)
        if value is None:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"Missing {key}")
        return value

    def required_rgb(self, payload: dict[str, Any]) -> tuple[int, int, int]:
        value = payload.get("rgb")
        if not isinstance(value, list) or len(value) != 3:
            raise RequestError(HTTPStatus.BAD_REQUEST, "rgb must be a 3-item array")
        try:
            parts = [int(part) for part in value]
        except (TypeError, ValueError):
            raise RequestError(HTTPStatus.BAD_REQUEST, "rgb values must be integers")
        return tuple(max(0, min(255, part)) for part in parts)  # type: ignore[return-value]

    def respond_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_error(self, status: HTTPStatus, message: str) -> None:
        self.respond_json({"ok": False, "error": message}, status=status.value)


class YeelightBridgeServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], bridge_state: BridgeState) -> None:
        super().__init__(address, YeelightBridgeHandler)
        self.bridge_state = bridge_state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local HTTP bridge for Yeelight Codex status lights."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument(
        "--ip",
        default=DEFAULT_IP,
        help=f"Yeelight LAN IP address, defaults to {DEFAULT_IP}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"effect config file, defaults to {DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="serve requests without touching the bulb",
    )
    parser.add_argument(
        "--no-music-mode",
        action="store_true",
        help="use normal Yeelight LAN commands instead of music mode",
    )
    parser.add_argument(
        "--music-host",
        help="local IP address the bulb should connect back to for music mode",
    )
    parser.add_argument(
        "--music-port",
        type=int,
        default=0,
        help="local music-mode port; 0 chooses a random free port",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        bridge_state = BridgeState(
            args.ip,
            args.dry_run,
            music_mode=not args.no_music_mode,
            music_host=args.music_host,
            music_port=args.music_port,
            config_path=args.config,
        )
    except BulbException as exc:
        print(f"Yeelight setup failed: {exc}")
        return 1
    except OSError as exc:
        print(f"Could not reach Yeelight at {args.ip}: {exc}")
        return 1

    server = YeelightBridgeServer((args.host, args.port), bridge_state)
    print(f"Yeelight bridge listening on http://{args.host}:{args.port}")
    print(
        f"Target bulb: {args.ip} dry_run={args.dry_run} "
        f"music_mode_active={bridge_state.music_mode_active}"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping bridge.")
    finally:
        bridge_state.close()
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
