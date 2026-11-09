"""DEVELOPMENT.md's slash-command table must be the registry's list.

The table was typed by hand and had drifted: ten commands the
backend answers were missing from it (``/ctx``, ``/plan``, ``/accept``,
``/bypass``, ``/output-style``, ``/programs``, ``/watcher``, ``/eject``,
``/fork`` and ``/extensions``), and the rows that were there described
a terminal chat UI that igni stopped shipping. A reader checking what
they can type in a session was told about two thirds of it.

The command list is not a fact about the documentation — it is a fact
about the backend. ``backend/builtin_command_registry.py`` is the single
source of truth: every entry there is a command igni answers, and
``docs/igni-commands.json`` is generated from the same list (see
``tests/test_igni_commands_are_listed.py``). This test holds the
document to that list, in both directions:

* a command the registry has and the table does not is a missing row;
* a row the table has and the registry does not is a command igni would
  refuse.

The second direction is the one that keeps the table honest as the
registry changes: dropping a command from the backend does not leave the
document claiming it still works.

The table lists a command by its first word — ``/schedule add <when>
<prompt>`` is the ``/schedule`` command with arguments, and
``/plugins enable <name>`` is ``/plugins`` — so a row is read as the
command it starts with. The one row that is not a built-in is a skill:
``/<skill-name> [args]`` invokes a skill by its own name, and the skills
that ship are not commands.
"""

from __future__ import annotations

import re
from pathlib import Path

from igni_core.backend.builtin_command_registry import BUILTIN_REGISTRY

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "DEVELOPMENT.md"

#: The row that is not a built-in command.
_SKILL_FORM = "/<skill-name>"

#: A backticked span inside a table cell.
_CODE_SPAN = re.compile(r"`([^`]+)`")


def _slash_commands_section() -> str:
    """The text under ``## Slash Commands``, up to the next heading."""
    text = DOC.read_text(encoding="utf-8")
    _, _, rest = text.partition("## Slash Commands")
    assert rest, "DEVELOPMENT.md has no '## Slash Commands' section"
    return rest.split("\n## ", 1)[0]


def _registry_commands() -> set[str]:
    """Every command the backend answers, with its slash."""
    return {f"/{name}" for name in BUILTIN_REGISTRY.names()}


def _documented_commands() -> set[str]:
    """Every command the document's table names.

    Only the first column is read: the description column mentions
    commands too (``/loop stop``, ``/memory optimize``), and those are
    arguments of the command in the row, not commands of their own.
    """
    commands: set[str] = set()
    for line in _slash_commands_section().splitlines():
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1]
        for span in _CODE_SPAN.findall(cell):
            command = span.split()[0]
            if command.startswith("/"):
                commands.add(command)
    return commands


def test_the_table_names_every_command_the_registry_has() -> None:
    """A command the backend answers is a row in the table."""
    documented = _documented_commands()
    missing = sorted(_registry_commands() - documented)
    assert not missing, (
        "DEVELOPMENT.md's slash-command table is missing commands the "
        f"backend answers: {missing}. Add a row for each, or take the "
        "command out of the registry."
    )


def test_the_table_names_no_command_the_registry_does_not_have() -> None:
    """A row is a command igni answers, or the skill form."""
    documented = _documented_commands()
    unknown = sorted(documented - _registry_commands() - {_SKILL_FORM})
    assert not unknown, (
        "DEVELOPMENT.md's slash-command table names commands the backend "
        f"does not have: {unknown}. They would be refused at the "
        "dispatcher."
    )


def test_the_table_still_lists_the_skill_form() -> None:
    """Skills are invoked by name; the row that says so stays."""
    assert _SKILL_FORM in _documented_commands(), (
        "DEVELOPMENT.md's slash-command table lost the skill form "
        f"(`{_SKILL_FORM} [args]`). Skills are not built-in commands, and "
        "without the row a reader does not know they can be typed."
    )


def test_the_table_has_a_row_for_every_command_it_names() -> None:
    """The table is a table: a command cell, then a description cell."""
    rows = [
        line
        for line in _slash_commands_section().splitlines()
        if line.startswith("| `") and not line.startswith("|---")
    ]
    assert len(rows) >= len(_registry_commands()), (
        "DEVELOPMENT.md's slash-command table has "
        f"{len(rows)} rows for {len(_registry_commands())} commands."
    )
    for row in rows:
        cells = [cell for cell in row.split("|") if cell.strip()]
        assert len(cells) >= 2, f"a row has no description: {row!r}"
