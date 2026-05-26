# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **AGENTS.md** at the repo root has the full development guide (architecture, pitfalls, config system, plugins, delegation). Read it for deep context. This file is the quick-reference companion.

## Quick Start

```bash
source .venv/bin/activate   # or: source venv/bin/activate
# Fresh clone: ./setup-hermes.sh installs uv, creates venv, installs .[all], symlinks hermes
```

## Commands

```bash
# Testing — ALWAYS use the wrapper (enforces CI parity: -n 4, TZ=UTC, no creds)
scripts/run_tests.sh                                    # full suite
scripts/run_tests.sh tests/gateway/                     # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x    # one test
scripts/run_tests.sh -v --tb=long                       # pass-through pytest flags

# Linting & formatting
ruff check .
ruff format .

# TUI dev
cd ui-tui && npm install && npm run dev   # watch mode
cd ui-tui && npm run build                # production build
cd ui-tui && npm test                     # vitest
```

Python 3.11+, managed with `uv`. Install: `uv pip install -e ".[all,dev]"`.

## Architecture Overview

```
hermes (CLI entry) → hermes_cli/main.py → cli.py (HermesCLI)  | Interactive terminal
                   → gateway/run.py                            | Messaging platforms
                   → acp_adapter/                              | VS Code/Zed/JetBrains

run_agent.py     — AIAgent class, core conversation loop, ~60 init params
model_tools.py   — Tool orchestration, handle_function_call(), tool discovery
hermes_state.py  — SQLite session store with FTS5 search
toolsets.py      — TOOLSETS dict + _HERMES_CORE_TOOLS (default tool bundle)
cli.py           — HermesCLI, Rich+prompt_toolkit, slash command dispatch
hermes_cli/      — Subcommands, setup wizard, config, skin engine, plugins loader
tools/           — Tool implementations, auto-discovered via tools/registry.py
gateway/         — Messaging gateway (Telegram, Discord, Slack, WhatsApp, etc.)
agent/           — Provider adapters, memory manager, curator, context compression
plugins/         — Model providers, memory backends, context engines, kanban
skills/          — Bundled skills (active by default)
optional-skills/ — Heavier/niche skills shipped but not active by default
ui-tui/          — Ink (React) TUI; tui_gateway/ is its Python JSON-RPC backend
tests/           — pytest suite, ~17k tests
```

### File Dependency Chain

```
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

### Config Loaders (three paths — adding to wrong one causes silent misses)

| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` |
| `load_config()` | `hermes tools`, `hermes setup`, most subcommands | `hermes_cli/config.py` |
| Direct YAML load | Gateway runtime | `gateway/run.py` + `gateway/config.py` |

### Top-level `config.yaml` sections (where to put new keys)

`model`, `agent`, `terminal`, `compression`, `display`, `stt`, `tts`, `memory`, `security`, `delegation`, `smart_model_routing`, `checkpoints`, `auxiliary`, `curator`, `skills`, `gateway`, `logging`, `cron`, `profiles`, `plugins`, `honcho`.

`auxiliary` holds per-task overrides for side-LLM work (curator, vision, embedding, title generation, etc.). `curator` holds background skill-maintenance config.

### Skin/Theme System

`hermes_cli/skin_engine.py` — data-driven CLI theming. Skins are pure YAML data (4 built-in: `default`, `ares`, `mono`, `slate`; user skins in `~/.hermes/skins/*.yaml`). Customize banner colors, spinner faces/verbs/wings, tool prefix, branding text. Activate via `/skin <name>` or `display.skin` in config. Missing values inherit from `default`. Add a built-in skin by adding to `_BUILTIN_SKINS` dict.

### TUI

`hermes --tui` spawns Node (Ink/React) ↔ Python (tui_gateway) over stdio JSON-RPC. TypeScript owns the screen; Python owns sessions, tools, and model calls. The dashboard (`hermes dashboard`) embeds the real `hermes --tui` via a PTY bridge — do not re-implement the transcript/composer in React.

## Key Rules

- **Use `get_hermes_home()`** from `hermes_constants` for all `~/.hermes` paths. Never hardcode `~/.hermes`. Use `display_hermes_home()` for user-facing messages. Profiles use separate `HERMES_HOME` directories — hardcoded paths break multi-profile setups.
- **`scripts/run_tests.sh`** — never call `pytest` directly. The script enforces CI-parity hermetic environment.
- **Tests must not write to `~/.hermes/`** — the `_isolate_hermes_home` autouse fixture handles redirection.
- **Don't break prompt caching** — don't alter past context, change toolsets, or reload memories mid-conversation. Slash commands that mutate system-prompt state must be cache-aware: default to deferred invalidation (next session), with an opt-in `--now` flag for immediate invalidation.
- **Slash commands** — defined in `hermes_cli/commands.py` (COMMAND_REGISTRY). Adding an alias only requires updating the `aliases` tuple on the existing CommandDef.
- **New tools** — create `tools/<name>.py` with `registry.register()` + add to a toolset in `toolsets.py`. For local/custom tools, prefer `~/.hermes/plugins/<name>/`.
- **New config keys** — add to `DEFAULT_CONFIG` in `hermes_cli/config.py`. Secrets (API keys) go in `.env` via `OPTIONAL_ENV_VARS`.
- **Plugins must NOT modify core files** (`run_agent.py`, `cli.py`, `gateway/run.py`, `hermes_cli/main.py`). Expand the plugin surface instead.
- **No new `simple_term_menu`** usage — use `hermes_cli/curses_ui.py` for interactive menus.

## Profiles

Hermes supports multiple isolated instances via profiles (`hermes -p <name>`). Each profile gets its own `HERMES_HOME` directory. All path references in code **must** use `get_hermes_home()` / `display_hermes_home()` from `hermes_constants` — never hardcode `~/.hermes`. Profile operations are HOME-anchored (not HERMES_HOME-anchored): `_get_profiles_root()` always returns `~/.hermes/profiles/` so `hermes -p coder profile list` sees all profiles.

## Testing Notes

`scripts/run_tests.sh` enforces CI-parity by unsetting all `*_API_KEY`/`*_TOKEN` vars, forcing `TZ=UTC`, `LANG=C.UTF-8`, and `-n 4` xdist workers. If you can't use the wrapper (IDE, Windows), at minimum pass `-n 4` — higher worker counts surface ordering flakes CI never sees. Always run the full suite before pushing.

## Key Pitfalls

- **Hardcoded `~/.hermes` paths break profiles** — each profile has its own `HERMES_HOME`.
- **ANSI `\033[K`** leaks as literal `?[K` under `prompt_toolkit`'s `patch_stdout`. Use space-padding instead.
- **`_last_resolved_tool_names`** in `model_tools.py` is process-global — delegate_tool saves/restores it around subagent runs.
- **Cross-tool references in schema descriptions** — don't mention tools from other toolsets by name. If a tool is unavailable, the model will hallucinate calls to it. Add dynamic references in `get_tool_definitions()` instead.
- **Gateway has two message guards** — new commands that must reach the runner while the agent is blocked must bypass both the base adapter queue and the gateway runner intercept.
- **Squash merges from stale branches** silently revert recent fixes on main. Always rebase the PR branch onto latest main before squash-merging.
- **Don't write change-detector tests** — tests that assert specific model names, catalog counts, or config version literals break on every routine update. Write invariant/relationship tests instead.
