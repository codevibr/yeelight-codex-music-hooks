# Yeelight D2 Codex Status Bridge

This project turns a Yeelight D2 into a local status lamp for Codex.

It can:

- show Codex status states (`idle`, `thinking`, `prompt`, `error`);
- survive noisy Codex hook bursts by centralizing state in one bridge process;
- use Yeelight music mode to avoid LAN command rate limits;
- let `prompt` and `error` override music beat lighting until Codex resumes;
- turn the bulb off after a configurable idle timeout;
- react to desktop audio beats through Windows WASAPI loopback.

The default bulb IP in the scripts is `192.168.1.52`. Change it with `--ip` or
edit `DEFAULT_IP` in `yeelight_codex.py` if your bulb uses another address.

## Project Layout

| Path | Purpose |
| --- | --- |
| `assistant_light.py` | Small client used by hooks and assistants. It posts states to the local bridge and starts the bridge if needed. |
| `yeelight_bridge.py` | Long-running HTTP bridge. Owns debouncing, priority, effects, idle timeout, music-mode connection, and music override rules. |
| `yeelight_codex.py` | Direct/manual Yeelight command tool and shared bulb wrapper. Useful for smoke tests. |
| `music_audio.py` | WASAPI loopback monitor and beat-to-light driver. |
| `config.ini` | Main user configuration for bridge behavior, status effects, and music-light tuning. |
| `.env.example` | Example local path configuration for rendering local Codex files. |
| `.env` | Your local path configuration. Ignored by git. |
| `build_codex_config.py` | Renders `.codex/hooks.json` and `AGENTS.md` from examples and `.env`. |
| `requirements.txt` | Python package dependencies. |
| `.codex/hooks.json.example` | Checked-in Codex hook template. Render it with `build_codex_config.py`. |
| `.codex/hooks.json` | Generated local Codex hook config. Ignored by git because it contains machine-specific paths. |
| `.codex/hooks/yeelight_status_hook.py` | Codex hook entrypoint. Reads hook JSON from stdin and calls `assistant_light.py`. |
| `.codex/config.toml` | Minimal hook feature flags for Codex project config. |
| `AGENTS.md.example` | Checked-in assistant instruction template. Render it with `build_codex_config.py`. |
| `AGENTS.md` | Generated local assistant instructions. Ignored by git because it contains machine-specific paths. |
| `CODEX_INTEGRATION.md` | Short integration notes. The README is the canonical guide. |

Runtime files such as `yeelight_hook.log`, `yeelight_last_state.json`,
`yeelight_prompt_latch.json`, `bridge_stdout.log`, and `__pycache__/` are
generated locally and should not be committed.

## Requirements

- Windows.
- Python 3.10 or newer. This project was tested with Python 3.14.
- A Yeelight D2 with LAN control enabled.
- The bulb and this computer must be on the same network.
- Optional for beat detection: desktop audio routed through a WASAPI output
  device.
- Codex with local hooks enabled.

Install Python packages:

```powershell
python -m pip install -r requirements.txt
```

Dependencies:

- `yeelight`: Yeelight LAN control.
- `numpy`: audio analysis.
- `pyaudiowpatch`: Windows WASAPI loopback audio capture.

## Yeelight Setup

1. Put the bulb on the same LAN as your computer.
2. Enable LAN control in the Yeelight app.
3. Give the bulb a stable DHCP lease if possible.
4. Test direct control:

```powershell
python .\yeelight_codex.py --ip 192.168.1.52 status
python .\yeelight_codex.py --ip 192.168.1.52 idle
python .\yeelight_codex.py --ip 192.168.1.52 thinking
python .\yeelight_codex.py --ip 192.168.1.52 prompt
python .\yeelight_codex.py --ip 192.168.1.52 error
```

If your bulb is not `192.168.1.52`, pass `--ip YOUR.BULB.IP` to commands or
change `DEFAULT_IP` in `yeelight_codex.py`.

## Quick Start

Start the bridge:

```powershell
python .\yeelight_bridge.py --music-host 192.168.1.49
```

