// workflow_runtime.mjs
// ─────────────────────────────────────────────────────────────────
// Node bridge for executing .igni/workflows/*.mjs scripts.
//
// Provides the `phase / agent / parallel / pipeline / log` globals
// the script body calls (CC's Workflow() semantics), emits
// structured events on stdout, and bridges agent() calls back to
// the BE via JSON-RPC over stdin.
//
// CLI:
//   node workflow_runtime.mjs <workflow_path> \
//     --workflow-run-id <id> --session-id <sid> --args '<json>'
//
//   node workflow_runtime.mjs <workflow_path> --discovery
//
// Stdout envelope (one JSON per line):
//   {"ts":<ms>,"run_id":"<wid>","seq":<n>,"type":"<event>","payload":{...}}
//
// Stdin envelope (BE → Node, agent call responses):
//   {"type":"agent_response","id":"<uuid>","ok":<bool>,"result":<any>,"error":<str>}
//   {"type":"cancel"}
//
// Agent call requests are emitted on stdout as events of type
// "agent_request"; the BE resolves them and writes the response
// back via stdin.

import { createContext, runInContext, Script } from "node:vm"
import { readFileSync } from "node:fs"
import { argv, cwd, exit, stdout, stdin } from "node:process"

// How long the runtime waits before it forces the process down, when
// something the script left behind keeps the event loop alive (a
// stray ``setInterval``, say). The events are already written by then.
const FORCE_EXIT_MS = 3000

// ── CLI parsing ────────────────────────────────────────────────────

function parseArgs(raw) {
  const args = { _: [] }
  let i = 0
  while (i < raw.length) {
    const a = raw[i]
    if (a.startsWith("--")) {
      const key = a.slice(2)
      const next = raw[i + 1]
      if (next === undefined || next.startsWith("--")) {
        args[key] = true
        i += 1
      } else {
        args[key] = next
        i += 2
      }
    } else {
      args._.push(a)
      i += 1
    }
  }
  return args
}

const cli = parseArgs(argv.slice(2))
const workflowPath = cli._[0]
if (!workflowPath) {
  process.stderr.write("workflow_runtime: missing <workflow_path> argument\n")
  exit(2)
}

const runId = cli["workflow-run-id"] || "wf_local"
const sessionId = cli["session-id"] || ""
let workflowArgs = {}
if (cli.args) {
  try {
    workflowArgs = JSON.parse(cli.args)
  } catch (err) {
    process.stderr.write(`workflow_runtime: --args is not valid JSON: ${err.message}\n`)
    exit(2)
  }
  if (workflowArgs === null || typeof workflowArgs !== "object" || Array.isArray(workflowArgs)) {
    process.stderr.write("workflow_runtime: --args must be a JSON object\n")
    exit(2)
  }
}
const discovery = !!cli.discovery

// ── Event emission ──────────────────────────────────────────────────

let seq = 0
function emit(type, payload) {
  const event = {
    ts: Date.now(),
    run_id: runId,
    seq: seq++,
    type,
    payload,
  }
  stdout.write(JSON.stringify(event) + "\n")
}

// ── Discovery short-circuit ────────────────────────────────────────

if (discovery) {
  try {
    const raw = readFileSync(workflowPath, "utf8")
    // Strip ESM `export` keywords (vm.Script is CommonJS-shaped),
    // then run the whole file inside an async IIFE so the script
    // body can use top-level ``await``, ``phase(...)``, and a
    // trailing ``return <value>`` like the real CC workflow
    // contract.
    const code = raw
      .replace(
        /^\s*export\s+(const|let|var|function|async\s+function|class)\s+/gm,
        "$1 ",
      )
      // The workflow's contract is `export const meta = {...}`.
      // After stripping the `export`, also rebind to
      // ``module.exports.meta`` so the discovery path can read it
      // synchronously without awaiting the IIFE.
      .replace(
        /^\s*(const|let|var)\s+meta\s*=/m,
        "module.exports.meta =",
      )
    const wrapped = `(async () => { ${code}\n })()`
    const mod = { exports: {} }
    const ctx = createContext({ module: mod, exports: mod.exports })
    ctx.module.exports = ctx.exports
    // The IIFE returns a Promise but we don't need the value for
    // discovery — just need ``module.exports.meta`` to be set
    // synchronously by the script before any await. If the script
    // awaits before exporting, the meta is undefined; CC's
    // convention is that meta is set at the top of the file
    // (before any await), so the wait doesn't matter.
    runInContext(wrapped, ctx, { filename: workflowPath })
    const meta = ctx.module.exports?.meta || ctx.exports?.meta
    if (!meta) {
      process.stderr.write(`workflow_runtime: ${workflowPath} did not export 'meta'\n`)
      exit(3)
    }
    emit("workflow_meta", { meta, path: workflowPath })
    // The meta must reach the reader before the process goes down, and
    // the discovery ends here: the run path below is not taken.
    await flushStdout()
    exit(0)
  } catch (err) {
    process.stderr.write(`workflow_runtime: discovery failed: ${err.stack || err.message}\n`)
    exit(4)
  }
}

