#!/usr/bin/env python
"""WASAPI loopback discovery and dry-run audio monitor."""

from __future__ import annotations

import argparse
import configparser
import json
import math
import sys
import time
from collections import deque
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen

DEFAULT_CONFIG = "config.ini"

try:
    import numpy as np
    import pyaudiowpatch as pyaudio
except ImportError as exc:  # pragma: no cover - dependency guidance path.
    print(
        "Missing audio dependency. Install with "
        "`python -m pip install -r requirements.txt`.",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


@dataclass(frozen=True)
class AudioLevel:
    rms: float
    peak: float
    db: float
    beat: bool
    bar: str


PALETTE = [
    (0, 170, 255),
    (255, 52, 130),
    (255, 214, 36),
    (68, 255, 154),
    (164, 96, 255),
]


@dataclass(frozen=True)
class MusicLightConfig:
    bridge_url: str
    seconds: float
    cooldown_ms: int
    min_send_ms: int
    beat_brightness: int
    idle_brightness: int
    gain: float
    idle_rgb: tuple[int, int, int]
    restore_state: str | None
    palette: list[tuple[int, int, int]]


def loopback_devices(pa: pyaudio.PyAudio) -> list[dict]:
    return list(pa.get_loopback_device_info_generator())


def default_loopback_device(pa: pyaudio.PyAudio) -> dict:
    return pa.get_default_wasapi_loopback()


def find_device(pa: pyaudio.PyAudio, index: int | None) -> dict:
    if index is None:
        return default_loopback_device(pa)

    for device in loopback_devices(pa):
        if int(device["index"]) == index:
            return device

    raise ValueError(f"No WASAPI loopback device with index {index}")


def print_devices() -> None:
    pa = pyaudio.PyAudio()
    try:
        default_index = int(default_loopback_device(pa)["index"])
        print("WASAPI loopback devices:")
        for device in loopback_devices(pa):
            marker = "*" if int(device["index"]) == default_index else " "
            channels = int(device["maxInputChannels"])
            sample_rate = int(float(device["defaultSampleRate"]))
            print(
                f"{marker} index={device['index']:>2} "
                f"channels={channels} rate={sample_rate}Hz "
                f"name={device['name']}"
            )
    finally:
        pa.terminate()


def load_music_light_config(path: str) -> MusicLightConfig:
    parser = configparser.ConfigParser()
    parser.read(path)
    section = parser["music"] if parser.has_section("music") else {}

    return MusicLightConfig(
        bridge_url=section.get("bridge_url", "http://127.0.0.1:8765"),
        seconds=float(section.get("seconds", 20.0)),
        cooldown_ms=int(section.get("cooldown_ms", 320)),
        min_send_ms=int(section.get("min_send_ms", 350)),
        beat_brightness=max(1, min(100, int(section.get("beat_brightness", 100)))),
        idle_brightness=max(1, min(100, int(section.get("idle_brightness", 10)))),
        gain=float(section.get("gain", 700.0)),
        idle_rgb=parse_rgb_arg(section.get("idle_rgb", "28,42,72")),
        restore_state=section.get("restore_state", "idle") or None,
        palette=parse_palette_arg(section.get("palette", "")),
    )


def dbfs(rms: float) -> float:
    if rms <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms))


def make_bar(rms: float, width: int) -> str:
    filled = min(width, max(0, round(rms * width * 3.0)))
    return "#" * filled + "." * (width - filled)


def analyze_chunk(samples: np.ndarray, history: deque[float], width: int) -> AudioLevel:
    mono = samples.mean(axis=1) if samples.ndim > 1 else samples
    rms = float(np.sqrt(np.mean(np.square(mono), dtype=np.float64)))
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0

    baseline = sum(history) / len(history) if history else 0.0
    beat = rms > 0.018 and rms > max(baseline * 1.65, baseline + 0.018)
    history.append(rms)

    return AudioLevel(
        rms=rms,
        peak=peak,
        db=dbfs(rms),
        beat=beat,
        bar=make_bar(rms, width),
    )


