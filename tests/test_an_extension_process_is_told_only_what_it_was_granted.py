"""What an extension's process is told: its service, its tokens, its directories.

The environment is built in one place from parts with a precedence, and
only for a process igni starts. Tried here on its own, with the two
loopback endpoints played by recorders so the secret each was handed can
be compared, and a real store for the settings and the data directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_environment import ExtensionEnvironment, ProcessEnvironment
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.store import DATA_DIR, DATA_DIR_ENV, ExtensionStore
from tests.extension_distribution_helpers import manifest as raw_manifest

SERVICE = "https://notes.example.com"


def load(**changes: Any) -> ExtensionManifest:
    return ExtensionManifest.model_validate(raw_manifest(**changes))


def endpoint() -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "acme.notes",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "partner",
            "endpoint": {"url": "https://svc.example.com/mcp"},
        }
    )


def record(url: str = SERVICE) -> DistributionRecord:
    return DistributionRecord(
        extension_id="acme.notes", version="1.0.0", sha256="0" * 64, service_url=url
    )


SERVICE_SETTING = {
    "contributes": {"settings": [{"key": "serviceUrl", "type": "string", "label": "Service"}]}
}


class Tokens:
    """The token endpoint: what it was asked to grant and revoke."""

    def __init__(self) -> None:
        self.granted: list[tuple[str, str, str | None]] = []
        self.revoked: list[str] = []

    async def grant(
        self, extension_id: str, service_url: str, *, secret: str | None = None
    ) -> dict[str, str]:
        self.granted.append((extension_id, service_url, secret))
        return {"IGNI_SERVICE_TOKEN_URL": "http://127.0.0.1:1/token", "TOKEN_SECRET": secret or ""}

    def revoke(self, extension_id: str) -> None:
        self.revoked.append(extension_id)


class Host:
    """The host model's channel."""

    def __init__(self) -> None:
        self.granted: list[tuple[str, str]] = []
        self.revoked: list[str] = []

    async def grant(self, extension_id: str, secret: str) -> dict[str, str]:
        self.granted.append((extension_id, secret))
        return {"IGNI_HOST_URL": "ws://127.0.0.1:2", "HOST_SECRET": secret}

    def revoke(self, extension_id: str) -> None:
        self.revoked.append(extension_id)


def environment(
    tmp_path: Path,
    *,
    tokens: Tokens | None = None,
    host: Host | None = None,
    store: bool = True,
    **context: Any,
) -> ExtensionEnvironment:
    return ExtensionEnvironment(
        context=InstallContext(**context),
        service_tokens=tokens,  # type: ignore[arg-type]
        host_channel=host,  # type: ignore[arg-type]
        store=ExtensionStore(tmp_path / "store") if store else None,
    )


class TestTheMergedEnvironment:
    def test_with_literals_is_a_copy_and_leaves_the_original_alone(self) -> None:
        original = ProcessEnvironment(grants={"IGNI_HOST_URL": "ws://x"})
        shipped = {"COLOUR": "green"}

        copy = original.with_literals(shipped)
        shipped["COLOUR"] = "red"

        assert copy.literals == {"COLOUR": "green"}
        assert original.literals == {}
        assert copy.grants == original.grants

    def test_the_extension_s_own_grants_outrank_the_bundle_and_igni_outranks_both(self) -> None:
        merged = ProcessEnvironment(
            literals={"A": "bundle", "B": "bundle", "C": "bundle", "IGNI_WORKSPACE": "/etc"},
            service={"B": "service", "C": "service"},
            grants={"C": "igni"},
        ).merged()

        assert (merged["A"], merged["B"], merged["C"]) == ("bundle", "service", "igni")
        assert merged["IGNI_WORKSPACE"] == "/etc"  # nothing granted it, so the literal stands

    def test_a_directory_that_is_not_given_is_not_named(self) -> None:
        merged = ProcessEnvironment().merged()

        assert DATA_DIR_ENV not in merged
        assert "IGNI_EXTENSION_ASSETS" not in merged


