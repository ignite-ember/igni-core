# Coming from Claude Code

Already using Claude Code? igni is designed to feel familiar while giving you more. Agent definitions, MCP configs, and hooks use the **same formats** — most things just work when you switch.

## What Just Works (Zero Migration)

| What | Where | Status |
|---|---|---|
| Agent `.md` files | `.claude/agents/` | Loaded automatically (cross-tool support is on by default) |
| Skills | `.claude/skills/` | Loaded automatically |
| MCP config | `.mcp.json` | Same format, same location, works as-is |
| Hooks | `.claude/settings.json` | Same event names, same JSON format, same exit codes |
| Project instructions | `CLAUDE.md` | Read automatically (root + subdirectories) |

Cross-tool support is **enabled by default** — igni reads `CLAUDE.md` files, `.claude/agents/`, `.claude/skills/`, and `.claude/settings.json` out of the box. No configuration needed.

---

## Quick Start (for Claude Code users)

```bash
brew install ignite-ember/tap/ignite-ember
# or: pip install 'igni-core[decisions]==1.0.4'

igni login                            # sign in to your igni server account
igni -m "explain this repository"      # run one task from the terminal
```

That installs v1.0.4 — this repository's build, published as `igni-core`
(the old `ignite-ember` name installs the same package). See
[Quickstart](../README.md#quick-start) for how to run it from a checkout.

The chat itself lives in the desktop app or the web client — `igni` on its
own prints where to get them. Your existing Claude Code agents, skills, and
`CLAUDE.md` files are picked up automatically — cross-tool support is enabled
by default.

---

## What's Different

### 1. Dynamic Teams Instead of Manual Sub-Agents

**Claude Code:** You spawn sub-agents manually. The parent agent decides when to delegate. Sub-agents can't spawn their own sub-agents (depth limit: 1).

**igni:** An Orchestrator meta-agent automatically assembles teams for each task. It picks agents, chooses the team mode (route, coordinate, broadcast, tasks), and runs them. Every agent can spawn sub-teams — no depth limit.

```
Claude Code:                       igni:
  You → Agent → Sub-agent            You → Orchestrator → Team
                                            (auto-assembled)
                                         ├─ Agent A
                                         ├─ Agent B → Sub-team
                                         └─ Agent C    ├─ Agent D
                                                       └─ Agent E
```

You don't need to tell igni which agents to use. Just describe the task.

### 2. Team Modes

Claude Code has one execution model: single agent loop with optional sub-agent spawning.

igni has four team modes, picked automatically per-task:

| Mode | What It Does | Example |
|---|---|---|
| **Route** | Pick one agent, pass through | "What does this function do?" → Explorer |
| **Coordinate** | Sequential multi-agent | "Add endpoint with tests" → Planner → Editor → Reviewer |
| **Broadcast** | Parallel independent agents | "Review for security + performance" → both run simultaneously |
| **Tasks** | Autonomous multi-step | "Migrate test suite" → iterates through files |

### 3. Agents as Extensible Data

Both use `.md` files with YAML frontmatter. igni adds **optional** extension fields:

```yaml
---
name: my-agent
description: Does things
tools: Read, Write, Edit, Bash

# igni extensions (ignored by Claude Code)
tags: [backend, api]
reasoning: true
can_orchestrate: true       # can this agent spawn sub-teams?
---
```

Claude Code agents work in igni. igni agents work in Claude Code (extensions are ignored).

### 4. CodeIndex (Semantic Code Intelligence)

Claude Code agents grep for patterns. igni agents can also search by **meaning** via CodeIndex:

```
"How does authentication work?"
  Claude Code: grep for "auth", read matching files
  igni:  CodeIndex returns pre-processed summary with
               security analysis, dependency graph, and references
```

See `igni-integration.md` in the CodeIndex repository's docs (`igni__code-index`) for details.

### 5. Default Model

Claude Code defaults to Anthropic models (Claude). igni defaults to **MiniMax M2.7** through the igni hosted endpoint, but supports any model:

```yaml
# Use any OpenAI-compatible model
models:
  custom:
    - name: "claude-sonnet"
      url: "https://api.anthropic.com/v1"
      api_key: "sk-ant-..."           # or api_key_env: "ANTHROPIC_API_KEY"
      model_id: "claude-sonnet-4-6"
```

---

## Configuration Mapping

### Settings Files

| Claude Code | igni | Notes |
|---|---|---|
| `~/.claude/settings.json` | `~/.igni/settings.json` | Global settings |
| `.claude/settings.json` | `.igni/settings.json` | Project settings |
| `.claude/settings.local.json` | `.igni/settings.local.json` | Local overrides (gitignored) |
| `CLAUDE.md` | `igni.md` | `CLAUDE.md` also read by default (cross-tool support) |
| `~/.claude/CLAUDE.md` | `~/.igni/rules.md` | User-level global rules |
| `.claude/agents/*.md` | `.igni/agents/*.md` | Agent definitions (both dirs scanned) |
| `.mcp.json` | `.mcp.json` | MCP config (same file, same format) |

### CLI Flags

| Claude Code | igni | Notes |
|---|---|---|
| `claude` | `igni` | Main command (`ignite-ember` is the old name and still works) |
| `--model claude-sonnet-4-6` | `--model MiniMax-M2.7` | Different default, both support overrides |
| `--continue` / `-c` | `--continue` | Resume last session |
| `--resume <id>` | `--session-id <id>` | Resume specific session |
| `--print` / `-p` | `-m "prompt"` | Non-interactive single task |
| `--dangerously-skip-permissions` | `--auto-approve` | Skip all permission prompts |
| `--permission-mode plan` | `--read-only` | Read-only mode: reading files (read-only shell commands) and the web (search, URL fetch) runs, everything that writes or executes is refused (plan mode would ask) |
| `--permission-mode acceptEdits` | `--accept-edits` | Auto-approve file edits |
| `--worktree` | `--worktree` | Isolated git worktree session |
| `--effort low\|high` | Not applicable | igni uses Agno reasoning instead |
| `--tools <list>` | Per-agent in `.md` file | Tool access is per-agent, not global |
| `--add-dir <path>` | `--add-dir <path>` | Additional directories (repeatable) |
| `--file <path>` | N/A | igni auto-detects file paths and URLs in message text instead |

### Slash Commands

| Claude Code | igni | Notes |
|---|---|---|
| `/help` | `/help` | Same |
| `/clear` | `/clear` | Same |
| `/config` | `/config` | Same |
| `/model <name>` | `/model <name>` | Same |
| `/agent` | `/agents` | List/manage agents |
| `/hooks` | `/hooks` | View loaded hooks |
| — | `/memory` | List stored memories |
| — | `/memory optimize` | Consolidate memories |
| — | `/sessions` | Browse and resume past sessions |
| — | `/rename <name>` | Rename current session |
| — | `/skills` | List loaded skills |
| — | `/mcp` | Browse and toggle MCP server connections |
| — | `/login` | Device-flow authentication (opens browser) |
| — | `/logout` | Sign out |
| — | `/whoami` | Show current user |
| — | `/schedule` | Manage scheduled tasks |
| — | `/evals` | Run agent evaluations |

### Permissions

| Claude Code | igni | Notes |
|---|---|---|
| `"allow": ["Bash(npm run *)"]` | `permissions.allow: ["Bash(npm run *)"]` | Same rule syntax |
| `"deny": ["Read(.env)"]` | `safety.protected_paths: [".env"]` | igni uses a dedicated protected paths list |
| `"ask": ["Bash(git push *)"]` | `safety.require_confirmation: ["git push"]` | Same concept, different syntax |

### Memory & Storage

| Claude Code | igni | Notes |
|---|---|---|
| `~/.claude/projects/<id>/memory/` | Agno Memory (DB-backed) | igni uses Agno's memory system |
| File-based `MEMORY.md` | `~/.igni/projects/<id>/state.db` (SQLite) | Structured storage, not files |
| Local sessions only | SQLite by default, a shared database available | Set `storage.database_url` to sync |

---

## Migration Checklist

### Minimal (just switch)

- [ ] `brew install ignite-ember/tap/ignite-ember` (or `pip install ignite-ember`)
- [ ] `igni login` — sign in to your igni server account
- [ ] Run `igni -m "…"` — it automatically picks up:
  - `.mcp.json` (your MCP servers)
  - `igni.md` and `CLAUDE.md` (project instructions)
  - `.claude/agents/` and `.claude/skills/` (cross-tool support is on by default)

### Recommended (get the full benefit)

- [ ] Copy `CLAUDE.md` → `igni.md` (or keep both — igni reads both by default)
- [ ] Add igni extensions to agents that benefit from them:
  - `tags` for better Orchestrator routing
  - `reasoning: true` for agents that need chain-of-thought
  - `can_orchestrate: false` for agents that shouldn't spawn sub-teams
- [ ] Copy hooks from `.claude/settings.json` → `.igni/settings.json`
- [ ] Run `/agents refresh` to reload the agent pool

### Optional (power features)

- [ ] Set `storage.database_url` (or `IGNI_DATABASE_URL`) to a shared database for cross-device sync
- [ ] Configure BYOM if you want to use specific models
- [ ] Create project-specific evals for your custom agents
- [ ] Add CodeIndex-aware search to your agents' system prompts

---

## What igni Adds

Features you get that Claude Code doesn't have:

| Feature | What It Does |
|---|---|
| **Dynamic team assembly** | Orchestrator auto-picks agents and team mode per task |
| **Unlimited nesting** | Agents spawn sub-teams recursively (no depth limit) |
| **CodeIndex** | Semantic code intelligence with multi-category analysis |
| **Ephemeral agents** | Auto-generated agents for tasks no existing agent fits |
| **Agent evals** | Built-in regression testing for agent definitions |
| **First-run onboarding** | Proposes project-specific agents based on your codebase |
| **Cross-device sync** | Session + memory sync through a shared database (`storage.database_url`) |
| **Model agnostic** | Any model from any provider (not just Anthropic) |
| **Documents** | The Artifacts extension's document store; the code index is the CodeIndex extension's |
| **Learning** | Agno LearningMachine carries user memory across sessions |
| **Guardrails** | PII detection, prompt injection detection, content moderation as pre-hooks |
| **HITL** | Agents pause for confirmation or user input before sensitive operations |
| **Run cancellation** | Cancel a running agent mid-execution from the client |
| **Where you chat** | The desktop app or the web client; the `igni` command runs work and exits |
| **Task scheduling** | Schedule one-shot or recurring background tasks with configurable timeout and concurrency |
| **Task visualization** | Live task list showing the task, its statuses and assignees during tasks-mode execution |
| **IDE MCP support** | Agents declare which MCP servers they use via `mcp_servers` in their `.md` file |
| **Team modes** | Route, Coordinate, Broadcast, Tasks — right tool for each job |

---

## What igni Doesn't Have (Yet)

Features in Claude Code that igni hasn't implemented:

| Feature | Status | Notes |
|---|---|---|
| Effort levels (`--effort`) | N/A | igni uses Agno reasoning instead |

---

## Side-by-Side: Same Task, Different Approach

**Task:** "Add rate limiting to the /api/users endpoint with Redis and tests"

**Claude Code:**
```
You → Claude (single agent)
  1. Claude reads existing code (Read, Grep)
  2. Claude writes implementation (Edit, Write)
  3. Claude writes tests (Edit, Write)
  4. Claude runs tests (Bash)
  5. All in one agent loop, one context window
```

**igni:**
```
You → Orchestrator
  → Assembles team: [architect, editor, reviewer] in coordinate mode

  1. Architect reads code, designs approach (Read, Grep, CodeIndex)
  2. Editor implements rate limiting (Read, Edit, Write, Bash)
     └─ Editor spawns Explorer sub-team to understand Redis patterns
  3. Editor writes tests (Read, Edit, Write)
  4. Reviewer validates implementation (Read, Grep, CodeIndex)
     └─ Checks security category for rate limiting best practices
  5. Editor runs tests (Bash)
```

Same result. igni's approach gives better results on complex tasks because:
- Each agent has a focused role and system prompt
- CodeIndex provides architectural context that raw code search misses
- The Reviewer catches issues the Editor might overlook
- Sub-teams provide help exactly when needed
