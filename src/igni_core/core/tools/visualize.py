"""VisualizeTools — the visualizer sub-agent's render tool.

The visualizer sub-agent (``bundled_agents/visualizer.md``) owns the
json-render schema in its system prompt and produces a spec conforming
to it. The card reaches the FE from the tool-call *stream*, not from
this tool: ``orchestrate.py`` forwards every argument fragment the
model streams as a ``visualization_delta`` and marks the card
``final=True`` once the arguments are complete. By the time this
method runs, the card is already on screen.

That is what makes the tool a confirmation. It exists so the
sub-agent has a tool to CALL — the whole point of moving from
content-stream to tool-call — and so ``VisualizeTools`` shows up in
``ToolRegistry``. The spec is one-way: it goes to the FE on the
stream and never comes back into the model's context.

Design notes:

- **No server-side validation of what the card says.** ``@json-render/core``
  ships ``validateSpec`` / ``autoFixSpec``; the FE's ``Renderer`` also
  falls through to a placeholder for unknown components. Duplicating
  validation in Python invents a schema we don't own and drifts from
  the library. What the BE does instead is *accept* whatever the model
  wrote: the tool's arguments are annotated with forgiving
  normalisers, so a payload the FE would have drawn can never fail the
  call — see :mod:`igni_core.core.tools.visualize_schemas`.
- **No idempotency guard.** Streaming means multiple ``visualize``
  invocations per run are legitimate — each is a patch/refinement of
  the same card. Deduping happens on the FE via a stable
  ``spec_id``, which the orchestrate stream assigns per visualizer
  run (``SubAgentStreamState.vis_spec_id``). One card per stream,
  updated in place.
"""

from __future__ import annotations

import logging
from typing import Annotated

from agno.tools import Toolkit
from pydantic import BeforeValidator

from igni_core.core.tools.visualize_schemas import JsonRenderSpec, SpecArgument

logger = logging.getLogger(__name__)


def _title_text(raw: object) -> str:
    """The title as a string, whatever the model wrote.

    The visualizer's prompt marks the title optional, and a model
    that leaves it out often writes ``null``. The card is already on
    screen by the time this runs, so a title that is not a string
    must not fail the call: a missing title is the empty title the FE
    renders without a heading. A title that is not a scalar at all
    is dropped rather than stringified — a heading is text, and a
    container's repr is not text.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, int | float):
        return str(raw)
    return ""


#: The title as the ``visualize`` tool takes it. Agno validates a
#: tool call's arguments against the tool's own annotations *before*
#: the tool body runs, so a plain ``str`` annotation turns the title
#: into a hard gate on the whole call — over a heading that is
#: optional to begin with.
TitleArgument = Annotated[str, BeforeValidator(_title_text)]


class VisualizeTools(Toolkit):
    """Single-method toolkit: ``visualize(spec, title)``."""

    def __init__(self) -> None:
        super().__init__(name="ember_visualize")
        self.register(self.visualize)

    async def visualize(
        self,
        spec: SpecArgument,
        title: TitleArgument = "",
    ) -> str:
        """Render a json-render spec inline in the chat.

        The FE has already rendered the card progressively as the
        model streamed this call's arguments — the ``_LoggingModel``
        wrapper emits ``CustomEvent(event="tool_call_input_delta")``
        on every model chunk, and ``orchestrate.py`` forwards each
        to the FE as a ``visualization_delta``. By the time this
        method actually executes, the card is already on screen and
        marked ``final=True`` (via the ``ToolCallStartedEvent``
        handler in orchestrate.py). This method just returns a
        confirmation for the sub-agent's own recap; the wire-side
        work is done.

        Args:
            spec: A json-render spec of the form
                ``{"root": "<id>", "elements": {"<id>": {"type": ..., "props": ..., "children": [...]}}}``.
                Agno validates the tool call's arguments against this
                annotation *before* the body runs, so the annotation
                absorbs whatever the model wrote instead of rejecting
                it — a spec the FE would have drawn must never fail
                the call. It goes to the FE via the stream
                interception; ``@json-render/core``'s ``validateSpec``
                / ``Renderer`` fallback handle malformed input.
            title: A short label for the card (e.g. ``"AAPL — Monthly
                Close"``). The FE draws the card's own heading from the
                spec's components, so this is the label the BE records
                about the card, not a heading it renders. Optional — a
                missing title is the empty string, not a failure.

        Returns a one-line confirmation so the sub-agent has
        something to recap. Never puts the spec back into the
        model's context — the payload is one-way.
        """
        rendered = JsonRenderSpec.absorb(spec)
        logger.debug("visualize confirmed: %d element(s), title=%r", len(rendered.elements), title)
        return "Emitted visualization."
