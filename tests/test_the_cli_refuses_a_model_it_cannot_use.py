"""What ``igni`` does with a model it cannot use.

``--model`` names a model. When the name resolves to nothing the run
used to start anyway: the session built a placeholder, the agent
called it, and the failure reached stderr three times over — the
registry's own warning, agno's error line and igni's error line —
with a timing footer after it, and the failed run was saved as a
session.

The CLI refuses the name before the session starts: the name is said
once, with the remedy, on stderr; the exit code is 1; nothing is
written.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.cli import invocation as invocation_module
from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.invocation import CliInvocation
from igni_core.cli.options import CliOptions
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas import ModelsConfig
from igni_core.core.config.settings import Settings


def _entry(model_id: str) -> ModelRegistryEntry:
    return ModelRegistryEntry(
        provider="openai_like",
        model_id=model_id,
        url="https://example.invalid/v1",
        api_key="k",
    )


def _invocation(settings: Settings, **options: Any) -> CliInvocation:
    return CliInvocation(CliOptions(**options), settings, HeadlessRun(drain_timeout_seconds=0.05))


@pytest.fixture(autouse=True)
def no_catalogue_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches a server: the catalogue refresh is stubbed out."""
    monkeypatch.setattr(invocation_module.CloudModelCatalog, "refresh", lambda self: 0)


def test_a_name_nobody_registers_is_refused_once_with_the_remedy(capsys) -> None:
    settings = Settings(models=ModelsConfig(registry={"known": _entry("known")}, default="gone"))
    with pytest.raises(SystemExit) as raised:
        _invocation(settings).check_model()
    assert raised.value.code == 1
    out, err = capsys.readouterr()
    assert out == ""
    # The name is said once — not by the registry, not by agno, not by a footer.
    assert err.count("'gone'") == 1
    assert "not in `models.registry`" in err
    # And the remedy names the models that are there.
    assert "known" in err


def test_a_model_that_is_there_is_not_refused(capsys) -> None:
    settings = Settings(models=ModelsConfig(registry={"known": _entry("known")}, default="known"))
    _invocation(settings).check_model()
    assert capsys.readouterr() == ("", "")


def test_no_model_at_all_still_starts_the_session(capsys) -> None:
    """Nothing is named: the placeholder's own remedy says what to do, and
    the session still starts — a first run reaches ``/login`` that way."""
    _invocation(Settings(models=ModelsConfig())).check_model()
    assert capsys.readouterr() == ("", "")


def test_a_provider_nobody_has_is_refused_by_name(capsys) -> None:
    settings = Settings(models=ModelsConfig(default="nosuchprovider:model"))
    with pytest.raises(SystemExit) as raised:
        _invocation(settings).check_model()
    assert raised.value.code == 1
    err = capsys.readouterr().err
    assert "nosuchprovider" in err
    assert "Available:" in err


def test_a_model_the_server_offers_is_not_refused(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A signed-in person may name a model their server offers and this
    machine has never seen: the catalogue is refreshed before the name is
    refused."""
    settings = Settings(models=ModelsConfig(default="cloud-model"))

    def refresh(self) -> int:
        settings.models.registry["cloud-model"] = _entry("cloud-model")
        return 1

    monkeypatch.setattr(invocation_module.CloudModelCatalog, "refresh", refresh)
    _invocation(settings).check_model()
    assert capsys.readouterr() == ("", "")


def test_a_model_the_server_no_longer_offers_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The refresh found nothing: the name resolves to nothing, and the
    refusal says so."""
    settings = Settings(models=ModelsConfig(default="cloud-model"))

    def refresh(self) -> int:
        settings.models.registry["something-else"] = _entry("something-else")
        return 1

    monkeypatch.setattr(invocation_module.CloudModelCatalog, "refresh", refresh)
    with pytest.raises(SystemExit):
        _invocation(settings).check_model()
    assert "'cloud-model'" in capsys.readouterr().err
