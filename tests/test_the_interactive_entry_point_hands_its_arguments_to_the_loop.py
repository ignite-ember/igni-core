"""``run_session_interactive`` is the REPL's public entry point.

It builds an :class:`InteractiveSessionLoop` from exactly the arguments it
was given and awaits its ``run()``: the loop's own behaviour is tested
with the loop; what is pinned here is that nothing is lost, defaulted
differently or swallowed on the way in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import igni_core.core.session.interactive as interactive
from igni_core.core.config.settings import Settings


class _Loop:
    built: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    ran = 0
    error: Exception | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).built.append((args, kwargs))

    async def run(self) -> str:
        type(self).ran += 1
        if type(self).error:
            raise type(self).error
        return "ignored"


@pytest.fixture(autouse=True)
def loop(monkeypatch: pytest.MonkeyPatch):
    _Loop.built, _Loop.ran, _Loop.error = [], 0, None
    monkeypatch.setattr(interactive, "InteractiveSessionLoop", _Loop)
    return _Loop


async def test_the_arguments_reach_the_loop_and_the_loop_runs(tmp_path: Path, loop) -> None:
    settings = Settings()
    extra = [tmp_path / "more"]

    result = await interactive.run_session_interactive(
        settings, resume_session_id="abc", project_dir=tmp_path, additional_dirs=extra
    )

    assert result is None
    assert loop.ran == 1
    [(args, kwargs)] = loop.built
    assert args == (settings,)
    assert kwargs == {
        "resume_session_id": "abc",
        "project_dir": tmp_path,
        "additional_dirs": extra,
    }


async def test_what_is_left_out_is_none(loop) -> None:
    await interactive.run_session_interactive(Settings())

    [(_, kwargs)] = loop.built
    assert kwargs == {"resume_session_id": None, "project_dir": None, "additional_dirs": None}


async def test_a_loop_that_raises_is_not_swallowed(loop) -> None:
    loop.error = RuntimeError("repl died")

    with pytest.raises(RuntimeError, match="repl died"):
        await interactive.run_session_interactive(Settings())


def test_the_package_still_exports_the_entry_point() -> None:
    from igni_core.core.session import run_session_interactive

    assert run_session_interactive is interactive.run_session_interactive