class TestThePermissionsItIsBuiltFrom:
    def test_the_deployment_resolves_a_declared_secret(self, tmp_path: Path) -> None:
        extension = load(permissions={"secrets": ["ledger-token"]})
        asked: list[str] = []

        def resolver(name: str) -> str | None:
            asked.append(name)
            return "s3cret"

        permissions = environment(tmp_path, secret_resolver=resolver).permissions(extension)

        assert permissions.service_environment(None) == {"IGNI_SECRET_LEDGER_TOKEN": "s3cret"}
        assert asked == ["ledger-token"]

    @pytest.mark.parametrize("development", [False, True])
    def test_the_development_flag_decides_whether_localhost_egress_may_be_plain(
        self, tmp_path: Path, development: bool
    ) -> None:
        extension = load(permissions={"egress": ["localhost:9000"]})

        permissions = environment(tmp_path, allow_development=development).permissions(extension)

        assert ("http://localhost:9000" in permissions.egress.csp_connect_src()) is development


class TestTheServiceItBelongsTo:
    def test_a_distributed_extension_belongs_to_the_service_that_shipped_it(
        self, tmp_path: Path
    ) -> None:
        extension = load(**SERVICE_SETTING)
        env = environment(tmp_path)
        env._store.save_settings(extension, {"serviceUrl": "https://elsewhere.example"})  # type: ignore[union-attr]

        assert env.service_url(extension, record()) == SERVICE

    def test_a_development_install_reads_its_serviceurl_setting_trimmed(
        self, tmp_path: Path
    ) -> None:
        extension = load(**SERVICE_SETTING)
        env = environment(tmp_path)
        env._store.save_settings(extension, {"serviceUrl": "  https://dev.example.com/api \n"})  # type: ignore[union-attr]

        assert env.service_url(extension, None) == "https://dev.example.com/api"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_a_blank_or_missing_setting_is_no_service(self, tmp_path: Path, value: Any) -> None:
        extension = load(**SERVICE_SETTING)
        env = environment(tmp_path)
        env._store.save_settings(extension, {"serviceUrl": value})  # type: ignore[union-attr]

        assert env.service_url(extension, None) is None

    def test_an_extension_that_declares_no_such_setting_has_no_service(
        self, tmp_path: Path
    ) -> None:
        assert environment(tmp_path).service_url(load(), None) is None

    def test_without_a_store_a_development_install_has_no_service(self, tmp_path: Path) -> None:
        env = environment(tmp_path, store=False)

        assert env.service_url(load(**SERVICE_SETTING), None) is None
        assert env.service_url(load(**SERVICE_SETTING), record()) == SERVICE