`--music-host` should be this computer's LAN IP address. It lets the bulb call
back to the Python process for Yeelight music mode. Music mode removes the
normal Yeelight LAN command rate limit, which matters for pulses, flashes, and
beat lighting.

Find your LAN IP:

```powershell
Get-NetIPAddress -AddressFamily IPv4
```

Then drive states through the client:

```powershell
python .\assistant_light.py thinking
python .\assistant_light.py prompt
python .\assistant_light.py error
python .\assistant_light.py idle
```

The client posts to `http://127.0.0.1:8765` by default. If the bridge is not
running, it tries to start `yeelight_bridge.py` automatically.

## Bridge Endpoints

The bridge listens on `127.0.0.1:8765` by default.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Health check. |
| `GET /state` | Compact current state. |
| `GET /states` | Full snapshot including effects, scheduler stats, music flags, and idle timeout. |
| `POST /state` | Set a named state with JSON, e.g. `{"state":"prompt"}`. |
| `POST /state/idle` | Shortcut for a named state. |
| `POST /state/thinking` | Shortcut for a named state. |
| `POST /state/prompt` | Shortcut for a named state. |
| `POST /state/error` | Shortcut for a named state. |
| `POST /color` | Direct RGB/brightness update used by `music_audio.py lights`. |

Examples:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/state `
  -ContentType application/json `
  -Body '{"state":"prompt"}'
```

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/color `
  -ContentType application/json `
  -Body '{"rgb":[0,170,255],"brightness":100}'
```

```powershell
Invoke-RestMethod http://127.0.0.1:8765/states
```

## Status Semantics

The bridge recognizes these user-facing states:

| State | Meaning | Default effect |
| --- | --- | --- |
| `idle` | Codex is finished or waiting. | Dim warm solid. |
| `thinking` | Codex is working. | Blue pulse. |
| `prompt` | Codex needs approval/input. | Yellow pulse. |
| `error` | Codex is blocked or needs intervention. | Persistent red flash flow. |

Internal music states:

| State | Meaning |
| --- | --- |
| `music_start` | `music_audio.py lights` is taking background ownership. |
| `music` | Beat lighting is currently controlling the bulb. |
| `music_stop` | Stop music ownership and restore idle. |
| `off` | Idle timeout turned the bulb off. |

During music mode:

- `thinking` and `idle` do not clobber active beat lighting.
- `prompt` and `error` take ownership of the bulb.
- Music color updates are ignored while `prompt` or `error` owns the bulb.
- A later `thinking` or `idle` hook releases the prompt/error ownership and
  lets music resume.