def iter_audio_levels(
    *,
    device_index: int | None,
    chunk_ms: int,
    history_size: int,
    bar_width: int,
) -> Iterable[AudioLevel]:
    pa = pyaudio.PyAudio()
    stream = None
    try:
        device = find_device(pa, device_index)
        channels = int(device["maxInputChannels"])
        sample_rate = int(float(device["defaultSampleRate"]))
        frames_per_buffer = max(1, int(sample_rate * chunk_ms / 1000))
        history: deque[float] = deque(maxlen=history_size)
        chunks: Queue[bytes] = Queue(maxsize=8)

        def on_audio(
            in_data: bytes,
            frame_count: int,
            time_info: dict,
            status_flags: int,
        ) -> tuple[None, int]:
            del frame_count, time_info, status_flags
            try:
                chunks.put_nowait(in_data)
            except Exception:
                pass
            return None, pyaudio.paContinue

        stream = pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=int(device["index"]),
            frames_per_buffer=frames_per_buffer,
            stream_callback=on_audio,
        )
        stream.start_stream()

        while True:
            try:
                raw = chunks.get(timeout=max(0.1, chunk_ms / 1000 * 4))
            except Empty:
                silent_samples = np.zeros((frames_per_buffer, channels), dtype=np.float32)
                yield analyze_chunk(silent_samples, history, bar_width)
                continue

            samples = np.frombuffer(raw, dtype=np.float32)
            if channels > 1:
                samples = samples.reshape(-1, channels)
            yield analyze_chunk(samples, history, bar_width)
    finally:
        if stream is not None:
            stream.stop_stream()
            stream.close()
        pa.terminate()


def monitor(args: argparse.Namespace) -> None:
    pa = pyaudio.PyAudio()
    try:
        device = find_device(pa, args.device)
        print(
            "Monitoring "
            f"index={device['index']} name={device['name']} "
            f"for {args.seconds:.1f}s"
        )
    finally:
        pa.terminate()

    started_at = time.monotonic()
    for level in iter_audio_levels(
        device_index=args.device,
        chunk_ms=args.chunk_ms,
        history_size=args.history,
        bar_width=args.width,
    ):
        elapsed = time.monotonic() - started_at
        beat_marker = "BEAT" if level.beat else "    "
        print(
            f"{elapsed:6.2f}s {beat_marker} "
            f"rms={level.rms:0.4f} peak={level.peak:0.4f} "
            f"db={level.db:6.1f} |{level.bar}|"
        )

        if elapsed >= args.seconds:
            break


