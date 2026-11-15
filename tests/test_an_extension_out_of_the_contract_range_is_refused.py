"""An extension whose contract range leaves out igni's contract is refused.

EXTENSIBILITY.md §7: "contract out of range — refused, naming the version
and which half is behind — never 'works mostly'". The range was parsed by
nobody: an extension declaring ``>=2.0 <3`` (or ``banana``) against
contract 1.0 passed doctor, installed and ran READY (campaign R236).
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.extensions.contract import CONTRACT_VERSION, ContractRange
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.install_preflight import InstallPreflight
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.profile import DeploymentProfile


def _manifest(contract: str) -> ExtensionManifest:
    raw: dict[str, Any] = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": contract,
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
    }
    return ExtensionManifest.model_validate(raw)


def _preflight() -> InstallPreflight:
    return InstallPreflight(
        context=InstallContext(profile=DeploymentProfile.SINGLE_USER, allow_development=True)
    )


class TestTheRange:
    @pytest.mark.parametrize(
        ("text", "version", "admitted"),
        [
            (">=1.0 <2", "1.0", True),
            (">=1.0 <2", "1.9", True),
            (">=1.0 <2", "2.0", False),
            (">=2.0 <3", "1.0", False),
            ("<1.0", "1.0", False),
            (">=1.5 <2", "1.0", False),
            ("==1.0", "1.0", True),
            ("1.0", "1.0", True),
            (">1.0", "1.0", False),
            ("<=1.0", "1.0", True),
            (">=1", "1.0", True),
        ],
    )
    def test_it_admits_what_its_bounds_admit(self, text: str, version: str, admitted: bool):
        assert ContractRange.parse(text).admits(version) is admitted

    @pytest.mark.parametrize("text", ["banana", "", ">=", ">=1.x", "1.0 || 2.0"])
    def test_text_that_is_not_a_range_is_refused(self, text: str):
        with pytest.raises(ValueError):
            ContractRange.parse(text)

    def test_the_reason_says_which_half_is_behind(self):
        assert "newer igni" in ContractRange.parse(">=2.0 <3").why_not("1.0")
        assert "update the extension" in ContractRange.parse("<1.0").why_not("1.0")


class TestTheInstallRefusesIt:
    def test_the_contract_this_igni_implements_is_admitted(self):
        _preflight().check(_manifest(">=1.0 <2"), None)

    @pytest.mark.parametrize("contract", [">=2.0 <3", "<1.0", "banana"])
    def test_a_range_without_this_contract_is_refused_naming_it(self, contract: str):
        with pytest.raises(InstallError) as caught:
            _preflight().check(_manifest(contract), None)
        assert CONTRACT_VERSION in str(caught.value)
        assert contract in str(caught.value)


class TestTheDoctorSaysSo:
    def test_the_doctor_names_it_as_a_problem(self, tmp_path):
        from igni_core.cli.ext_doctor import Findings
        from igni_core.cli.ext_manifest_check import ManifestCheck

        findings = Findings()
        ManifestCheck(_manifest(">=2.0 <3"), tmp_path).check(findings)

        assert any("contract" in p and CONTRACT_VERSION in p for p in findings.problems)

    def test_the_cli_prints_the_same_version(self):
        import importlib

        module = importlib.import_module("igni_core.cli.ext")
        assert module.CONTRACT_VERSION == CONTRACT_VERSION
