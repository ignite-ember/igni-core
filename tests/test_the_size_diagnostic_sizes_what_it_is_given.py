"""``scripts/debug_prompt_size.py`` sizes whatever the session hands it.

The session is a stand-in with chosen instructions and tools, so each
shape the report has to survive can be given exactly: an instruction
that is blank, empty or not a string, a tool that cannot be serialised,
a session whose ``close`` is a plain function.

It used to take the first line of an instruction with
``s.lstrip().splitlines()[0]``, which is an IndexError for an
instruction made only of whitespace, and to ``await sess.close()``, a
TypeError for a ``close`` that is not a coroutine function. Either ended
the report in a traceback after the work was done.
"""

from __future__ import annotations

import json
import pathlib
import runpy
import sys
from types import SimpleNamespace

import pytest

from igni_core.core.session import core as session_core

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts/debug_prompt_size.py"


class _Tool:
    def __init__(self, spec):
        self._spec = spec

    def to_dict(self):
        return self._spec


class _BrokenTool:
    def to_dict(self):
        raise RuntimeError("cannot serialise")


class _Bare:
    def __repr__(self):
        return "B" * 30


def _session_class(instructions, tools, close=None, learning=None):
    class _Session:
        def __init__(self, project_dir, settings):
            self.main_team = SimpleNamespace(instructions=instructions, tools=tools)
            self._learning = learning
            self.closed = False
            if close is not None:
                self.close = close

    return _Session


def _report(monkeypatch, capsys, tmp_path, session_cls) -> list[str]:
    monkeypatch.setattr(session_core, "Session", session_cls)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), str(tmp_path)])
    runpy.run_path(str(_SCRIPT), run_name="__main__")
    return capsys.readouterr().out.splitlines()


def _row(lines, needle):
    return next(line for line in lines if needle in line)


def test_sections_are_sized_and_ordered_with_a_total(monkeypatch, capsys, tmp_path):
    spec = {"name": "t", "x": "y" * 40}
    cls = _session_class(
        ["a" * 100, "b" * 300, 12345],
        [_Tool(spec), _Bare(), _BrokenTool()],
    )

    lines = _report(monkeypatch, capsys, tmp_path, cls)

    tool_chars = len(json.dumps(spec)) + 30 + 200
    assert f"{tool_chars:>7,} chars" in _row(lines, "tools (3 resolved)")
    assert " 300 chars  ~    75 tok" in _row(lines, "instructions[1]")
    assert "   5 chars  ~     1 tok" in _row(lines, "instructions[2]")
    total = 100 + 300 + 5 + tool_chars
    assert f"{total:>7,} chars  ~{total // 4:>6,} tok" in _row(lines, "TOTAL")
    assert f"({300 / total * 100:5.1f}%)" in _row(lines, "instructions[1]")
    names = [line.split("  ")[1] for line in lines if " chars  ~" in line and "TOTAL" not in line]
    assert names[0].startswith("instructions[1]"), names


def test_a_blank_or_empty_instruction_is_labelled_empty(monkeypatch, capsys, tmp_path):
    cls = _session_class(["", "   \n  "], [])

    lines = _report(monkeypatch, capsys, tmp_path, cls)

    assert "'(empty)'" in _row(lines, "instructions[0]")
    assert "'(empty)'" in _row(lines, "instructions[1]")
    assert "0 chars" in _row(lines, "instructions[0]")


def test_a_label_is_the_first_line_cut_to_sixty(monkeypatch, capsys, tmp_path):
    cls = _session_class(["\n\n  " + "w" * 90 + "\nsecond line"], [])

    lines = _report(monkeypatch, capsys, tmp_path, cls)

    assert repr("w" * 60) in _row(lines, "instructions[0]")
    assert "second line" not in "\n".join(lines)


def test_an_empty_session_does_not_divide_by_zero(monkeypatch, capsys, tmp_path):
    lines = _report(monkeypatch, capsys, tmp_path, _session_class([], []))

    assert "0.0%" in _row(lines, "tools (0 resolved)")
    assert "TOTAL" in "\n".join(lines)


def test_a_failing_learning_context_is_a_row_not_a_crash(monkeypatch, capsys, tmp_path):
    class _Learning:
        async def abuild_context(self, **kwargs):
            raise RuntimeError("store down")

    cls = _session_class(["x"], [], learning=_Learning())

    lines = _report(monkeypatch, capsys, tmp_path, cls)

    assert "learning context — error: store down" in "\n".join(lines)


def test_a_working_learning_context_is_sized(monkeypatch, capsys, tmp_path):
    class _Learning:
        async def abuild_context(self, **kwargs):
            return "m" * 80

    lines = _report(monkeypatch, capsys, tmp_path, _session_class(["x"], [], learning=_Learning()))

    assert "80 chars" in _row(lines, "learning context (abuild_context)")


@pytest.mark.parametrize("is_async", [False, True])
def test_the_session_is_closed_whether_close_is_sync_or_async(
    monkeypatch, capsys, tmp_path, is_async
):
    calls: list[str] = []

    if is_async:

        async def close():
            calls.append("closed")

    else:

        def close():
            calls.append("closed")

    lines = _report(monkeypatch, capsys, tmp_path, _session_class(["x"], [], close=close))

    assert calls == ["closed"]
    assert "TOTAL" in "\n".join(lines)
