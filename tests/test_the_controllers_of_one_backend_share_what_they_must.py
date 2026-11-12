"""One backend's controllers are built together and share the few things they must.

:meth:`ControllerRegistry.build` is the only place the controllers meet
their collaborators, and no controller points back at the backend. What
has to be one object across them: the supervisor and the run gate the
backend process owns, and the portal the person signs in through and the
group's policy is fetched from.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from igni_core.backend.controller_registry import ControllerRegistry, Controllers
from igni_core.backend.group_policy_sync import GroupPolicySync
from igni_core.backend.hitl_tracer import HITLTracer
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.settings import Settings
from igni_core.core.session.pending_messages import PendingMessageStore
from igni_core.core.tools.process_supervisor import ProcessSupervisor

SLOTS = [
    "mcp",
    "hitl",
    "context",
    "auth",
    "group_policy",
    "files",
    "search",
    "panels",
    "loop",
    "processes",
    "sessions",
    "rehydrate",
    "lifecycle",
    "plan_snapshots",
    "viz_actions",
    "team_wiring",
    "model_switcher",
    "plugins",
    "marketplaces",
    "runs",
    "chat_history",
]


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings.model_validate(
        {
            "api_url": "https://api.igni.example.invalid",
            "storage": {"data_dir": str(tmp_path / "data")},
            "auth": {"credentials_file": str(tmp_path / "credentials.json")},
            **overrides,
        }
    )


def _session(token: str | None = None) -> MagicMock:
    session = MagicMock()
    session.cloud_auth.access_token = token
    return session


def _build(
    tmp_path: Path,
    *,
    session: MagicMock | None = None,
    settings: Settings | None = None,
    supervisor: ProcessSupervisor | None = None,
    gate: RunGate | None = None,
) -> Controllers:
    return ControllerRegistry.build(
        session=session or _session(),
        settings=settings or _settings(tmp_path),
        hitl_store=PendingRequirementsStore(),
        pending_store=PendingMessageStore(tmp_path / "pending.db"),
        session_prefs=MagicMock(),
        user_config_store=MagicMock(),
        hitl_tracer=HITLTracer(enabled=False),
        message_queue=None,
        supervisor=supervisor or ProcessSupervisor(),
        gate=gate or RunGate(),
    )


def test_every_slot_holds_a_controller(tmp_path: Path):
    controllers = _build(tmp_path)

    for slot in SLOTS:
        assert getattr(controllers, slot) is not None, slot
    assert sorted(vars(controllers)) == sorted(SLOTS)


def test_the_supervisor_and_gate_given_are_the_ones_every_controller_uses(tmp_path: Path):
    supervisor, gate = ProcessSupervisor(), RunGate()

    controllers = _build(tmp_path, supervisor=supervisor, gate=gate)

    assert controllers.processes._supervisor is supervisor
    assert controllers.rehydrate._supervisor is supervisor
    assert controllers.runs._supervisor is supervisor
    assert controllers.runs._gate is gate
    assert controllers.loop.scheduler._gate is gate


def test_the_loop_runs_its_work_through_the_run_pipeline_and_lifecycle_stops_its_scheduler(
    tmp_path: Path,
):
    controllers = _build(tmp_path)

    assert controllers.loop.scheduler._as_run == controllers.runs.run_unstreamed
    assert controllers.lifecycle._shutdown_pipeline is not None
    assert controllers.lifecycle._runs is controllers.runs
    assert controllers.lifecycle._rehydrate is controllers.rehydrate


def test_sign_in_and_the_group_policy_fetch_talk_to_the_same_portal(tmp_path: Path):
    controllers = _build(tmp_path)

    portal = controllers.auth._portal
    assert isinstance(portal, PortalClient)
    assert controllers.group_policy._hydrator._portal is portal
    assert portal.api_url == "https://api.igni.example.invalid"


def test_a_portal_url_that_is_set_is_used_as_it_is(tmp_path: Path):
    """The derivation only rewrites host labels, so it cannot say a portal
    that differs from the API by a port."""
    settings = _settings(tmp_path, portal_url="https://portal.igni.example.invalid:8443/")

    controllers = _build(tmp_path, settings=settings)

    assert controllers.auth._portal.portal_url == "https://portal.igni.example.invalid:8443"


def test_an_empty_portal_url_is_derived_from_the_api(tmp_path: Path):
    settings = _settings(tmp_path, portal_url="")

    controllers = _build(tmp_path, settings=settings)

    derived = PortalClient(api_url=settings.api_url).portal_url
    assert controllers.auth._portal.portal_url == derived


@pytest.mark.asyncio
async def test_a_stored_token_has_the_group_refreshed_and_polled_at_build(
    tmp_path: Path, monkeypatch
):
    asked: list[tuple[str, bool]] = []

    async def record(self, token: str, *, force: bool = False) -> bool:
        asked.append((token, force))
        return False

    monkeypatch.setattr(GroupPolicySync, "hydrate", record)

    controllers = _build(tmp_path, session=_session("stored-token"))
    try:
        for _ in range(200):
            if asked:
                break
            await asyncio.sleep(0.01)
        assert asked == [("stored-token", True)]
        assert controllers.group_policy.polling
    finally:
        controllers.group_policy.stop_polling()


@pytest.mark.asyncio
async def test_with_no_stored_token_nothing_is_fetched_or_polled_at_build(
    tmp_path: Path, monkeypatch
):
    asked: list[str] = []

    async def record(self, token: str, *, force: bool = False) -> bool:
        asked.append(token)
        return False

    monkeypatch.setattr(GroupPolicySync, "hydrate", record)

    controllers = _build(tmp_path, session=_session(None))
    await asyncio.sleep(0.05)

    assert asked == []
    assert not controllers.group_policy.polling


def test_with_no_running_loop_a_stored_token_schedules_nothing(tmp_path: Path):
    """A bare CLI utility builds the controllers with no loop to schedule on."""
    controllers = _build(tmp_path, session=_session("stored-token"))

    assert not controllers.group_policy.polling


def test_two_builds_share_only_what_they_were_given(tmp_path: Path):
    supervisor, gate = ProcessSupervisor(), RunGate()

    first = _build(tmp_path / "a", supervisor=supervisor, gate=gate)
    second = _build(tmp_path / "b", supervisor=supervisor, gate=gate)

    assert first.runs._supervisor is second.runs._supervisor
    assert first.auth is not second.auth
    assert first.group_policy is not second.group_policy
    assert first.auth._portal is not second.auth._portal
    assert first.hitl is not second.hitl
