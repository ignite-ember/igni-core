"""The Node bridge: spawning ``workflow_runtime.mjs`` and talking to it.

```
.igni/workflows/<name>.mjs     (the project's, committed)
        │  imported via
        ▼
workflow_runtime.mjs           (the Node bridge — emits events on
                               stdout, reads agent responses on
                               stdin)
        │
        ▼
WorkflowRun                   (backend/workflow_run.py — persists +
                               pushes events, bridges agent() calls)
```

Why a subprocess? ``vm`` in Python can't safely run a workflow that needs
top-level ``await``, the CC-style ``return <value>`` at the top of the
file, or an async-iife wrapper without bringing in the full Node module
system. The Node bridge is small, has no runtime deps, and
``vm.createContext`` already gives fresh-per-run isolation inside it.

:class:`WorkflowRuntime` is the only place the bridge's CLI flags are
written down. ``asyncio.create_subprocess_exec`` and ``asyncio.wait_for``
are called through the module so tests can patch them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from igni_core.backend.schemas_workflow_events import (
    WorkflowCancelCommand,
    WorkflowMetaEvent,
    WorkflowRuntimeEvent,
    WorkflowRuntimeEventReader,
)
from igni_core.backend.schemas_workflows import (
    MetaProbeResult,
    WorkflowMetaEnvelope,
    WorkflowRunIdentity,
)

logger = logging.getLogger(__name__)

#: The Node bridge runtime, shipped next to this module.
RUNTIME_PATH = Path(__file__).parent / "workflow_runtime.mjs"

#: How long a cancelled run gets to exit by itself before SIGTERM.
CANCEL_GRACE_SECONDS = 5.0

#: How long a terminated run gets to exit before SIGKILL.
TERMINATE_GRACE_SECONDS = 2.0


class WorkflowRuntimeProcess:
    """One running bridge subprocess.

    Takes any object shaped like :class:`asyncio.subprocess.Process`
    (``stdout``, ``stdin``, ``wait``, ``terminate``, ``kill``,
    ``returncode``); nothing checks its class.
    """

    def __init__(self, proc: Any) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def wait(self) -> int:
        return await self._proc.wait()

    async def events(self) -> AsyncIterator[WorkflowRuntimeEvent]:
        """Every event the runtime prints, until stdout closes.

        Lines that are not events are skipped and logged.
        """
        stdout = self._proc.stdout
        if stdout is None:
            return
        while line := await stdout.readline():
            raw = line.decode("utf-8", errors="replace")
            event = WorkflowRuntimeEventReader.parse_line(raw)
            if event is None:
                if raw.strip():
                    logger.debug("workflow event validation failed: %s", raw[:200])
                continue
            yield event

    async def send(self, message: BaseModel) -> bool:
        """Write one stdin line. False when stdin is gone or closing."""
        stdin = self._proc.stdin
        if stdin is None or stdin.is_closing():
            return False
        try:
            stdin.write((message.model_dump_json() + "\n").encode("utf-8"))
            await stdin.drain()
        except OSError as exc:
            logger.debug("workflow stdin write failed: %s", exc)
            return False
        return True

    async def stop(self) -> None:
        """Ask the runtime to stop, then SIGTERM, then SIGKILL.

        The cancel line makes in-flight ``agent()`` calls reject at once,
        so a well-behaved script exits within the grace period.
        """
        await self.send(WorkflowCancelCommand())
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=CANCEL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()


class WorkflowRuntime:
    """Starts the bridge, for a probe or for a run, from ``cwd``."""

    def __init__(self, *, runtime_path: Path, cwd: Path) -> None:
        self._runtime_path = Path(runtime_path)
        self._cwd = Path(cwd)

    @property
    def available(self) -> bool:
        return self._runtime_path.exists()

    @property
    def missing_message(self) -> str:
        return (
            f"workflow runtime missing at {self._runtime_path}; "
            "the .mjs file is part of the repo, this is a build issue"
        )

    async def probe_meta(self, path: Path) -> MetaProbeResult:
        """Run ``path`` with ``--discovery`` and read its ``meta`` export."""
        if not self.available:
            logger.warning("workflow runtime missing at %s", self._runtime_path)
            return MetaProbeResult(reason=f"workflow runtime missing at {self._runtime_path}")
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(self._runtime_path),
            str(path),
            "--discovery",
            "--workflow-run-id",
            "discovery",
            cwd=str(self._cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        if proc.returncode != 0:
            stderr = stderr_b.decode("utf-8", errors="replace")[:400]
            return MetaProbeResult(reason=f"exit {proc.returncode}: {stderr}")
        for line in stdout_b.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                event = WorkflowRuntimeEventReader.parse(line)
            except ValidationError as exc:
                if WorkflowRuntimeEventReader.failed_type(exc) == "meta":
                    return MetaProbeResult(reason=f"meta invalid: {exc}")
                continue
            if isinstance(event, WorkflowMetaEvent):
                return MetaProbeResult(meta=event.payload.meta)
        return MetaProbeResult(reason="no workflow_meta line")

    async def start(
        self,
        identity: WorkflowRunIdentity,
        envelope: WorkflowMetaEnvelope,
        *,
        session_id: str,
        args: dict[str, Any],
    ) -> WorkflowRuntimeProcess:
        """Spawn a run of the workflow ``envelope`` describes."""
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(self._runtime_path),
            str(self._cwd / envelope.path),
            "--workflow-run-id",
            identity.workflow_run_id,
            "--session-id",
            session_id,
            "--name",
            envelope.meta.name,
            "--args",
            json.dumps(args),
            cwd=str(self._cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        return WorkflowRuntimeProcess(proc)
