"""The per-tool switch: applied live first, kept on disk second, never reported kept when it was not.

:class:`MCPToolFilter` hides a disabled tool from the toolkit's live
``functions`` dict and brings it back from the connect-time snapshot. The
real-server path is in
``test_an_mcp_server_is_connected_filtered_and_asked_for_more.py`` and the
no-snapshot edges in
``test_the_mcp_tool_filter_works_without_a_connect_time_snapshot.py``; this
is the switch itself, on a plain toolkit and a real state file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.mcp.schemas import MCPToolInfo
from igni_core.core.mcp.tool_filter import MCPToolFilter
from igni_core.core.mcp.tool_state import MCPToolStateStore
from igni_core.core.paths import CONFIG_DIR


@dataclass
class _Function:
    description: str | None = None
    requires_confirmation: bool = False


@dataclass
class _Toolkit:
    functions: Any = field(default_factory=dict)


def _toolkit() -> _Toolkit:
    return _Toolkit({"search": _Function("Search the notes."), "erase": _Function("Erase a note.")})


def _filter(tmp_path: Path) -> MCPToolFilter:
    return MCPToolFilter(MCPToolStateStore(tmp_path))


def _state_file(tmp_path: Path) -> Path:
    return tmp_path / CONFIG_DIR / "mcp-tool-state.json"


def _block_the_state_file(tmp_path: Path) -> None:
    # A directory where the file belongs: nothing can be written there.
    _state_file(tmp_path).mkdir(parents=True)


def test_switching_a_tool_off_hides_it_and_on_brings_back_the_same_function(tmp_path):
    filt, kit = _filter(tmp_path), _toolkit()
    filt.snapshot("notes", kit)
    erase = kit.functions["erase"]

    filt.set_enabled("notes", "erase", False, kit)
    assert list(kit.functions) == ["search"]

    filt.set_enabled("notes", "erase", True, kit)
    assert kit.functions["erase"] is erase


def test_the_choice_is_kept_for_the_next_session(tmp_path):
    kit = _toolkit()
    first = _filter(tmp_path)
    first.snapshot("notes", kit)
    first.set_enabled("notes", "erase", False, kit)

    again = _filter(tmp_path)
    assert again.list_disabled("notes") == ["erase"]
    fresh = _toolkit()
    again.apply("notes", fresh)
    assert list(fresh.functions) == ["search"]


def test_switching_the_last_tool_back_on_drops_the_server_from_the_file(tmp_path):
    filt, kit = _filter(tmp_path), _toolkit()
    filt.set_enabled("notes", "erase", False, kit)
    filt.set_enabled("notes", "erase", True, kit)

    assert json.loads(_state_file(tmp_path).read_text()) == {"disabled": {}}
    assert filt.list_disabled("notes") == []


def test_a_tool_the_server_does_not_have_can_be_switched_and_a_never_off_one_on(tmp_path):
    filt, kit = _filter(tmp_path), _toolkit()
    filt.set_enabled("notes", "ghost", False, kit)
    assert filt.list_disabled("notes") == ["ghost"]
    assert sorted(kit.functions) == ["erase", "search"]

    filt.set_enabled("notes", "search", True, kit)
    assert filt.list_disabled("notes") == ["ghost"]


def test_switching_a_tool_off_twice_keeps_one_entry_and_the_list_is_sorted(tmp_path):
    filt = _filter(tmp_path)
    for tool in ("zeta", "alpha", "zeta", "mid"):
        filt.set_enabled("notes", tool, False, None)
    assert filt.list_disabled("notes") == ["alpha", "mid", "zeta"]
    assert filt.list_disabled("elsewhere") == []


def test_with_no_toolkit_the_choice_is_still_kept(tmp_path):
    _filter(tmp_path).set_enabled("notes", "erase", False, None)
    assert json.loads(_state_file(tmp_path).read_text()) == {"disabled": {"notes": ["erase"]}}


def test_a_state_file_that_cannot_be_written_raises_after_this_session_honours_the_choice(
    tmp_path,
):
    filt, kit = _filter(tmp_path), _toolkit()
    filt.snapshot("notes", kit)
    _block_the_state_file(tmp_path)

    with pytest.raises(OSError):
        filt.set_enabled("notes", "erase", False, kit)

    # Not reported as saved, but the live session already hides it.
    assert list(kit.functions) == ["search"]
    assert filt.list_disabled("notes") == ["erase"]


def test_descriptions_come_from_the_snapshot_with_blank_for_a_missing_one(tmp_path):
    filt, kit = _filter(tmp_path), _toolkit()
    kit.functions["erase"].description = None
    filt.snapshot("notes", kit)
    filt.set_enabled("notes", "search", False, kit)

    assert filt.tool_descriptions("notes", kit) == {"search": "Search the notes.", "erase": ""}


def test_without_a_snapshot_the_live_descriptions_skip_what_has_none(tmp_path):
    kit = _Toolkit({"search": _Function("Search the notes."), "odd": object()})
    assert _filter(tmp_path).tool_descriptions("notes", kit) == {"search": "Search the notes."}
    assert _filter(tmp_path).tool_descriptions("notes", None) == {}


def test_the_tool_rows_name_description_and_whether_each_is_on(tmp_path):
    filt, kit = _filter(tmp_path), _toolkit()
    filt.snapshot("notes", kit)
    filt.set_enabled("notes", "erase", False, kit)

    assert filt.list_tool_info("notes", kit) == [
        MCPToolInfo(name="search", description="Search the notes.", enabled=True),
        MCPToolInfo(name="erase", description="Erase a note.", enabled=False),
    ]
    assert filt.list_tool_info("elsewhere", None) == []


def test_a_confirmation_with_no_toolkit_reaches_the_snapshot_only(tmp_path):
    filt, kit = _filter(tmp_path), _toolkit()
    filt.snapshot("notes", kit)
    filt.require_confirmation("notes", {"erase"}, None)
    assert kit.functions["erase"].requires_confirmation is True
    assert kit.functions["search"].requires_confirmation is False
    # And with nothing known at all it is a no-op.
    filt.require_confirmation("elsewhere", {"erase"}, None)


def test_a_snapshot_of_a_toolkit_whose_functions_is_not_a_dict_is_not_taken(tmp_path):
    filt = _filter(tmp_path)
    filt.snapshot("notes", _Toolkit(["search"]))
    assert filt.list_tools("notes", None) == []
