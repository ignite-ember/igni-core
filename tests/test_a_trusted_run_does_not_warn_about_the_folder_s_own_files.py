"""``igni --trust -m`` run twice: the folder's own files are read, and not warned about.

The first settings load of a run happens before ``--trust`` applies (it
reads the policy that says whether it may). It used to say the folder's
``settings.local.json`` "sets permissions … ignored" even though the run
then trusted the folder and read it. Two runs, as a person types them.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import igni_core.cli as cli_module
from igni_core.cli.headless_run import HeadlessRun
from igni_core.core.workspace_trust import process_trust

pytestmark = pytest.mark.workspace_trust

WARNING = "which a project may set only once you trust the folder"


@pytest.fixture(autouse=True)
def _environment_is_put_back():
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "project"
    (folder / ".igni").mkdir(parents=True)
    (folder / ".igni" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["WebSearch"]}})
    )
    monkeypatch.chdir(folder)
    monkeypatch.setattr(Path, "cwd", lambda: folder)  # the suite pins Path.cwd to a scratch dir
    monkeypatch.setattr(HeadlessRun, "run", lambda _self, coro, **kw: coro.close())
    return folder


def _run(*argv: str) -> None:
    result = CliRunner().invoke(cli_module.cli, list(argv))
    assert result.exit_code == 0, result.output


def _said(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if WARNING in r.getMessage()]


def test_two_trusted_runs_read_the_file_and_never_call_it_ignored(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        caplog.clear()
        _run("-m", "hello", "--trust")
        assert _said(caplog) == []
        assert process_trust().is_trusted(project)
        process_trust().revoke(project)  # a new process starts with no run-only answer


def test_an_untrusted_run_still_says_the_file_is_ignored(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    _run("-m", "hello")
    said = _said(caplog)
    assert said and "settings.local.json" in said[0]
    assert not process_trust().is_trusted(project)


def test_the_held_warnings_are_said_when_loading_fails(
    project: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from igni_core.cli.run_settings import CliSettingsLoader

    caplog.set_level(logging.WARNING)
    real = CliSettingsLoader.load

    def load(self: Any) -> Any:
        real(self)
        raise RuntimeError("boom")

    monkeypatch.setattr(CliSettingsLoader, "load", load)
    CliRunner().invoke(cli_module.cli, ["-m", "hello"])
    assert _said(caplog)
