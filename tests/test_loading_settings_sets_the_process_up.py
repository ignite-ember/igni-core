"""``ProcessEnvironment.enter``: what a backend's settings change for the whole process.

Air-gapped settings are exported to the environment child processes inherit and
force Agno's run telemetry off; the OTLP exporter is judged against the same
settings; and the process's tenancy is bound to the deployment's tenant, or to
whoever is signed in here.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from igni_core.backend.process_environment import ProcessEnvironment
from igni_core.core.air_gap import AIR_GAPPED_ENV
from igni_core.core.config.schemas.telemetry import OtlpConfig
from igni_core.core.db.scope import Tenant, tenancy

AGNO = "AGNO_TELEMETRY"


class _Person:
    def __init__(self, tenant: Tenant | None) -> None:
        self._tenant = tenant

    def tenant(self) -> Tenant | None:
        return self._tenant


def _settings(*, air_gapped: bool, otlp: OtlpConfig | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        air_gapped=air_gapped,
        telemetry=SimpleNamespace(otlp=otlp or OtlpConfig()),
    )


def _enter(settings, tenant: Tenant | None = None, person: _Person | None = None) -> None:
    ProcessEnvironment.enter(settings, tenant=tenant, person=person or _Person(None))  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    # ``enter`` writes os.environ directly: set then delete, so that
    # monkeypatch puts back whatever was there (or nothing) afterwards.
    for name in (AIR_GAPPED_ENV, AGNO):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


def test_air_gapped_settings_are_exported_for_the_processes_the_backend_starts(monkeypatch):
    import os

    _enter(_settings(air_gapped=True))

    assert os.environ[AIR_GAPPED_ENV] == "1"


def test_settings_that_are_not_air_gapped_export_nothing(monkeypatch):
    import os

    _enter(_settings(air_gapped=False))

    assert AIR_GAPPED_ENV not in os.environ


def test_an_air_gapped_backend_turns_agno_telemetry_off_whatever_the_environment_said(
    monkeypatch, caplog
):
    import os

    monkeypatch.setenv(AGNO, "true")

    with caplog.at_level(logging.WARNING):
        _enter(_settings(air_gapped=True))

    assert os.environ[AGNO] == "false"
    assert "air-gapped" in caplog.text


def test_a_backend_that_is_not_air_gapped_leaves_an_explicit_agno_choice_alone(monkeypatch):
    import os

    monkeypatch.setenv(AGNO, "true")

    _enter(_settings(air_gapped=False))

    assert os.environ[AGNO] == "true"


def test_agno_telemetry_defaults_to_off(monkeypatch):
    import os

    _enter(_settings(air_gapped=False))

    assert os.environ[AGNO] == "false"


def test_the_collector_is_judged_against_the_settings_it_was_started_with(caplog):
    otlp = OtlpConfig(enabled=True, endpoint="ftp://collector.example")

    with caplog.at_level(logging.WARNING):
        _enter(_settings(air_gapped=False, otlp=otlp))

    assert "not an http:// or https:// URL" in caplog.text


def test_a_backend_with_no_collector_says_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        _enter(_settings(air_gapped=False))

    assert "telemetry.otlp" not in caplog.text


def test_the_deployments_tenant_wins_over_the_person_signed_in():
    deployment = Tenant(org="acme", producer="app:vm")
    person = _Person(Tenant(org="other", producer="person:1"))

    _enter(_settings(air_gapped=False), tenant=deployment, person=person)

    assert tenancy.tenant() == deployment


def test_without_a_deployment_tenant_rows_belong_to_the_person_signed_in():
    person = _Person(Tenant(org="acme", producer="person:7"))

    _enter(_settings(air_gapped=False), person=person)

    assert tenancy.tenant() == Tenant(org="acme", producer="person:7")


def test_the_person_is_asked_afresh_each_time_the_tenancy_is_read():
    person = _Person(None)
    _enter(_settings(air_gapped=False), person=person)
    assert tenancy.tenant() is None

    person._tenant = Tenant(org="acme", producer="person:7")

    assert tenancy.tenant() == Tenant(org="acme", producer="person:7")


def test_entering_again_rebinds_the_tenancy():
    _enter(_settings(air_gapped=False), tenant=Tenant(org="a", producer="p"))
    _enter(_settings(air_gapped=False), tenant=Tenant(org="b", producer="q"))

    assert tenancy.tenant() == Tenant(org="b", producer="q")