// ── Stdin reader (for agent responses + cancel) ────────────────────

const cancelRequested = { value: false }
// The agent calls the script is still waiting for:
// id → { resolve, reject, label, started_at_ms, timer }.
const pending = new Map()
let stdinBuf = ""

stdin.setEncoding("utf8")
stdin.on("data", (chunk) => {
  stdinBuf += chunk
  let nl
  while ((nl = stdinBuf.indexOf("\n")) >= 0) {
    const line = stdinBuf.slice(0, nl).trim()
    stdinBuf = stdinBuf.slice(nl + 1)
    if (!line) continue
    let msg
    try {
      msg = JSON.parse(line)
    } catch {
      continue
    }
    if (msg.type === "cancel") {
      cancelRequested.value = true
      for (const { reject } of [...pending.values()]) {
        reject(new Error("workflow cancelled"))
      }
      pending.clear()
      continue
    }
    if (msg.type === "agent_response" && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id)
      pending.delete(msg.id)
      if (msg.ok) {
        resolve(msg.result)
      } else {
        reject(new Error(msg.error || "agent call failed"))
      }
    }
  }
})
stdin.on("end", () => {
  for (const { reject } of [...pending.values()]) {
    reject(new Error("subprocess stdin closed"))
  }
  pending.clear()
})

// ── Ending the run ─────────────────────────────────────────────────

/**
 * End the run with `code`, letting the process exit on its own.
 *
 * ``process.exit()`` throws away whatever is still queued on a piped
 * stdout: the last events of a busy run were simply lost, and the run
 * card the window drew ended on whatever came before them. Setting the
 * exit code and letting the loop drain writes every event out first.
 *
 * The stdin reader is released (``pause`` + ``unref``): its ``data``
 * listener is a live handle, so the process would otherwise never
 * exit. A stray handle the script left behind would hang the run, so
 * an unref'd timer forces the process down if it is still alive after
 * a grace period.
 */
function shutdown(code) {
  process.exitCode = code
  stdin.pause()
  // A pipe is a socket and can be released; a terminal is not, and
  // there is nothing to release.
  if (typeof stdin.unref === "function") stdin.unref()
  const force = setTimeout(() => exit(code), FORCE_EXIT_MS)
  if (typeof force.unref === "function") force.unref()
}

/**
 * Settle every agent call the script is still waiting for.
 *
 * A run that ends while a call is open leaves the window showing that
 * call as running for ever: the call is never answered, so no
 * ``agent_completed`` ever arrives for it. Each open call is failed
 * here, so every call the window knows about reaches a terminal state.
 */
function settleOpenAgents(reason) {
  const open = [...pending.entries()]
  for (const [id, entry] of open) {
    pending.delete(id)
    entry.reject(new Error(reason))
  }
  return open.map(([id]) => id)
}

/**
 * Wait for what was written to reach the reader.
 *
 * A piped stdout is written asynchronously, so a process that goes
 * down straight after a write can lose it.
 */
function flushStdout() {
  return new Promise((resolve) => {
    stdout.write("", () => resolve())
  })
}

// ── Globals provided to the workflow script ────────────────────────

const phaseStack = []
let phaseCounter = 0
let parallelCounter = 0
let pipelineCounter = 0

function phase(title) {
  while (phaseStack.length > 0) {
    const prev = phaseStack.pop()
    emit("phase_completed", {
      phase_id: prev.phase_id,
      title: prev.title,
      status: cancelRequested.value ? "cancelled" : "completed",
      duration_ms: Date.now() - prev.started_at_ms,
    })
  }
  // Monotonic counter so each phase has a unique id across the
  // whole run (the per-call Date.now() approach was racy at ms
  // granularity and produced colliding ids for phases that
  // started in the same millisecond).
  phaseCounter++
  const phase_id = `phase_${phaseCounter}_${Date.now().toString(36)}`
  phaseStack.push({ phase_id, title, started_at_ms: Date.now() })
  emit("phase_started", { phase_id, title })
}

