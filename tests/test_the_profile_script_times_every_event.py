"""``scripts/profile_run.py`` times the backend's events and summarises the runs.

It needs real cloud credentials and a model, so nothing ran it: how the
first event, the last content-bearing event and the "tail" after it are
computed, what the summary says about an empty or partial series, what
file ``--output`` writes and which errors stop the run were all unpinned.

The backend, the Agno team and the cloud catalogue are replaced by small
fakes that yield named events; the script's own functions run unmodified.
Time is a scripted clock so the offsets are exact.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types

import pytest

_SOURCE = pathlib.Path(__file__).resolve().parent.parent / "scripts/profile_run.py"


@pytest.fixture
def prof(monkeypatch):
    spec = importlib.util.spec_from_file_location("profile_run_under_test", _SOURCE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _clock(*values):
    """A ``time`` stand-in whose ``monotonic`` replays ``values`` then holds the last."""
    ticks = list(values)

    def monotonic():
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    return types.SimpleNamespace(monotonic=monotonic)


def _event(name):
    return type(name, (), {})()


class _Backend:
    def __init__(self, names, fail=False):
        self._names = names
        self._fail = fail

    async def run_message(self, prompt):
        for name in self._names:
            yield _event(name)
        if self._fail:
            raise RuntimeError("stream broke")


def _run(coro):
    return asyncio.run(coro)


# -- _profile_one_run --------------------------------------------------------


def test_a_run_reports_first_event_last_content_and_the_tail_after_it(prof, monkeypatch):
    # send, then four events, then the stream closes
    monkeypatch.setattr(prof, "time", _clock(100.0, 100.1, 100.2, 100.5, 100.7, 101.0))
    backend = _Backend(["RunStarted", "ContentDelta", "ToolCompleted", "RunCompleted"])

    record = _run(prof._profile_one_run(backend, "hi", 4))

    assert record["run_index"] == 4
    assert record["prompt"] == "hi"
    assert record["total_s"] == 1.0
    assert record["time_to_first_event_s"] == 0.1
    assert record["time_to_last_content_s"] == 0.5
    assert record["tail_s"] == 0.5
    assert record["events_total"] == 4
    assert record["histogram"] == {
        "RunStarted": 1,
        "ContentDelta": 1,
        "ToolCompleted": 1,
        "RunCompleted": 1,
    }
    assert record["tail_events"] == [{"i": 3, "t": 0.7, "type": "RunCompleted"}]


def test_a_run_whose_last_event_is_content_has_an_empty_tail_of_zero_events(prof, monkeypatch):
    monkeypatch.setattr(prof, "time", _clock(0.0, 0.5, 0.9))

    record = _run(prof._profile_one_run(_Backend(["ContentDelta"]), "p", 0))

    assert record["tail_events"] == []
    assert record["tail_s"] == 0.4


def test_a_run_without_content_events_has_no_last_content_tail_or_tail_events(prof, monkeypatch):
    monkeypatch.setattr(prof, "time", _clock(0.0, 0.2, 0.4, 1.0))

    record = _run(prof._profile_one_run(_Backend(["RunStarted", "RunCompleted"]), "p", 0))

    assert record["time_to_first_event_s"] == 0.2
    assert record["time_to_last_content_s"] is None
    assert record["tail_s"] is None
    assert record["tail_events"] == []
    assert record["events_total"] == 2


def test_a_run_with_no_events_has_no_first_event_either(prof, monkeypatch):
    monkeypatch.setattr(prof, "time", _clock(0.0, 0.3))

    record = _run(prof._profile_one_run(_Backend([]), "p", 0))

    assert record["time_to_first_event_s"] is None
    assert record["time_to_last_content_s"] is None
    assert record["tail_s"] is None
    assert record["events_total"] == 0
    assert record["histogram"] == {}
    assert record["total_s"] == 0.3


def test_content_events_are_matched_by_class_name_so_other_names_are_not_content(prof, monkeypatch):
    monkeypatch.setattr(prof, "time", _clock(0.0, 0.1, 0.2))

    record = _run(prof._profile_one_run(_Backend(["ContentDeltaV2"]), "p", 0))

    assert record["time_to_last_content_s"] is None


def test_a_stream_that_raises_mid_run_propagates(prof):
    with pytest.raises(RuntimeError, match="stream broke"):
        _run(prof._profile_one_run(_Backend(["RunStarted"], fail=True), "p", 0))


def test_verbose_prints_every_event_with_its_offset_and_gap(prof, monkeypatch, capsys):
    monkeypatch.setattr(prof, "time", _clock(0.0, 0.25, 0.75, 1.0))

    _run(prof._profile_one_run(_Backend(["A", "B"]), "p", 0, verbose=True))

    out = capsys.readouterr().out
    assert "[  0.250s  +0.250] A" in out
    assert "[  0.750s  +0.500] B" in out


# -- _profile_raw_agno -------------------------------------------------------


def _raw_session(monkeypatch, names):
    class Team:
        async def arun(self, prompt, stream):
            assert stream is True
            for name in names:
                yield _event(name)

    class Session:
        def __init__(self, settings, project_dir):
            self.main_team = Team()

    import igni_core.core.session as session_pkg

    monkeypatch.setattr(session_pkg, "Session", Session)


def test_raw_mode_takes_the_last_run_content_or_tool_call_completed_as_visible_end(
    prof, monkeypatch, tmp_path
):
    _raw_session(monkeypatch, ["RunStarted", "RunContent", "ToolCallCompletedEvent", "RunCompleted"])
    monkeypatch.setattr(prof, "time", _clock(10.0, 10.1, 10.2, 10.6, 10.8, 11.0))

    record = _run(prof._profile_raw_agno(object(), tmp_path, "p"))

    assert record["mode"] == "raw_agno"
    assert record["total_s"] == 1.0
    assert record["time_to_last_content_s"] == 0.6
    assert record["tail_s"] == 0.4
    assert record["events_total"] == 4
    assert record["tail_events"] == [{"i": 3, "t": 0.8, "type": "RunCompleted", "gap": 0.2}]


def test_raw_mode_without_content_has_no_tail(prof, monkeypatch, tmp_path):
    _raw_session(monkeypatch, ["RunStarted"])
    monkeypatch.setattr(prof, "time", _clock(0.0, 0.1, 0.2))

    record = _run(prof._profile_raw_agno(object(), tmp_path, "p"))

    assert record["time_to_last_content_s"] is None
    assert record["tail_s"] is None
    assert record["tail_events"] == []


# -- _summarize --------------------------------------------------------------


def test_the_summary_gives_n_min_max_mean_and_median_to_three_decimals(prof):
    records = [
        {"total_s": 1.0, "time_to_first_event_s": 0.1, "time_to_last_content_s": 0.5, "tail_s": 0.5},
        {"total_s": 2.0, "time_to_first_event_s": 0.2, "time_to_last_content_s": 1.5, "tail_s": 0.5},
        {"total_s": 4.5, "time_to_first_event_s": 0.4, "time_to_last_content_s": 4.0, "tail_s": 0.5},
    ]

    summary = prof._summarize(records)

    assert summary["runs"] == 3
    assert summary["total_s"] == {"n": 3, "min": 1.0, "max": 4.5, "mean": 2.5, "median": 2.0}
    assert summary["time_to_first_event_s"]["mean"] == 0.233
    assert summary["time_to_last_content_s"] == {
        "n": 3,
        "min": 0.5,
        "max": 4.0,
        "mean": 2.0,
        "median": 1.5,
    }


def test_the_summary_drops_none_values_from_each_series_and_counts_tail_event_types(prof):
    records = [
        {
            "total_s": 1.0,
            "time_to_first_event_s": None,
            "time_to_last_content_s": None,
            "tail_s": None,
            "tail_events": [],
        },
        {
            "total_s": 3.0,
            "time_to_first_event_s": 0.2,
            "time_to_last_content_s": 2.0,
            "tail_s": 1.0,
            "tail_events": [{"type": "RunCompleted"}, {"type": "Metrics"}],
        },
        {"total_s": 2.0, "tail_events": [{"type": "RunCompleted"}]},
    ]

    summary = prof._summarize(records)

    assert summary["runs"] == 3
    assert summary["total_s"]["n"] == 3
    assert summary["tail_s"] == {"n": 1, "min": 1.0, "max": 1.0, "mean": 1.0, "median": 1.0}
    assert summary["time_to_first_event_s"]["n"] == 1
    assert summary["tail_event_types"] == {"RunCompleted": 2, "Metrics": 1}


def test_the_summary_of_no_runs_has_n_zero_everywhere(prof):
    summary = prof._summarize([])

    assert summary == {
        "runs": 0,
        "total_s": {"n": 0},
        "time_to_first_event_s": {"n": 0},
        "time_to_last_content_s": {"n": 0},
        "tail_s": {"n": 0},
        "tail_event_types": {},
    }


# -- _ensure_cloud_models ----------------------------------------------------


class _Reason:
    value = "unreachable"


def _cloud(monkeypatch, *, token, ok=True, entries=("m1", "m2")):
    seen = {}

    class Credentials:
        def __init__(self, path):
            seen["credentials_file"] = path
            self.access_token = token

    class Catalog:
        def __init__(self, api_url, tok):
            seen["api_url"] = api_url
            seen["token"] = tok

        def fetch(self):
            return types.SimpleNamespace(ok=ok, entries=list(entries), reason=_Reason())

        def merge_into(self, registry, entries):
            for entry in entries:
                registry[entry] = {"id": entry}

    import igni_core.core.auth.credentials as credentials
    import igni_core.core.config.cloud_models as cloud_models

    monkeypatch.setattr(credentials, "CloudCredentials", Credentials)
    monkeypatch.setattr(cloud_models, "CloudModelCatalogClient", Catalog)
    return seen


def _settings():
    return types.SimpleNamespace(
        api_url="https://api.example",
        auth=types.SimpleNamespace(credentials_file="/creds.json"),
        models=types.SimpleNamespace(default="d", registry={}),
    )


def test_missing_credentials_tell_the_user_to_log_in(prof, monkeypatch):
    _cloud(monkeypatch, token=None)

    with pytest.raises(RuntimeError, match="/login"):
        _run(prof._ensure_cloud_models(_settings()))


def test_failed_discovery_names_the_api_url_and_the_reason(prof, monkeypatch):
    _cloud(monkeypatch, token="tok", ok=False, entries=())

    with pytest.raises(RuntimeError) as err:
        _run(prof._ensure_cloud_models(_settings()))

    assert "https://api.example" in str(err.value)
    assert "unreachable" in str(err.value)


def test_a_successful_discovery_that_found_nothing_is_also_an_error(prof, monkeypatch):
    _cloud(monkeypatch, token="tok", ok=True, entries=())

    with pytest.raises(RuntimeError, match="returned no entries"):
        _run(prof._ensure_cloud_models(_settings()))


def test_discovered_models_are_merged_into_the_registry(prof, monkeypatch):
    seen = _cloud(monkeypatch, token="tok")
    settings = _settings()

    _run(prof._ensure_cloud_models(settings))

    assert seen == {
        "credentials_file": "/creds.json",
        "api_url": "https://api.example",
        "token": "tok",
    }
    assert set(settings.models.registry) == {"m1", "m2"}


# -- main --------------------------------------------------------------------


def _wire_main(prof, monkeypatch, names, *, token="tok"):
    settings = _settings()
    loaded = {}

    class Loader:
        @staticmethod
        def load(project_dir):
            loaded["project_dir"] = project_dir
            return settings

    class Backend:
        def __init__(self, s, project_dir):
            assert s is settings

        async def startup(self):
            pass

        async def run_message(self, prompt):
            for name in names:
                yield _event(name)

    monkeypatch.setattr(prof, "SettingsLoader", Loader)
    monkeypatch.setattr(prof, "BackendServer", Backend)
    _cloud(monkeypatch, token=token)
    return settings, loaded


def _args(prof, *argv):
    return prof._argparser().parse_args(list(argv))


def test_a_project_that_is_not_a_directory_exits_2_on_stderr(prof, tmp_path, capsys):
    code = _run(prof.main(_args(prof, "--project", str(tmp_path / "nope"))))

    assert code == 2
    assert "project dir not found" in capsys.readouterr().err


def test_main_applies_the_model_override_and_prints_before_and_after_discovery(
    prof, monkeypatch, tmp_path, capsys
):
    settings, loaded = _wire_main(prof, monkeypatch, ["ContentDelta"])

    code = _run(prof.main(_args(prof, "--project", str(tmp_path), "--runs", "1", "--model", "m2")))

    out = capsys.readouterr().out
    assert code == 0
    assert settings.models.default == "m2"
    assert loaded["project_dir"] == tmp_path.resolve()
    assert f"project_dir={tmp_path.resolve()}" in out
    assert "api_url=https://api.example" in out
    assert "default model (before discovery): m2" in out
    assert "registry size (after discovery):  2" in out
    assert "default model (after discovery):  m2" in out


def test_a_project_path_with_a_tilde_is_expanded(prof, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _, loaded = _wire_main(prof, monkeypatch, [])

    _run(prof.main(_args(prof, "--project", "~/proj", "--runs", "0")))

    assert loaded["project_dir"] == (home / "proj").resolve()


def test_output_writes_one_json_line_per_run_then_the_summary_creating_parents(
    prof, monkeypatch, tmp_path
):
    _wire_main(prof, monkeypatch, ["ContentDelta", "RunCompleted"])
    out = tmp_path / "deep" / "er" / "profile.jsonl"

    code = _run(
        prof.main(
            _args(prof, "--project", str(tmp_path), "--runs", "2", "--prompt", "héllo", "--output", str(out))
        )
    )

    lines = out.read_text().splitlines()
    assert code == 0
    assert len(lines) == 3
    first, second, last = (json.loads(line) for line in lines)
    assert [first["run_index"], second["run_index"]] == [0, 1]
    assert first["prompt"] == "héllo"
    assert "h\\u00e9llo" in lines[0]
    assert list(last) == ["_summary"]
    assert last["_summary"]["runs"] == 2
    assert last["_summary"]["tail_event_types"] == {"RunCompleted": 2}


def test_zero_runs_write_only_an_empty_summary(prof, monkeypatch, tmp_path):
    _wire_main(prof, monkeypatch, ["ContentDelta"])
    out = tmp_path / "p.jsonl"

    _run(prof.main(_args(prof, "--project", str(tmp_path), "--runs", "0", "--output", str(out))))

    (line,) = out.read_text().splitlines()
    summary = json.loads(line)["_summary"]
    assert summary["runs"] == 0
    assert summary["total_s"] == {"n": 0}


def test_without_output_nothing_is_written(prof, monkeypatch, tmp_path):
    _wire_main(prof, monkeypatch, ["ContentDelta"])
    project = tmp_path / "proj"
    project.mkdir()

    _run(prof.main(_args(prof, "--project", str(project), "--runs", "1")))

    assert list(tmp_path.iterdir()) == [project]


def test_raw_mode_returns_zero_prints_no_summary_and_writes_no_file(
    prof, monkeypatch, tmp_path, capsys
):
    _wire_main(prof, monkeypatch, [])
    _raw_session(monkeypatch, ["RunContent", "RunCompleted"])
    out = tmp_path / "raw.jsonl"

    code = _run(
        prof.main(_args(prof, "--project", str(tmp_path), "--runs", "1", "--raw", "--output", str(out)))
    )

    text = capsys.readouterr().out
    assert code == 0
    assert "raw Agno profile" in text
    assert "tail events: ['RunCompleted']" in text
    assert "=== summary ===" not in text
    assert not out.exists()


def test_missing_credentials_stop_main_before_any_run(prof, monkeypatch, tmp_path):
    _wire_main(prof, monkeypatch, ["ContentDelta"], token=None)

    with pytest.raises(RuntimeError, match="/login"):
        _run(prof.main(_args(prof, "--project", str(tmp_path))))


def test_the_script_loads_the_real_settings_for_the_project(prof, monkeypatch, tmp_path, capsys):
    """The loader the script imports exists: it ran into an ImportError once."""
    monkeypatch.setenv("HOME", str(tmp_path))

    async def no_discovery(settings):
        return None

    class Backend:
        def __init__(self, settings, project_dir):
            pass

        async def startup(self):
            pass

    monkeypatch.setattr(prof, "_ensure_cloud_models", no_discovery)
    monkeypatch.setattr(prof, "BackendServer", Backend)
    project = tmp_path / "proj"
    project.mkdir()

    code = _run(prof.main(_args(prof, "--project", str(project), "--runs", "0")))

    out = capsys.readouterr().out
    assert code == 0
    assert f"project_dir={project.resolve()}" in out
    assert "api_url=" in out
    assert "default model (before discovery): " in out