This matches the simple Codex lifecycle used by
[loopbrew/codex-lamp](https://github.com/loopbrew/codex-lamp):

```text
prompt submitted -> working/thinking
approval needed  -> prompt/input
approved action  -> working/thinking
turn complete    -> idle
```

Error state note: the red `error` effect is implemented and can be triggered
manually with `assistant_light.py error`. For an indefinite flash, the bridge
uses a Yeelight color flow when supported, so the bulb keeps flashing without a
rapid Python command loop. The automatic assistant behavior for "permission
denied" or other blocked Codex failures is functionally wired by the assistant
instructions, but it is not strictly proven as a Codex hook-driven event. Treat
automatic error signaling as best-effort until it is tested with a real blocked
workflow.

## Configuration

Edit `config.ini`.

The file is intentionally heavily commented. It contains:

- `[bridge]`: bridge-level options such as `idle_off_seconds`.
- `[music]`: defaults for `music_audio.py lights`.
- `[idle]`, `[thinking]`, `[prompt]`, `[error]`: visual effect definitions.

The bridge reloads `config.ini` whenever a state is applied, so most effect
changes do not need a bridge restart. If you change command-line startup details
such as host, port, or bulb IP, restart the bridge.

### Bridge Settings

```ini
[bridge]
idle_off_seconds = 120
```

Examples:

```ini
idle_off_seconds = 120   ; turn off after 2 minutes of idle
idle_off_seconds = 900   ; turn off after 15 minutes
idle_off_seconds = 0     ; never auto-off
```

### Effect Settings

Supported effects:

```ini
effect = solid
effect = pulse
effect = flash
```

Common fields:

```ini
color = 34,94,255
brightness = 100
low_brightness = 1
on_ms = 1000
off_ms = 1000
duration_ms = 0
count = 0
after = hold
```

Meaning:

- `color`: RGB color.
- `brightness`: high brightness, 1..100.
- `low_brightness`: low brightness for pulse/flash, 1..100.
- `on_ms`: high-brightness phase duration.
- `off_ms`: low-brightness phase duration.
- `duration_ms`: total duration; `0` means until replaced.
- `count`: flash cycle count; `0` means until replaced.
- `after`: `hold`, `idle`, or `off` after a finite effect ends.

## Codex Hook Setup

This repo includes project hook files under `.codex/`.

Local filesystem paths are not committed directly into Codex files. Instead,
copy `.env.example` to `.env`, edit it for your machine, and render the local
`.codex/hooks.json` and `AGENTS.md` files.

Example `.env`:

```dotenv
YEELIGHT_CODEX_ROOT=C:\Users\you\Documents\Codex\yeelight-codex-music-hooks
YEELIGHT_CODEX_PYTHON=python
```

For this project:

- `YEELIGHT_CODEX_ROOT` is the absolute path to the cloned repo.
- `YEELIGHT_CODEX_PYTHON` is the Python executable Codex should use in hooks.
  `python` is fine if Python is on your PATH. Use an absolute path if needed.

Render local Codex config:

```powershell
python .\build_codex_config.py
```

Check whether generated config is current:

```powershell
python .\build_codex_config.py --check
```

The generated `.codex/hooks.json` and `AGENTS.md` are intentionally ignored by
git. Commit `.env.example`, `.codex/hooks.json.example`, and
`AGENTS.md.example`, not your local `.env` or generated files.

Enable hooks in `.codex/config.toml`:

```toml
[features]
hooks = true
codex_hooks = true
```

Install hooks in one of two ways.

### Option A: Project Hooks

Generate `.codex/hooks.json` with `build_codex_config.py` and keep it in this
project directory.

After editing hooks, restart Codex or reload the workspace. Review/trust the
hook when Codex asks. In the Codex CLI, use:

```text
/hooks
```

### Option B: Global Hooks

Generate `.codex/hooks.json`, then copy or merge it into:

```text
~/.codex/hooks.json
```

Use global hooks if you want the lamp to follow Codex across all workspaces.
Use project hooks if you only want this behavior inside this repo.

### Hook Mapping

| Codex hook | Lamp state |
| --- | --- |
| `PermissionRequest` | `prompt` |
| `Stop` | `idle` |

`UserPromptSubmit` and `PreToolUse` are intentionally not wired as automatic
`thinking` triggers. Codex can emit those while a workspace starts or resumes,
before the assistant is doing visible work, which makes the bulb pulse blue for
minutes. Explicit `thinking` updates are handled by the generated `AGENTS.md`
instructions instead.

The hook writes diagnostics to `yeelight_hook.log`. That file is ignored by git.

## Assistant Instructions

`AGENTS.md.example` contains local instructions for Codex itself. Run
`build_codex_config.py` to render `AGENTS.md` with your local paths. The
important generated commands look like:

```powershell
python "C:\Users\YOU\path\to\Yeelight D2\assistant_light.py" thinking --quiet --ignore-errors
python "C:\Users\YOU\path\to\Yeelight D2\assistant_light.py" prompt --quiet --ignore-errors
python "C:\Users\YOU\path\to\Yeelight D2\assistant_light.py" error --quiet --ignore-errors
python "C:\Users\YOU\path\to\Yeelight D2\assistant_light.py" idle --quiet --ignore-errors
```

If you publish or clone this repo elsewhere, do not commit your generated
`AGENTS.md`. Commit `AGENTS.md.example`; each user should render their own local
copy from `.env`.

## Music Beat Lighting

List loopback audio devices:

```powershell
python .\music_audio.py devices
```

Monitor levels without touching the bulb:

```powershell
python .\music_audio.py monitor --seconds 10
```

Monitor a specific loopback device:

```powershell
python .\music_audio.py monitor --device 14 --seconds 10
```

Drive the lamp from beats:

```powershell
python .\music_audio.py lights
```

Run for six minutes:

```powershell
python .\music_audio.py lights --seconds 360
```

Override tuning for one run:

```powershell
python .\music_audio.py lights `
  --seconds 60 `
  --cooldown-ms 420 `
  --min-send-ms 500 `
  --gain 500 `
  --beat-brightness 90
```

Music settings live in `[music]` in `config.ini`.

| Setting | Meaning |
| --- | --- |
| `seconds` | Default run duration. |
| `cooldown_ms` | Minimum time between beat hits. Higher is calmer; lower is busier. |
| `min_send_ms` | Minimum time between non-beat brightness updates. |
| `beat_brightness` | Brightness for beat hits. |
| `idle_brightness` | Minimum brightness between beats. |
| `gain` | Converts audio RMS into between-beat brightness. |
| `idle_rgb` | Color used between beats. |
| `palette` | Semicolon-separated beat colors. |
| `restore_state` | State to restore after music exits. |

Examples:

```ini
cooldown_ms = 180  ; fast, busy, may double-hit
cooldown_ms = 320  ; balanced
cooldown_ms = 500  ; calmer
```

```ini
gain = 400   ; subtle between-beat glow
gain = 700   ; balanced
gain = 1000  ; brighter between beats
```

## Troubleshooting

### The bulb stays blue during music

Check whether Codex status hooks are clobbering music:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/states
```

Look for:

```text
music_lighting_active = true
current_state = music
```

If `current_state` is `thinking`, start music with:

```powershell
python .\music_audio.py lights
```

### Music mode fails with `invalid params`

The bulb may have stale music-mode state after a killed process. Reset it:

```powershell
python - <<'PY'
from yeelight_codex import build_bulb, DEFAULT_IP
bulb = build_bulb(DEFAULT_IP)
print(bulb.stop_music())
PY
```

PowerShell does not support that heredoc syntax directly. In PowerShell, use:

```powershell
@'
from yeelight_codex import build_bulb, DEFAULT_IP
bulb = build_bulb(DEFAULT_IP)
print(bulb.stop_music())
'@ | python -
```

Then restart the bridge:

```powershell
python .\yeelight_bridge.py --music-host 192.168.1.49
```

### Music mode cannot start

Try an explicit callback host:

```powershell
python .\yeelight_bridge.py --music-host 192.168.1.49
```

Also check Windows Firewall. The bulb must be able to connect back to your
computer.

Disable music mode if needed:

```powershell
python .\yeelight_bridge.py --no-music-mode
```

### Prompt hangs yellow for several seconds after approval

This depends on Codex hook timing. The project uses the same lifecycle model as
`loopbrew/codex-lamp`: `PermissionRequest` turns on prompt, and the next
`PreToolUse` releases it back to thinking/music. If Codex emits the next tool
hook a few seconds after you approve, the lamp will remain prompt during that
gap.

### The bridge is running but the lamp looks wrong

Inspect bridge state:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/states
```

Force idle:

```powershell
python .\assistant_light.py idle
```

Restart bridge:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'yeelight_bridge\.py' } |
  Select-Object ProcessId, CommandLine
```

Then stop the listed process and restart:

```powershell
Stop-Process -Id YOUR_PROCESS_ID -Force
python .\yeelight_bridge.py --music-host 192.168.1.49
```

## Git Hygiene

Recommended files to commit:

- Python scripts.
- `config.ini`.
- `requirements.txt`.
- `.env.example`.
- `AGENTS.md.example`.
- `.codex/config.toml`.
- `.codex/hooks.json.example`.
- `.codex/hooks/yeelight_status_hook.py`.
- docs.

Do not commit generated local/runtime files:

- `.env`
- `AGENTS.md`
- `.codex/hooks.json`
- `.codex/prompt_latch.json`
- `__pycache__/`
- `*.pyc`
- `yeelight_hook.log`
- `yeelight_last_state.json`
- `yeelight_prompt_latch.json`
- bridge stdout/stderr logs
