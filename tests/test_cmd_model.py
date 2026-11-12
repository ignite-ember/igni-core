"""``/model`` — the picker with no name, a switch with one.

Driven against a real :class:`Session`'s ``set_default_model`` and its
real registry check; only the team rebuild (which needs agents and a
provider) is stood in for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.cmd_model import ModelCommand, cmd_model
from igni_core.core.config.settings import Settings
from igni_core.core.session.core import Session
from igni_core.protocol.messages import CommandAction, CommandResultKind


def _session(monkeypatch) -> Session:
    settings = Settings()
    settings.models.registry = {"alpha": object(), "beta": object()}
    settings.models.default = "alpha"
    session = Session.__new__(Session)
    session.settings = settings
    session.rebuilds = 0

    def rebuild() -> None:
        session.rebuilds += 1

    monkeypatch.setattr(session, "rebuild_main_team", rebuild, raising=False)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["", "   "])
async def test_no_name_opens_the_picker_and_changes_nothing(monkeypatch, args: str):
    session = _session(monkeypatch)
    result = await cmd_model(SimpleNamespace(session=session), args)
    assert result.action is CommandAction.MODEL
    assert session.settings.models.default == "alpha"
    assert session.rebuilds == 0


@pytest.mark.asyncio
async def test_a_known_name_switches_and_tells_the_window(monkeypatch):
    session = _session(monkeypatch)
    result = await cmd_model(SimpleNamespace(session=session), "  beta  ")
    assert session.settings.models.default == "beta"
    assert session.rebuilds == 1
    assert result.kind is CommandResultKind.INFO
    assert result.content == "Switched to model: beta"
    assert result.action is CommandAction.MODEL_SWITCHED


@pytest.mark.asyncio
async def test_an_unknown_name_is_an_error_listing_what_exists(monkeypatch):
    session = _session(monkeypatch)
    result = await ModelCommand(session).dispatch("gamma")
    assert result.kind is CommandResultKind.ERROR
    assert "Unknown model: 'gamma'." in result.content
    assert "Available: alpha, beta" in result.content
    assert session.settings.models.default == "alpha"
    assert session.rebuilds == 0