function flushPhases(finalStatus) {
  while (phaseStack.length > 0) {
    const p = phaseStack.pop()
    emit("phase_completed", {
      phase_id: p.phase_id,
      title: p.title,
      status: finalStatus,
      duration_ms: Date.now() - p.started_at_ms,
    })
  }
}

function agent(prompt, opts = {}) {
  const {
    label = "agent",
    phase: agentPhase = null,
    schema = null,
    timeoutSeconds = 600,
  } = opts
  const id = `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`
  const started_at_ms = Date.now()
  const promise = new Promise((resolve, reject) => {
    emit("agent_started", {
      id,
      label,
      phase: agentPhase,
      phase_id: phaseStack.length > 0 ? phaseStack[phaseStack.length - 1].phase_id : null,
      prompt_preview: typeof prompt === "string" ? prompt.slice(0, 240) : "",
      schema_keys: schema && typeof schema === "object" ? Object.keys(schema) : null,
    })
    let timer
    if (timeoutSeconds > 0) {
      timer = setTimeout(() => {
        if (pending.has(id)) {
          pending.delete(id)
          emit("agent_completed", {
            id,
            label,
            status: "timeout",
            duration_ms: Date.now() - started_at_ms,
            error: `agent timed out after ${timeoutSeconds}s`,
          })
          reject(new Error(`agent '${label}' timed out after ${timeoutSeconds}s`))
        }
      }, timeoutSeconds * 1000)
    }
    pending.set(id, {
      label,
      started_at_ms,
      timer,
      resolve: (result) => {
        clearTimeout(timer)
        pending.delete(id)
        emit("agent_completed", {
          id,
          label,
          status: "completed",
          duration_ms: Date.now() - started_at_ms,
        })
        resolve(result)
      },
      reject: (err) => {
        clearTimeout(timer)
        pending.delete(id)
        emit("agent_completed", {
          id,
          label,
          status: "failed",
          duration_ms: Date.now() - started_at_ms,
          error: err.message,
        })
        reject(err)
      },
    })
    emit("agent_request", {
      id,
      prompt,
      label,
      phase: agentPhase,
      timeout_seconds: timeoutSeconds,
    })
  })
  // A call the script does not await must not take the runtime down
  // when it is settled: the rejection is still there for the script
  // that does await it, but the runtime has a handler of its own.
  promise.catch(() => {})
  return promise
}

function parallel(thunks) {
  const group_id = `par_${++parallelCounter}_${Date.now().toString(36)}`
  const started_at_ms = Date.now()
  emit("parallel_started", {
    group_id,
    phase_id: phaseStack.length > 0 ? phaseStack[phaseStack.length - 1].phase_id : null,
    lane_count: thunks.length,
  })
  // The lanes of this group and whether each has reported. A lane that
  // is still open when the group fails is reported here: the group
  // fails fast, so a lane that is still working is abandoned, and a
  // lane the window never hears from stays RUNNING for ever.
  const lanes = new Map()
  const settleLane = (lane_id, payload) => {
    const lane = lanes.get(lane_id)
    if (!lane || lane.settled) return
    lane.settled = true
    emit("parallel_lane_completed", payload)
  }
  return Promise.all(
    thunks.map((thunk, idx) => {
      const lane_id = `${group_id}_lane_${idx}`
      lanes.set(lane_id, { index: idx, settled: false })
      emit("parallel_lane_started", { group_id, lane_id, index: idx })
      return Promise.resolve()
        .then(() => thunk())
        .then(
          (result) => {
            settleLane(lane_id, {
              group_id,
              lane_id,
              index: idx,
              status: "completed",
            })
            return result
          },
          (err) => {
            settleLane(lane_id, {
              group_id,
              lane_id,
              index: idx,
              status: "failed",
              error: err.message,
            })
            throw err
          },
        )
    }),
  )
    .then((results) => {
      emit("parallel_completed", {
        group_id,
        status: "completed",
        duration_ms: Date.now() - started_at_ms,
      })
      return results
    })
    .catch((err) => {
      // The lanes that were still running are abandoned with the group.
      for (const [lane_id, lane] of lanes) {
        settleLane(lane_id, {
          group_id,
          lane_id,
          index: lane.index,
          status: "cancelled",
          error: `the parallel group failed: ${err.message}`,
        })
      }
      emit("parallel_completed", {
        group_id,
        status: "failed",
        duration_ms: Date.now() - started_at_ms,
        error: err.message,
      })
      throw err
    })
}

