"""``igni_cloud/guest/paths.py``: where igni and the guest agent meet."""

from __future__ import annotations

from pathlib import Path

import pytest
from igni_cloud.guest.paths import GuestPaths
from pydantic import ValidationError


def test_every_file_has_its_place_on_run_igni():
    paths = GuestPaths.from_env({})
    run = Path("/run/igni")
    assert paths.activity == run / "activity.json"
    assert paths.ready == run / "ready"
    assert paths.producer_env == run / "producer.env"
    assert paths.restored == run / "restored"
    assert paths.suspending == run / "suspending"
    assert paths.suspend_ready == run / "suspend-ready"
    assert paths.token == run / "token"
    assert paths.managed_settings == run / "managed-settings.yaml"
    assert paths.vm_credential == Path("/vol/igni/vm-credential")


def test_the_run_directory_moves_every_file_but_the_credential_on_the_volume():
    paths = GuestPaths.from_env({GuestPaths.RUN_DIR_ENV: "/x"})
    assert paths.activity == Path("/x/activity.json")
    assert paths.restored == Path("/x/restored")
    assert paths.suspending == Path("/x/suspending")
    assert paths.suspend_ready == Path("/x/suspend-ready")
    assert paths.token == Path("/x/token")
    assert paths.managed_settings == Path("/x/managed-settings.yaml")
    assert paths.vm_credential == Path("/vol/igni/vm-credential")


def test_each_file_has_a_variable_of_its_own_and_the_credential_one_too():
    paths = GuestPaths.from_env(
        {
            GuestPaths.RUN_DIR_ENV: "/x",
            GuestPaths.TOKEN_ENV: "/elsewhere/t",
            GuestPaths.MANAGED_SETTINGS_ENV: "/elsewhere/m.yaml",
            GuestPaths.RESTORED_ENV: "/elsewhere/r",
            GuestPaths.SUSPENDING_ENV: "/elsewhere/s",
            GuestPaths.SUSPEND_READY_ENV: "/elsewhere/sr",
            GuestPaths.PRODUCER_ENV_ENV: "/elsewhere/p",
            GuestPaths.ACTIVITY_ENV: "/elsewhere/a",
            GuestPaths.VM_CREDENTIAL_ENV: "/elsewhere/c",
        }
    )
    assert paths.token == Path("/elsewhere/t")
    assert paths.managed_settings == Path("/elsewhere/m.yaml")
    assert paths.restored == Path("/elsewhere/r")
    assert paths.suspending == Path("/elsewhere/s")
    assert paths.suspend_ready == Path("/elsewhere/sr")
    assert paths.producer_env == Path("/elsewhere/p")
    assert paths.activity == Path("/elsewhere/a")
    assert paths.vm_credential == Path("/elsewhere/c")
    assert paths.ready == Path("/x/ready"), "what is not named stays in the run directory"


def test_a_blank_variable_is_as_good_as_none_and_a_value_is_stripped():
    paths = GuestPaths.from_env(
        {
            GuestPaths.RUN_DIR_ENV: "   ",
            GuestPaths.READY_ENV: "",
            GuestPaths.TOKEN_ENV: "  \t ",
            GuestPaths.VM_CREDENTIAL_ENV: " \n",
            GuestPaths.ACTIVITY_ENV: "  /y/a.json \n",
        }
    )
    assert paths.ready == Path("/run/igni/ready")
    assert paths.token == Path("/run/igni/token")
    assert paths.vm_credential == Path("/vol/igni/vm-credential")
    assert paths.activity == Path("/y/a.json")


def test_without_a_mapping_it_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch):
    for name in (
        GuestPaths.READY_ENV,
        GuestPaths.VM_CREDENTIAL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(GuestPaths.RUN_DIR_ENV, "/from-os")
    monkeypatch.setenv(GuestPaths.VM_CREDENTIAL_ENV, "/vol/c")
    paths = GuestPaths.from_env()
    assert paths.ready == Path("/from-os/ready")
    assert paths.vm_credential == Path("/vol/c")


def test_the_paths_cannot_be_changed_once_made():
    paths = GuestPaths.from_env({})
    with pytest.raises(ValidationError):
        paths.ready = Path("/else")  # type: ignore[misc]
