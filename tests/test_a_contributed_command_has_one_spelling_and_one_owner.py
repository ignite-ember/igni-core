"""The table of slash commands extensions answer: one bare name, one owner, sorted.

``core/contributed_commands.py``. The catalog and the dispatcher read the
same table, so what is typed and what is listed must agree on a name.
"""

from __future__ import annotations

import pytest

from igni_core.core.contributed_commands import ContributedCommands, contributed_commands


async def _noop(args: str) -> str:
    return args


def _claim(table: ContributedCommands, name: str, owner: str = "ext-a") -> str | None:
    return table.register(name, owner=owner, description=f"{name} by {owner}", invoke=_noop)


class TestClaimingAName:
    def test_a_name_is_stored_bare_and_lowercase(self):
        table = ContributedCommands()
        assert _claim(table, "/Scan") is None
        found = table.get("scan")
        assert found is not None and found.name == "scan" and found.owner == "ext-a"

    @pytest.mark.parametrize("spelling", ["scan", "/scan", "SCAN", " /Scan ", "//scan"])
    def test_every_spelling_of_the_name_finds_it(self, spelling: str):
        table = ContributedCommands()
        _claim(table, "scan")
        assert table.get(spelling) is not None
        assert table.get(spelling).name == "scan"  # type: ignore[union-attr]

    @pytest.mark.parametrize("blank", ["", "/", "   ", " / "])
    def test_a_blank_name_is_refused_and_nothing_is_stored(self, blank: str):
        table = ContributedCommands()
        assert _claim(table, blank) == "a command needs a name"
        assert table.all() == []

    def test_a_name_held_by_another_owner_is_refused_and_keeps_its_owner(self):
        table = ContributedCommands()
        _claim(table, "scan", "ext-a")
        reason = _claim(table, "/SCAN", "ext-b")
        assert reason == "'/scan' is already answered by 'ext-a'"
        assert table.get("scan").owner == "ext-a"  # type: ignore[union-attr]

    def test_the_same_owner_registering_again_replaces_its_command(self):
        table = ContributedCommands()
        _claim(table, "scan")
        assert table.register("scan", owner="ext-a", description="new", invoke=_noop) is None
        assert [c.description for c in table.all()] == ["new"]


class TestReadingTheTable:
    def test_an_unknown_name_is_none(self):
        assert ContributedCommands().get("nothing") is None
        assert ContributedCommands().get("") is None

    def test_an_empty_table_lists_nothing(self):
        assert ContributedCommands().all() == []

    def test_the_list_is_sorted_by_name(self):
        table = ContributedCommands()
        for name in ["zeta", "alpha", "Mid"]:
            _claim(table, name)
        assert [c.name for c in table.all()] == ["alpha", "mid", "zeta"]

    async def test_the_stored_invoker_is_the_one_that_runs(self):
        table = ContributedCommands()
        _claim(table, "echo")
        assert await table.get("/echo").invoke("hi") == "hi"  # type: ignore[union-attr]


class TestLettingGo:
    def test_removing_an_owner_drops_all_its_commands_and_only_those(self):
        table = ContributedCommands()
        _claim(table, "a1", "ext-a")
        _claim(table, "a2", "ext-a")
        _claim(table, "b1", "ext-b")
        table.remove_owner("ext-a")
        assert [c.name for c in table.all()] == ["b1"]

    def test_a_dropped_name_can_be_claimed_by_somebody_else(self):
        table = ContributedCommands()
        _claim(table, "scan", "ext-a")
        table.remove_owner("ext-a")
        assert _claim(table, "scan", "ext-b") is None

    def test_removing_an_owner_with_nothing_is_a_no_op(self):
        table = ContributedCommands()
        _claim(table, "scan")
        table.remove_owner("nobody")
        assert [c.name for c in table.all()] == ["scan"]


def test_the_process_wide_table_is_one_object():
    from igni_core.core import contributed_commands as module

    assert module.contributed_commands is contributed_commands
    assert isinstance(contributed_commands, ContributedCommands)
