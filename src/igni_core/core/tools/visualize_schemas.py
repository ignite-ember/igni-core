"""The json-render spec the visualizer sub-agent produces.

``@json-render/core`` owns the component catalog: the FE's renderer
draws whatever arrives and falls through to a placeholder for a
component it does not know. Restating that catalog in Python would
create a second source of truth that drifts from the renderer, so
this module models the *envelope* the renderer reads — the root id,
the element map, the state bag — and leaves an element's own ``props``
free-form.

The models are permissive on purpose (``extra="allow"``): inside the
envelope — the root id, the element map, the state bag — everything
is the renderer's vocabulary, and a spec the FE would have rendered
must never be rejected on the BE. The BE has no business deciding
what a card looks like, and the card was already drawn by the time
the spec reaches the tool.

Consumers:

* :class:`JsonRenderSpec` — the spec as the visualizer sub-agent
  produced it.
* :class:`JsonRenderElement` — one entry of the element map.
* :data:`SpecArgument` — the annotation the ``visualize`` tool takes
  for its spec.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, ValidationError


class JsonRenderElement(BaseModel):
    """One element of a json-render spec.

    ``type`` names a component from the FE's catalog and ``props``
    carries that component's own data, so both stay free-form.
    """

    model_config = ConfigDict(extra="allow")

    type: str = ""
    props: dict[str, Any] = Field(default_factory=dict)
    children: list[str] = Field(default_factory=list)
    on: dict[str, Any] = Field(default_factory=dict)
    #: The visibility condition, in the shapes the FE's renderer
    #: accepts: ``true``/``false``, one condition, a list of
    #: conditions (implicit AND), or an ``$and``/``$or`` object. The
    #: boolean is strict — ``"yes"`` is not a visibility, and reading
    #: it as one would be the BE deciding what the card means.
    visible: StrictBool | dict[str, Any] | list[dict[str, Any]] | None = None


class JsonRenderSpec(BaseModel):
    """The spec the visualizer sub-agent produced.

    The envelope the FE's renderer reads. :meth:`absorb` takes the
    tool-call payload as it came instead of validating it against a
    shape the FE owns, and the tool's argument annotation is that
    same forgiving entry point — see :data:`SpecArgument`.
    """

    model_config = ConfigDict(extra="allow")

    #: The id of the top-level element. The visualizer's prompt marks
    #: it and the element map as always present and ``state`` as
    #: optional, so the model says the same.
    root: str
    elements: dict[str, JsonRenderElement]
    state: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def absorb(cls, raw: object) -> JsonRenderSpec:
        """The spec as it came, never raising.

        A payload that is not a mapping at all — or one whose fields
        are not the shapes above — still comes back as a spec. The
        card was drawn (or placeholdered) by the FE before this tool
        ran, so a malformed payload must not fail the call.
        """
        if isinstance(raw, cls):
            return raw
        try:
            return cls.model_validate(raw)
        except ValidationError:
            return cls.model_construct(root="", elements={})


#: The spec as the ``visualize`` tool takes it.
#:
#: Agno validates a tool call's arguments against the tool's own
#: annotations *before* the tool body runs, so an annotation that
#: rejects a malformed payload rejects the call itself — and the card
#: is already on the FE's screen by then. Routing the annotation
#: through :meth:`JsonRenderSpec.absorb` keeps the model strict for
#: anyone building a spec by hand while making the tool's argument
#: impossible to fail.
SpecArgument = Annotated[JsonRenderSpec, BeforeValidator(JsonRenderSpec.absorb)]
