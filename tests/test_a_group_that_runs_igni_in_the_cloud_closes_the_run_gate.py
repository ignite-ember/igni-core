"""A group whose igni runs in the cloud closes the run gate on the person's computer.

``docs/design/local-cloud-switch.md`` §6. igni server carries the group's
``mode`` — ``local``, ``cloud`` or ``local_and_cloud`` — as a top-level
field of the signed pack. A pack without it (an older server) means
``local_and_cloud``, and so, with a warning, does a value this igni does
not know: what an igni that ignored the field would do.

In ``cloud`` mode the organisation runs igni for its people in the cloud.
The desktop's backend still boots — signing in and the status still work —
but its run gate is closed with a person-facing reason, and a hand-started
``igni -m`` is refused with the same words. The igni in the cloud reads the
same pack and keeps running: it is where the runs go.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import click
import pytest

from igni_core.backend.run_controller import RunController, RunPhase
from igni_core.backend.settings_loader import BackendSettingsLoader
from igni_core.cli.options import CliOptions
from igni_core.cli.run_settings import CliSettingsLoader
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.environment import GroupPolicyEnvironment
from igni_core.core.config.group_policy.gate import GatePhase, RunGate
from igni_core.core.config.group_policy.pack import GroupMode, GroupPolicyPack, SignedPolicy
from igni_core.core.config.group_policy.policy_settings import PolicySettingsLoader, mode_of
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.group_policy.transport import LiveFetchResult, SignedPackTransport
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.layer import Layer, layers
from igni_core.protocol import messages as msg
from tests.group_policy_signing import ISSUER, KEY, sign_in, store

REASON = "Your organisation runs igni for you in the cloud."
PACK_LOGGER = "igni_core.core.config.group_policy.pack"


@pytest.fixture(autouse=True)
def _no_organisation_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GroupPolicyEnvironment.TRUSTED_KEYS_VAR, raising=False)
    monkeypatch.delenv(GroupPolicyEnvironment.REQUIRED_VAR, raising=False)
    monkeypatch.setattr(ManagedPolicySource, "platform_path", staticmethod(lambda: None))
    # A CLI run fetches the pack first when signed in; the server is offline here.
    monkeypatch.setattr(
        SignedPackTransport,
        "fetch_into",
        lambda self, cache: LiveFetchResult(stored=False, reason="offline in this test"),
    )


class _TheCloud(Layer):
    """A layer that is the cloud a ``cloud`` group sends its people to."""

    def runs_on_the_persons_computer(self) -> bool:
        return False


def _home() -> Path:
    return Path.home() / ".igni"


def _signed_in_with(mode: GroupMode | None, *, learning: bool = False) -> None:
    """This install, signed in, with a verified pack in ``mode`` cached."""
    _home().mkdir(parents=True, exist_ok=True)
    (_home() / "config.yaml").write_text(f"api_url: {ISSUER}\n")
    sign_in(_home() / "credentials.json")
    if mode is None:
        return
    pack = GroupPolicyPack(
        group_id="g1",
        group_name="Field",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy",
                content=json.dumps({"learning": {"enabled": learning}}),
                content_type="json",
            )
        ],
        mode=mode,
    )
    store(_home() / "group-policy", pack)


# ── The pack's mode ────────────────────────────────────────────────


def _signed(**policy: object) -> SignedPolicy:
    return SignedPolicy.model_validate({"group_id": "g1", "group_name": "Field", **policy})


def test_a_pack_without_a_mode_is_local_and_cloud():
    assert _signed().mode is GroupMode.local_and_cloud
    assert _signed(mode=None).mode is GroupMode.local_and_cloud
    assert _signed(mode="").mode is GroupMode.local_and_cloud


@pytest.mark.parametrize("mode", ["local", "cloud", "local_and_cloud"])
def test_each_mode_igni_server_signs_is_read_as_itself(mode: str):
    assert _signed(mode=mode).mode == GroupMode(mode)
    assert GroupPolicyPack.from_signed(_signed(mode=mode)).mode == GroupMode(mode)


def test_a_mode_this_igni_does_not_know_is_local_and_cloud_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=PACK_LOGGER):
        assert _signed(mode="orbit").mode is GroupMode.local_and_cloud
    assert [r.getMessage() for r in caplog.records if r.name == PACK_LOGGER] == [
        "group policy: unknown mode 'orbit'; read as local_and_cloud"
    ]


def test_a_known_or_absent_mode_says_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger=PACK_LOGGER):
        _signed()
        _signed(mode="cloud")
    assert [r for r in caplog.records if r.name == PACK_LOGGER] == []


def test_the_pack_itself_reads_its_mode_the_same_way(caplog):
    base = {"group_id": "g", "group_name": "G", "fetched_at": datetime.now(timezone.utc)}
    assert GroupPolicyPack(**base).mode is GroupMode.local_and_cloud
    assert GroupPolicyPack(**base, mode="cloud").mode is GroupMode.cloud
    with caplog.at_level(logging.WARNING, logger=PACK_LOGGER):
        assert GroupPolicyPack(**base, mode="orbit").mode is GroupMode.local_and_cloud
    assert len([r for r in caplog.records if r.name == PACK_LOGGER]) == 1


def test_the_mode_is_signed_with_the_rest_of_the_pack():
    """The signed form carries it, so a pack built from what was signed has it."""
    pack = GroupPolicyPack(
        group_id="g", group_name="G", fetched_at=datetime.now(timezone.utc), mode=GroupMode.cloud
    )
    compact = KEY.sign(pack)
    segment = compact.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    assert claims["policy"]["mode"] == "cloud"
    assert GroupPolicyPack.from_signed(SignedPolicy.model_validate(claims["policy"])).mode is (
        GroupMode.cloud
    )


# ── The gate ───────────────────────────────────────────────────────


def test_cloud_mode_closes_the_gate_on_the_person_s_computer_with_the_person_s_words():
    gate = RunGate()

    assert gate.close_for_mode(GroupMode.cloud) is True

    assert gate.phase is GatePhase.closed
    assert gate.reason == REASON == RunGate.IN_THE_CLOUD


@pytest.mark.parametrize("mode", [GroupMode.local, GroupMode.local_and_cloud])
def test_the_other_modes_leave_the_gate_open(mode: GroupMode):
    gate = RunGate()

    assert gate.close_for_mode(mode) is False

    assert gate.phase is GatePhase.open and gate.reason is None


def test_the_igni_in_the_cloud_keeps_running_in_cloud_mode(monkeypatch):
    monkeypatch.setattr(layers, "current", lambda: _TheCloud())
    gate = RunGate()

    assert gate.close_for_mode(GroupMode.cloud) is False

    assert gate.phase is GatePhase.open


def test_the_desktop_runs_on_the_person_s_computer():
    assert Layer().runs_on_the_persons_computer() is True


# ── Where the mode is read from ───────────────────────────────────


def test_no_fetcher_no_pack_or_a_policy_that_cannot_be_had_is_local_and_cloud():
    assert mode_of(None) is GroupMode.local_and_cloud

    no_pack = MagicMock(return_value=None)
    assert mode_of(no_pack) is GroupMode.local_and_cloud

    unavailable = MagicMock(side_effect=GroupPolicyUnavailable("sign in"))
    assert mode_of(unavailable) is GroupMode.local_and_cloud


def test_the_policy_settings_loader_knows_no_mode_before_it_loads():
    assert PolicySettingsLoader().mode() is GroupMode.local_and_cloud


def test_the_policy_settings_loader_reads_the_mode_of_the_pack_it_applied(tmp_path: Path):
    _signed_in_with(GroupMode.cloud)
    loader = PolicySettingsLoader(tmp_path)

    settings = loader.load()

    assert settings.learning.enabled is False, "the pack applied"
    assert loader.mode() is GroupMode.cloud


# ── The backend ────────────────────────────────────────────────────


def test_the_backend_in_cloud_mode_boots_with_the_pack_but_closes_the_gate(tmp_path, caplog):
    _signed_in_with(GroupMode.cloud)
    gate = RunGate()

    with caplog.at_level(logging.INFO, logger="igni_core.backend.settings_loader"):
        settings = BackendSettingsLoader(tmp_path, gate).load()

    assert settings.learning.enabled is False, "the rest of the pack still applies"
    assert gate.reason == REASON
    assert any("runs igni in the cloud" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("mode", [GroupMode.local, GroupMode.local_and_cloud, None])
def test_the_backend_in_any_other_mode_or_without_a_pack_runs(tmp_path, mode):
    _signed_in_with(mode)
    gate = RunGate()

    BackendSettingsLoader(tmp_path, gate).load()

    assert gate.phase is GatePhase.open


def test_the_igni_in_the_cloud_runs_whatever_its_pack_says(tmp_path, monkeypatch):
    _signed_in_with(GroupMode.cloud)
    monkeypatch.setattr(layers, "current", lambda: _TheCloud())
    gate = RunGate()

    BackendSettingsLoader(tmp_path, gate).load()

    assert gate.phase is GatePhase.open


def test_a_required_policy_that_cannot_be_had_keeps_its_own_reason(tmp_path, monkeypatch):
    managed = tmp_path / "managed-settings.yaml"
    managed.write_text("group_policy:\n  required: true\n")
    monkeypatch.setattr(ManagedPolicySource, "platform_path", staticmethod(lambda: managed))
    gate = RunGate()

    BackendSettingsLoader(tmp_path, gate).load()

    assert gate.reason and gate.reason != REASON


def test_a_run_in_cloud_mode_is_refused_with_the_reason_before_anything_reaches_a_model(
    tmp_path,
):
    """The refusal is an ``Error`` for the person's window: the run ends there,
    before the prompt is built or the team is touched."""
    _signed_in_with(GroupMode.cloud)
    gate = RunGate()
    BackendSettingsLoader(tmp_path, gate).load()
    controller = RunController.__new__(RunController)
    controller._phase = RunPhase.idle
    controller._session = MagicMock()
    controller._gate = gate
    controller._prompt_builder = MagicMock()

    async def every_message() -> list[msg.Message]:
        return [m async for m in controller._run_locked("hello", None)]

    said = asyncio.run(every_message())

    assert len(said) == 1
    assert isinstance(said[0], msg.Error) and said[0].text == REASON
    assert controller._phase is RunPhase.done
    controller._session.main_team.arun.assert_not_called()
    controller._prompt_builder.build.assert_not_called()


# ── The command line ──────────────────────────────────────────────


def test_the_cli_settings_loader_knows_no_mode_before_it_loads():
    assert CliSettingsLoader(CliOptions()).mode() is GroupMode.local_and_cloud


def test_the_cli_settings_loader_reads_the_mode_of_the_pack_it_applied():
    _signed_in_with(GroupMode.cloud)
    loader = CliSettingsLoader(CliOptions())

    loader.load()

    assert loader.mode() is GroupMode.cloud


def test_the_cli_reload_without_auto_approve_keeps_the_mode():
    """The pass that drops a refused ``--auto-approve`` reads the pack again."""
    _home().mkdir(parents=True, exist_ok=True)
    _signed_in_with(None)
    pack = GroupPolicyPack(
        group_id="g1",
        group_name="Field",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy",
                content=json.dumps({"permissions": {"allow_bypass": False}}),
                content_type="json",
            )
        ],
        mode=GroupMode.cloud,
    )
    store(_home() / "group-policy", pack)
    loader = CliSettingsLoader(CliOptions(auto_approve=True))

    loader.load()

    assert loader.mode() is GroupMode.cloud


@pytest.mark.parametrize("argv", [["-m", "hello"], ["-p"]])
def test_a_hand_started_run_in_cloud_mode_is_refused_with_the_reason(argv):
    from igni_core.cli import cli

    _signed_in_with(GroupMode.cloud)

    with pytest.raises(click.ClickException) as refused:
        cli.main(argv, standalone_mode=False)

    assert refused.value.message == REASON


def test_igni_with_no_run_is_not_refused_in_cloud_mode(capsys):
    """No message, no pipe: nothing runs, so nothing is refused."""
    from igni_core.cli import cli

    _signed_in_with(GroupMode.cloud)

    cli.main([], standalone_mode=False)

    assert REASON not in capsys.readouterr().err


# ── A pack igni server really signed ──────────────────────────────

_SERVER_PACK = Path(__file__).parent / "fixtures" / "contracts" / "group-pack-with-mode.json"


def test_a_pack_igni_server_signed_verifies_and_carries_its_mode_after_the_theme():
    """igni-server's own signer made this (its ``group-pack-with-mode.json``,
    with a throwaway key): igni's verifier accepts it, and reads ``mode`` from
    the signed ``policy`` payload, where it follows ``theme``."""
    from igni_core.core.config.group_policy.issuer import IgniServerUrl
    from igni_core.core.config.group_policy.jwks import Jwks
    from igni_core.core.config.group_policy.signature import PackSignatureVerifier

    fixture = json.loads(_SERVER_PACK.read_text())
    verifier = PackSignatureVerifier(
        issuer=IgniServerUrl(url=fixture["issuer"]), subject=fixture["subject"], org=fixture["org"]
    )

    verified = verifier.verify(fixture["signed_pack"], Jwks.model_validate(fixture["jwks"]))

    assert verified.policy is not None
    segment = fixture["signed_pack"].split(".")[1]
    policy = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))["policy"]
    keys = list(policy)
    assert keys.index("mode") == keys.index("theme") + 1, keys
    assert verified.policy.mode is GroupMode(policy["mode"])
    pack = GroupPolicyPack.from_signed(verified.policy)
    assert pack.mode is GroupMode(policy["mode"])
