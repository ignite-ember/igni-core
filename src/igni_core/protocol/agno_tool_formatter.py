"""Format Agno tool-call events into TUI-friendly strings.

Replaces the pre-refactor free helpers ``format_tool_args`` +
``extract_result`` (which took an event as their state-first arg
and reached into it repeatedly). All logic is now on classes with
explicit collaborators:

* :class:`ToolCallFormatter` (ABC) — how to format one tool's args.
* :class:`DefaultFormatter` — the generic ``key=value`` fallback.
* :class:`SpawnFormatter` — the ``spawn_agent`` / ``spawn_team``
  special case (agent name + first-line task snippet).
* :class:`ToolCallFormatterRegistry` — dispatches
  ``tool_name → formatter`` polymorphically. The routing table is
  per-instance state — nothing is shared at module level.
* :class:`AgnoToolEventFormatter` — the coordinator caller-facing
  surface with :meth:`args_summary`, :meth:`friendly_name`, and
  :meth:`extract_result`. Composes a registry + optional
  :class:`EditDiffComputer` for the ``edit_file`` diff branch.

The friendly-name catalog (raw Agno tool name → display name) is
not formatter state — it is immutable data shared by several
callers, so it lives in :mod:`igni_core.protocol.tool_friendly_names`
as a class constant rather than behind a process-wide registry
singleton that anyone could mutate.

This module imports no Agno at all: every public method takes Agno
event objects duck-typed via ``getattr``, so importing the protocol
layer never loads Agno and the permission evaluator's import path
stays light.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from igni_core.protocol.messages import ToolResultData
from igni_core.protocol.tool_friendly_names import ToolFriendlyNames

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from igni_core.protocol.edit_diff_computer import EditDiffComputer


# ── ToolCallFormatter hierarchy ───────────────────────────────────


class ToolCallFormatter:
    """ABC for per-tool arg-summary formatters.

    Concrete subclasses override :meth:`format_args`. The registry
    routes each tool name to one instance (or falls back to the
    default). The pre-refactor if/elif on ``tool_name`` collapses
    to a dict lookup + polymorphic dispatch.
    """

    def format_args(self, tool_args: dict[str, Any]) -> str:  # pragma: no cover - ABC
        raise NotImplementedError


class DefaultFormatter(ToolCallFormatter):
    """Generic ``key=value, ...`` summary, capped at 3 entries.

    Values over 30 chars are truncated with ``...``. Matches the
    pre-refactor free-function fallback path exactly (tested in
    ``test_agno_events.py::TestFormatToolArgsGeneric``).
    """

    _MAX_ENTRIES = 3
    _MAX_VALUE_LEN = 30
    _TRUNCATE_AT = 27  # 27 + len("...") == 30

    def format_args(self, tool_args: dict[str, Any]) -> str:
        parts: list[str] = []
        for k, v in list(tool_args.items())[: self._MAX_ENTRIES]:
            val = str(v)
            if len(val) > self._MAX_VALUE_LEN:
                val = val[: self._TRUNCATE_AT] + "..."
            parts.append(f"{k}={val}")
        return ", ".join(parts)


class SpawnFormatter(ToolCallFormatter):
    """Special-case ``spawn_agent`` / ``spawn_team``.

    Their ``task`` arg is often a multi-paragraph markdown brief;
    showing it verbatim drowns the tool-call header and leaks raw
    markdown into the terminal. This formatter surfaces the agent
    name + optional mode + first non-empty line of the task
    (capped at 80 chars).
    """

    _TASK_MAX_LEN = 80
    _TASK_TRUNCATE_AT = 77

    def format_args(self, tool_args: dict[str, Any]) -> str:
        agent = self._agent_display(tool_args)
        task_line = self._first_task_line(tool_args)
        mode = tool_args.get("mode", "")

        parts: list[str] = []
        if agent:
            parts.append(agent)
        if mode:
            parts.append(f"mode={mode}")
        if task_line:
            parts.append(f'"{task_line}"')
        return ", ".join(parts)

    @staticmethod
    def _agent_display(tool_args: dict[str, Any]) -> str:
        """Coerce ``agent_name`` (str) or ``agent_names`` (list) to
        a single comma-joined display string.

        ``spawn_team`` passes a list; ``spawn_agent`` passes a
        scalar. Without this coercion the pre-refactor code hit a
        TypeError on the list path — pinned by
        ``test_spawn_team_joins_agent_names_list``.
        """
        raw = tool_args.get("agent_name") or tool_args.get("agent_names") or ""
        if isinstance(raw, (list, tuple)):
            return ", ".join(str(n) for n in raw)
        return str(raw)

    def _first_task_line(self, tool_args: dict[str, Any]) -> str:
        """First non-empty line of the task arg, truncated to 80."""
        task = str(tool_args.get("task", ""))
        first_line = next((ln.strip() for ln in task.splitlines() if ln.strip()), "")
        if len(first_line) > self._TASK_MAX_LEN:
            first_line = first_line[: self._TASK_TRUNCATE_AT] + "..."
        return first_line


# ── Registry ─────────────────────────────────────────────────────


class ToolCallFormatterRegistry:
    """Owns the ``tool_name → formatter`` routing table.

    The table is per-instance state: every registry owns its own
    formatters, and nothing is shared at module level. A plain
    construction is already the production shape — callers that
    want a different routing (an extra special-case formatter, say)
    build their own registry and inject it into
    :class:`AgnoToolEventFormatter`.

    The friendly-name catalog is *not* part of this class: it is
    immutable shared data and lives in
    :class:`~igni_core.protocol.tool_friendly_names.ToolFriendlyNames`.
    """

    def __init__(self) -> None:
        self._default_formatter: ToolCallFormatter = DefaultFormatter()
        self._formatters: dict[str, ToolCallFormatter] = {}

        # Register the two orchestration tools that get the spawn
        # special-case. Any new tool with the same shape (task +
        # agent_name) can be routed to the same instance.
        spawn = SpawnFormatter()
        self.register("spawn_agent", spawn)
        self.register("spawn_team", spawn)

    def register(self, tool_name: str, formatter: ToolCallFormatter) -> None:
        """Route ``tool_name`` to ``formatter`` for arg formatting.

        Idempotent — a second registration overwrites (last write
        wins). Callers wire this at boot; concurrent registration
        during a run is not supported.
        """
        self._formatters[tool_name] = formatter

    def formatter_for(self, tool_name: str) -> ToolCallFormatter:
        """Return the formatter that handles ``tool_name``.

        Falls back to :class:`DefaultFormatter` when no specific
        registration matches — matches the pre-refactor generic
        path.
        """
        return self._formatters.get(tool_name, self._default_formatter)

    def format_args(self, tool_name: str, tool_args: dict[str, Any] | None) -> str:
        """Format ``tool_args`` for ``tool_name`` via the routed
        formatter. Empty / non-dict args short-circuit to ``""``
        (defensive — the header is on the hot path and a TypeError
        here would kill the streaming display).

        ``tool_args`` stays a plain mapping: it carries tool-invocation
        payloads produced by Agno / third-party MCP servers whose
        schema we don't control, so there is no closed model to type
        it with.
        """
        if not tool_args or not isinstance(tool_args, dict):
            return ""
        return self.formatter_for(tool_name).format_args(tool_args)


# ── Coordinator ──────────────────────────────────────────────────


class AgnoToolEventFormatter:
    """Coordinator: turn an Agno tool event into TUI-friendly parts.

    Composes a :class:`ToolCallFormatterRegistry` plus an optional
    :class:`EditDiffComputer` (only needed for the ``edit_file``
    branch of :meth:`extract_result`). The registry is owned by this
    instance — a caller that wants a different routing injects its
    own; there is no shared registry behind a module-level getter.
    """

    def __init__(
        self,
        registry: ToolCallFormatterRegistry | None = None,
        diff_computer: EditDiffComputer | None = None,
    ) -> None:
        self._registry = registry if registry is not None else ToolCallFormatterRegistry()
        self._diff_computer = diff_computer

    # ── Public API ───────────────────────────────────────────

    def args_summary(self, tool_name: str, tool_args: dict[str, Any] | None) -> str:
        """Format ``tool_args`` for the tool-call header."""
        return self._registry.format_args(tool_name, tool_args)

    def friendly_name(self, tool_name: str, default: str | None = None) -> str:
        """Return the display name for an Agno tool."""
        return ToolFriendlyNames.friendly_name(tool_name, default=default)

    def extract_result(self, event: Any) -> ToolResultData:
        """Extract a :class:`ToolResultData` from a completed tool
        event.

        Handles the ``edit_file`` diff branch (composing with
        :class:`EditDiffComputer` when injected — otherwise treats
        the tool as unknown and falls through to the summary
        path). Preserves every pinned behavior from the pre-
        refactor free function:

        * ``"None"`` / ``"null"`` / ``"undefined"`` → empty result.
        * Single-line results truncated at 80 chars.
        * Multi-line results collapsed to ``"N lines of output"``.
        * ``"completed in <timing>"`` fallback when no summary text.
        * Failed edits (``Error:`` prefix) skip the diff branch so
          the error prefix survives
          :class:`~igni_core.protocol.tool_error_conventions.ToolResultErrorDetector`
          detection.
        """
        tool = getattr(event, "tool", None)
        timing = self._extract_timing(tool)
        result = getattr(tool, "result", None) if tool else None
        tool_name = getattr(tool, "tool_name", "?") if tool else "?"

        raw_text = str(result) if result is not None else ""
        result_str = raw_text.strip() if result else ""

        logger.debug(
            "extract_result [%s]: result type=%s, is_none=%s, len=%d",
            tool_name,
            type(result).__name__,
            result is None,
            len(raw_text),
        )

        # Diff branch: only when the diff computer is available AND
        # this is an edit_file success (see docstring for the
        # ``Error:`` skip rationale).
        if (
            self._diff_computer is not None
            and tool_name == "edit_file"
            and tool is not None
            and not result_str.startswith("Error:")
        ):
            rows = self._diff_computer.compute(getattr(tool, "tool_args", None))
            if rows:
                summary_msg = result_str or "Edited"
                if timing:
                    summary_msg = f"{summary_msg}, {timing}"
                return ToolResultData.from_edit_diff(
                    summary=summary_msg,
                    diff_rows=rows,
                )

        # Non-diff / non-edit path.
        full_text = self._normalize_full_text(result_str)
        summary = self._compose_summary(full_text, timing)
        return ToolResultData(summary=summary, full_result=full_text)

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _extract_timing(tool: Any) -> str:
        """Read the tool's duration metric and format it as ``X.XXs``.

        Returns ``""`` when no metrics are attached. Kept as a static
        helper so ``extract_result`` reads top-to-bottom without
        an inline metrics-walk block.
        """
        if not tool:
            return ""
        metrics = getattr(tool, "metrics", None)
        if not metrics:
            return ""
        duration = getattr(metrics, "duration", None)
        if duration is None:
            return ""
        return f"{duration:.2f}s"

    @staticmethod
    def _normalize_full_text(text: str) -> str:
        """MCP tools return literal ``"None"`` / ``"null"`` /
        ``"undefined"`` for empty responses. Treat all three as
        empty so the tool card doesn't show a misleading pill.
        """
        if text in ("None", "null", "undefined"):
            return ""
        return text

    @staticmethod
    def _compose_summary(full_text: str, timing: str) -> str:
        """Compose the collapsed-card summary line.

        * Single-line body → truncated at 80 chars.
        * Multi-line body → "N lines of output".
        * No body but timing → "completed in <timing>".
        * Body + timing → "<body>, <timing>".
        """
        summary = ""
        if full_text:
            lines = full_text.splitlines()
            if len(lines) <= 1:
                short = full_text[:80]
                summary = short + ("..." if len(full_text) > 80 else "")
            else:
                summary = f"{len(lines)} lines of output"

        if summary and timing:
            return f"{summary}, {timing}"
        if not summary and timing:
            return f"completed in {timing}"
        return summary


__all__ = [
    "AgnoToolEventFormatter",
    "DefaultFormatter",
    "SpawnFormatter",
    "ToolCallFormatter",
    "ToolCallFormatterRegistry",
]
