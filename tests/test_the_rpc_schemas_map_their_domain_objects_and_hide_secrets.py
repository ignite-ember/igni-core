"""The backend RPC wire models: what each mapping keeps, drops and defaults."""

from __future__ import annotations

import pytest

from igni_core.backend.schemas_rpc import (
    AttachSessionResult,
    CloudPlan,
    DirListResult,
    FileCompletion,
    GroupPolicyPackResult,
    LifecyclePhase,
    LoginResult,
    LoginStarted,
    ModelRegistryEntryView,
    ModelRegistryResult,
    PickDirResult,
    RunShellResult,
    SessionPoolConfig,
    SkillDefinition,
    UpdateAvailable,
)
from igni_core.core.auth.schemas import UserInfo
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.utils.update_checker_schemas import UpdateInfo


def test_an_update_is_always_available_and_names_the_package():
    info = UpdateInfo(
        available=False,
        current_version="1.0.0",
        latest_version="1.1.0",
        download_url="https://example.test/dl",
        release_notes="ignored",
    )
    update = UpdateAvailable.from_update_info(info, "igni-core")
    assert update.model_dump() == {
        "available": True,
        "current_version": "1.0.0",
        "latest_version": "1.1.0",
        "download_url": "https://example.test/dl",
        "pkg_name": "igni-core",
    }


def test_a_login_started_has_no_flow_id_unless_given():
    assert LoginStarted(started=True).flow_id == ""
    assert LoginStarted(started=True, flow_id="f1").flow_id == "f1"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (LoginResult(ok=True, email="a@b.test"), "a@b.test"),
        (LoginResult(ok=True), ""),
        (LoginResult(ok=False, error="timed out"), "timed out"),
        (LoginResult(ok=False), ""),
        (LoginResult(ok=True, email="a@b.test", error="stale"), "a@b.test"),
        (LoginResult(ok=True, error="stale"), ""),
        (LoginResult(ok=False, email="a@b.test", error="bad"), "bad"),
    ],
    ids=[
        "ok",
        "ok-no-email",
        "failed",
        "failed-no-error",
        "ok-ignores-error",
        "ok-no-email-ignores-error",
        "failed-ignores-email",
    ],
)
def test_a_login_result_collapses_to_the_email_or_the_reason(result, expected):
    assert result.wire_result_string() == expected


def test_a_cloud_plan_carries_tier_org_email_and_name():
    plan = CloudPlan.from_user_info(
        UserInfo(email="m@a.test", full_name="Maya", tier="pro", org_display_name="Acme")
    )
    assert plan.model_dump() == {
        "tier": "pro",
        "org_name": "Acme",
        "email": "m@a.test",
        "full_name": "Maya",
    }


def test_a_cloud_plan_reports_an_empty_email_as_null():
    plan = CloudPlan.from_user_info(UserInfo())
    assert plan.model_dump() == {"tier": None, "org_name": None, "email": None, "full_name": None}


def test_a_group_policy_pack_of_nobody_is_empty():
    assert GroupPolicyPackResult().model_dump() == {
        "group_id": None,
        "group_name": None,
        "fetched_at": None,
        "entry_count": 0,
        "default_model": None,
    }


def test_a_shell_result_is_foreground_unless_it_outlived_the_wait():
    done = RunShellResult(output="hi\n", exit_code=0)
    assert (done.pid, done.backgrounded) == (None, False)
    background = RunShellResult(output="", exit_code=0, pid=4242, backgrounded=True)
    assert (background.pid, background.backgrounded) == (4242, True)


def test_the_small_rpc_results_keep_their_wire_keys():
    assert set(
        DirListResult(path="/a", parent="/", dirs=["b"], home="/h", error="").model_dump()
    ) == {"path", "parent", "dirs", "home", "error"}
    assert PickDirResult(path="", cancelled=True, error="").model_dump() == {
        "path": "",
        "cancelled": True,
        "error": "",
    }
    assert FileCompletion(matches=["a.py"], total=1).model_dump() == {
        "matches": ["a.py"],
        "total": 1,
    }
    assert AttachSessionResult(session_id="s", project_dir="/p").model_dump() == {
        "session_id": "s",
        "project_dir": "/p",
    }
    assert SkillDefinition(name="n", description="d", prompt="p").model_dump() == {
        "name": "n",
        "description": "d",
        "prompt": "p",
    }


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (
            "https://user:hunter2@api.example.test:8443/v1?key=SECRET",
            "https://api.example.test:8443/v1",
        ),
        ("http://localhost:11434/v1", "http://localhost:11434/v1"),
        (None, None),
        ("", ""),
    ],
    ids=["credentials", "plain", "null", "empty"],
)
def test_a_model_view_shows_its_url_without_credentials(url, shown):
    view = ModelRegistryEntryView.of(ModelRegistryEntry(model_id="m", url=url))
    assert view.url == shown
    assert "hunter2" not in view.model_dump_json()
    assert "SECRET" not in view.model_dump_json()


