"""What an install context says when nobody told it anything.

The defaults are the safe answers: no development bundles, no project,
no secret source, and the profile the environment says.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.profile import ENV_VAR, DeploymentProfile


def test_a_bare_context_allows_nothing_and_knows_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)

    context = InstallContext()

    assert context.profile is DeploymentProfile.SINGLE_USER
    assert context.allow_development is False
    assert context.project_dir is None
    assert context.secret_resolver is None


def test_the_profile_is_the_one_in_the_environment_when_the_context_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, "shared")
    shared = InstallContext()
    monkeypatch.setenv(ENV_VAR, "single-user")
    single = InstallContext()

    assert shared.profile is DeploymentProfile.SHARED
    assert single.profile is DeploymentProfile.SINGLE_USER


def test_a_project_given_as_text_becomes_a_path(tmp_path: Path) -> None:
    context = InstallContext(project_dir=str(tmp_path))  # type: ignore[arg-type]

    assert context.project_dir == tmp_path
    assert isinstance(context.project_dir, Path)


def test_a_secret_resolver_is_kept_as_given() -> None:
    def resolver(name: str) -> str | None:
        return "v" if name == "token" else None

    context = InstallContext(secret_resolver=resolver)

    assert context.secret_resolver is resolver
