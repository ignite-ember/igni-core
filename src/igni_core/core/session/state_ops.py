"""Session mode-state mutators.

Small helpers extracted from :mod:`igni_core.core.session.core`
that flip runtime state without rebuilding the agent:

* :meth:`RuntimeModeCoordinator.set_output_style` — hot-patches
  the main team's ``instructions`` list to swap the ``# Output
  style: ...`` block. The next ``arun`` sees the new tone; no
  team rebuild needed.
* :meth:`RuntimeModeCoordinator.set_permission_mode` — flips the
  live ``PermissionEvaluator`` mode. The evaluator instance is
  shared with the tool-event hook, so the change takes effect
  on the very next tool call.

Both broadcast the corresponding change event to attached
clients (``output_style_changed`` / ``permission_mode_changed``)
so the FE's status chip updates without polling. The
broadcasts are the typed :class:`OutputStyleChangedEvent` /
:class:`PermissionModeChangedEvent` from
:mod:`~igni_core.core.session.schemas`.

The ``# Output style:`` block encoding — the marker prefix and
the empty-body-prune rule — is owned by
:class:`OutputStyleInstructionsPatcher` in this same module.
Callers cross the class boundary; the coordinator composes the
patcher rather than reaching into the team's ``instructions``
list itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from igni_core.core.config.permission_eval import PermissionEvaluator, PermissionMode
from igni_core.core.config.permission_eval.mode_policy import ModePolicy
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.output_style_selection import OutputStyleSelection
from igni_core.core.session.schemas import (
    OutputStyleChangedEvent,
    PermissionModeChangedEvent,
)

if TYPE_CHECKING:
    from agno.agent import Agent


class OutputStyleInstructionsPatcher:
    """Owns the ``# Output style: <name>`` block encoding inside a
    team's ``instructions`` list.

    Stateless — one instance per :class:`RuntimeModeCoordinator`
    (composed in ``__init__``). Concentrates the marker prefix
    (``_MARKER_PREFIX``) and the empty-body-prune rule so there
    is exactly one place in the codebase that knows how the
    block is spelled. A future reader (e.g. a status pane that
    shows the active-style body) imports from here.
    """

    _MARKER_PREFIX: ClassVar[str] = "# Output style:"

    def apply(self, team: Agent | None, style_name: str, body: str) -> None:
        """Replace any existing style block on ``team.instructions``
        with the new one.

        Behaviour:

        * ``team is None`` is a no-op — the style change takes
          effect on the next team build.
        * The existing block (zero or one — only one active style
          at a time) is pruned.
        * If ``body`` is empty, no new block is appended (the
          "silent" style should remove the header rather than
          leave a bare marker line).
        * Otherwise a fresh ``# Output style: <name>\\n\\n<body>``
          block is appended.
        """
        if team is None:
            return
        insts = team.instructions
        if not isinstance(insts, list):
            return
        pruned = [
            s for s in insts if not (isinstance(s, str) and s.startswith(self._MARKER_PREFIX))
        ]
        if body:
            pruned.append(f"{self._MARKER_PREFIX} {style_name}\n\n{body}")
        team.instructions = pruned


@dataclass(frozen=True)
class RuntimeModeDeps:
    """What the mode coordinator reads and writes.

    * ``evaluator_ref`` — the live evaluator; a plugin reload replaces it.
    * ``output_style`` — the session's style selection (never replaced).
    * ``team_ref`` — the main team; compaction, plugin reload and
      MCP rebuild all replace it.
    * ``bus`` — where the change events go.
    * ``permissions`` — the settings the session started with: its
      starting mode and the organisation's mode policy. ``None`` resolves
      the starting mode from the evaluator and applies no policy.
    """

    evaluator_ref: Callable[[], PermissionEvaluator]
    output_style: OutputStyleSelection
    team_ref: Callable[[], Agent | None]
    bus: BroadcastBus
    permissions: PermissionsConfig | None = None


class RuntimeModeCoordinator:
    """Owns runtime-state mutations that don't require an agent rebuild."""

    def __init__(self, deps: RuntimeModeDeps) -> None:
        self._deps = deps
        self._patcher = OutputStyleInstructionsPatcher()
        # Recorded once, at session start: the mode the session began in
        # (where ``off`` returns to) and the organisation's mode policy
        # (the floor and ``allow_bypass``).
        perms = deps.permissions
        self._starting_mode: PermissionMode | None = None
        if perms is not None:
            try:
                self._starting_mode = PermissionMode(perms.mode)
            except ValueError:
                self._starting_mode = None
        policy = perms.mode_policy if perms is not None else None
        self._policy: ModePolicy = policy if isinstance(policy, ModePolicy) else ModePolicy()

    @property
    def policy(self) -> ModePolicy:
        """The organisation's mode policy recorded at session start."""
        return self._policy

    @property
    def starting_mode(self) -> PermissionMode:
        """The mode this session started in — where ``/accept off`` and
        friends return to."""
        if self._starting_mode is None:
            mode = self._deps.evaluator_ref().mode
            self._starting_mode = (
                mode if isinstance(mode, PermissionMode) else PermissionMode.DEFAULT
            )
        return self._starting_mode

    def set_output_style(self, name: str) -> str:
        """Switch the active output style at runtime.

        Returns a short status string for the slash command to echo.
        Unknown style names produce a list of available options.
        """
        selection = self._deps.output_style
        clean = (name or "").strip()
        if clean not in selection.styles:
            available = sorted(selection.styles)
            return (
                f"Error: unknown output style {clean!r}. "
                f"Available: {', '.join(available) if available else '(none configured)'}"
            )
        prev = selection.active
        selection.select(clean)
        self._patcher.apply(
            self._deps.team_ref(),
            clean,
            selection.styles[clean].body,
        )
        # Broadcast so the FE can show the active style in a status
        # chip (parallel to permission_mode_changed).
        self._deps.bus.emit(OutputStyleChangedEvent(style=clean, previous=prev))
        if prev == clean:
            return f"Output style already {clean}."
        return f"Output style: {prev or '(none)'} → {clean}."

    def set_permission_mode(self, mode: str) -> str:
        """Flip the live ``PermissionEvaluator`` mode (e.g. into or
        out of plan mode) without rebuilding the agent.

        Broadcasts a ``permission_mode_changed`` push so the FE's
        mode badge updates without polling. Returns a short status
        string for the caller (slash command, ``exit_plan_mode``
        tool) to surface.
        """
        try:
            new_mode = PermissionMode(mode)
        except ValueError:
            valid = ", ".join(m.value for m in PermissionMode)
            return f"Error: unknown permission mode {mode!r}. Valid: {valid}"
        # Every runtime switch — the slash commands, the HITL "accept all
        # edits" flip, ``enter_plan_mode`` and plan approval — arrives here,
        # so this is the one place the organisation's floor and
        # ``allow_bypass: false`` are enforced. Callers match on "Error".
        refusal = self._policy.refusal(new_mode)
        if refusal is not None:
            return refusal
        # Pin the starting mode before the first switch changes what the
        # evaluator would report.
        _ = self.starting_mode
        evaluator = self._deps.evaluator_ref()
        prev = evaluator.mode
        evaluator.mode = new_mode
        if prev == new_mode:
            return f"Permission mode already {new_mode.value}."
        self._deps.bus.emit(PermissionModeChangedEvent(mode=new_mode, previous=prev))
        return f"Permission mode: {prev.value} → {new_mode.value}."


__all__ = [
    "OutputStyleInstructionsPatcher",
    "RuntimeModeCoordinator",
    "RuntimeModeDeps",
]
