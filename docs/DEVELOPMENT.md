# Development

Guide for contributing to igni and understanding the codebase.

## Project Structure

Packages, not files. A tree that names every module is wrong the week
after it is written — the last one listed 19 top-level paths and all 19
had been deleted. What follows is the set of directories that have
survived the refactors, with one line on what each is *for*. For the
current file list inside any of them, `ls` it.

```
igni/
├── src/igni_core/         # the Python package (import name stays `igni_core`)
│   ├── __main__.py         # entry point behind `igni`, `ignite-ember`, `python -m igni_core`
│   ├── cli/                # non-interactive command surface: flags, `-m`, pipe mode, resume
│   ├── backend/            # the long-lived server the GUI clients speak to — slash-command
│   │   │                   # and RPC dispatch, run streaming, HITL, session pool, panels
│   │   ├── panels/         # data behind the clients' agents / skills / hooks panels
│   │   └── rpc_handlers/   # one handler module per RPC domain (auth, extensions, hooks,
│   │                       # mcp, panels, session, skills, plugins, scheduler, …)
│   ├── core/               # everything that is not CLI, wire format, or server plumbing
│   │   ├── agents/         # agent `.md` discovery, frontmatter parsing, AgentPool, agent build
│   │   ├── auth/           # browser login, local callback server, credential storage
│   │   ├── config/         # Settings, model registry, permission evaluation, policy tiers
│   │   ├── db/             # SQLAlchemy engine + sessionmaker cache; `schema.py` migrates only a file that is new or behind
│   │   ├── decisions/      # the client for igni's decisions service: the Bypass guard and `host.decide`
│   │   ├── evals/          # YAML eval loading, assertions, runner, reporter
│   │   ├── guardrails/     # one rule per file (PII, injection, moderation) behind one runner
│   │   ├── hooks/          # hook discovery, matcher, executor, tool-event pipeline
│   │   ├── init/           # first-run and per-project initialization, hook provisioning
│   │   ├── loop/           # `/loop` state — caps, stored prompt, per-iteration tool results
│   │   ├── lsp/            # language-server client and per-project manager
│   │   ├── mcp/            # MCP client, transports, `.mcp.json` config, approval, tool filter
│   │   ├── memory/         # Agno memory / DB setup
│   │   ├── monitors/       # plugin-declared background monitors (config, supervisor, handles)
│   │   ├── output_styles/  # output-style catalog loading
│   │   ├── plugins/        # plugin + marketplace discovery, install, enable/disable state
│   │   ├── prompts/        # the main-agent and plan-mode system prompts (`.md`)
│   │   ├── scheduler/      # `/schedule`: time-expression parsing, recurrence, store, runner
│   │   ├── session/        # the Session — wiring, run loop, persistence, compaction, commands
│   │   ├── skills/         # `SKILL.md` discovery, frontmatter parsing, invocation
│   │   ├── tools/          # every agent tool: edit, shell/process, orchestrate, plan, todo, web
│   │   └── utils/          # project context (`igni.md`), audit log, display, mentions, tips
│   ├── decisions_server/   # igni's decisions service: Laya in its own process (`[decisions]` extra)
│   ├── protocol/           # the BE↔FE wire contract — Pydantic message models under `schemas/`
│   ├── transport/          # how those messages move: unix socket, websocket, in-process, NDJSON
│   ├── bundled_agents/     # the agent `.md` files that ship inside the wheel
│   ├── bundled_skills/     # the `SKILL.md` directories that ship inside the wheel
│   └── migrations/         # Alembic environment + `versions/`
├── agents/                 # repo-root copies of most agent `.md` files, read by a few
│                           # scripts and one parity test. `bundled_agents/` is what ships,
│                           # and the two have already drifted apart.
├── skills/                 # repo-root copies of the bundled skill directories
├── docs/                   # this file and its siblings — `ls docs/` is the list
├── evals/                  # YAML eval suites and their fixtures
├── scripts/                # dev + ops scripts: profiling, eval drivers, client builds
├── tests/                  # hundreds of files, flat, one per behaviour.
│   │                       # Not listed here: a hand-kept sample reads as the
│   │                       # whole suite and goes stale silently — the last one
│   │                       # still named test_knowledge.py, test_permissions.py,
│   │                       # test_widgets.py and test_tui_handlers.py long after
│   │                       # all four were deleted. `ls tests/` is the list.
│   └── conftest.py         # Shared fixtures; .env loading; the `integration`
│                           # marker and its skip
├── Makefile                # Build commands (see below)
├── pyproject.toml          # Package metadata, deps, tool config
├── igni.md                 # Project instructions for self-use
├── README.md
└── LICENSE
```

