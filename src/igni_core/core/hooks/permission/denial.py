"""What the model reads when the permission policy refuses a call."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar


@dataclass(frozen=True)
class Denial:
    """One refused call: the tool, and the evaluator's reason for refusing it.

    ``reason`` carries the mode's way out (``exit_plan_mode(plan)``,
    "read-only shell calls are fine"), which the model sees nowhere else.
    """

    tool_name: str
    reason: str = ""

    #: What to suggest when a tool being off leaves the model with no move.
    #: igni reads files through the shell, so a session with the shell
    #: denied refuses an agent's first move; a model told no alternative
    #: tends to retry the same call in a new costume. Phrased "if another
    #: of your tools can" because under ``--strict`` the alternatives are
    #: denied too.
    ALTERNATIVES: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            "run_shell_command": (
                " Shell access is off in this session, which also rules out reading "
                "files with cat/sed. If another of your tools can answer the question "
                "without the shell, use it; otherwise say what you would have run and "
                "why, rather than retrying."
            ),
        }
    )

    @property
    def has_alternatives(self) -> bool:
        """Whether a denial of this tool can name a way forward."""
        return self.tool_name in self.ALTERNATIVES

    def policy_message(self, *, tool_is_off_entirely: bool) -> str:
        """The block text for a policy DENY.

        The alternatives are attached only when the tool is off for every
        argument list: in plan mode or under a scoped deny rule the tool
        still works for the calls that were not denied.
        """
        text = f"Blocked: permission policy denied '{self.tool_name}'."
        if self.reason:
            text += f" {self.reason}"
        if tool_is_off_entirely:
            text += self.ALTERNATIVES.get(self.tool_name, "")
        return text

    def unasked_message(self) -> str:
        """The block text for a call that needed a yes nobody could give."""
        text = f"Blocked: '{self.tool_name}' needs the person's approval"
        if self.reason:
            text += f" ({self.reason})"
        return text + (
            ", and this session has no way to ask for it here. Ask the person in your "
            "reply instead of retrying. In plan mode, put the step in your plan and call "
            "exit_plan_mode(plan)."
        )
