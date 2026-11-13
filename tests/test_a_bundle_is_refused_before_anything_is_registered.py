"""The rules a bundle must pass before it is registered, and the order they run in.

Where a bundle came from decides whether it may run, not what its
manifest says of itself; a shared backend refuses what only makes sense
for one person; a grant its packaging cannot deliver is refused rather
than approved and silently dropped; and ``own-agents`` with no plugin is
refused because it would offer the tools to nobody.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.install_preflight import InstallPreflight
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.profile import DeploymentProfile
from igni_core.extensions.provenance import DistributionRecord

PAGES = {"pages": [{"kind": "acme.notes.board", "label": "Board"}]}


def load(**changes: Any) -> ExtensionManifest:
    """A ``python`` packaged manifest, as a service ships one."""
    raw: dict[str, Any] = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "first-party",
        "python": {
            "package": "acme-notes",
            "entry": "acme-notes",
            "wheels": "wheels",
            "mcp": {"transport": "stdio"},
        },
    }
    raw.update(changes)
    return ExtensionManifest.model_validate(raw)


def load_endpoint(**changes: Any) -> ExtensionManifest:
    raw: dict[str, Any] = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "partner",
        "endpoint": {"url": "https://svc.example.com/mcp"},
    }
    raw.update(changes)
    return ExtensionManifest.model_validate(raw)


def load_dev(**changes: Any) -> ExtensionManifest:
    raw: dict[str, Any] = {
        "id": "acme.notes",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["python", "server.py"]},
    }
    raw.update(changes)
    return ExtensionManifest.model_validate(raw)


def record(extension_id: str = "acme.notes", version: str = "1.0.0") -> DistributionRecord:
    return DistributionRecord(
        extension_id=extension_id,
        version=version,
        sha256="0" * 64,
        service_url="https://notes.example.com",
    )


def preflight(
    *, development: bool = False, profile: DeploymentProfile = DeploymentProfile.SINGLE_USER
) -> InstallPreflight:
    return InstallPreflight(context=InstallContext(allow_development=development, profile=profile))


def shared(*, development: bool = False) -> InstallPreflight:
    return preflight(development=development, profile=DeploymentProfile.SHARED)


class TestWhereTheBundleCameFrom:
    def test_the_bundle_that_was_verified_passes(self) -> None:
        preflight().check(load(), record())

    def test_a_distributed_bundle_needs_no_development_flag(self) -> None:
        preflight(development=False).check(load(), record())

    def test_a_trust_label_is_not_consulted_for_a_distributed_bundle(self) -> None:
        for trust in ("first-party", "partner", "unsigned"):
            preflight().check(load(trust=trust), record())

    def test_a_bundle_whose_version_changed_after_it_was_verified_is_refused(self) -> None:
        with pytest.raises(InstallError, match=r"is not what igni distributed.*changed after"):
            preflight().check(load(version="1.0.1"), record(version="1.0.0"))

    def test_a_bundle_that_is_another_extension_than_the_one_verified_is_refused(self) -> None:
        with pytest.raises(InstallError, match="is not what igni distributed") as caught:
            preflight().check(load(), record(extension_id="acme.other"))

        assert "acme.other" in str(caught.value)

    def test_a_distributed_bundle_may_not_run_a_command_out_of_its_manifest(self) -> None:
        with pytest.raises(InstallError, match="'dev' packaging.*A service ships 'python'"):
            preflight(development=True).check(load_dev(), record())

    def test_a_bundle_no_service_distributed_needs_the_development_flag(self) -> None:
        with pytest.raises(InstallError, match="IGNI_DEV_EXTENSIONS is not set") as caught:
            preflight(development=False).check(load_dev(), None)

        assert "IGNI_DEV_EXTENSIONS=1" in str(caught.value)

    def test_with_the_flag_it_installs_and_says_it_is_a_development_install(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            preflight(development=True).check(load_dev(), None)

        assert "acme.notes is a development install" in caplog.text


class TestWhatASharedBackendForbids:
    def test_one_person_may_run_a_process_and_show_pages(self) -> None:
        preflight().check(load(contributes=PAGES), record())

    def test_a_process_would_be_one_instance_shared_by_everybody_and_is_refused(self) -> None:
        with pytest.raises(InstallError, match="serves more than one principal.*'endpoint'"):
            shared().check(load(), record())

    def test_a_development_process_is_refused_as_well(self) -> None:
        with pytest.raises(InstallError, match="'endpoint' packaging"):
            shared(development=True).check(load_dev(), None)

    def test_an_endpoint_without_pages_is_what_a_shared_backend_takes(self) -> None:
        shared().check(load_endpoint(), record())

    def test_pages_would_be_served_and_never_shown_and_are_refused(self) -> None:
        with pytest.raises(InstallError, match="contributes pages.*never shown"):
            shared().check(load_endpoint(contributes=PAGES), record())


class TestAGrantThePackagingCannotDeliver:
    def test_a_process_is_given_its_workspace_and_secrets(self) -> None:
        preflight().check(
            load(permissions={"workspace": "read", "secrets": ["ledger-token"]}), record()
        )

    def test_an_endpoint_that_wants_a_workspace_is_refused_naming_it(self) -> None:
        with pytest.raises(InstallError, match=r"declares workspace: write, and") as caught:
            preflight().check(load_endpoint(permissions={"workspace": "write"}), record())

        assert "secrets" not in str(caught.value).split("and igni")[0]

    def test_an_endpoint_that_wants_secrets_is_refused_naming_them(self) -> None:
        with pytest.raises(InstallError, match=r"declares secrets: \['ledger-token'\]"):
            preflight().check(load_endpoint(permissions={"secrets": ["ledger-token"]}), record())

    def test_an_endpoint_that_wants_both_has_both_named(self) -> None:
        with pytest.raises(InstallError) as caught:
            preflight().check(
                load_endpoint(permissions={"workspace": "read", "secrets": ["a"]}), record()
            )

        assert "workspace: read and secrets: ['a']" in str(caught.value)

    def test_an_endpoint_that_wants_neither_is_fine(self) -> None:
        preflight().check(load_endpoint(permissions={"workspace": "none", "secrets": []}), record())


class TestToolAudience:
    def test_own_agents_with_no_plugin_have_no_agents_of_their_own(self) -> None:
        with pytest.raises(InstallError, match=r"toolAudience: own-agents.*carries no plugin"):
            preflight().check(load(permissions={"toolAudience": "own-agents"}), record())

    def test_an_empty_plugin_name_is_no_plugin(self) -> None:
        with pytest.raises(InstallError, match="carries no plugin"):
            preflight().check(
                load(permissions={"toolAudience": "own-agents"}, contributes={"plugin": ""}),
                record(),
            )

    def test_own_agents_with_a_plugin_pass(self) -> None:
        preflight().check(
            load(permissions={"toolAudience": "own-agents"}, contributes={"plugin": "plugin"}),
            record(),
        )

    def test_all_agents_needs_no_plugin(self) -> None:
        preflight().check(load(permissions={"toolAudience": "all-agents"}), record())


class TestHostGrantsAreOnlyNoted:
    def test_an_object_this_igni_does_not_have_is_warned_about_and_the_install_goes_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        extension = load(permissions={"host": ["telepathy.read", "workspace"]})

        with caplog.at_level(logging.WARNING):
            preflight().check(extension, record())

        assert "asks for host-model objects igni does not have" in caplog.text
        assert "telepathy.read, workspace" in caplog.text

    def test_known_objects_are_not_warned_about(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            preflight().check(load(permissions={"host": ["chat.read"]}), record())

        assert "host-model objects" not in caplog.text


class TestTheOrderTheRulesRun:
    def test_where_the_bundle_came_from_is_asked_before_the_deployment_s_shape(self) -> None:
        with pytest.raises(InstallError, match="was not distributed by igni server"):
            shared(development=False).check(load_dev(), None)

    def test_the_deployment_s_shape_is_asked_before_what_the_packaging_delivers(self) -> None:
        extension = load_endpoint(contributes=PAGES, permissions={"secrets": ["a"]})

        with pytest.raises(InstallError, match="contributes pages"):
            shared().check(extension, record())

    def test_what_the_packaging_delivers_is_asked_before_the_audience(self) -> None:
        extension = load_endpoint(permissions={"secrets": ["a"], "toolAudience": "own-agents"})

        with pytest.raises(InstallError, match="declares secrets"):
            preflight().check(extension, record())
