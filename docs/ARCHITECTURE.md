# Architecture

## Overview

igni is a terminal-based AI coding assistant built on [Agno](https://docs.agno.com/). Its core innovation is **dynamic team assembly**: instead of a fixed agent hierarchy, an Orchestrator analyzes each task and builds a purpose-fit team on the fly from a pool of agent definitions.

```
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┐
  FRONTEND PROCESS (a client — the React apps)
│                                                     │
  ┌─────────────────────────────────────────────────┐
│ │            Client (the React web UI)            │ │
  │     the messages │ the approvals │ the session  │
│ │   the resume │ the run settings │ extensions    │ │
  └──────────────────────┬──────────────────────────┘
│                        │                            │
                         │ NDJsonStreamTransport
│                        │                            │
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┘
                         │
              Unix socket or WebSocket, NDJSON protocol
              (newline-delimited JSON messages)
                         │
┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┐
  BACKEND PROCESS       │
│                        ▼                            │
  ┌─────────────────────────────────────────────────┐
│ │         BackendServer + CommandHandler           │ │
  │         (protocol dispatch, slash commands)      │
│ └──────────────────────┬──────────────────────────┘ │
                         │
│ ┌──────────────────────▼──────────────────────────┐ │
  │              Session Manager                    │
│ │      (conversation state, history, memory)      │ │
  └──────────────────────┬──────────────────────────┘
│                        │                            │
  ┌──────────────────────▼──────────────────────────┐
│ │              Orchestrator (meta-agent)           │ │
  │              → Agent Pool (.md files)           │
│ │              → Dynamic Team Assembly            │ │
  │                (route/coord/broadcast/tasks)    │
│ │              → Unlimited nesting depth          │ │
  └──────────────────────┬──────────────────────────┘
│                        │                            │
  ┌──────────────────────▼──────────────────────────┐
│ │              Guardrails Layer                    │ │
  │   PII Detection │ Prompt Injection │ Moderation │
│ └─────────────────────────────────────────────────┘ │
  ┌─────────────────────────────────────────────────┐
│ │              Tool Layer (Agno Toolkits)          │ │
  │   Shell │ File │ Edit │ Search │ Git │ Web      │
│ └─────────────────────────────────────────────────┘ │
  ┌─────────────────────────────────────────────────┐
│ │                   Memory Layer                   │ │
  │     Agno's memory db │ SQLite or PostgreSQL     │
│ └─────────────────────────────────────────────────┘ │
  ┌─────────────────────────────────────────────────┐
│ │              MCP Layer                           │ │
  │   Server (expose tools to IDEs via stdio)       │
│ │   Client (consume external MCP servers)         │ │
  └─────────────────────────────────────────────────┘
│ ┌─────────────────────────────────────────────────┐ │
  │              Storage Layer                      │
│ │      SQLite (sessions) │ Memory (user prefs)    │ │
  └─────────────────────────────────────────────────┘
│                                                     │
└ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─┘
```

The `igni` command itself does not go through this split: a headless run
(`igni -m`, `igni -p`) builds its session in-process
(`core/session/single_message_run.py`). The split is how the clients reach
the backend.

## Core Design Principles

### 1. Dynamic Team Assembly

Nothing is hardcoded. The Orchestrator is a reasoning-enabled meta-agent that reads the full agent pool (descriptions, tools, tags) and the user's message, then decides:

- **Which agents** to include (minimal set needed)
- **Which team mode** to use (route, coordinate, broadcast, tasks)
- **What instructions** to give the team leader

This means the system adapts to new agents automatically. Drop a `database.md` into the agents folder — the Orchestrator can start including it in teams immediately, without any code changes.

When no existing agent fits a task, the Orchestrator can **generate an ephemeral agent** on the fly — writing a new `.md` file to `.igni/agents.tmp/` with a task-specific system prompt and tools. Ephemeral agents are session-scoped and auto-cleaned, but can be promoted by the user — to `.igni/agents/` (shared through git) or `.igni/agents.local/` (personal); see [AGENTS.md](AGENTS.md#the-lifecycle-create-promote-discard).

### 2. Agents as Data, Not Code

Agents are `.md` files with YAML frontmatter, not Python classes. This makes them:

- **Easy to create** — anyone can write markdown
- **Easy to share** — commit to the repo, whole team gets them
- **Easy to override** — project-level definitions override built-ins
- **Inspectable** — read the file, understand the agent

The agent loader parses these files and constructs `agno.Agent` objects at runtime. See [Agents](AGENTS.md) for the full format specification.

### 3. Right-Sized Teams

Not every task needs a team. The Orchestrator's primary optimization is **minimizing overhead**:

- **Single agent** — for simple questions or single-capability tasks, skip team creation entirely. Run one agent directly.
- **Route mode** — when the task is clear but could go to different agents, use routing (one decision, then passthrough).
- **Coordinate mode** — for multi-step tasks needing different capabilities in sequence.
- **Broadcast mode** — when parallel independent perspectives add value (e.g., security + performance review).
- **Tasks mode** — for large autonomous goals requiring iteration and progress tracking.

| Mode | Overhead | When |
|---|---|---|
| Single agent | Lowest | One agent clearly fits |
| Route | Low | Need to pick one from several |
| Coordinate | Medium | Multi-step, different capabilities |
| Broadcast | High | Independent parallel perspectives |
| Tasks | Highest | Large autonomous goals |

### 4. Unlimited Nesting

Claude Code caps sub-agents at one level (sub-agents can't spawn sub-agents). igni has **no nesting limit**. Every agent can access the agent pool and spawn sub-teams by default (`can_orchestrate: true`). Opt out per-agent with `can_orchestrate: false`.

This matters because **the agent closest to the problem is best positioned to decide if it needs help**. An editor mid-refactor might spawn an explorer to understand unfamiliar code. A reviewer might spawn a security-auditor for a specific concern. Each sub-team picks its own mode.

Practical guardrails (configurable depth limits, agent caps, timeouts) prevent runaway recursion without restricting the design. See [Agents](AGENTS.md#recursive-nesting-agents-that-build-teams) for details.

### 5. Persistent Memory

Agno's built-in memory system replaces file-based memory:

- **User memory** — preferences, role, expertise level (persists across sessions)
- **Session storage** — conversation history, tool outputs (per-session, DB-backed)
- **Session state** — current task progress, open files, working context
- **Progress tracking** — `.igni/TODO.md` auto-loaded into context for cross-session task continuity

## Request Lifecycle

```
User Input
    │
    ▼
┌──────────────┐
│ Permission   │──── blocked? → inform user
│ Pre-check    │
└──────┬───────┘
       │
       ▼
┌──────────────────────────┐
│      Orchestrator        │
│                          │
│  1. Read agent pool      │
│  2. Analyze message +    │
│     conversation context │
│  3. Output: TeamPlan     │
│     • agent_names        │
│     • team_mode          │
│     • instructions       │
└──────────┬───────────────┘
           │
           ▼
    ┌──────────────┐
    │ Build Team   │──── instantiate agents from pool
    │ or single    │     set mode, instructions
    │ agent        │
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ Execute      │──── team/agent runs with tools
    │              │     (may involve multiple turns)
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ Post-process │──── format, log, audit
    └──────┬───────┘
           │
           ▼
     Display to User
```

### Orchestrator Shortcut

For obvious single-agent tasks, the Orchestrator can skip team assembly:

```
"What does auth.py do?"
  → Orchestrator recognizes: read-only question, one agent needed
  → Runs Explorer directly (no team overhead)
  → ~1 LLM call for orchestration + N calls for the agent
```

For complex tasks, the orchestration overhead (one extra LLM call) is negligible compared to the multi-step execution.

## Agent Pool Lifecycle

```
Startup
    │
    ├── Copy built-in agents → .igni/agents/   (first run, checksum-merged on update)
    ├── Copy built-in skills → .igni/skills/   (first run, checksum-merged on update)
    │
    ├── Scan ~/.igni/agents/              (global)
    ├── Scan ~/.igni/skills/              (global)
    │
    ├── Scan .igni/agents.local/          (project, personal; .igni/.gitignore)
    ├── Scan .igni/agents/                (project)
    ├── Scan .igni/skills.local/          (project, gitignored)
    ├── Scan .igni/skills/                (project)
    └── Scan .igni/agents.tmp/            (session-scoped, auto-cleaned)
           │
           │  With cross_tool_support: true, also scans:
           │  ├── ~/.claude/agents/        (Claude Code global)
           │  ├── ~/.claude/skills/        (Claude Code global)
           │  ├── ~/.codex/               (Codex global)
           │  ├── .claude/agents/          (Claude Code project)
           │  ├── .claude/skills/          (Claude Code project)
           │  └── AGENTS.md / .codex/      (Codex project)
           │
           ▼
    ┌──────────────────────────────────────────────┐
    │  Agent Pool + Skill Pool (combined)          │
    │                                              │
    │  Name conflict? → project wins over global   │
    │                   → global wins over built-in │
    └──────────────────────────────────────────────┘
           │
           ├── Hot reload: watch for file changes
           └── Orchestrator reads pool on each request
```

By default, igni loads agents and skills from both its own directories and Claude Code / Codex directories (`cross_tool_support` is on by default). Set `cross_tool_support: false` to only scan igni directories. See [Agents](AGENTS.md) and [Skills](SKILLS.md) for format details.

## Session Management

Each igni session:

1. **First run?** — if `.igni/agents/` doesn't exist, run the [onboarding flow](ONBOARDING.md): create default agents, ask about the user's work, fetch project context from CodeIndex, propose tailored agents
2. **Loads** — agent pool (from igni/Claude/Codex directories), user memory, project context (`igni.md`), MCP servers, session history
3. **Runs** — interactive loop: user message → Orchestrator → team/agent → response
3. **Persists** — updated memory, session state to SQLite
4. **Cleans up** — MCP connections, temp files, background processes

By default, everything is stored locally in SQLite: the project's own
database at `~/.igni/projects/<id>/state.db` (sessions, scheduler, loop,
processes — and user memory, through Agno's `AsyncSqliteDb` over the same
file), and the one global database at `~/.igni/igni.db` (which folder a
session is in, window state, local notifications).

**Cross-device sync:** Claude Code stores sessions locally only — they don't sync across devices. igni defaults to the same (SQLite, local). To move the state somewhere shared, set `storage.database_url` to a SQLAlchemy URL — PostgreSQL in the cloud (`docs/CLOUD.md` §9); `IGNI_DATABASE_URL` overrides the config file. See [Configuration](CONFIGURATION.md) for details.

## Documents and Knowledge

The core keeps no vector store and embeds no text: the code index left igni
for the CodeIndex extension, and the tables it used here were dropped in
v0.10. What a session knows about documents comes from the extension that
owns them.

### Documents

The document store is the Artifacts extension (igni__artifacts), not part
of the host: its tools reach its own agents over MCP, and the prompt's
document sections are switched on by what it declares while it runs
(`core/prompts/conditionals.py`).

## Learning & Reasoning

### Learning

Agno's `LearningMachine` carries what a session learns about the user
across conversations. Configured via:

```yaml
learning:
  enabled: true
  user_memory: true
  user_profile: false
  entity_memory: false
  session_context: false
  learned_knowledge: false
```

Only `user_memory` is on by default, and it is agent-driven: the agent
calls `update_user_memory` itself when it decides a turn was memorable
(`core/learn.py`). Agno's auto-extraction — a separate model call after
every run — is off, because it added 5–10 s to the tail between
`streaming_done` and `run_completed`: the answer was already on screen
while the session still looked busy.

### Reasoning

Agents can use `think` and `analyze` tools for step-by-step reasoning during complex tasks. Enable per-agent in `.md` frontmatter with `reasoning: true` (with `reasoning_min_steps` / `reasoning_max_steps`), or globally:

```yaml
reasoning:
  enabled: true
  add_instructions: true
  add_few_shot: false
```

The global switch builds Agno's `ReasoningTools` toolkit
(`core/session/agent_factory.py`); the frontmatter sets Agno's own
reasoning step budget.

## Guardrails

Built-in safety guardrails run as Agno pre-hooks before each agent turn:

| Guardrail | What It Does | Default |
|---|---|---|
| **PII Detection** | Flags personally identifiable information in prompts | on |
| **Prompt Injection** | Detects injection attempts in user input and tool output | off |
| **Moderation** | Content moderation via OpenAI's moderation API | off |

```yaml
guardrails:
  pii_detection: true
  prompt_injection: false
  moderation: false
```

`create_guardrails()` (`core/session/agent_factory.py`) turns the flags into
pre-hooks that run before the model is called; if a guardrail triggers, the
agent is informed and can adjust. Moderation is the one with an external
dependency (the OpenAI SDK), and it imports that SDK inside the check, so a
machine without it builds a session fine and fails the first moderated turn
instead.

The core keeps its own guardrails too, under `core/guardrails/`.
`GuardrailRunner` checks the message that came in and prefixes a warning
when something trips (`core/session/message_handler.py`); its membership is
data-driven — a guardrail joins the list by being a `Guardrail` subclass
whose flag is truthy on the config.

## Human-in-the-Loop (HITL)

Agents can pause execution to request confirmation or user input before proceeding with sensitive operations. HITL is implemented via Agno's callback system: the agent raises the question, the backend turns it into a `HITLRequest` on the wire, and the connected client answers with a `HITLResponse`.

Two HITL modes:
- **Confirmation** — agent asks "Should I proceed with X?" (yes/no)
- **User input** — agent asks an open-ended question and waits for a response

A headless CLI run has no client to answer, and the code that resolves a
pause lives in the backend behind a client RPC — so a confirmation raised
there waits until it times out. The permission flags decide this before
the run starts: `--auto-approve` runs in `bypassPermissions`,
`--accept-edits` in `acceptEdits`, `--read-only` in `plan`, and
`--strict` in `dontAsk` (never prompt, deny what has no allow rule).

## Run Cancellation

A running agent or team can be cancelled mid-execution: the client sends a `Cancel` message, and the backend's `RunController.cancel_run()` (`backend/run_controller.py`) cancels the running task, kills the foreground shell that session owns, and stamps the pending message as an interrupted run (`reason='cancelled'`) so the record survives the restart.

## CLI Architecture

The Textual TUI was removed in v0.9.7: interactive chat lives in the React
clients (igni-app's `clients/web` and `clients/tauri`), which talk to the
backend. What the `igni` command does is run work and exit.

The command surface is in `src/igni_core/cli/`:

| Module | Responsibility |
|---|---|
| `__init__.py` | the Click group: every global flag lands here |
| `options.py` | `CliOptions` — the flags as one typed model |
| `run_settings.py` | `CliSettingsLoader` — flags and settings file into one `Settings` |
| `invocation.py` | `CliInvocation` — the mode dispatch: message, pipe, or help pointer |
| `headless_run.py` | `HeadlessRun` — runs one session, then cleans up after it |
| `resume.py` | resolving `--continue` / `--session-id` to a session id |
| `context.py` | `CliContext` — what the CLI stashes on `click.Context.obj` |
| `trust.py` | the `trust` command — what this folder is trusted for |
| `login.py` | the `login` command |
| `migrate.py` | the `migrate` command — moving older data forward |
| `ext.py`, `ext_*.py` | the `ext` command — extensions: `doctor`, `install`, `sync`, `list`, `remove`, `init` |

Three modes:

- `igni -m "<message>"` — one message, then exit.
- `igni -p` — pipe mode: read stdin, run one message, write stdout.
- `igni` — no message and no subcommand: prints the pointer to the React clients.

The interactive REPL is still in the core (`core/session/interactive_loop.py`:
`InteractiveSessionLoop` and its `PromptHandler` chain — quit, slash command,
then message), but the CLI does not run it; the clients render their own
chat over the backend protocol.

### Task Visualization

When teams run in **tasks mode**, the team's task list travels as protocol
messages rather than as a widget: `TaskCreated`, `TaskUpdated`,
`TaskIteration` and `TaskStateUpdated`
(`protocol/schemas/be_events.py`), which the client renders as a live task
list with assignees and status. On the CLI's side the same events are
written out as they arrive, with the status icons from `TASK_STATUS_ICONS`
(`core/tools/orchestrate_events.py`).

### Background Task Scheduling

The backend runs a `SchedulerRunner` (`core/scheduler/runner.py`) that polls
for due tasks. Configuration:
- `poll_interval`: how often to check (default 30s)
- `task_timeout`: max duration per task (default 300s)
- `max_concurrent`: bounded concurrency via `asyncio.Semaphore` (default 1, sequential)

Completions reach the client as `SchedulerEvent` messages.

## Process Split Architecture

igni runs as two OS processes connected by a Unix domain socket or a loopback WebSocket port. This isolates the client from heavy AI/tool execution, so a long-running agent never blocks the client's rendering.

```
┌──────────────┐   socket / ws   ┌──────────────┐
│   Frontend   │◄───────────────►│   Backend    │
│  (a client)  │  NDJSON framing │  (Session +  │
│              │                 │   Agents)    │
└──────────────┘                 └──────────────┘
   PID A                            PID B
```

The frontend is not in this repository: both shipped clients — igni-app's
desktop shell and its web UI — speak the protocol from the outside.

### Key Components

| Component | Location | Role |
|---|---|---|
| `BackendApp` | `backend/app.py` | Starts the backend: binds the socket (or the WebSocket port), then serves until shutdown |
| `BackendServer` | `backend/server.py` | Wraps Session; dispatches client messages to Session/CommandHandler; streams Agno events back as protocol messages |
| `RunController` | `backend/run_controller.py` | The execution pipeline: streaming, cancellation, task visualisation |
| `CommandHandler` | `backend/command_handler.py` | Slash command dispatch, in the backend so commands execute with direct Session access |
| `UnixSocketClientTransport` / `UnixSocketServerTransport` | `transport/unix_socket.py` | Async Unix socket transport with NDJSON framing (one JSON object per `\n`) |
| `NDJsonStreamTransport` | `transport/ndjson_stream.py` | The framing the socket and WebSocket transports share |

### Protocol Messages

All messages are Pydantic models with plain types only (no Agno imports), so
the contract stays serialization-safe. They live in `protocol/schemas/` —
`fe_actions.py` for what the client sends, `be_events.py` for what the
backend streams back — and `protocol/messages.py` re-exports them, because
the deserializer's registry scans that module's namespace for `Message`
subclasses.

**Client to backend (actions):**
- `UserMessage` — send a message for agent execution
- `Cancel` — cancel a running agent/team
- `Command` — execute a slash command
- `HITLResponse` / `HITLResponseBatch` — the user's answer to a confirmation/input prompt

**Backend to client (events, streamed):**
- `ContentDelta` — streamed text chunk from the model
- `ToolStarted` / `ToolCompleted` / `ToolError` — tool lifecycle events
- `HITLRequest` / `RunPaused` — HITL prompts (the question + run-level pause wrapper)
- `RunStarted` / `RunCompleted` / `RunError` / `StreamingDone` — run lifecycle bookends
- `TaskCreated` / `TaskUpdated` / `TaskIteration` / `TaskStateUpdated` — team task progress
- `StatusUpdate` — token counts, context usage, model info
- `SchedulerEvent` — a scheduled task fired

### Startup Sequence

1. The client spawns `python -m igni_core.backend --ws-port 0 --project-dir <dir>` (the desktop shell does; the web client can attach to a backend that is already running)
2. The backend binds the port and prints a ready line naming it
3. The client opens the WebSocket to that port and starts talking

### Design Rationale

- **Non-blocking client** — the client's event loop stays responsive while agents run. Token streaming, tool progress, and HITL prompts all arrive as async messages.
- **Crash isolation** — a backend crash (OOM from large context, tool segfault) does not kill the client. The client can detect the lost connection and offer to restart.
- **Future flexibility** — the socket protocol is transport-agnostic (the `Transport` base class abstracts over Unix socket, TCP, or in-process). Remote backends and multi-session servers become possible without changing the client.

## Error Handling

- **Tool failures** — agents retry with alternative approaches (Agno's built-in retry)
- **Model errors** — graceful fallback with user notification
- **Permission denied** — clear explanation of what was blocked and why
- **Context overflow** — two-layer compression (tool result compression + conversation history compaction) triggers at 80% of `min(model_window, models.max_context_window)`, default ceiling 200k tokens
- **Agent not found** — Orchestrator falls back to available agents with a warning
- **Team failure** — if a coordinated team fails mid-execution, partial results are preserved and shown

## Security Model

igni follows a defense-in-depth approach:

1. **Permission tiers** — configurable per-tool approval requirements
2. **Command blocking** — dangerous shell commands blocked, others require confirmation
3. **File guards** — sensitive paths (`.env`, credentials) protected from writes
4. **Confirmation prompts** — destructive/irreversible actions require explicit approval
5. **Audit log** — all tool executions logged to `~/.igni/audit.log`
6. **Agent isolation** — agents only get the tools declared in their definition
7. **Guardrails** — PII detection, prompt injection detection, and content moderation as pre-hooks

See [Configuration](CONFIGURATION.md) for permission settings.
