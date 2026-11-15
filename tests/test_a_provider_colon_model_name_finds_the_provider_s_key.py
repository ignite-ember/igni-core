"""``provider:model_id`` finds the key where that provider's own tools keep it.

CONFIGURATION.md's resolution order parses a name with a ``:`` as
``provider:model_id``. The entry built from it carried no key and no way to
find one, so the key sent was the ``not-set`` placeholder and every keyed
provider answered 401, whatever was in the environment (campaign R066). The
shorthand now reads the provider's conventional variable.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.settings import Settings


@pytest.mark.parametrize(
    ("provider", "variable"),
    [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai_like", "OPENAI_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
    ],
)
def test_the_shorthand_reads_the_provider_s_variable(
    monkeypatch: pytest.MonkeyPatch, provider: str, variable: str
) -> None:
    monkeypatch.setenv(variable, "key-from-env")
    entry = ModelRegistry(Settings())._resolve_entry(f"{provider}:some-model")

    assert entry is not None
    assert entry.resolve_api_key() == "key-from-env"
    assert variable in entry.why_no_api_key()


@pytest.mark.parametrize(
    ("provider", "variable"),
    [("anthropic", "ANTHROPIC_BASE_URL"), ("openai_like", "OPENAI_BASE_URL")],
)
def test_the_shorthand_reads_the_provider_s_base_url(
    monkeypatch: pytest.MonkeyPatch, provider: str, variable: str
) -> None:
    """A key from the environment goes to the endpoint it was issued for,
    not the vendor's default host."""
    monkeypatch.setenv(variable, "https://gateway.example/anthropic")
    entry = ModelRegistry(Settings())._resolve_entry(f"{provider}:some-model")
    assert entry is not None and entry.url == "https://gateway.example/anthropic"


def test_without_a_base_url_variable_the_shorthand_names_no_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    entry = ModelRegistry(Settings())._resolve_entry("anthropic:some-model")
    assert entry is not None and entry.url is None


def test_an_unknown_provider_still_parses_with_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = ModelRegistry(Settings())._resolve_entry("elsewhere:m")
    assert entry is not None and entry.provider == "elsewhere"
    assert entry.api_key_env is None


def test_an_entry_that_does_not_validate_is_a_message_not_a_traceback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Following the missing-key warning's remedy with a partial row
    (``"anthropic:m": {api_key_env: K}``) crashed igni with a raw pydantic
    traceback."""
    from pathlib import Path

    from click.testing import CliRunner

    from igni_core.cli import cli
    from igni_core.core.paths import CONFIG_DIR

    config = Path.home() / CONFIG_DIR / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('models:\n  registry:\n    "anthropic:m": {api_key_env: K}\n')
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["--model", "anthropic:m", "-m", "hi"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), "a message, not a traceback"
    assert "models.registry.anthropic:m.model_id: Field required" in result.output
