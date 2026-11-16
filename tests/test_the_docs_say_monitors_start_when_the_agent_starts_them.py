"""The docs say a monitor starts when the agent starts it.

CLAUDE_CODE_PARITY row 33 said ``MonitorManager`` starts monitors eagerly,
TESTING_PLAN that they start on session boot, and PLUGINS.md that plugin
monitors are not loaded. Nothing calls ``MonitorManager.start_all``: every
session begins with its monitors ``stopped`` until ``monitor_restart``, and
plugin monitors are discovered like the rest (campaign R142).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src/igni_core"


def _start_all_callers() -> list[Path]:
    return [
        path
        for path in _SRC.rglob("*.py")
        if ".start_all(" in path.read_text() and "monitors" not in path.parts
    ]


def test_nothing_starts_the_monitors_at_session_start():
    assert _start_all_callers() == [], "monitors start eagerly now: update the docs"


def test_the_docs_do_not_promise_an_eager_start():
    parity = (_ROOT / "CLAUDE_CODE_PARITY.md").read_text()
    assert "`MonitorManager` with eager start" not in parity
    plan = (_ROOT / "TESTING_PLAN.md").read_text()
    assert "processes start on session boot" not in plan
    plugins = (_ROOT / "docs/PLUGINS.md").read_text()
    assert "**background monitors**, plugin `bin/`" not in plugins