function pipeline(steps) {
  const group_id = `pipe_${++pipelineCounter}_${Date.now().toString(36)}`
  const started_at_ms = Date.now()
  emit("pipeline_started", {
    group_id,
    phase_id: phaseStack.length > 0 ? phaseStack[phaseStack.length - 1].phase_id : null,
    step_count: steps.length,
  })
  return steps
    .reduce(
      async (accP, step, idx) => {
        const acc = await accP
        const result = await step(acc)
        emit("pipeline_step_completed", { group_id, index: idx })
        return result
      },
      Promise.resolve(undefined),
    )
    .then(
      (result) => {
        emit("pipeline_completed", {
          group_id,
          status: "completed",
          duration_ms: Date.now() - started_at_ms,
        })
        return result
      },
      (err) => {
        emit("pipeline_completed", {
          group_id,
          status: "failed",
          duration_ms: Date.now() - started_at_ms,
          error: err.message,
        })
        throw err
      },
    )
}

function log(text) {
  emit("log", {
    phase_id: phaseStack.length > 0 ? phaseStack[phaseStack.length - 1].phase_id : null,
    text: String(text),
  })
}

// ── Load + execute the workflow file ───────────────────────────────

let scriptCode = readFileSync(workflowPath, "utf8")
// Strip ESM `export` keywords so vm.Script can run the file in a
// plain context. The workflow's contract is `export const meta = …`
// (used by the discovery path) and the body is plain JS — neither
// uses ESM features that this strip would break.
scriptCode = scriptCode.replace(/^\s*export\s+(const|let|var|function|async\s+function|class)\s+/gm, "$1 ")
const started_at_ms = Date.now()
emit("workflow_started", {
  name: cli.name || null,
  session_id: sessionId,
  args: workflowArgs,
  cwd: cwd(),
})

const ctx = createContext({
  args: workflowArgs,
  phase,
  agent,
  parallel,
  pipeline,
  log,
  console: {
    log: (...parts) =>
      log(
        parts.map((p) => (typeof p === "string" ? p : JSON.stringify(p))).join(" "),
      ),
  },
  // Useful standard-library bindings a workflow might want. The
  // sandbox intentionally excludes Node-only APIs (fs, process,
  // etc.) — those are bridged through the BE, not the runtime.
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  URL,
  URLSearchParams,
  TextEncoder,
  TextDecoder,
})

// Always wrap the script in an async IIFE — workflow files use
// top-level `return` (CC convention for the result value) and
// top-level `await`. vm.Script doesn't support either, but a
// thin async arrow-function body does.
const wrapper = `(async () => { ${scriptCode}\n })()`

runInContext(wrapper, ctx, { filename: workflowPath })
  .then(async (result) => {
    if (cancelRequested.value) {
      settleOpenAgents("the workflow was cancelled")
      flushPhases("cancelled")
      emit("workflow_completed", {
        status: "cancelled",
        duration_ms: Date.now() - started_at_ms,
      })
      shutdown(0)
      return
    }
    // A call the script left open is settled before the run ends, so
    // the window does not keep it at RUNNING under a finished card.
    settleOpenAgents("the workflow ended before this agent call finished")
    flushPhases("completed")
    emit("workflow_completed", {
      status: "completed",
      duration_ms: Date.now() - started_at_ms,
      result: result === undefined ? null : result,
    })
    shutdown(0)
  })
  .catch((err) => {
    if (cancelRequested.value) {
      // The cancel itself rejected the script's pending agent() call;
      // that is how a cancelled run ends, not a failure of the script.
      settleOpenAgents("the workflow was cancelled")
      flushPhases("cancelled")
      emit("workflow_completed", {
        status: "cancelled",
        duration_ms: Date.now() - started_at_ms,
      })
      shutdown(0)
      return
    }
    // The lanes the failed group abandoned are settled too: a call
    // nobody answers is a call the window shows as running for ever.
    settleOpenAgents("the workflow ended before this agent call finished")
    flushPhases("failed")
    emit("workflow_failed", {
      error: err.message,
      stack: err.stack,
      duration_ms: Date.now() - started_at_ms,
    })
    shutdown(1)
  })
