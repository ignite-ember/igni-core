"""What the warm imports do with modules that are missing, broken or listed twice.

``docs/CLOUD.md`` §9: an optional dependency must not stop igni from serving,
but a module that is there and broken is not hidden.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from igni_cloud.guest.warm import WarmImports


def test_nothing_to_import_is_a_run_that_skips_nothing(caplog: pytest.LogCaptureFixture) -> None:
    warm = WarmImports(())
    with caplog.at_level(logging.INFO, logger="igni_cloud.guest.warm"):
        warm.run()
    assert warm.skipped == []
    assert "warmed 0 modules" in caplog.text


def test_the_default_list_is_the_classes_own() -> None:
    assert WarmImports().modules is WarmImports.MODULES
    assert WarmImports(("json",)).modules == ("json",)


def test_the_logged_count_leaves_out_what_was_skipped(caplog: pytest.LogCaptureFixture) -> None:
    warm = WarmImports(("json", "igni_no_such_module_anywhere", "tomllib"))
    with caplog.at_level(logging.INFO, logger="igni_cloud.guest.warm"):
        warm.run()
    assert warm.skipped == ["igni_no_such_module_anywhere"]
    assert "warmed 2 modules" in caplog.text


def test_running_again_adds_to_what_was_skipped() -> None:
    warm = WarmImports(("igni_no_such_module_anywhere",))
    warm.run()
    warm.run()
    assert warm.skipped == ["igni_no_such_module_anywhere"] * 2


def test_a_module_that_fails_to_import_for_another_reason_is_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "igni_warm_broken.py").write_text("raise RuntimeError('broken on import')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    warm = WarmImports(("igni_warm_broken",))
    with pytest.raises(RuntimeError, match="broken on import"):
        warm.run()
    assert warm.skipped == []


def test_a_present_module_whose_own_import_is_missing_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "igni_warm_halfway.py").write_text("import igni_not_installed_below\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    warm = WarmImports(("igni_warm_halfway",))
    warm.run()
    assert warm.skipped == ["igni_warm_halfway"]
    assert "igni_warm_halfway" not in sys.modules


def test_what_is_listed_is_imported_by_the_run() -> None:
    sys.modules.pop("colorsys", None)
    WarmImports(("colorsys",)).run()
    assert "colorsys" in sys.modules