@pytest.mark.parametrize(
    ("keys", "has_key"),
    [
        ({}, False),
        ({"api_key": "sk-1"}, True),
        ({"api_key_env": "MY_KEY"}, True),
        ({"api_key_cmd": "pass show k"}, True),
    ],
)
def test_a_model_view_says_whether_a_key_exists_and_never_shows_it(keys, has_key):
    view = ModelRegistryEntry(model_id="m", **keys)
    shown = ModelRegistryEntryView.of(view)
    assert shown.has_api_key is has_key
    dumped = shown.model_dump_json()
    for secret in keys.values():
        assert secret not in dumped
    assert not {"api_key", "api_key_env", "api_key_cmd"} & set(shown.model_dump())


@pytest.mark.parametrize(
    ("extra", "source"),
    [({}, ""), ({"source": "cloud"}, "cloud"), ({"source": None}, ""), ({"other": "x"}, "")],
)
def test_a_model_view_names_the_source_from_the_extras_and_nothing_else(extra, source):
    shown = ModelRegistryEntryView.of(ModelRegistryEntry(model_id="m", **extra))
    assert shown.source == source
    assert "other" not in shown.model_dump()


def test_a_model_view_passes_the_thinking_dict_unchanged():
    entry = ModelRegistryEntry(
        model_id="m", provider="anthropic", thinking={"type": "adaptive"}, effort="high"
    )
    shown = ModelRegistryEntryView.of(entry)
    assert shown.thinking == {"type": "adaptive"}
    assert shown.effort == "high"


def test_the_registry_result_views_every_row_and_the_default():
    models = ModelsConfig(
        default="fast",
        registry={
            "fast": ModelRegistryEntry(model_id="f", api_key="sk-secret"),
            "slow": ModelRegistryEntry(model_id="s", url="https://u:p@h.test/v1"),
        },
    )
    result = ModelRegistryResult.from_settings(models)
    assert result.default == "fast"
    assert result.max_context_window == models.max_context_window
    assert set(result.registry) == {"fast", "slow"}
    assert result.registry["fast"].has_api_key is True
    assert result.registry["slow"].url == "https://h.test/v1"
    assert "sk-secret" not in result.model_dump_json()


def test_an_empty_registry_is_an_empty_result():
    result = ModelRegistryResult.from_settings(ModelsConfig(default="", registry={}))
    assert result.registry == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("soon", None),
        ("0", None),
        ("-5", None),
        ("nan", None),
        ("90", 90.0),
        ("0.5", 0.5),
        ("inf", float("inf")),
        ("-inf", None),
    ],
)
def test_the_idle_timeout_is_a_positive_number_of_seconds_or_the_pool_default(
    raw, expected, monkeypatch
):
    if raw is None:
        monkeypatch.delenv("IGNI_SESSION_IDLE_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("IGNI_SESSION_IDLE_TIMEOUT", raw)
    assert SessionPoolConfig.from_env().idle_timeout_seconds == expected


def test_the_ready_line_keeps_only_what_was_set(capsys):
    from igni_core.backend.schemas_rpc import BackendReadyLine

    BackendReadyLine().emit()
    assert capsys.readouterr().out == '{"status": "ready"}\n'
    BackendReadyLine(ws_url="ws://h:1").emit()
    assert capsys.readouterr().out == '{"status": "ready", "ws_url": "ws://h:1"}\n'


def test_the_lifecycle_has_four_phases_by_word():
    assert [p.value for p in LifecyclePhase] == ["booting", "running", "shutting_down", "stopped"]
