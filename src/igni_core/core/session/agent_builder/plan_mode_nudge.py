"""Plan-mode nudge — loads the plan-mode prose from bundled prompts.

The plan-mode instructions used to be an 84-line embedded string
inside ``build_main_agent``. That coupled prose editing to Python
source edits and made the file a mixed-concern blob. The prose now
lives in ``core/prompts/plan_mode.base.md``, matching how
``main_agent.md`` is stored, and :class:`PlanModeNudge` exposes
``.render()`` for the instructions builder.

It is the ``plan_mode`` prompt: an extension may override it, or edit
it, as it may the main prompt (:mod:`igni_core.core.prompts.contributions`).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.core.prompts.contributions import PromptContributions
from igni_core.core.prompts.document import PromptKind
from igni_core.core.prompts.loader import PromptLoader


class PlanModeNudge(BaseModel):
    """Render the plan-mode instructions block appended to the main
    agent's ``instructions`` list, as extensions have shaped it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: What extensions have done to the plan-mode prompt.
    contributions: PromptContributions
    #: igni's prompt files, and the anchors extensions fill into them.
    prompt_loader: PromptLoader

    def render(self) -> str:
        """Return the full plan-mode block as a single string."""
        composition = self.contributions.compose(
            PromptKind.PLAN_MODE.value, self.prompt_loader.load("plan_mode.base")
        )
        assert composition is not None  # igni always has a plan-mode prompt
        return self.prompt_loader.expand(composition.document.render()).strip()
