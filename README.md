# igni-core

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/ignite-ember/igni-core/badge)](https://scorecard.dev/viewer/?uri=github.com/ignite-ember/igni-core)
[![PyPI](https://img.shields.io/pypi/v/igni-core)](https://pypi.org/project/igni-core/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**igni is a self-hosted, air-gapped agentic platform.** Its agents work in
your environment, with the models you choose, on infrastructure you control,
and nothing in it needs to call home. igni-core is the agent runtime at its
centre: the orchestrator, the tools and the policy that governs them, and
the backend the desktop app and the web client run on.

Writing and reviewing code is one thing igni's agents do, and the
built-in agents are coding specialists. It is not the whole of it: an agent
is a markdown file, so an organisation defines its own agents, skills, MCP
servers and extensions for any team, and ships them to that team from its
igni server.

[igni.sh](https://igni.sh)

## Where igni-core sits

igni is several repositories, all Apache-2.0:

| Part | Repository | What it does |
|---|---|---|
| **igni-core** (this one) | [ignite-ember/igni-core](https://github.com/ignite-ember/igni-core) | The agent runtime and its backend: orchestration, tools, permissions, hooks, skills, MCP, extensions, sessions. Runs on a person's machine, behind the desktop app and the web client, or headless as the `igni` command. |
| **igni server** | [ignite-ember/igni-server](https://github.com/ignite-ember/igni-server) | Self-hosted, and runs with no internet access. Identity (sign-in, OIDC, SCIM, organisations), inference routing and the model key pool (your GPU nodes or any OpenAI-compatible endpoint, Anthropic, AWS Bedrock, Vertex AI, Azure OpenAI and Microsoft Foundry, with the server holding the cloud credentials), the extensions approved services ship, group policy, and the audit trail. |
| **igni app** | [ignite-ember/igni-app](https://github.com/ignite-ember/igni-app) | The desktop app (Tauri) and the web client: one web UI, where people chat with agents, approve what they do, and use extensions' pages. |
| **SDKs** | [ignite-ember/igni-sdk](https://github.com/ignite-ember/igni-sdk) | `igni-service` (an extension's backend service), `igni-extension` (an extension's process), `igni-client` (a program driving an igni), `@ignite-ember/extension-bridge` (an extension's page), `@ignite-ember/ui-react` and `@ignite-ember/ui-css` (its UI). |

Paid modules (CodeIndex, Artifacts, Fleet) are separate products that plug
in through the same extension and service-identity interfaces. igni-core
runs no graph database or embedder of its own.

## What igni-core gives you

- **Multi-agent orchestration.** An orchestrator reads each task and
  assembles a team from the agent pool, in one of four team modes
  (route, coordinate, broadcast, tasks). Any agent may spawn a sub-team,
  within configurable depth limits, agent caps and timeouts.
  [Agents](docs/AGENTS.md), [Architecture](docs/ARCHITECTURE.md).
- **Tools under a permission system.** Every tool call is checked against
  `allow` / `ask` / `deny` rules and a permission mode (default, accept
  edits, plan, bypass, `dontAsk`). A `deny` rule holds in every mode, and
  an organisation's managed settings set a floor no user flag gets past.
  `--read-only` runs only reads; `--strict` denies anything without an
  explicit allow rule. Protected paths and dangerous command shapes are
  blocked. [Security](docs/SECURITY.md#1-permission-system), [Tools](docs/TOOLS.md).
- **Workspace trust.** Opening a folder never runs its code: until you
  trust it, its tools, hooks, workflows and permission settings stay off.
  [Workspace trust](docs/CONFIGURATION.md#workspace-trust).
- **Hooks.** Commands or HTTP calls on agent lifecycle events, which can
  block, change or record a tool call. Hooks your organisation ships run
  only as your `hooks.group` setting allows. [Hooks](docs/HOOKS.md).
- **Skills.** Reusable workflows as `SKILL.md` files, invoked by slash
  command or picked by the orchestrator. [Skills](docs/SKILLS.md).
- **MCP.** igni is an MCP client over stdio and SSE; servers' tools reach
  the agents you allow them for. [MCP](docs/MCP.md).
- **Extensions.** A capability with a service behind it: tools over MCP,
  its own page in the app, hooks and host access, each declared in a
  manifest an administrator approves. The extension's process runs in an
  OS sandbox (Seatbelt, bubblewrap, AppContainer) that reaches only its own
  service and declared hosts. A bundle igni server distributes is installed
  only if its signature verifies against the service a super admin
  approved; anything else is a development install behind
  `IGNI_DEV_EXTENSIONS=1`.
  [Extensibility](docs/EXTENSIBILITY.md), [Plugins](docs/PLUGINS.md).
- **Sessions and memory.** Sessions, scheduled tasks and memory are kept in
  local SQLite, or in PostgreSQL through the `igni-cloud` package and
  `storage.database_url`.
  Resume with `-c` or `--session-id`. [Architecture](docs/ARCHITECTURE.md#session-management).
- **Organisation policy.** igni server publishes a signed policy pack per
  group (settings, agents, hooks, MCP servers); igni verifies the signature,
  issuer, person and version before merging any of it.
  [Group policy](docs/CONFIGURATION.md#organisation-group-policy).
- **Any model.** A model registry with three providers: `openai_like` (any
  OpenAI-compatible API: vLLM, llama.cpp, Ollama, OpenAI, Groq, …),
  `anthropic` (Claude, or an Anthropic-compatible gateway) and `gemini`.
  Or the models your igni server serves, with no key on the laptop.
  [Configuration](docs/CONFIGURATION.md#model-registry).
- **Air-gapped operation.** One switch, `air_gapped: true`, stops every
  outbound connection igni makes on its own. [Running air-gapped](#running-air-gapped).
- **Auditability.** A JSON-lines audit log on the machine
  (`~/.igni/audit.log`), OpenTelemetry traces and metrics to your own
  collector (each agent run, model call and tool call, with igni's
  permission decision; prompts and code left out by default), and the
  organisation's append-only audit trail in igni server.
  [OpenTelemetry](docs/CONFIGURATION.md#opentelemetry).
- **Verifiable releases.** Every release carries SHA-256 checksums,
  CycloneDX SBOMs and build-provenance attestations you can check on a
  machine with no network. [Verifying a release](docs/VERIFYING_RELEASES.md).

## Quick Start

```bash
brew install ignite-ember/tap/igni-core  # or: pip install igni-core, or: uv tool install igni-core
cd your-project
igni --read-only -m "In one sentence, what does this project do?"
```

`igni` is the command, and the only one pip, uv and the Homebrew formula
install. igni is supported on Apple Silicon macOS and x86-64 Linux: it
searches code with ripgrep: the formula brings Homebrew's, pip
and uv bring the PyPI package on macOS arm64 and Linux x86_64, and
elsewhere you install `rg` yourself (`brew install ripgrep`, `sudo apt
install ripgrep`) — see [Quickstart](QUICKSTART.md#install). On the command line it runs **one task and exits** (`-m`, or `-p` for a
pipe); there is no terminal chat UI any more — run it bare and it says so.
The interactive chat is the desktop app and the web client.

**What the published package holds today.** `igni-core` on PyPI is
**v1.0.4**, and it is this repository's build: the `igni_core` module,
the `igni` command and the decisions service, and the Homebrew formula
installs the same package. The old name, `ignite-ember`, is retired: its
releases are yanked from PyPI and there is no formula or command by it.

**Why `--read-only`.** A headless run cannot ask before it runs a command,
so decide up front what it may do. igni reads files with the shell, which
asks by default, so without `--read-only` the question above stops at its
first `cat` with *"Run paused for confirmation"* and exits 1 — see
[A headless run cannot ask you anything](QUICKSTART.md#a-headless-run-cannot-ask-you-anything).
The first run in a folder also asks whether to trust it, and writes
`igni.md` and `.igni/` there.

To work on igni-core instead of installing it:

```bash
git clone https://github.com/ignite-ember/igni-core.git
cd igni-core
uv venv && uv pip install -e ".[dev]"
source .venv/bin/activate          # puts `igni` on your PATH
```

## Configuration

Settings merge, strongest first, from the managed-settings file (which a
user cannot override), your organisation's group policy, command-line
flags, a project's `.igni/config.local.yaml` and `.igni/config.yaml`
(once you trust the folder), and your `~/.igni/config.yaml`.
[Configuration](docs/CONFIGURATION.md) has every key.

**Connecting to igni server.** To point it at your own igni server, set `api_url` in your user config,
`~/.igni/config.yaml` — not a project's `.igni/config.yaml` or
`config.local.yaml`, which may not name a server and are ignored for it,
with a warning — and run `igni login` (or `igni login --token-stdin` with a
token). That writes `~/.igni/credentials.json`, which `igni -m` and the desktop
app both use. `/login` is not a shell subcommand: `igni /login` answers
`Error: No such command`.

```yaml
# ~/.igni/config.yaml
api_url: https://igni-api.acme.example
# Only when the portal (the sign-in page) is not on the API's origin,
# e.g. the API on :7777 and the portal on :3000:
portal_url: https://igni.acme.example
```

`portal_url` is worked out from `api_url` when both run under one name and
differ by a host label (`api.acme.example` → `acme.example`); set it when
they differ any other way, such as by port, or sign-in opens a page the API
does not serve. Signed in, the models your server serves join the registry
with `api_key: cloud_token`, a token igni sends only to that server's
origin. There is no default server: until you set `api_url`, igni contacts
none.

**Bringing your own model.** Add it to the registry. The endpoint can be a
model server inside your network:

```yaml
# ~/.igni/config.yaml
models:
  default: local-coder
  registry:
    local-coder:
      provider: openai_like            # vLLM, llama.cpp, Ollama, or any OpenAI-compatible API
      model_id: qwen2.5-coder-32b
      url: http://vllm.acme.internal:8000/v1
      api_key_env: VLLM_API_KEY        # or api_key, or api_key_cmd: "op read ..."
```

See [Model registry](docs/CONFIGURATION.md#model-registry) for the
`anthropic` and `gemini` providers, and
[Cloud models through igni server](docs/CONFIGURATION.md#cloud-models-through-igni-server)
for Claude on Bedrock or Vertex AI and models on Azure OpenAI.

## Running air-gapped

Set the switch in `~/.igni/config.yaml`, in the managed-settings file
(`/etc/igni/managed-settings.yaml` on Linux,
`/Library/Application Support/igni/managed-settings.yaml` on macOS,
`%PROGRAMDATA%\igni\managed-settings.yaml` on Windows), in a group's policy,
or with `IGNI_AIR_GAPPED=1`:

```yaml
air_gapped: true
network:
  internal_hosts: ["git.acme.internal", "*.svc.acme.internal"]
```

With it on, igni makes no connection outside your network on its own:

- no update check to PyPI; the desktop updater uses only your
  `update_endpoint`, if you set one;
- web search and URL fetch are not built at all;
- plugins, extensions, MCP servers reached by URL, HTTP hooks and an
  OpenTelemetry collector are allowed only on hosts inside the network —
  loopback, local paths, the hosts of `api_url`, `portal_url`,
  `update_endpoint` and your plugin marketplaces, and
  `network.internal_hosts`;
- the local decision model is loaded only from a pre-seeded cache, and the
  desktop app downloads no runtime: it runs on the `runtime_python` you
  provision;
- Agno's telemetry is off, even if `AGNO_TELEMETRY=true` is set.

A project's own config cannot switch it off or name a host as internal.
What the switch does not cover: programs the agent runs (`curl`, `pip`,
`git`), MCP servers started as local processes, command hooks, and the model
endpoints you configure. Run igni behind a deny-all egress rule for those.

Without the switch, an unconfigured install contacts no vendor service: the
only connections it makes on its own are the daily PyPI update check
(`update_check_ttl: 0` turns it off), the desktop app's updater and
first-launch runtime download, and the decision model's first download.
Web search and URL fetch ask each time.
[Outbound connections by default](docs/SECURITY.md#outbound-connections-by-default)
lists each one, and `tests/test_an_air_gapped_install_makes_no_outbound_call.py`
proves each row.

[Air-gapped mode](docs/CONFIGURATION.md#air-gapped-mode) covers providing
the desktop runtime and pre-seeding the decision model. For the server side,
igni server ships each release as one signed offline bundle
([Offline updates](https://github.com/ignite-ember/igni-server/blob/main/docs/OFFLINE_UPDATES.md),
[Air-gap rehearsal](https://github.com/ignite-ember/igni-server/blob/main/docs/AIR_GAP_REHEARSAL.md),
[Self-hosting](https://github.com/ignite-ember/igni-server/blob/main/docs/self-hosting.md)).

## Clients

igni ships the desktop app (Tauri), the web client and the CLI. The desktop
app and the web client share one web UI, both in
[igni-app](https://github.com/ignite-ember/igni-app); the desktop app spawns
this backend and talks to it over a loopback WebSocket
(`python -m igni_core.backend --ws-port 0`) that checks the page's origin
and a per-launch token. In a terminal, `igni -m` and `igni -p` run one task.

## Documentation

**Start here**

- [Quickstart](QUICKSTART.md) — install, first run, permissions for a headless run
- [Configuration](docs/CONFIGURATION.md) — settings tiers, the model registry, igni server, air-gapped mode, telemetry
- [Security](docs/SECURITY.md) — threat model, permission system, protected paths, outbound connections, enterprise hardening
- [Architecture](docs/ARCHITECTURE.md) — the process split, the request lifecycle, sessions

**Agents and what they use**

- [Agents](docs/AGENTS.md) — agent definitions, the orchestrator, team modes, sub-teams
- [Tools](docs/TOOLS.md) — the toolkits and which agent gets which
- [Skills](docs/SKILLS.md) — reusable workflows invoked by slash command
- [Hooks](docs/HOOKS.md) — lifecycle hooks, including your organisation's
- [MCP](docs/MCP.md) — connecting MCP servers
- [Plugins](docs/PLUGINS.md) — Claude-Code-compatible bundles of skills, agents, hooks and MCP servers
- [Onboarding](docs/ONBOARDING.md) — what the first run in a folder sets up

**Extending igni**

- [Extensibility](docs/EXTENSIBILITY.md) — the contract extensions build against: manifest, permissions, sandbox, trust
- [Your first extension](https://github.com/ignite-ember/igni-sdk/blob/main/docs/YOUR_FIRST_EXTENSION.md) — from an empty directory to a running extension (in igni-sdk)
- [Host object model](docs/HOST_OBJECT_MODEL.md) — what an extension's process and page may reach
- [Design system](https://github.com/ignite-ember/igni-app/blob/main/docs/DESIGN_SYSTEM.md) — the UI an extension's page is drawn with (in igni-app)

**Releases and assurance**

- [Verifying a release](docs/VERIFYING_RELEASES.md) — checksums, SBOMs, build provenance, online and air-gapped
- [Vulnerability management](docs/VULNERABILITY_MANAGEMENT.md) — what is scanned, what gates a release, remediation targets
- [Reporting a vulnerability](SECURITY.md) — where to send it, and what happens next

**Working on igni-core**

- [Contributing](CONTRIBUTING.md) — how to send a change, and the Developer Certificate of Origin
- [Development](docs/DEVELOPMENT.md) — project structure, setup, the Makefile
- [Testing](docs/TESTING.md) — running the suite
- [Evals](docs/EVALS.md) — the agent evaluation framework
- [Benchmarks](docs/BENCHMARKS.md) — a 12-case software-engineering benchmark
- [Migration](docs/MIGRATION.md) — coming from Claude Code or Codex
- [Platform plan](docs/PLATFORM_PLAN.md) and [Build plan](docs/BUILD_PLAN.md) — what is still to be built, in order

## Security

igni-core runs on a person's own machine and executes what agents decide,
so its defences are local: the permission system, workspace trust,
protected paths, the extension sandbox, and an origin check plus a
per-launch token on the backend's WebSocket.
[docs/SECURITY.md](docs/SECURITY.md) is the threat model. Report a
vulnerability as [SECURITY.md](SECURITY.md) says, not in a public issue.

Releases are built by `.github/workflows/release.yml` and published to PyPI
with trusted publishing (PEP 740 attestations). Each GitHub release carries
`SHA256SUMS`, CycloneDX SBOMs and Sigstore build-provenance bundles;
[Verifying a release](docs/VERIFYING_RELEASES.md) shows how to check them,
including on an air-gapped machine. A cosign signature over `SHA256SUMS`
follows once the release key exists.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Every commit carries a
Developer Certificate of Origin sign-off (`git commit -s`).

## License

Apache 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Upgrading

The VS Code and JetBrains extensions are no longer shipped or supported;
release notes below that mention them are history.

### Unreleased

- **The `ignite-ember` name and command are retired.** The package is `igni-core` and the command is `igni`; nothing is published or installed under the old name any more. Every `ignite-ember` release on PyPI is yanked, the `ignite-ember` console script is gone from the package, and the Homebrew tap has a single formula, `igni-core` — `ignite-ember/tap/ignite-ember` no longer resolves. Install with `pip install igni-core`, `uv tool install igni-core` or `brew install ignite-ember/tap/igni-core`; a script that runs `ignite-ember` has to run `igni`.
- **`pip install igni-core` works where PyPI's `ripgrep` has no wheel.** The `ripgrep` package is required only on macOS arm64 and Linux x86_64, the platforms it ships wheels for; its sdist needs Rust, so on Linux arm64 and Intel macOS the install used to fail. An `rg` on `PATH` is always used first; the package's binary is found even when the venv is not on `PATH` (a `uv tool` install, the desktop app) and is put on the agent shell's `PATH` after the user's own. With no `rg` at all, code search falls back to a slower walk and the log says how to install ripgrep (`brew install ripgrep`, `sudo apt install ripgrep`).

### The split release (v1.0.3 → v1.0.4)

- **`igni-core` is on PyPI.** v1.0.4 is the first release published under igni's own name: `pip install 'igni-core[decisions]==1.0.4'` installs the `igni_core` module, the `igni` command and the decisions service. The `ignite-ember` name that 1.0.4 also shipped as an alias is retired since: its PyPI releases are yanked, and the Homebrew formula is `igni-core`.
- **The cloud layer is its own package.** `igni-cloud` holds the PostgreSQL storage and the producer/guest binding, found through the `igni.layers` and `igni.storage` entry points, rather than inside the core.
- **The wire contract is typed.** The messages the backend and the app exchange have one definition, checked from both sides — the app's client is igni-core's contract, byte for byte.
- **One module per concern.** The backend is split into modules that each own one thing — settings, auth, the cloud, the tools, the transport — behind interfaces the tests can substitute. Nothing reaches into another's internals.
- **`igni login` is a command.** It opens the portal's sign-in page in a browser, takes the token back over a local callback, writes `~/.igni/credentials.json` and hydrates the group pack. `igni login --token-stdin` does it without a browser.
- **The Windows sandbox is behind one interface.** What it allows is one thing to state, and one thing to test.
- **An install made under the old name keeps working.** The desktop app still reads the cache directory and the install marker under their old names until the next write, so an app installed before the rename does not reinstall its runtime.

### Rebrand + signed desktop app (v0.7.0 → v0.7.1)

- **Rebrand to `igni`.** Shorter, single-word product name — the Tauri window, the macOS Dock, the VSCode + JetBrains marketplace listings, the welcome screen, the agent's self-identification all read `igni`. Internal identifiers (PyPI package `ignite-ember`, Python module `igni_core`, Tauri bundle id `sh.ignite-ember.desktop`, VSCode/JetBrains plugin slugs) are preserved so existing installs keep auto-updating without breakage.
- **`igni` CLI alias.** The terminal command is now `igni` (the old `ignite-ember` invocation kept working through v1.0.4 and is retired since).
- **Apple Developer ID signing + notarization** on the macOS desktop app. First-launch no longer triggers the "unidentified developer" Gatekeeper warning; the notarization ticket is stapled into the `.app` so offline first-launch works too. The auto-updater pulls a fully signed + notarized bundle.
- **Mermaid diagrams in the chat.** Fenced ` ```mermaid ` blocks render as SVG (sequence / flowchart / state / ER / pie / gantt / mindmap / xy-chart / quadrant / timeline). Mermaid lazy-loads — first diagram in a session triggers the download.
- **Collapsible code blocks.** Anything taller than ~220px starts collapsed behind a chevron with a fade overlay; click anywhere on the clip (or the chevron) to expand. Copy chip stays always-visible top-right.
- **GitHub-Light syntax colors on light theme.** Stops the dark-dimmed `highlight.js` palette from painting neon-on-white when the user picks the light theme.
- **Per-turn stats restored on resumed conversations.** Re-opening a session now shows the same `✦ in · think · out · duration` pill the live path emits — chars/4 estimates pulled from each persisted run's content (Agno's raw `input_tokens` reads non-monotonic across iterations and was useless to restore literally).
- **Header drag-region fix.** The first user message's edit / delete chip is no longer captured by the Tauri shell's window-drag region — clicks reach the buttons.
- **Updater signature bug fixed (v0.7.1).** The macOS auto-updater's tarball-signature pair now matches at the asset level: stapled bytes are uploaded under the arch-suffixed name `latest.json` references, so signature verification succeeds. Anyone who installed v0.7.0 before this fix should update to v0.7.1 — the auto-updater itself will pull it through cleanly.

### GUI clients (v0.6.0 → v0.6.4)

- **Tauri desktop app.** Native window hosting the shared web UI; spawns the Python backend over a Unix socket, kills it on app exit (with `IGNI_PARENT_PID` watchdog for crash safety). macOS / Windows / Linux installers from CI; signed auto-updater backed by minisign.
- **VSCode extension.** WebView hosting the same shared UI; activates on workspace open with one command. Auto-installs `ignite-ember` via `uv` on first run — zero-touch.
- **JetBrains plugin.** JCEF panel hosting the same UI. Plugin SDK build wired into the release pipeline; published to the JetBrains Marketplace.
- **Shared web UI.** `clients/web` is the single React+TS surface; the three shells embed it. Single bundle, single React tree, single CSS — visual + behavioral parity across the three hosts.
- **Backend multi-session reliability.** SQLite hardening (WAL + checkpoint tuning), idle session eviction, slow-client back-pressure, sync-in-async call-site audit. Removes the long-running-session memory-leak class.
- **Release pipeline matured.** Marketplace publishing for VSCode + JetBrains; signed Tauri updater via minisign; per-tag draft → publish flow that avoids race-y duplicate releases under parallel matrix uploads. Pre-release tags (`-rc`) skip marketplace pushes + Homebrew updates so iteration doesn't pollute user-facing channels.
- **UI polish (v0.6.1).** Header / sidebar use a backdrop blur stack so messages frost as they scroll under; custom scroll-indicator thumbs (the native scrollbar would have rendered inside the blur layer); dark-theme icon variants.

**v0.5.0** is the "Beat Claude Code" release. Cumulative changes since v0.3.8:

### Benchmarks vs Claude Code (new in v0.5.0)

- **Head-to-head benchmark suite.** 12 cases × 5 runs vs Claude Code on the same target codebase. Programmatic deterministic grading. igni wins **49 / 60 ✅** vs **31 / 60 ✅** at one-eightieth of the per-run cost. Full breakdown in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
- **CodeIndex-first specialist agents.** New `*.codeindex.md` variants for `architect`, `debugger`, `explorer`, `reviewer`, `security`, `simplifier` — each with prompts tuned to query the index first instead of grepping. Auto-selected when a populated CodeIndex exists for the current commit.
- **Mandatory "What already exists" preamble.** The main-agent prompt now requires every code-write response to start with a four-bullet section naming (a) the reuse target, (b) the closest near-miss it considered and rejected, (c) the conventions to match, (d) the parallel infrastructure it will *not* introduce. Forces contrastive reasoning before code is written.
- **Encapsulation rule.** When a service class wraps a resource (db client, cache, queue, storage), new code must call methods on that class instead of inlining the raw client. Catches the "copy a private prefix into a new file and instantiate the client myself" anti-pattern.

### Plan-and-Align workflow (v0.4.2)

- **HITL approval for complex action work.** Main agent now plans first and waits for user approval before executing non-trivial tasks. Read-only review/audit work bypasses this and goes direct to the agent loop.
- **"Working with the User" framing.** Prompt restructured around "the human owns the bigger picture" — agent surfaces trade-offs, asks when uncertain, takes pushback seriously instead of defending its plan.
- **Strengthened safety rules.** Explicit handling for `.env` files, raw SQL, destructive shell commands, and blind-edit prevention.
- **Eval framework rebuilt.** Per-case Session isolation, per-case timeout overrides, `tool_arg_assertions` (e.g. asserts `spawn_team(mode="...")`), `prior_messages` for multi-turn cases, work-dir-aware file assertions. New eval suites for bulk_edits, file_safety, mcp, team_modes, team_tasks, knowledge_corner, knowledge_proactive, memory_knowledge, multiturn, error_recovery, long_running, schedule, web_discipline, background_notifications.

### Async shell + background notifications (v0.4.1)

- **Pure async shell tool.** `run_shell_command` / `read_process_output` / `watch_process` / `stop_process` / `list_processes` are now `async def` using `asyncio.create_subprocess_exec`. Previously blocked the event loop for up to the timeout (and a hard 3s on every `background=True` call), starving the HITL multiplexer drain and the FE message stream.
- **Background-process completion notifications.** When a backgrounded shell command finishes, the agent receives a queued `BACKGROUND PROCESS COMPLETED` notice with the output tail; the TUI also gets a `PushNotification`.
- **Idempotent `read_process_output`.** Multiple reads with different tails work; eviction moved to a 10-min TTL after the most recent read.
- **Schedule eval suite.** 12-case suite verifies the agent reaches for `Schedule` exactly when the user describes deferred/recurring work — and not otherwise. Achieves 12/12.

### Sub-agent HITL + run resumption (v0.4.0)

- **Sub-agent HITL bridge.** Specialist sub-agents (e.g. architect) can now request user confirmation through the same TUI flow as the main agent. Multiplexer was extracted from `run_message` and `resolve_hitl` so parent pauses no longer drop the specialist's pause requirements.
- **`acontinue_run` "No runs found" fix.** Session DB is now threaded into pool specialists and the `Team(...)` constructor so paused runs are persisted and can be resumed.
- **Concurrent-spawn race fix.** Per-spawn `copy.copy()` of pool specialists in `spawn_agent` / `spawn_team`. Without this, two concurrent spawns of the same specialist raced on shared `Agent` per-run state and Agno errored with "No runs found for run ID".
- **BE/TUI cleanup robustness.** Signal handlers, `atexit`, process-group kill, and an `IGNI_PARENT_PID` watchdog. Closes the runaway-BE failure mode where the TUI crash left a zombie backend.
- **`httpx` timeout actually applied.** Was previously shadowed by the SDK timeout when an `http_client` was provided.
- **Eval harness.** Auto-approve HITL drain, `--case-timeout` (default 60 s), `--case-retries` (default 2), `--spawn-timeout`, `retries=0` for determinism, WebSearch stub, ENV vs FAIL output classification, default concurrency 5.
- **New tests.** `test_subagent_hitl_e2e` (8 cases, bridge in isolation), `test_orchestrate_real_agno` (real Agno Agent + AsyncSqliteDb).
- **1363 tests pass.**

### Earlier v0.3.x fixes still relevant

**v0.3.8** includes the following changes:

- **Problem:** Escape key did not close the MCP panel (`/mcp`) or the task panel.
  - **Solution:** Added all dialog/panel widgets to the Escape handler, including MCPPanelWidget and TaskPanel.

**v0.3.7** includes the following changes:

- **Problem:** Edit/Write/Bash tools never asked for user confirmation, even when permissions were set to "ask".
  - **Solution:** `requires_confirmation` flag is now set on each function after registration in all custom toolkits.

**v0.3.6** includes the following changes:

- **Problem:** Debug logs grow unbounded and can fill up disk space.
  - **Solution:** Switched to RotatingFileHandler with 10MB limit and 2 backup files.

- **Problem:** Shell command output can be extremely large, causing issues when sending to the LLM.
  - **Solution:** Truncate tool results to 30,000 characters, keeping start and end of output.

- **Problem:** TUI escape key only closes the login dialog, leaving other panels open.
  - **Solution:** Now closes any open panel: login, help, model picker, or session picker.

- **Problem:** Learning features get stuck and never complete (caused by httpx connection pool issues in threads).
  - **Solution:** Run learning extraction as an async task on the main event loop instead of a separate thread.

To upgrade igni to the latest version:

```bash
brew upgrade ignite-ember/tap/igni-core
```

If `brew upgrade` doesn't work or you encounter issues, manually reinstall:

```bash
brew uninstall igni-core && brew untap ignite-ember/tap && brew install ignite-ember/tap/igni-core
```

