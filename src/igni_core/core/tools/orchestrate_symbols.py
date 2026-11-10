"""The characters the parent-recap activity log is drawn with.

The log the parent agent reads back is a tree: a trunk, a branch per
tool call, a leaf per result. The characters that draw it are
load-bearing — they are the Claude-Code-parity shape, and
:meth:`AgentSpawn.format_result` scans the same log for ``RUN ERROR``
— so they live in one named place instead of as bare literals spread
through the streaming code.

:class:`LogSymbols` names each character. :data:`TASK_STATUS_ICONS`
names the ones that stand for a task's status.
"""

from __future__ import annotations

from enum import Enum


class LogSymbols(str, Enum):
    """The tree-drawing and status characters of the activity log."""

    PAUSE = "⏸"
    COMPLETED = "✓"
    FAILED = "✗"
    RUNNING = "…"
    WARNING = "⚠"

    T_BRANCH = "├─"
    T_TRUNK = "│"
    T_ROOT = "┌─"
    T_JOIN = "╞═"
    T_LEAF = "└─"


#: The character that stands for a task's status.
TASK_STATUS_ICONS: dict[str, str] = {
    "completed": LogSymbols.COMPLETED.value,
    "failed": LogSymbols.FAILED.value,
    "running": LogSymbols.RUNNING.value,
}


#: Activity-log prefix carrying a rendered ``json-render`` spec back
#: to whoever spawned the visualizer.
#:
#: The spec is streamed to the client as a chat card, and the caller
#: used to receive only the visualizer's prose — "Rendered a 6-element
#: 'Database Migration' card". Fine for an orchestrator that just
#: wanted the user to see something; useless to an agent that has to
#: put the spec *in a document*, where the document's viewer renders
#: the fence live. A description of a chart cannot be stored as one.
#:
#: The activity log is the channel because it already exists and is
#: already read this way: ``AgentSpawn.format_result`` scans it for
#: ``RUN ERROR`` and lifts what it finds into the parent's result.
#: This is the same move with a different prefix, which is why it
#: needs no signature change anywhere.
VIS_SPEC_PREFIX: str = "VISUALIZE SPEC "


__all__ = ["TASK_STATUS_ICONS", "VIS_SPEC_PREFIX", "LogSymbols"]
