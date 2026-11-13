"""The Windows AppContainer's edges: names, grants, the ledger, a failed start, the sweep.

The shape of a run (grants recorded before they are made, undone on
release, a crashed igni's run swept) is in
``test_an_extension_process_is_confined_on_windows.py``. Here are the
edges: odd extension ids, what a grant leaves out, a damaged ledger, a
start that fails or is cancelled halfway, and the sweep's guards. The
Win32 calls are faked at their boundary, as there; nothing here needs
Windows.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import types
from pathlib import Path

import pytest

import igni_core.extensions.sandbox as sandbox_package
from igni_core.extensions.sandbox import appcontainer
from igni_core.extensions.sandbox.appcontainer import (
    LOW_LABEL_DIR,
    LOW_LABEL_FILE,
    MAX_PROFILE_NAME,
    AppContainer,
    ContainerRun,
    Grant,
    GrantLedger,
    explicit_label,
)
from igni_core.extensions.sandbox.plan import SandboxPlan

pytestmark = pytest.mark.extension_sandbox


class Windows:
    """The Win32 calls, recorded."""

    def __init__(self, *, alive: set[int] | None = None, user_error: BaseException | None = None):
        self.alive = alive or set()
        self.user_error = user_error
        self.calls: list[tuple] = []
        self.labels: dict[Path, str] = {}
        self.label_error: OSError | None = None
        self.set_label_error: OSError | None = None
        self.revoke_error: OSError | None = None
        self.delete_error: OSError | None = None
        self.granted: set[Path] = set()

    def create_profile(self, name, display, description):
        self.calls.append(("create_profile", name, display, description))
        return "S-1-15-2-1"

    def delete_profile(self, name):
        if self.delete_error:
            raise self.delete_error
        self.calls.append(("delete_profile", name))

    def current_user_sid(self):
        if self.user_error:
            raise self.user_error
        return "S-1-5-21-1"

    def grant(self, path, sid, *, write):
        self.granted.add(path)
        self.calls.append(("grant", path, write))

    def revoke(self, path, sid):
        if self.revoke_error:
            raise self.revoke_error
        self.granted.discard(path)
        self.calls.append(("revoke", path))

    def label(self, path):
        if self.label_error:
            raise self.label_error
        return self.labels.get(path, "")

    def set_label(self, path, sddl):
        if self.set_label_error:
            raise self.set_label_error
        self.labels[path] = sddl
        self.calls.append(("set_label", path, sddl))

    def process_alive(self, pid):
        return pid in self.alive


def _plan(tmp_path: Path, *, read=(), write=()) -> SandboxPlan:
    run = tmp_path / "igni-ext-acme-run"
    (run / "tmp" / "home").mkdir(parents=True, exist_ok=True)
    plan = SandboxPlan(extension_id="acme.probe", run_dir=run)
    plan.read = list(read)
    plan.write = list(write)
    return plan


def _grants(plan, *, extra=(), home=None, environ=None):
    return AppContainer.grants(plan, extra_read=list(extra), home=home, environ=environ or {})


def _start(tmp_path: Path, calls: Windows, plan: SandboxPlan | None = None, **kw):
    container = AppContainer(calls=calls, ledger_dir=tmp_path / "ledger")
    plan = plan or _plan(tmp_path, write=[tmp_path / "data"])
    (tmp_path / "data").mkdir(exist_ok=True)
    kw.setdefault("cwd", None)
    kw.setdefault("forwards", [])
    kw.setdefault("home", tmp_path / "home")
    return container, plan, container.start(plan, "py", ["x"], **kw)


# ── Names ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("extension", "expected"),
    [
        ("acme.probe", "igni.acme.probe.tok"),
        ("acme probe/é", "igni.acme-probe.tok"),
        ("...", "igni.ext.tok"),
        ("-.-", "igni.ext.tok"),
        ("", "igni.ext.tok"),
        ("日本語", "igni.ext.tok"),
        ("..acme..", "igni.acme.tok"),
    ],
)
def test_a_profile_name_keeps_letters_digits_dots_and_dashes_and_calls_nothing_ext(
    extension, expected
):
    assert AppContainer.profile_name(extension, "tok") == expected


def test_a_cut_extension_name_does_not_end_in_a_separator_and_the_token_is_kept():
    token = "0123456789ab"
    room = MAX_PROFILE_NAME - len("igni.") - len(token) - 1
    # The cut falls just after a dot and a dash: neither may be left dangling.
    for tail in (".", "-", "..", ".-"):
        name = AppContainer.profile_name("a" * (room - len(tail)) + tail + "zzzz", token)
        assert name == f"igni.{'a' * (room - len(tail))}.{token}"
        assert len(name) <= MAX_PROFILE_NAME
    assert AppContainer.profile_name("x" * 500, token).endswith(f".{token}")


def test_a_longer_token_leaves_less_room_for_the_name():
    short = AppContainer.profile_name("x" * 100, "ab")
    long = AppContainer.profile_name("x" * 100, "ab" * 10)
    assert len(short) == len(long) == MAX_PROFILE_NAME


def test_the_runs_bridge_lives_in_its_run_directory(tmp_path):
    assert AppContainer.bridge_dir(_plan(tmp_path)) == tmp_path / "igni-ext-acme-run" / "bridge"


# ── What it is granted ────────────────────────────────────────────────


def test_a_write_path_inside_a_read_grant_still_gets_its_own_entry(tmp_path):
    outer, inner = tmp_path / "ws", tmp_path / "ws" / "out"
    inner.mkdir(parents=True)
    grants = _grants(_plan(tmp_path, read=[outer], write=[inner]))
    assert [(g.path, g.write) for g in grants] == [(outer, False), (inner, True)]


def test_a_read_path_inside_a_write_grant_is_left_out(tmp_path):
    outer, inner = tmp_path / "ws", tmp_path / "ws" / "src"
    inner.mkdir(parents=True)
    grants = _grants(_plan(tmp_path, read=[inner], write=[outer]))
    assert [(g.path, g.write) for g in grants] == [(outer, True)]


def test_a_path_both_read_and_written_is_granted_once_as_write(tmp_path):
    both = tmp_path / "both"
    both.mkdir()
    grants = _grants(_plan(tmp_path, read=[both], write=[both]), extra=[both])
    assert [(g.path, g.write) for g in grants] == [(both, True)]


def test_extra_read_paths_are_granted_like_the_plans(tmp_path):
    extra = tmp_path / "interpreter"
    extra.mkdir()
    grants = _grants(_plan(tmp_path), extra=[extra])
    assert [(g.path, g.write) for g in grants] == [(extra, False)]


def test_a_system_directory_named_empty_or_not_at_all_excludes_nothing(tmp_path):
    inside = tmp_path / "Program Files" / "tool"
    inside.mkdir(parents=True)
    plan = _plan(tmp_path, read=[inside])
    assert _grants(plan, environ={"ProgramFiles": ""}) != []
    assert _grants(plan, environ={}) != []
    # Named, it is already readable to every AppContainer.
    assert _grants(plan, environ={"ProgramFiles": str(tmp_path / "Program Files")}) == []


def test_a_write_path_inside_a_system_directory_is_still_granted(tmp_path):
    inside = tmp_path / "Program Files" / "tool"
    inside.mkdir(parents=True)
    grants = _grants(
        _plan(tmp_path, write=[inside]), environ={"ProgramFiles": str(tmp_path / "Program Files")}
    )
    assert [(g.path, g.write) for g in grants] == [(inside, True)]


def test_without_a_home_nothing_is_too_broad_but_a_drive_root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    plan = _plan(tmp_path, write=[data, Path(Path.cwd().anchor)])
    assert [g.path for g in _grants(plan, home=None)] == [data]


def test_the_path_above_the_home_and_the_home_itself_are_refused_with_a_warning(tmp_path, caplog):
    home = tmp_path / "users" / "me"
    home.mkdir(parents=True)
    below = home / "project"
    below.mkdir()
    plan = _plan(tmp_path, write=[home, home.parent, below])
    with caplog.at_level(logging.WARNING):
        grants = _grants(plan, home=home)
    assert [g.path for g in grants] == [below]
    assert caplog.text.count("is not granted to its sandbox") == 2
    assert "acme.probe" in caplog.text


# ── The environment the process sees ──────────────────────────────────


def test_the_environment_names_app_data_inside_the_runs_home_and_makes_it(tmp_path):
    plan = _plan(tmp_path)
    env = AppContainer.environment(plan)
    home = plan.home_dir
    assert env == {
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
    }
    assert (home / "AppData" / "Roaming").is_dir() and (home / "AppData" / "Local").is_dir()
    assert AppContainer.environment(plan) == env  # again: nothing to complain about


# ── The ledger ────────────────────────────────────────────────────────


def test_a_run_survives_the_ledger_in_full(tmp_path):
    run = ContainerRun(
        profile="igni.a.b",
        sid="S-1",
        owner=7,
        run_dir=str(tmp_path / "igni-ext-a"),
        grants=[
            Grant(path=tmp_path / "w", write=True, label="S:(ML;OICI;NW;;;ME)"),
            Grant(path=tmp_path / "r", write=False),
            Grant(path=tmp_path / "e", write=True, label=""),
        ],
    )
    back = ContainerRun.from_json(run.to_json())
    assert back == run
    assert [g.label for g in back.grants] == ["S:(ML;OICI;NW;;;ME)", None, ""]


def test_a_run_written_with_only_what_it_needs_reads_back_with_the_rest_empty():
    run = ContainerRun.from_json('{"profile": "p", "sid": "S-1", "owner": "12"}')
    assert (run.profile, run.sid, run.owner, run.run_dir, run.grants) == ("p", "S-1", 12, "", [])
    grant = ContainerRun.from_json(
        json.dumps(
            {
                "profile": "p",
                "sid": "S-1",
                "owner": 1,
                "grants": [{"path": "/x", "write": 1}],
            }
        )
    ).grants[0]
    assert grant == Grant(path=Path("/x"), write=True, label=None)


def test_the_ledger_keeps_one_file_per_run_and_replaces_it_whole(tmp_path):
    ledger = GrantLedger(tmp_path / "not" / "yet" / "made")
    assert ledger.entries() == []
    ledger.record(ContainerRun(profile="a", sid="S-1", owner=1))
    ledger.record(ContainerRun(profile="b", sid="S-2", owner=1))
    ledger.record(ContainerRun(profile="a", sid="S-1", owner=2))
    assert sorted(p.name for p in ledger.directory.iterdir()) == ["a.json", "b.json"]
    assert [(e.profile, e.owner) for e in ledger.entries()] == [("a", 2), ("b", 1)]
    ledger.remove("a")
    ledger.remove("a")  # already gone: nothing to say
    assert [e.profile for e in ledger.entries()] == ["b"]


@pytest.mark.parametrize(
    "text",
    ["", "{not json", "[]", "5", '{"profile": "p"}', '{"profile": "p", "sid": "s", "owner": "x"}'],
)
def test_a_damaged_ledger_entry_is_skipped_with_a_warning_and_the_rest_are_read(
    tmp_path, caplog, text
):
    ledger = GrantLedger(tmp_path)
    ledger.record(ContainerRun(profile="good", sid="S-1", owner=1))
    (tmp_path / "damaged.json").write_text(text)
    with caplog.at_level(logging.WARNING):
        entries = ledger.entries()
    assert [e.profile for e in entries] == ["good"]
    assert "unreadable sandbox ledger entry" in caplog.text
    assert "damaged.json" in caplog.text


def test_a_half_written_ledger_file_is_not_an_entry(tmp_path):
    ledger = GrantLedger(tmp_path)
    (tmp_path / "p.json.partial").write_text('{"profile": "p"')
    assert ledger.entries() == []


# ── A start ───────────────────────────────────────────────────────────


def test_the_profile_is_named_for_the_extension_with_a_display_and_a_description(tmp_path):
    calls = Windows()
    _, _, (run, python, argv) = _start(tmp_path, calls)
    [(_, name, display, description)] = [c for c in calls.calls if c[0] == "create_profile"]
    assert name == run.profile and name.startswith("igni.acme.probe.")
    assert (display, description) == ("igni: acme.probe", "igni sandbox for acme.probe")
    assert python == sys.executable


def test_the_runner_starts_in_the_given_directory_or_the_runs_own_temp(tmp_path):
    calls = Windows()
    _, plan, (_, _, argv) = _start(tmp_path, calls)
    assert argv[argv.index("--cwd") + 1] == str(plan.temp_dir)
    _, _, (_, _, argv) = _start(tmp_path / "again", Windows(), cwd=tmp_path / "bundle")
    assert argv[argv.index("--cwd") + 1] == str(tmp_path / "bundle")


def test_the_runner_is_given_the_user_igni_and_the_python_it_runs_under(tmp_path):
    _, _, (_, _, argv) = _start(tmp_path, Windows(), python="C:\\py\\python.exe")
    assert argv[argv.index("--user") + 1] == "S-1-5-21-1"
    assert argv[argv.index("--python") + 1] == "C:\\py\\python.exe"
    assert argv[argv.index("--parent") + 1] == str(os.getpid())
    assert "--forward" not in argv  # none asked


def test_a_write_grant_is_labelled_as_a_directory_or_a_file(tmp_path):
    calls = Windows()
    a_file = tmp_path / "state.db"
    a_file.write_text("x")
    data = tmp_path / "data"
    data.mkdir()
    _start(tmp_path, calls, _plan(tmp_path, write=[data, a_file]))
    assert calls.labels[data] == LOW_LABEL_DIR
    assert calls.labels[a_file] == LOW_LABEL_FILE


def test_an_integrity_label_that_cannot_be_read_is_a_warning_and_not_lowered(tmp_path, caplog):
    calls = Windows()
    calls.label_error = PermissionError(5, "Access is denied")
    with caplog.at_level(logging.WARNING):
        container, _, (run, _, _) = _start(tmp_path, calls)
    assert "could not read the integrity label" in caplog.text
    assert tmp_path / "data" in calls.granted and calls.labels == {}
    assert run.grants and all(g.label is None for g in run.grants)
    container.stop(run)
    assert calls.labels == {}  # nothing of the person's was changed, so nothing is put back


def test_a_start_that_cannot_name_the_user_undoes_everything_and_says_why(tmp_path):
    calls = Windows(user_error=OSError("no token"))
    container = AppContainer(calls=calls, ledger_dir=tmp_path / "ledger")
    (tmp_path / "data").mkdir()
    plan = _plan(tmp_path, write=[tmp_path / "data"])
    with pytest.raises(OSError, match="no token"):
        container.start(plan, "py", [], cwd=None, forwards=[], home=tmp_path / "home")
    assert calls.granted == set()
    assert [c[0] for c in calls.calls if c[0] == "delete_profile"] == ["delete_profile"]
    assert container.ledger.entries() == []


def test_a_cancelled_start_undoes_everything_too(tmp_path):
    calls = Windows(user_error=KeyboardInterrupt())
    container = AppContainer(calls=calls, ledger_dir=tmp_path / "ledger")
    (tmp_path / "data").mkdir()
    plan = _plan(tmp_path, write=[tmp_path / "data"])
    with pytest.raises(KeyboardInterrupt):
        container.start(plan, "py", [], cwd=None, forwards=[], home=tmp_path / "home")
    assert calls.granted == set()
    assert container.ledger.entries() == []
    assert any(c[0] == "delete_profile" for c in calls.calls)


def test_the_sweep_runs_once_per_container_however_many_runs_it_starts(tmp_path):
    calls = Windows()
    ledger = GrantLedger(tmp_path / "ledger")
    container = AppContainer(calls=calls, ledger_dir=ledger.directory)
    plan = _plan(tmp_path, write=[tmp_path / "data"])
    (tmp_path / "data").mkdir()
    ledger.record(ContainerRun(profile="igni.first.dead", sid="S-9", owner=999_999))
    container.start(plan, "py", [], cwd=None, forwards=[], home=tmp_path / "home")
    assert ("delete_profile", "igni.first.dead") in calls.calls
    # A leftover that appears afterwards waits for the next igni.
    ledger.record(ContainerRun(profile="igni.second.dead", sid="S-9", owner=999_999))
    container.start(
        _plan(tmp_path / "again", write=[tmp_path / "data"]),
        "py",
        [],
        cwd=None,
        forwards=[],
        home=tmp_path / "home",
    )
    assert ("delete_profile", "igni.second.dead") not in calls.calls


# ── Stop ──────────────────────────────────────────────────────────────


def test_stop_takes_entries_back_last_first_and_puts_back_only_labels_it_lowered(tmp_path):
    calls = Windows()
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    calls.labels[second] = "S:(ML;OICI;NW;;;ME)"
    container, _, (run, _, _) = _start(tmp_path, calls, _plan(tmp_path, write=[first, second]))
    calls.calls.clear()
    container.stop(run)
    revoked = [c[1] for c in calls.calls if c[0] == "revoke"]
    bridge = AppContainer.bridge_dir(_plan(tmp_path))
    assert revoked.index(second) < revoked.index(first)
    assert bridge in revoked
    assert calls.labels[second] == "S:(ML;OICI;NW;;;ME)"
    assert calls.labels[first] == "S:"
    assert not any(c[0] == "set_label" and c[1] == bridge for c in calls.calls)


def test_a_path_that_is_gone_is_nothing_to_undo_and_the_run_is_forgotten(tmp_path):
    calls = Windows()
    container, _, (run, _, _) = _start(tmp_path, calls)
    calls.revoke_error = FileNotFoundError(2, "gone")
    container.stop(run)
    assert container.ledger.entries() == []
    assert any(c[0] == "delete_profile" for c in calls.calls)


def test_a_profile_that_cannot_be_deleted_keeps_the_run_in_the_ledger(tmp_path, caplog):
    calls = Windows()
    container, _, (run, _, _) = _start(tmp_path, calls)
    calls.delete_error = PermissionError(5, "Access is denied")
    with caplog.at_level(logging.WARNING):
        container.stop(run)
    assert "could not delete its profile" in caplog.text
    assert [e.profile for e in container.ledger.entries()] == [run.profile]


def test_a_label_that_cannot_be_put_back_keeps_the_run_in_the_ledger(tmp_path, caplog):
    calls = Windows()
    container, _, (run, _, _) = _start(tmp_path, calls)
    calls.set_label_error = PermissionError(5, "Access is denied")
    with caplog.at_level(logging.WARNING):
        container.stop(run)
    assert "could not remove its access" in caplog.text
    assert [e.profile for e in container.ledger.entries()] == [run.profile]


# ── The sweep ─────────────────────────────────────────────────────────


def test_the_sweep_leaves_a_run_of_this_igni_or_a_live_one_alone(tmp_path):
    calls = Windows(alive={4242})
    ledger = GrantLedger(tmp_path / "ledger")
    ledger.record(ContainerRun(profile="mine", sid="S-1", owner=os.getpid()))
    ledger.record(ContainerRun(profile="live", sid="S-2", owner=4242))
    assert AppContainer(calls=calls, ledger_dir=ledger.directory).sweep() == 0
    assert calls.calls == []
    assert sorted(e.profile for e in ledger.entries()) == ["live", "mine"]


def test_the_sweep_deletes_a_run_directory_only_if_igni_made_it(tmp_path):
    calls = Windows()
    ledger = GrantLedger(tmp_path / "ledger")
    mine = tmp_path / f"{appcontainer.RUN_DIR_PREFIX}acme-1"
    someone_elses = tmp_path / "Documents"
    for directory in (mine, someone_elses):
        directory.mkdir()
        (directory / "keep.txt").write_text("x")
    ledger.record(ContainerRun(profile="a", sid="S-1", owner=999_999, run_dir=str(mine)))
    ledger.record(ContainerRun(profile="b", sid="S-2", owner=999_999, run_dir=str(someone_elses)))
    ledger.record(ContainerRun(profile="c", sid="S-3", owner=999_999, run_dir=""))
    assert AppContainer(calls=calls, ledger_dir=ledger.directory).sweep() == 3
    assert not mine.exists()
    assert (someone_elses / "keep.txt").read_text() == "x"
    assert ledger.entries() == []


def test_a_run_the_sweep_could_not_undo_is_still_counted_and_still_recorded(tmp_path):
    calls = Windows()
    calls.delete_error = PermissionError(5, "Access is denied")
    ledger = GrantLedger(tmp_path / "ledger")
    ledger.record(ContainerRun(profile="stuck", sid="S-1", owner=999_999))
    assert AppContainer(calls=calls, ledger_dir=ledger.directory).sweep() == 1
    assert [e.profile for e in ledger.entries()] == ["stuck"]


# ── explicit_label ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sddl", "kept"),
    [
        ("S:", "S:"),
        ("S:AI", "S:"),
        ("S:(ML)", "S:(ML)"),  # an entry with no flags field is not inherited
        ("S:(ML;OICIID;NW;;;LW)", "S:"),  # ID after OICI: read in two-character units
        ("S:(ML;CIOI;NW;;;LW)", "S:(ML;CIOI;NW;;;LW)"),  # "OI" + "ID" would not match by substring
        ("S:(ML;ID;NW;;;LW)(ML;OICI;NW;;;HI)", "S:(ML;OICI;NW;;;HI)"),
    ],
)
def test_a_label_keeps_only_its_own_entries(sddl, kept):
    assert explicit_label(sddl) == kept


# ── The probe, as Windows would answer ────────────────────────────────


def _as_windows(monkeypatch, *, build: int, api: types.SimpleNamespace) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        sys, "getwindowsversion", lambda: types.SimpleNamespace(build=build), raising=False
    )
    monkeypatch.setattr(sandbox_package, "winapi", api, raising=False)
    monkeypatch.setitem(sys.modules, "igni_core.extensions.sandbox.winapi", api)


def _api(**over) -> types.SimpleNamespace:
    def present():
        return None

    values = {
        "appcontainer_api_present": present,
        "in_appcontainer": lambda: False,
        "current_user_sid": lambda: "S-1",
    }
    values.update(over)
    return types.SimpleNamespace(**values)


async def test_a_windows_that_can_confine_has_no_reason_against(monkeypatch):
    _as_windows(monkeypatch, build=19045, api=_api())
    assert await AppContainer.probe() is None


async def test_a_windows_older_than_ten_says_its_build(monkeypatch):
    _as_windows(monkeypatch, build=MIN_BUILD_MINUS_ONE, api=_api())
    reason = await AppContainer.probe()
    assert reason == (
        f"Windows build {MIN_BUILD_MINUS_ONE} is older than Windows 10, which the sandbox needs"
    )


async def test_the_first_build_of_windows_ten_is_enough(monkeypatch):
    _as_windows(monkeypatch, build=appcontainer.MIN_BUILD, api=_api())
    assert await AppContainer.probe() is None


async def test_igni_inside_an_appcontainer_cannot_start_another(monkeypatch):
    _as_windows(monkeypatch, build=19045, api=_api(in_appcontainer=lambda: True))
    assert await AppContainer.probe() == (
        "igni itself runs in an AppContainer, which cannot start another"
    )


@pytest.mark.parametrize("failing", ["appcontainer_api_present", "current_user_sid"])
async def test_a_windows_call_that_fails_is_a_reason_not_a_crash(monkeypatch, failing):
    def broken(*_args):
        raise OSError("userenv.dll has no CreateAppContainerProfile")

    _as_windows(monkeypatch, build=19045, api=_api(**{failing: broken}))
    reason = await AppContainer.probe()
    assert reason == (
        "AppContainers are not available here: userenv.dll has no CreateAppContainerProfile"
    )


MIN_BUILD_MINUS_ONE = appcontainer.MIN_BUILD - 1
