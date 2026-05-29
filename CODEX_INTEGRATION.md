# Codex Integration Notes

The full setup guide lives in `README.md`. This file is a short checklist.

## Enable Hooks

Project-local `.codex/config.toml`:

```toml
[features]
hooks = true
codex_hooks = true
```

## Install Hooks

Copy `.env.example` to `.env`, edit `YEELIGHT_CODEX_ROOT`, and render the
project hook config:

```powershell
python .\build_codex_config.py
```

Use the generated project copy:

```text
.codex/hooks.json
.codex/hooks/yeelight_status_hook.py
```

or merge `.codex/hooks.json` into:

```text
~/.codex/hooks.json
```

Important: `.codex/hooks.json` is generated and ignored by git because it
contains local paths. Commit `.codex/hooks.json.example` and `.env.example`.

## Hook Mapping

| Hook | State |
| --- | --- |
| `PermissionRequest` | `prompt` |
| `Stop` | `idle` |

`UserPromptSubmit` and `PreToolUse` are intentionally not wired as automatic
`thinking` triggers. Codex can emit those during startup/resume work that is not
visible to the user, which makes the lamp blink blue for minutes. The generated
`AGENTS.md` instructions handle explicit assistant-side `thinking` updates when
real work is happening.

## Bridge

Start manually:

```powershell
python .\yeelight_bridge.py --music-host YOUR_LAN_IP
```

Or let `assistant_light.py` auto-start it:

```powershell
python .\assistant_light.py thinking --quiet --ignore-errors
```

Check state:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/states
```
