"""The seam between the host model and igni's backend, checked member by member.

``HostBackend`` is a protocol with no behaviour of its own; what it
promises is that every member the host model calls exists on the real
backend, with the same parameters and the same sync-or-async nature. A
member renamed on one side only would otherwise surface as an
``AttributeError`` or ``TypeError`` in an extension's call.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from igni_core.backend.host_backend import OrchestratorHostBackend
from igni_core.extensions.host.backend import HostBackend

MEMBERS = sorted(
    name
    for name, member in vars(HostBackend).items()
    if inspect.isfunction(member) and not name.startswith("_")
)


def shape(function: typing.Callable[..., object]) -> list[tuple[str, object, bool]]:
    """Each parameter's name, kind and whether it has a default; ``self`` left out."""
    return [
        (p.name, p.kind, p.default is not inspect.Parameter.empty)
        for p in inspect.signature(function).parameters.values()
        if p.name != "self"
    ]


def test_the_protocol_lists_the_members_the_host_model_calls() -> None:
    assert MEMBERS == [
        "act",
        "activity",
        "ask",
        "attach",
        "broadcast",
        "default_session",
        "deliver",
        "live_sessions",
        "post",
        "project_dir_of",
        "rpc",
        "show_view",
        "title",
        "views",
        "waiting_approvals",
    ]


@pytest.mark.parametrize("name", MEMBERS)
def test_the_real_backend_has_each_member_as_the_protocol_declares_it(name: str) -> None:
    declared = getattr(HostBackend, name)
    real = getattr(OrchestratorHostBackend, name, None)

    assert real is not None, f"OrchestratorHostBackend has no {name}"
    assert inspect.iscoroutinefunction(real) == inspect.iscoroutinefunction(declared)
    assert shape(real) == shape(declared)


def test_the_real_backend_holds_the_two_attributes_the_protocol_names() -> None:
    names = typing.get_type_hints(HostBackend, include_extras=True).keys()

    assert {"project_dir", "client_state"} <= set(names)
    init = inspect.signature(OrchestratorHostBackend.__init__).parameters
    assert {"project_dir", "client_state"} <= set(init)
