"""The panel RPCs' arguments: what is defaulted, what is ignored, what
is refused, and what each one works out for the handler.

A key a newer client adds is ignored (``search_code`` alone refuses one,
so a snippet sent under another name is reported). Missing required
arguments and values of the wrong kind are refused before the handler
runs. A completion limit below zero is refused, as it sliced the matches
to all but the last. ``ListDirsArgs`` roots a listing at the given
folder or the home folder, ``PickDirArgs`` opens the picker in the given
folder or the project's when it is one, and ``PromoteAgentArgs`` saves
to the project unless told ``personal``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.backend.rpc_handlers import panel_args as args


def test_the_defaults_of_the_panel_arguments() -> None:
    assert args.CompleteFilesArgs.model_validate({}).query == ""
    assert args.CompleteFilesArgs.model_validate({}).limit == 50
    assert args.RunShellArgs.model_validate({}).command == ""
    assert args.ListDirsArgs.model_validate({}).show_hidden is False
    assert args.ListDirsArgs.model_validate({}).path is None
    assert args.PickDirArgs.model_validate({}).start is None
    assert args.PromoteAgentArgs.model_validate({"name": "a"}).target is None
    assert args.VisualizationActionArgs.model_validate({}).action == ""
    assert args.VisualizationActionArgs.model_validate({}).params is None
    assert args.ProcessTailArgs.model_validate({"pid": 1}).tail == 200


@pytest.mark.parametrize(
    ("model", "wire"),
    [
        (args.CompleteFilesArgs, {}),
        (args.ReadFileArgs, {"path": "a.py"}),
        (args.RunShellArgs, {}),
        (args.ListDirsArgs, {}),
        (args.PickDirArgs, {}),
        (args.PromoteAgentArgs, {"name": "a"}),
        (args.AgentNameArgs, {"name": "a"}),
        (args.VisualizationActionArgs, {}),
        (args.ProcessTailArgs, {"pid": 1}),
        (args.ProcessArgs, {"pid": 1}),
    ],
)
def test_a_key_a_newer_client_adds_is_ignored(model: type, wire: dict) -> None:
    parsed = model.model_validate({**wire, "added_later": True})

    assert not hasattr(parsed, "added_later")


def test_search_code_refuses_a_key_it_does_not_name() -> None:
    with pytest.raises(ValidationError, match="snippet"):
        args.SearchCodeArgs.model_validate({"query": "def main", "snippet": "x"})


@pytest.mark.parametrize(
    ("model", "wire"),
    [
        (args.ReadFileArgs, {}),
        (args.SearchCodeArgs, {}),
        (args.PromoteAgentArgs, {"target": "personal"}),
        (args.AgentNameArgs, {}),
        (args.ProcessTailArgs, {"tail": 5}),
        (args.ProcessArgs, {}),
    ],
)
def test_a_missing_required_argument_is_refused(model: type, wire: dict) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(wire)


@pytest.mark.parametrize("model", [args.ProcessArgs, args.ProcessTailArgs])
def test_a_process_id_that_is_not_a_number_is_refused(model: type) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"pid": "abc"})


def test_a_completion_limit_below_zero_is_refused() -> None:
    with pytest.raises(ValidationError, match="limit"):
        args.CompleteFilesArgs.model_validate({"limit": -1})


def test_a_completion_limit_of_zero_asks_for_no_matches() -> None:
    assert args.CompleteFilesArgs.model_validate({"limit": 0}).limit == 0


def test_visualization_params_accept_nested_json_and_refuse_anything_else() -> None:
    nested = {"a": [1, {"b": None}], "c": "d"}
    assert args.VisualizationActionArgs.model_validate({"params": nested}).params == nested

    with pytest.raises(ValidationError):
        args.VisualizationActionArgs.model_validate({"params": {"a": object()}})


def test_a_listing_is_rooted_at_the_folder_given(tmp_path: Path) -> None:
    assert args.ListDirsArgs.model_validate({"path": str(tmp_path)}).root == tmp_path


@pytest.mark.parametrize("path", [None, ""])
def test_a_listing_with_no_folder_is_rooted_at_home(path: str | None) -> None:
    assert args.ListDirsArgs.model_validate({"path": path}).root == Path.home()


def test_the_picker_opens_in_the_folder_given(tmp_path: Path) -> None:
    folder = tmp_path / "chosen"
    folder.mkdir()
    picker = args.PickDirArgs.model_validate({"start": str(folder)})

    assert picker.start_dir(tmp_path / "project") == str(folder)


@pytest.mark.parametrize("start", [None, ""])
def test_the_picker_opens_in_the_project_when_no_folder_is_given(
    start: str | None, tmp_path: Path
) -> None:
    assert args.PickDirArgs.model_validate({"start": start}).start_dir(tmp_path) == str(tmp_path)


def test_the_picker_expands_a_home_relative_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    picker = args.PickDirArgs.model_validate({"start": "~/work"})

    assert picker.start_dir(Path("/nowhere")) == str(tmp_path / "work")


def test_the_picker_chooses_its_own_start_when_the_folder_is_not_one(tmp_path: Path) -> None:
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")

    for start in (str(a_file), str(tmp_path / "missing")):
        assert args.PickDirArgs.model_validate({"start": start}).start_dir(tmp_path) == ""


@pytest.mark.parametrize(
    ("target", "destination"),
    [(None, "project"), ("", "project"), ("project", "project"), ("personal", "personal")],
)
def test_an_agent_is_kept_in_the_project_unless_told_personal(
    target: str | None, destination: str
) -> None:
    promoted = args.PromoteAgentArgs.model_validate({"name": "a", "target": target})

    assert promoted.destination == destination