def post_bridge_color(
    bridge_url: str,
    rgb: tuple[int, int, int],
    brightness: int,
    *,
    timeout: float,
) -> None:
    payload = {"rgb": list(rgb), "brightness": max(1, min(100, brightness))}
    request = Request(
        f"{bridge_url.rstrip('/')}/color",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        response.read()


def post_bridge_state(bridge_url: str, state: str, *, timeout: float) -> None:
    payload = {"state": state}
    request = Request(
        f"{bridge_url.rstrip('/')}/state",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        response.read()


def coalesce_music_args(args: argparse.Namespace) -> argparse.Namespace:
    defaults = load_music_light_config(args.config)
    args.bridge_url = args.bridge_url or defaults.bridge_url
    args.seconds = args.seconds if args.seconds is not None else defaults.seconds
    args.cooldown_ms = (
        args.cooldown_ms if args.cooldown_ms is not None else defaults.cooldown_ms
    )
    args.min_send_ms = (
        args.min_send_ms if args.min_send_ms is not None else defaults.min_send_ms
    )
    args.beat_brightness = (
        args.beat_brightness
        if args.beat_brightness is not None
        else defaults.beat_brightness
    )
    args.idle_brightness = (
        args.idle_brightness
        if args.idle_brightness is not None
        else defaults.idle_brightness
    )
    args.gain = args.gain if args.gain is not None else defaults.gain
    args.idle_rgb = args.idle_rgb or defaults.idle_rgb
    args.restore_state = (
        args.restore_state if args.restore_state is not None else defaults.restore_state
    )
    args.palette = args.palette or defaults.palette or PALETTE
    return args


def music_lights(args: argparse.Namespace) -> None:
    args = coalesce_music_args(args)
    pa = pyaudio.PyAudio()
    try:
        device = find_device(pa, args.device)
        print(
            "Driving bridge lights from "
            f"index={device['index']} name={device['name']} "
            f"for {args.seconds:.1f}s"
        )
    finally:
        pa.terminate()

    started_at = time.monotonic()
    last_send_at = 0.0
    last_beat_at = 0.0
    palette_index = 0

    try:
        post_bridge_state(args.bridge_url, "music_start", timeout=args.timeout)
        for level in iter_audio_levels(
            device_index=args.device,
            chunk_ms=args.chunk_ms,
            history_size=args.history,
            bar_width=args.width,
        ):
            now = time.monotonic()
            elapsed = now - started_at
            cooldown_s = args.cooldown_ms / 1000
            minimum_send_s = args.min_send_ms / 1000

            if level.beat and now - last_beat_at >= cooldown_s:
                rgb = args.palette[palette_index % len(args.palette)]
                palette_index += 1
                post_bridge_color(
                    args.bridge_url,
                    rgb,
                    args.beat_brightness,
                    timeout=args.timeout,
                )
                last_beat_at = now
                last_send_at = now
                print(
                    f"{elapsed:6.2f}s BEAT rgb={rgb} "
                    f"brightness={args.beat_brightness:3} |{level.bar}|"
                )
            elif now - last_send_at >= minimum_send_s:
                brightness = max(
                    args.idle_brightness,
                    min(args.beat_brightness, round(level.rms * args.gain)),
                )
                post_bridge_color(
                    args.bridge_url,
                    args.idle_rgb,
                    brightness,
                    timeout=args.timeout,
                )
                last_send_at = now

            if elapsed >= args.seconds:
                break
    except (TimeoutError, URLError, OSError) as exc:
        print(f"Bridge light update failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        if args.restore_state:
            try:
                if args.restore_state == "idle":
                    post_bridge_state(args.bridge_url, "music_stop", timeout=args.timeout)
                else:
                    post_bridge_state(
                        args.bridge_url,
                        args.restore_state,
                        timeout=args.timeout,
                    )
            except (TimeoutError, URLError, OSError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and dry-run monitor WASAPI loopback audio."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("devices", help="list WASAPI loopback devices")

    monitor_parser = subparsers.add_parser(
        "monitor", help="print audio levels and simple beat/onset detections"
    )
    monitor_parser.add_argument(
        "--device",
        type=int,
        help="loopback device index; defaults to the default WASAPI loopback",
    )
    monitor_parser.add_argument(
        "--seconds",
        type=float,
        default=10.0,
        help="monitor duration",
    )
    monitor_parser.add_argument(
        "--chunk-ms",
        type=int,
        default=50,
        help="audio analysis window size",
    )
    monitor_parser.add_argument(
        "--history",
        type=int,
        default=24,
        help="number of windows for rolling beat baseline",
    )
    monitor_parser.add_argument(
        "--width",
        type=int,
        default=36,
        help="ASCII level meter width",
    )

    lights_parser = subparsers.add_parser(
        "lights", help="drive bridge colors from loopback beat detections"
    )
    lights_parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="config.ini path with [music] defaults",
    )
    lights_parser.add_argument("--device", type=int, help="loopback device index")
    lights_parser.add_argument("--seconds", type=float)
    lights_parser.add_argument("--chunk-ms", type=int, default=50)
    lights_parser.add_argument("--history", type=int, default=24)
    lights_parser.add_argument("--width", type=int, default=28)
    lights_parser.add_argument(
        "--bridge-url",
        default=None,
        help="local Yeelight bridge URL",
    )
    lights_parser.add_argument("--timeout", type=float, default=1.0)
    lights_parser.add_argument("--cooldown-ms", type=int)
    lights_parser.add_argument("--min-send-ms", type=int)
    lights_parser.add_argument("--beat-brightness", type=int)
    lights_parser.add_argument("--idle-brightness", type=int)
    lights_parser.add_argument("--gain", type=float)
    lights_parser.add_argument(
        "--idle-rgb",
        type=lambda value: parse_rgb_arg(value),
        default=None,
        help="idle RGB as r,g,b",
    )
    lights_parser.add_argument(
        "--palette",
        type=lambda value: parse_palette_arg(value),
        help="semicolon-separated RGB list, e.g. 0,170,255;255,52,130",
    )
    lights_parser.add_argument(
        "--restore-state",
        default=None,
        choices=("idle", "thinking", "prompt", "error", ""),
        help="bridge state to restore when done; empty leaves last music color",
    )

    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        if args.command == "devices":
            print_devices()
        elif args.command == "monitor":
            monitor(args)
        elif args.command == "lights":
            if args.restore_state == "":
                args.restore_state = None
            music_lights(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Audio capture failed: {exc}", file=sys.stderr)
        return 1

    return 0


def parse_rgb_arg(value: str) -> tuple[int, int, int]:
    try:
        parts = [int(part.strip()) for part in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("RGB must be r,g,b")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("RGB must have three values")
    return tuple(max(0, min(255, part)) for part in parts)  # type: ignore[return-value]


def parse_palette_arg(value: str) -> list[tuple[int, int, int]]:
    if not value.strip():
        return []
    return [parse_rgb_arg(item.strip()) for item in value.split(";") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