There is no `tui/` package, and no `clients/` directory either. The
Textual TUI was removed and the front-ends moved to their own repo:
the React clients live in igni-app (`clients/web`, the web client and
the UI the desktop app loads, and `clients/tauri`, the desktop shell).
They talk to the backend started by `python -m igni_core.backend`.

## Setup

```bash
# Clone
git clone https://github.com/ignite-ember/igni-core.git
cd igni-core

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install with all extras
make install
# or: pip install -e ".[dev,mcp,web]"
# The extras are whatever `[project.optional-dependencies]` in
# pyproject.toml defines. `knowledge` was listed here for a long time
# after it was removed; pip only warns about an extra that does not
# exist, so the line kept working and kept installing nothing.

# The cloud layer (igni in a producer's VM, PostgreSQL storage) is its
# own package on top of the core; install it to work on the cloud:
# uv pip install -e packages/igni-cloud   (tests: packages/igni-cloud/tests)

# Verify
igni --version
make check
```

### Running the web client against a backend

A backend you start by hand (`python -m igni_core.backend --ws-port 8765
--project-dir .`) requires a token too. It mints its own window token at
start and prints it once to stderr (one line, "igni window token for this
backend"); it is never written to disk, so copy it from the terminal. A
window connection offers it as the subprotocol `igni-token.<token>`
(the web client takes it as `#igni_ws_token=…`), and a program passes it
as `Local(url=..., token=...)` in igni-client. A program on the same
project needs no window token: `Local(project_dir)` reads the program
token from the owner-only `.igni/backend.lock`. The dev server of the web
client (`npm --prefix clients/web run dev` in igni-app, which is where
the clients live) still has the `Origin` check besides the token. A
backend the desktop app spawns requires its per-launch token
(`IGNI_WS_TOKEN`, handed to the webview as `#igni_ws_token=…`; see
[SECURITY.md](SECURITY.md#10-the-backends-local-websocket)).

To start a backend with no token at all during development, set
`IGNI_DEV_NO_WS_TOKEN=1` (`IGNI_DEV_NO_WS_TOKEN=1 python -m
igni_core.backend --ws-port 8765`, or `IGNI_DEV_NO_WS_TOKEN=1 cargo tauri
dev` in igni-app, whose debug builds then spawn the backend without a
token). Both print a warning; any local process can then drive the
agent. Release builds ignore it.

## Makefile

All common tasks are available via `make`:

```bash
make help           # Show all targets
make install        # Install package with all extras
make test           # Run tests (pytest)
make test-v         # Run tests with verbose output
make test-cov       # Run tests with coverage report
make lint           # Run ruff linter
make format         # Auto-format code (ruff)
make format-check   # Check formatting without changes
make typecheck      # Run mypy type checking
make check          # Run ALL checks (lint + format + typecheck + test)
make fix            # Auto-fix lint and formatting issues
make clean          # Remove build artifacts and caches
```

The `make check` target is the single command to verify everything is clean before committing.

## Dependencies

**Core:**
- `agno[openai]` — Agent orchestration framework
- `rich` — Terminal formatting and markdown rendering
- `textual` — Terminal UI framework
- `pyyaml` — Configuration file parsing
- `click` — CLI argument parsing
- `pydantic` — Settings and data models
- `httpx` — HTTP client (embeddings, web tools)

**Optional** — one bullet per extra in `[project.optional-dependencies]`, and no others. There is no `knowledge` extra, and no graph database or embedder in igni at all: CodeIndex's local index (Neo4j per commit, a sentence-transformer) is its extension's, installed from its bundle.

- `mcp` — Model Context Protocol (`pip install -e ".[mcp]"`)
- `google-genai` — Gemini models (`pip install -e ".[gemini]"`)
- `anthropic` — Claude over the native Messages API (`pip install -e ".[anthropic]"`)
- `duckduckgo-search` — Web search (`pip install -e ".[web]"`)

**Dev:**
- `pytest` + `pytest-asyncio` — Testing
- `ruff` — Linting and formatting
- `mypy` — Type checking

## Key Implementation Details

### 1. Agent Pool (`core/agents/pool.py`)

The AgentPool discovers, parses, and manages agent definitions from `.md`
files. Discovery itself lives in `core/agents/loader.py`, which reads every
root and ranks it — on a name collision the higher priority wins, so the
project's own agent beats the user's global:

```python
dirs: list[tuple[Path, AgentPriority]] = [
    (Path.home() / CONFIG_DIR / "agents", AgentPriority.USER_EMBER),
]
if self._group_dir is not None:
    dirs.append((self._group_dir, AgentPriority.ORG_GROUP))
dirs += [
    (project_dir / CONFIG_DIR / "agents.local", AgentPriority.PROJECT_LOCAL),
    (project_dir / CONFIG_DIR / "agents", AgentPriority.PROJECT_EMBER),
]
```

The enum members still carry the pre-rename spelling (`USER_EMBER`,
`PROJECT_EMBER`); the paths they rank are igni's.

With `agents.cross_tool_support` on (the default), `.claude/agents` — the
project's and the user's — is read as well, ranked below the native roots.
`load_definitions` parses without building and returns a `LoadReport` the
backend surfaces to the client and the audit log; `build_agents` turns the
surviving definitions into Agno agents.

### 2. Orchestration (`core/tools/orchestrate.py`)

The main agent has `OrchestrateTools` which provides `spawn_agent()`, `spawn_team()`, and `create_agent()`. There is no separate orchestrator — the main agent itself decides when to delegate via tool calls. Sub-teams are built from the `AgentPool` with the Agno features igni uses (session persistence, compression, learning, guardrails) applied by `Session._build_main_agent()`. There is no Agno knowledge base: what an agent may search arrives from the extensions that provide it.

### 3. Backend Architecture (`backend/`)

The Textual TUI this section used to describe no longer exists. Its
separation-of-concerns shape survived the move: the backend server is a
thin shell that delegates to focused classes, and the interactive
surface is now the React clients in igni-app.

| Class | File | Responsibility |
|---|---|---|
| `BackendApp` | `backend/app.py` | Composition root: runs the boot in order, hands off to `ServingBackend` |
| `ServingBackend` | `backend/serving_backend.py` | Serves the first window onward; owns the teardown order |
| `SessionPool` | `backend/session_pool.py` | Per-project `SessionRuntime` instances, switching |
| `RunController` | `backend/run_controller.py` | Execution pipeline, streaming, cancellation |
| `StreamEventDispatcher` | `backend/stream_event_dispatcher.py` | Agno run events → wire messages |
| `MessageDispatcher` | `backend/message_dispatcher.py` | Inbound FE action routing |
| `CommandHandler` | `backend/command_handler.py` | Slash-command dispatch |
| `HitlController` | `backend/hitl_controller.py` | Permission prompts, confirmations, user input |

### 4. Model Resolver (`core/config/models.py`)

Agent `.md` files specify models by name. `ModelRegistry` maps a name to an
Agno model instance, resolving in this order:

```python
class ModelRegistry:
    def get_model(self, name: str | None = None) -> Model:
        # 1. The config registry (`models.registry`: the built-in defaults
        #    plus whatever the user or the project added)
        # 2. `provider:model_id` syntax (e.g. "openai_like:gpt-4o")
```

A name that resolves to nothing raises `ValueError`; an unset default that
resolves to nothing returns a `NoModelConfigured` placeholder, so a session
can still be built and `/login` reached.

## Testing

```bash
# Run all checks
make check

# Or individually:
make test            # pytest
make lint            # ruff check
make format          # ruff format
make typecheck       # mypy

# Quick iteration
make fix             # auto-fix lint + format
make test            # verify tests still pass
```

### Test Strategy

- **Unit tests** — agent pool, .md parsing, tool registry, config merging, backend server
- **Integration tests** — Orchestrator decisions, team assembly, AgnoFeatures application
- **Mock LLM calls** — mock model responses to test orchestration logic
- **Agent definition tests** — validate all built-in `.md` files parse correctly
- **One file per behaviour.** The suite is flat and large; `ls tests/` is
  the map of what the product does. A count of the files goes stale the
  week after it is written, so this is not one.

## Slash Commands

Every built-in command, in the order the registry lists them:

| Command | Description |
|---|---|
| `/help` | Show help and the available commands |
| `/quit` or `/exit` | Exit the session |
| `/clear` | Clear the current conversation |
| `/config` | Show the current settings (model, permissions, features) |
| `/agents` | List and manage agents |
| `/skills` | List installed skills |
| `/hooks` | List installed hooks |
| `/extensions` | Installed extensions and their settings |
| `/sessions` | List past sessions |
| `/rename <name>` | Rename the current session |
| `/fork` | Fork the current session under a new id |
| `/memory` | Inspect or optimise learned memories (`/memory optimize`) |
| `/evals [agent]` | Run evaluation suites (optionally filter by agent name) |
| `/login` | Sign in to your igni server (device flow — opens the browser) |
| `/logout` | Sign out of your igni server |
| `/whoami` | Show the signed-in user |
| `/schedule add <when> <prompt>` | Schedule a one-off or recurring prompt (e.g. `/schedule add review code at 5pm`, `/schedule add run linter every 2 hours`) |
| `/loop <prompt>` | Re-fire the same prompt as the next user turn until cap, `/loop stop`, or any non-`/loop` input (default cap 30, hard cap 200). Forms: `/loop` (status), `/loop <N> <prompt>` (explicit cap), `/loop stop` (cancel) |
| `/plugins` | Open the client's plugins panel (browse, toggle enable/disable, update/remove, browse marketplaces, install). See [Plugins](PLUGINS.md). |
| `/plugins enable <name>` / `/plugins disable <name>` | Toggle a plugin without opening the panel — takes effect on next session start. |
| `/plugin install <git-url\|@marketplace/plugin>` | Install a Claude-Code-compatible plugin into `~/.igni/plugins/`. Optional `--ref <branch\|tag\|sha>`. |
| `/plugin update <name>` / `/plugin remove <name>` | Update (fetch + reset to origin's HEAD or `--ref`) or uninstall. |
| `/plugin marketplace add\|list\|remove\|refresh` | Manage registered marketplaces — Claude-Code-compatible catalogs at the root of any git repo. |
| `/compact` | Compact the conversation context (otherwise at 80% of the context window) |
| `/ctx` | Show context window usage |
| `/plan` | Toggle plan mode (read-only sandbox, plan-then-execute) |
| `/accept` | Toggle acceptEdits mode (auto-approve file edits) |
| `/bypass` | Toggle bypassPermissions mode (auto-approve every tool — no HITL prompts) |
| `/output-style` | List or switch the active output style (tone / verbosity) |
| `/mcp` | Open the MCP servers panel |
| `/programs` | Let a program answer approval requests |
| `/watcher` | Open the background-process watcher panel |
| `/eject` | Copy something your group ships into this project, so yours wins |
| `/model` | Switch the active model (or show the current one) |
| `/bug` | Open a bug report |
| `/<skill-name> [args]` | Invoke a skill (e.g., `/commit`) |

A built-in is one entry in `backend/builtin_command_registry.py`: its
name, its description and where its handler lives, as `"module:attr"`.
The module is imported the first time the command runs, so nothing a
command needs is loaded by an idle backend. Listing and `/help` read
only the entries. `tests/test_commands_load_on_first_use.py` checks
every entry resolves and that an idle backend has not loaded them.

**Adding a database migration.** Add the alembic revision under
`igni_core/migrations/versions/`, then set `HEAD_REVISION` in
`core/db/schema.py` to it. A backend compares a database's revision with
that constant to decide whether to migrate at all;
`tests/test_migrations_stay_off_the_start_path.py` fails until they agree.

## Architecture Decisions

### Why Agno over raw API calls?

- **Team orchestration** — dynamic task decomposition and routing
- **Memory management** — built-in persistent memory with DB backends
- **Tool ecosystem** — 100+ pre-built toolkits
- **Model agnostic** — swap models without code changes
- **Reasoning** — built-in chain-of-thought with `reasoning=True`
- **Documents** — extensions keep their own stores and reach the agent over MCP
- **Learning** — LearningMachine for user memory
- **Guardrails** — PII detection, prompt injection, moderation as pre-hooks

### Why dynamic team assembly instead of a fixed hierarchy?

A fixed Router → Agent hierarchy forces you to pre-decide what agents exist and how they interact. The dynamic approach lets:
- Users add agents without code changes (just drop a `.md` file)
- The Orchestrator adapt team composition to each specific task
- Different tasks use different team modes (coordinate for multi-step, broadcast for review, etc.)
- Project teams customize behavior by overriding built-in agent definitions

### Why agents as .md files?

- **Accessibility** — anyone can write markdown, no Python needed
- **Portability** — commit to repo, share across team
- **Overridability** — project definitions override built-ins by name
- **Transparency** — read the file, understand exactly what the agent does
- **Claude Code compatibility** — same YAML frontmatter format, easy migration

### Why a backend server with separate controller classes?

The interactive surface is a GUI client, not a terminal app — `igni`
and `ignite-ember` are the non-interactive CLI, and chat happens in the
clients in igni-app, which connect to the server started by
`python -m igni_core.backend`. Splitting the front-end out means the
same backend serves the desktop app and the web client over one wire
protocol. Inside the backend, `BackendApp` is a thin composition root
that builds one small class per subsystem (`ExtensionsRuntime`,
`ExtensionStartup`, `BackendNotifications`, `ServiceEventIngress`,
`HostModelRuntime`) and delegates to the controllers listed above; each stays testable in
isolation and none of them becomes the god-class.

## Roadmap

See [GitHub Issues](https://github.com/ignite-ember/igni-app/issues) for the current roadmap.

**Planned features:**
- [x] Tracing (OpenTelemetry → the customer's own collector; see CONFIGURATION.md, "OpenTelemetry")
- [ ] Plugin system (installable agent/tool packages)
- [ ] Web UI (Agno Playground integration)
- [ ] Voice mode (speech-to-text input)
- [ ] Multi-repo support (workspaces)
