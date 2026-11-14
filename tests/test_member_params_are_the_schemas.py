"""A member's own model of its parameters is the schema's, name for name.

:meth:`Call.params_as` reads a method's parameters as a
:class:`MemberParams` model. Each model names its method; its fields must
be exactly that method's parameters in the host schema, so a model
cannot drift from what the binder has already checked.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.members.call import Arguments, Call
from igni_core.extensions.host.members.params import MemberParams
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.spec import ErrorCode, MethodSpec
from tests.host_model_helpers import manifest


class SearchChatParams(MemberParams):
    """``chat.search``'s parameters, as a member would model them."""

    PATH = "chat.search"

    query: str
    limit: int | None


def _every(base: type[MemberParams]) -> list[type[MemberParams]]:
    found: list[type[MemberParams]] = []
    for sub in base.__subclasses__():
        found.append(sub)
        found.extend(_every(sub))
    return found


def _call(params: dict[str, object]) -> Call:
    return Call(
        extension_id="acme.tool",
        grants=HostGrants(manifest("acme.tool", host=[])),
        window=None,
        target=SessionTarget(session_id="boot", on_screen=True),
        arguments=Arguments(params=params),
    )


def test_each_params_model_has_its_methods_parameters():
    models = _every(MemberParams)

    assert models, "no params models found"
    for model in models:
        _spec, member = HOST_SCHEMA.resolve(model.PATH)
        assert isinstance(member, MethodSpec), model.__name__
        assert set(model.model_fields) == {p.name for p in member.params}, model.__name__


def test_a_call_reads_its_parameters_as_the_members_model():
    params = _call({"query": "hello", "limit": None}).params_as(SearchChatParams)

    assert params == SearchChatParams(query="hello", limit=None)


def test_parameters_outside_the_model_are_invalid():
    with pytest.raises(HostModelError) as refused:
        _call({"query": "hello", "limit": None, "extra": 1}).params_as(SearchChatParams)

    assert refused.value.code is ErrorCode.INVALID
    assert refused.value.message.startswith("chat.search(extra):")


def test_a_params_model_cannot_be_changed_once_read():
    """The parameters a member read are the parameters it acts on."""
    params = SearchChatParams.model_validate({"query": "a", "limit": None})

    with pytest.raises(ValidationError):
        params.query = "b"  # type: ignore[misc]
    assert params.query == "a"
