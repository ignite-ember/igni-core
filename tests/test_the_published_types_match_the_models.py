"""A published type names the fields the model that produces it carries.

The host object model promises an extension a *shape*: the fields of a
named type, under the names the extension sees them. The member that
answers reads igni's answer back through the model that produced it —
one model per shape, so a field renamed in the backend is fixed once.

Two things must therefore agree, and nothing keeps them agreeing except
this test:

* the **published type** — what the schema says an extension gets, and
  what both SDKs are generated from;
* the **model** — what the member actually answers with.

If the published type names a field the model does not carry, the answer
fails the schema check in strict mode, and the SDKs are generated with a
field that is never there. If the model carries a field the published
type does not name, the extension silently loses it.

The one projection that is not equality is deliberate: a model registry
row carries the credential the model is reached with, and that is not
published. :data:`~igni_core.extensions.host.members.models.WITHHELD`
names it, and the check below holds the projection to exactly that.
"""

from __future__ import annotations

import typing

from pydantic import BaseModel

from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.members import ALL_MEMBERS
from igni_core.extensions.host.members.models import WITHHELD, ModelEntry
from igni_core.extensions.host.members.tag import MemberTag
from igni_core.extensions.host.spec import TypeSpec

#: The members that publish a named type but do not answer through a
#: model: they read a window's field, or a value the host keeps, and the
#: shape is the host's to name, not the member's.
#:
#: They are listed rather than ignored, so that one of them being typed
#: is a change to this file — the guard then holds it like any other.
NOT_YET_TYPED = frozenset(
    {
        "extensions.installed",
        "extensions.set_settings",
        "self.state",
        "sessions.attach",
        "sessions.create",
        "ui.back",
        "ui.composer",
        "ui.dialog",
        "ui.navigate",
        "ui.open_panel",
        "ui.open_preview",
        "ui.panel",
        "ui.preview",
        "ui.route",
        "ui.scroll",
        "ui.scroll_to",
        "ui.select",
        "ui.selection",
        "ui.set_composer",
        "ui.set_sidebar",
        "ui.sidebar",
        "ui.toast",
        "ui.toasts",
        "windows.list",
    }
)


def published_types() -> dict[str, str]:
    """Every published member: its path, and the type the schema promises."""
    out: dict[str, str] = {}
    for obj in HOST_SCHEMA.objects:
        for member in obj.properties:
            out[f"{obj.path}.{member.name}"] = member.type
        for member in obj.methods:
            out[f"{obj.path}.{member.name}"] = member.result
    return out


def member_returns() -> dict[str, typing.Any]:
    """Every member's path, and the type its implementation answers with."""
    out: dict[str, typing.Any] = {}
    for members in ALL_MEMBERS:
        for name in dir(members):
            if name.startswith("__"):
                continue
            method = getattr(members, name, None)
            key = MemberTag.of(method)
            if key is None:
                continue
            out[key.path] = typing.get_type_hints(method).get("return")
    return out


def models_in(annotation: typing.Any) -> list[type[BaseModel]]:
    """Every model the annotation carries, through lists, dicts and unions."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    origin = typing.get_origin(annotation)
    if origin is None:
        return []
    out: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        out.extend(models_in(arg))
    return out


def named_type(published_type: str) -> TypeSpec | None:
    """The named type a published type names, or ``None`` when it is one of
    the plain ones (``str``, ``json``, a list of them)."""
    base = published_type
    if base.startswith("list["):
        base = base[len("list[") : -1]
    if base.endswith(" | None"):
        base = base[: -len(" | None")]
    return HOST_SCHEMA.type(base)


def wire_names(model: type[BaseModel]) -> set[str]:
    """The names the model's fields go out under — its alias where it has
    one, the field's own name where it does not."""
    return {field.alias or name for name, field in model.model_fields.items()}


def test_every_published_type_names_the_model_it_promises() -> None:
    """The published fields and the model's own fields are the same fields."""
    published = published_types()
    returns = member_returns()
    checked = 0
    for path, annotation in sorted(returns.items()):
        published_type = published.get(path)
        if published_type is None:
            continue
        spec = named_type(published_type)
        if spec is None:
            # A plain type — a string, a number, ``json``. Nothing to
            # compare: no field is named.
            continue
        models = models_in(annotation)
        if not models:
            assert path in NOT_YET_TYPED, (
                f"{path} publishes {published_type!r} but answers with {annotation!r}: "
                "name the model it answers with, or list it in NOT_YET_TYPED"
            )
            continue
        promised = {field.name for field in spec.fields}
        carried = set().union(*(wire_names(model) for model in models))
        assert promised == carried, (
            f"{path} publishes {spec.name} with fields {sorted(promised)}, "
            f"but the model {[m.__name__ for m in models]} carries {sorted(carried)}"
        )
        checked += 1
    assert checked, "no published type was checked: the walk found nothing"


def test_the_untyped_members_are_the_ones_listed() -> None:
    """A member that publishes a named type and does not answer through a
    model is listed, so that it cannot be forgotten."""
    published = published_types()
    returns = member_returns()
    untyped = {
        path
        for path, annotation in returns.items()
        if path in published
        and named_type(published[path]) is not None
        and not models_in(annotation)
    }
    assert untyped == NOT_YET_TYPED


def test_the_model_row_hides_exactly_the_credential() -> None:
    """The registry row an extension sees is the row igni keeps, less the
    key — and nothing else."""
    registry_fields = set(ModelRegistryEntry.model_fields)
    assert {"api_key", "api_key_cmd"} == WITHHELD
    assert registry_fields >= WITHHELD, "the credential moved; the guard is stale"
    assert set(ModelEntry.model_fields) == registry_fields - WITHHELD


def test_the_projected_row_keeps_the_field_names() -> None:
    """The projection renames nothing: a field's name in the registry is its
    name in the projection."""
    for name, field in ModelRegistryEntry.model_fields.items():
        if name in WITHHELD:
            continue
        assert name in ModelEntry.model_fields
        assert ModelEntry.model_fields[name].annotation == field.annotation
