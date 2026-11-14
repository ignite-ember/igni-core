"""``IGNI_DEPLOYMENT`` and ``IGNI_DEV_EXTENSIONS``: read from the environment on every call.

An unset or unrecognised deployment is single-user, the safe default,
and an unrecognised one says so. The development flag is on only for
``1``, ``true`` and ``yes``.
"""

from __future__ import annotations

import logging

import pytest

from igni_core.extensions.profile import DEV_FLAG, ENV_VAR, DeploymentProfile, DevelopmentFlag


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv(DEV_FLAG, raising=False)


def test_nothing_set_is_single_user_and_silent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        profile = DeploymentProfile.current()

    assert profile is DeploymentProfile.SINGLE_USER and not profile.is_shared
    assert caplog.text == ""


@pytest.mark.parametrize("value", ["shared", "multi", "multi-user", " Shared ", "MULTI-USER"])
def test_a_shared_spelling_is_shared(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_VAR, value)

    profile = DeploymentProfile.current()

    assert profile is DeploymentProfile.SHARED and profile.is_shared


@pytest.mark.parametrize("value", ["", "  ", "single", "single-user", "local", " LOCAL "])
def test_a_single_user_spelling_is_single_user_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    monkeypatch.setenv(ENV_VAR, value)

    with caplog.at_level(logging.WARNING):
        assert DeploymentProfile.current() is DeploymentProfile.SINGLE_USER

    assert caplog.text == ""


def test_an_unrecognised_value_is_single_user_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ENV_VAR, "Cluster")

    with caplog.at_level(logging.WARNING):
        profile = DeploymentProfile.current()

    assert profile is DeploymentProfile.SINGLE_USER
    assert "IGNI_DEPLOYMENT='cluster' is not a deployment profile" in caplog.text


def test_the_profile_is_read_again_each_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "shared")
    assert DeploymentProfile.current().is_shared

    monkeypatch.setenv(ENV_VAR, "local")

    assert not DeploymentProfile.current().is_shared


def test_the_dev_flag_is_off_when_unset() -> None:
    assert DevelopmentFlag.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", " TRUE ", "Yes"])
def test_the_dev_flag_is_on_for_its_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(DEV_FLAG, value)

    assert DevelopmentFlag.enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "on", "2", "enabled"])
def test_the_dev_flag_is_off_for_anything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(DEV_FLAG, value)

    assert DevelopmentFlag.enabled() is False


def test_the_dev_flag_is_read_again_each_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEV_FLAG, "1")
    assert DevelopmentFlag.enabled()

    monkeypatch.setenv(DEV_FLAG, "0")

    assert not DevelopmentFlag.enabled()