class TestBuildingTheEnvironment:
    async def test_one_secret_names_the_process_at_both_loopback_endpoints(
        self, tmp_path: Path
    ) -> None:
        tokens, host = Tokens(), Host()
        env = environment(tmp_path, tokens=tokens, host=host)
        extension = load()

        built = await env.build(extension, env.permissions(extension), SERVICE)

        [(_, url, token_secret)] = tokens.granted
        [(_, host_secret)] = host.granted
        assert url == SERVICE
        assert token_secret and token_secret == host_secret
        assert built.grants["TOKEN_SECRET"] == built.grants["HOST_SECRET"] == host_secret

    async def test_each_start_has_a_secret_of_its_own(self, tmp_path: Path) -> None:
        host = Host()
        env = environment(tmp_path, host=host)
        extension = load()

        await env.build(extension, env.permissions(extension), None)
        await env.build(extension, env.permissions(extension), None)

        assert host.granted[0][1] != host.granted[1][1]

    async def test_without_a_service_there_is_no_token_to_fetch_but_the_host_is_reachable(
        self, tmp_path: Path
    ) -> None:
        tokens, host = Tokens(), Host()
        env = environment(tmp_path, tokens=tokens, host=host)
        extension = load()

        built = await env.build(extension, env.permissions(extension), None)

        assert tokens.granted == []
        assert "IGNI_SERVICE_TOKEN_URL" not in built.grants
        assert built.grants["HOST_SECRET"] == host.granted[0][1]

    async def test_without_either_endpoint_nobody_is_told_where_igni_is(
        self, tmp_path: Path
    ) -> None:
        env = environment(tmp_path)
        extension = load()

        built = await env.build(extension, env.permissions(extension), SERVICE)

        assert built.grants == {}

    async def test_an_endpoint_is_somebody_else_s_process_and_is_given_nothing(
        self, tmp_path: Path
    ) -> None:
        tokens, host = Tokens(), Host()
        env = environment(tmp_path, tokens=tokens, host=host)
        extension = endpoint()

        built = await env.build(extension, env.permissions(extension), SERVICE, tmp_path / "a")

        assert tokens.granted == [] and host.granted == []
        assert built.grants == {}
        assert built.data_dir is None
        assert not (tmp_path / "store" / DATA_DIR).exists()
        assert built.assets_dir == tmp_path / "a"

    async def test_the_service_environment_comes_from_the_grants_and_the_project(
        self, tmp_path: Path
    ) -> None:
        extension = load(permissions={"secrets": ["ledger-token"]})
        env = environment(tmp_path, secret_resolver=lambda name: f"value-of-{name}")

        built = await env.build(extension, env.permissions(extension), None)

        assert built.service == {"IGNI_SECRET_LEDGER_TOKEN": "value-of-ledger-token"}

    async def test_the_process_is_given_its_data_directory_and_assets(self, tmp_path: Path) -> None:
        env = environment(tmp_path)
        extension = load()

        built = await env.build(extension, env.permissions(extension), None, tmp_path / "assets")

        expected = tmp_path / "store" / DATA_DIR / "acme.notes"
        assert built.data_dir == expected and expected.is_dir()
        assert built.merged()[DATA_DIR_ENV] == str(expected)


class TestTheDataDirectory:
    def test_it_is_beside_the_store_and_made_when_asked_for(self, tmp_path: Path) -> None:
        path = environment(tmp_path).data_dir("acme.notes")

        assert path == tmp_path / "store" / DATA_DIR / "acme.notes"
        assert path.is_dir()

    def test_what_an_extension_kept_survives_asking_again(self, tmp_path: Path) -> None:
        env = environment(tmp_path)
        (env.data_dir("acme.notes") / "model.bin").write_text("expensive")

        again = env.data_dir("acme.notes")

        assert (again / "model.bin").read_text() == "expensive"

    def test_without_a_store_it_is_the_default_stores(self, tmp_path: Path) -> None:
        # The suite points HOME at a temporary directory, so this is
        # the default store of a machine that is not the developer's.
        expected = ExtensionStore().root / DATA_DIR / "acme.notes"

        path = environment(tmp_path, store=False).data_dir("acme.notes")

        assert path == expected and path.is_dir()

    def test_each_extension_has_its_own(self, tmp_path: Path) -> None:
        env = environment(tmp_path)

        assert env.data_dir("acme.notes") != env.data_dir("acme.ledger")


class TestTakingTheTokensBack:
    def test_both_endpoints_are_told_to_revoke(self, tmp_path: Path) -> None:
        tokens, host = Tokens(), Host()

        environment(tmp_path, tokens=tokens, host=host).revoke("acme.notes")

        assert tokens.revoked == ["acme.notes"]
        assert host.revoked == ["acme.notes"]

    def test_with_neither_there_is_nothing_to_take_back(self, tmp_path: Path) -> None:
        environment(tmp_path).revoke("acme.notes")

    def test_one_endpoint_alone_is_revoked(self, tmp_path: Path) -> None:
        host = Host()

        environment(tmp_path, host=host).revoke("acme.notes")

        assert host.revoked == ["acme.notes"]
