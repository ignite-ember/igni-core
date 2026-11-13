"""The edges of the permission model: the odd URL, the odd host, the odd name.

``test_a_declared_permission_is_an_enforced_one`` pins the rule for
each permission; this pins what the rule does with input that is
almost right — a port that is not a number, a literal IPv6 address, a
host that only ends like ``localhost``, a secret named with a dot.
Each is a place where a lenient reading would widen what an extension
was granted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.permissions import (
    PermissionDenied,
    Permissions,
    ServiceOrigin,
    is_loopback_host,
)

LOOKUP = {
    "name": "acme.scanner.lookup",
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


def manifest_of(**changes: object) -> ExtensionManifest:
    raw: dict = {
        "id": "acme.scanner",
        "version": "1.0.0",
        "contract": ">=1.0 <2",
        "trust": "unsigned",
        "dev": {"command": ["node", "server.js"]},
    }
    raw.update(changes)
    return ExtensionManifest.model_validate(raw)


class TestWhichHostsAreThisMachine:
    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "LOCALHOST",
            "localhost.",
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.254",
            "::1",
            "[::1]",
            "tickets.localhost",
            "Notes.Localhost.",
        ],
    )
    def test_these_are_loopback(self, host):
        assert is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "evil-localhost.com",
            "localhost.evil.com",
            "notlocalhost",
            "128.0.0.1",
            "10.0.0.1",
            "0.0.0.0",
            "::2",
            "example.com",
            "",
        ],
    )
    def test_these_only_look_like_it(self, host):
        assert is_loopback_host(host) is False


class TestWhatServiceUrlIsAnOrigin:
    @pytest.mark.parametrize(
        "url", [None, "", "   ", "https://", "ftp://svc.test", "ws://svc.test", "svc.test"]
    )
    def test_no_page_connection_without_an_https_or_http_host(self, url):
        assert ServiceOrigin.parse(url) is None

    def test_a_port_that_is_not_a_port_is_no_origin(self):
        assert ServiceOrigin.parse("https://svc.test:notaport/api") is None
        assert ServiceOrigin.parse("https://svc.test:99999/api") is None

    def test_the_origin_drops_the_path_and_keeps_the_port(self):
        origin = ServiceOrigin.parse("https://Svc.Test:8443/api/v1?x=1")
        assert origin is not None
        assert origin.url == "https://svc.test:8443"
        assert origin.authority == "svc.test:8443"
        assert origin.secure is True
        assert origin.csp_sources() == ["https://svc.test:8443", "wss://svc.test:8443"]

    def test_an_ipv6_service_is_bracketed(self):
        origin = ServiceOrigin.parse("http://[::1]:8080/x", development=True)
        assert origin is not None
        assert origin.authority == "[::1]:8080"
        assert origin.url == "http://[::1]:8080"
        assert origin.secure is False
        assert origin.csp_sources() == ["http://[::1]:8080", "ws://[::1]:8080"]

    def test_plain_http_is_for_a_loopback_host_in_development_only(self):
        assert ServiceOrigin.parse("http://127.0.0.1:9000") is None
        assert ServiceOrigin.parse("http://svc.test", development=True) is None
        assert ServiceOrigin.parse("http://127.0.0.1:9000", development=True) is not None
        assert ServiceOrigin.parse("http://evil-localhost.com", development=True) is None

    def test_a_remote_plain_http_service_is_a_warning_not_a_note(self, caplog):
        with caplog.at_level("INFO", logger="igni_core.extensions.permissions"):
            assert ServiceOrigin.parse("http://svc.test/api") is None
        assert [r.levelname for r in caplog.records] == ["WARNING"]
        assert "not on this machine" in caplog.text

    def test_an_https_service_says_nothing(self, caplog):
        with caplog.at_level("DEBUG", logger="igni_core.extensions.permissions"):
            assert ServiceOrigin.parse("https://svc.test") is not None
        assert caplog.records == []


class TestTheConnectSourceList:
    def test_a_declared_host_that_is_the_service_is_listed_once(self):
        permissions = Permissions(manifest_of(permissions={"egress": ["svc.test"]}))
        service = ServiceOrigin.parse("https://svc.test")
        src = permissions.egress.csp_connect_src(service).split()
        assert src == ["'self'", "https://svc.test", "wss://svc.test"]

    def test_the_service_comes_before_the_declared_hosts(self):
        permissions = Permissions(manifest_of(permissions={"egress": ["ledger.internal:8443"]}))
        service = ServiceOrigin.parse("https://svc.test")
        assert permissions.egress.csp_connect_src(service).split() == [
            "'self'",
            "https://svc.test",
            "wss://svc.test",
            "https://ledger.internal:8443",
            "wss://ledger.internal:8443",
        ]

    def test_plain_schemes_reach_only_a_loopback_host_and_only_in_development(self):
        raw = manifest_of(permissions={"egress": ["localhost:3000", "ledger.internal"]})
        dev = Permissions(raw, development=True).egress.csp_connect_src().split()
        assert "http://localhost:3000" in dev and "ws://localhost:3000" in dev
        assert "http://ledger.internal" not in dev and "ws://ledger.internal" not in dev
        prod = Permissions(raw).egress.csp_connect_src().split()
        assert not any(s.startswith(("http://", "ws://")) for s in prod)

    def test_a_dotted_localhost_name_counts_as_this_machine_in_development(self):
        permissions = Permissions(
            manifest_of(permissions={"egress": ["tickets.localhost"]}), development=True
        )
        assert "http://tickets.localhost" in permissions.egress.csp_connect_src().split()


class TestAnEndpointIsHeldToTheList:
    def test_a_url_with_no_host_is_refused(self):
        permissions = Permissions(manifest_of(permissions={"egress": ["ledger.internal"]}))
        with pytest.raises(PermissionDenied, match="has no host"):
            permissions.egress.check_endpoint("/just/a/path")

    def test_a_declared_host_and_port_matches_that_port_only(self):
        permissions = Permissions(manifest_of(permissions={"egress": ["ledger.internal:8443"]}))
        permissions.egress.check_endpoint("https://ledger.internal:8443/mcp")
        with pytest.raises(PermissionDenied, match="permissions.egress"):
            permissions.egress.check_endpoint("https://ledger.internal:9000/mcp")
        with pytest.raises(PermissionDenied, match="permissions.egress"):
            permissions.egress.check_endpoint("https://ledger.internal/mcp")

    def test_a_declared_bare_host_matches_any_port(self):
        permissions = Permissions(manifest_of(permissions={"egress": ["ledger.internal"]}))
        permissions.egress.check_endpoint("https://ledger.internal:9000/mcp")

    def test_a_declared_host_is_not_enough_over_plain_http(self):
        permissions = Permissions(manifest_of(permissions={"egress": ["ledger.internal"]}))
        with pytest.raises(PermissionDenied, match="not https"):
            permissions.egress.check_endpoint("http://ledger.internal/mcp")

    def test_plain_http_to_this_machine_needs_development_and_a_declaration(self):
        raw = manifest_of(permissions={"egress": ["localhost:3000"]})
        Permissions(raw, development=True).egress.check_endpoint("http://localhost:3000/mcp")
        with pytest.raises(PermissionDenied, match="not https"):
            Permissions(raw).egress.check_endpoint("http://localhost:3000/mcp")
        with pytest.raises(PermissionDenied, match="permissions.egress"):
            Permissions(raw, development=True).egress.check_endpoint("http://localhost:4000/mcp")

    def test_an_empty_list_says_so(self):
        permissions = Permissions(manifest_of())
        with pytest.raises(PermissionDenied, match="empty"):
            permissions.egress.check_endpoint("https://ledger.internal/mcp")


class TestWhichToolsAreMeant:
    def test_hints_are_found_by_the_bare_name_and_by_the_declared_one(self):
        tools = Permissions(manifest_of(permissions={"tools": [LOOKUP]})).tools
        by_declared = tools.hints("acme.scanner.lookup")
        assert by_declared is not None
        assert tools.hints("lookup") is by_declared

    def test_an_undeclared_tool_has_no_hints(self):
        tools = Permissions(manifest_of(permissions={"tools": [LOOKUP]})).tools
        assert tools.hints("shell") is None

    def test_another_extensions_prefix_does_not_match(self):
        tools = Permissions(manifest_of(permissions={"tools": [LOOKUP]})).tools
        assert tools.allowed("other.ext.lookup") is False
        assert tools.hints("other.ext.lookup") is None

    def test_declared_lists_the_manifest_names(self):
        tools = Permissions(manifest_of(permissions={"tools": [LOOKUP]})).tools
        assert tools.declared == {"acme.scanner.lookup"}
        tools.declared.add("shell")
        assert tools.allowed("shell") is False


class TestWhatTheWorkspaceGrantCovers:
    def test_none_permits_nothing_whatever_the_path(self):
        workspace = Permissions(manifest_of()).workspace
        assert workspace.permits("src/a.py") is False
        assert workspace.permits("src/a.py", write=True) is False

    def test_no_project_dir_means_nothing_to_tell_even_with_a_grant(self):
        workspace = Permissions(manifest_of(permissions={"workspace": "read"})).workspace
        assert workspace.environment(None) == {}

    def test_a_grant_without_globs_names_no_paths(self):
        workspace = Permissions(manifest_of(permissions={"workspace": "write"})).workspace
        assert workspace.environment(Path("/p")) == {
            "IGNI_WORKSPACE": "/p",
            "IGNI_WORKSPACE_MODE": "write",
        }
        assert workspace.permits("anything/at/all", write=True) is True

    def test_several_globs_are_joined_with_colons_and_any_may_match(self):
        workspace = Permissions(
            manifest_of(permissions={"workspace": "read", "workspacePaths": ["src/*", "docs/*"]})
        ).workspace
        assert workspace.environment(Path("/p"))["IGNI_WORKSPACE_PATHS"] == "src/*:docs/*"
        assert workspace.permits("docs/a.md") is True
        assert workspace.permits("tests/a.py") is False

    def test_a_star_crosses_directory_separators(self):
        workspace = Permissions(
            manifest_of(permissions={"workspace": "read", "workspacePaths": ["src/*"]})
        ).workspace
        assert workspace.permits("src/deep/er/a.py") is True


class TestHowSecretsAreNamed:
    @pytest.mark.parametrize(
        "name, variable",
        [
            ("ledger-token", "IGNI_SECRET_LEDGER_TOKEN"),
            ("ledger.token", "IGNI_SECRET_LEDGER_TOKEN"),
            ("Api Key", "IGNI_SECRET_API_KEY"),
            ("a/b:c", "IGNI_SECRET_A_B_C"),
            ("token2", "IGNI_SECRET_TOKEN2"),
        ],
    )
    def test_anything_but_letters_and_digits_becomes_an_underscore(self, name, variable):
        permissions = Permissions(
            manifest_of(permissions={"secrets": [name]}), secret_resolver=lambda _: "v"
        )
        assert permissions.secrets.environment() == {variable: "v"}

    def test_an_empty_value_is_a_value_and_none_is_a_refusal(self):
        raw = manifest_of(permissions={"secrets": ["a", "b"]})
        granted = Permissions(raw, secret_resolver=lambda _: "").secrets.resolve()
        assert [(g.name, g.value) for g in granted] == [("a", ""), ("b", "")]

    def test_every_missing_secret_is_named_at_once(self):
        raw = manifest_of(permissions={"secrets": ["a", "b", "c"]})
        permissions = Permissions(raw, secret_resolver=lambda n: "v" if n == "b" else None)
        with pytest.raises(PermissionDenied) as caught:
            permissions.secrets.resolve()
        assert "['a', 'c']" in str(caught.value)


class TestWhatTheServiceIsToldInTotal:
    def test_workspace_and_secrets_arrive_together(self):
        permissions = Permissions(
            manifest_of(
                permissions={
                    "workspace": "read",
                    "workspacePaths": ["src/*"],
                    "secrets": ["ledger-token"],
                }
            ),
            secret_resolver=lambda _: "s3cret",
        )
        assert permissions.service_environment(Path("/p")) == {
            "IGNI_WORKSPACE": "/p",
            "IGNI_WORKSPACE_MODE": "read",
            "IGNI_WORKSPACE_PATHS": "src/*",
            "IGNI_SECRET_LEDGER_TOKEN": "s3cret",
        }

    def test_nothing_granted_tells_the_service_nothing(self):
        assert Permissions(manifest_of()).service_environment(Path("/p")) == {}

    def test_a_secret_the_deployment_lacks_refuses_the_whole_environment(self):
        permissions = Permissions(
            manifest_of(permissions={"workspace": "read", "secrets": ["ledger-token"]}),
            secret_resolver=lambda _: None,
        )
        with pytest.raises(PermissionDenied):
            permissions.service_environment(Path("/p"))
