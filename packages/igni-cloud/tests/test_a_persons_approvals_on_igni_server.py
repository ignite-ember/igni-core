"""A person's MCP approvals: kept by igni server, offered by group policy.

``docs/CLOUD.md`` §9, igni server's ``service-identity.md`` §14. In a VM
there is no approvals file and nobody is prompted: the servers group
policy delivers are the ones offered, and the person's approval on igni
server is what approves them — once, for every VM of theirs. A server
that cannot be reached approves nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from igni_cloud.credentials import EnvironmentCredentials
from igni_cloud.mcp_approvals import (
    CloudMcpApprovals,
    ConfiguredApiUrl,
    PersonMcpApprovals,
    SessionToken,
)

from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.settings_loader import SettingsLoader


class IgniServer:
    """igni server's ``/v1/me/mcp-approvals``."""

    def __init__(self, answer: httpx.Response | None = None) -> None:
        self.rows: list[dict[str, str]] = []
        self.paths: list[str] = []
        self.answer = answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if self.answer is not None:
            return self.answer
        if request.method == "POST":
            body = json.loads(request.content)
            self.rows.append({**body, "approved_at": "2026-11-12T10:00:00Z"})
        return httpx.Response(200, json={"approvals": self.rows})


def _person(server: IgniServer, clock: Any = None) -> PersonMcpApprovals:
    return PersonMcpApprovals(
        api_url=lambda: "https://igni.test",
        token=lambda: "the-session",
        transport=httpx.MockTransport(server.handle),
        **({"clock": clock} if clock is not None else {}),
    )


def test_the_approvals_are_the_persons_own():
    server = IgniServer()
    person = _person(server)
    person.approved("search", "/gp/search.json")
    assert server.paths == ["/v1/me/mcp-approvals"]


def test_an_answer_that_refuses_approves_nothing():
    """A refused answer approves nothing, whatever it carries."""
    server = IgniServer(
        answer=httpx.Response(
            403, json={"approvals": [{"server": "search", "source": "/gp/search.json"}]}
        )
    )
    person = _person(server)
    assert not person.approved("search", "/gp/search.json")
    assert not person.approved("search", "/gp/search.json")


def test_an_app_s_token_is_refused_and_nothing_is_approved():
    server = IgniServer(
        answer=httpx.Response(
            403, json={"approvals": [{"server": "search", "source": "/gp/search.json"}]}
        )
    )
    person = _person(server)
    person.approve("search", "/gp/search.json")
    assert not person.approved("search", "/gp/search.json")


def test_an_answer_that_is_malformed_approves_nothing():
    server = IgniServer(answer=httpx.Response(200, content=b"not json at all"))
    person = _person(server)
    assert not person.approved("search", "/gp/search.json")


def test_a_server_that_cannot_be_reached_is_not_asked_again_before_the_ttl():
    asked: list[httpx.Request] = []

    def down(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        raise httpx.ConnectError("no route", request=request)

    now = [0.0]
    person = PersonMcpApprovals(
        api_url=lambda: "https://igni.test",
        token=lambda: "the-session",
        transport=httpx.MockTransport(down),
        clock=lambda: now[0],
    )
    assert not person.approved("search", "/gp/search.json")
    assert not person.approved("search", "/gp/search.json")
    assert len(asked) == 1, "not asked again before the minute is up"
    now[0] += 60
    assert not person.approved("search", "/gp/search.json")
    assert len(asked) == 2, "asked again once the minute is up"


def test_a_server_that_cannot_be_reached_forgets_what_it_approved():
    """Could not ask is not yes: what it approved is not kept either."""
    rows = [{"server": "search", "source": "/gp/search.json"}]
    down = [False]

    def handle(request: httpx.Request) -> httpx.Response:
        if down[0]:
            raise httpx.ConnectError("no route", request=request)
        return httpx.Response(200, json={"approvals": rows})

    now = [0.0]
    person = PersonMcpApprovals(
        api_url=lambda: "https://igni.test",
        token=lambda: "the-session",
        transport=httpx.MockTransport(handle),
        clock=lambda: now[0],
    )
    assert person.approved("search", "/gp/search.json")
    down[0] = True
    now[0] += 60
    assert not person.approved("search", "/gp/search.json")


def test_the_approvals_are_read_again_after_a_minute():
    server = IgniServer()
    server.rows.append({"server": "search", "source": "/gp/search.json"})
    now = [0.0]
    person = _person(server, clock=lambda: now[0])
    assert person.approved("search", "/gp/search.json")
    now[0] += 59
    assert person.approved("search", "/gp/search.json")
    assert server.paths == ["/v1/me/mcp-approvals"], "still cached"
    now[0] += 1
    person.approved("search", "/gp/search.json")
    assert len(server.paths) == 2, "a minute after, read again"


def test_forgetting_the_approvals_reads_them_again():
    server = IgniServer()
    server.rows.append({"server": "search", "source": "/gp/search.json"})
    person = _person(server)
    assert person.approved("search", "/gp/search.json")
    person.forget()
    assert person.approved("search", "/gp/search.json")
    assert len(server.paths) == 2


def test_the_call_gives_up_after_ten_seconds():
    assert PersonMcpApprovals.TIMEOUT_SECONDS == 10.0


def test_nothing_not_offered_is_asked_of_igni_server(tmp_path: Path):
    server = IgniServer()
    delivered = tmp_path / "group-policy" / "mcps"
    delivered.mkdir(parents=True)
    policy = CloudMcpApprovals(delivered_dir=delivered, person=_person(server))
    policy.approve("local", "/repo/.mcp.json")
    assert server.paths == [], "not offered: not asked"


def test_the_default_delivered_directory_is_the_group_policy_layouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake)
    layout = GroupPolicyLayout.for_data_dir(None)
    policy = CloudMcpApprovals()
    assert policy.offered(str(layout.dir_for("mcps") / "search.json"))
    assert not policy.offered(str(layout.dir_for("hooks") / "search.json"))


def test_the_api_url_comes_from_the_settings_and_is_read_once(
    monkeypatch: pytest.MonkeyPatch,
):
    class Loaded:
        api_url = "https://igni.test"

    reads: list[int] = []

    def load(*args: Any, **kwargs: Any) -> Loaded:
        reads.append(1)
        return Loaded()

    monkeypatch.setattr(SettingsLoader, "load", load)
    url = ConfiguredApiUrl()
    assert url() == "https://igni.test"
    Loaded.api_url = "https://another.test"
    assert url() == "https://igni.test", "read once"
    assert len(reads) == 1


def test_settings_that_do_not_load_leave_no_url(monkeypatch: pytest.MonkeyPatch):
    def load(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the settings do not load")

    monkeypatch.setattr(SettingsLoader, "load", load)
    assert ConfiguredApiUrl()() is None


def test_a_settings_without_a_url_leaves_no_url(monkeypatch: pytest.MonkeyPatch):
    class Loaded:
        api_url = ""

    monkeypatch.setattr(SettingsLoader, "load", lambda *a, **k: Loaded())
    assert ConfiguredApiUrl()() is None


def test_nobody_signed_in_asks_nothing():
    server = IgniServer()
    person = PersonMcpApprovals(
        api_url=lambda: None,
        token=lambda: "the-session",
        transport=httpx.MockTransport(server.handle),
    )
    assert not person.approved("search", "/gp/search.json")
    assert server.paths == []


def test_the_session_token_is_read_fresh(cloud_layer: Any, monkeypatch: pytest.MonkeyPatch):
    """The person's session is renewed; the token is read again every time."""
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "first")
    token = SessionToken()
    assert token() == "first"
    monkeypatch.setenv(EnvironmentCredentials.TOKEN_ENV, "second")
    assert token() == "second"


def test_the_refusal_names_the_persons_approval_endpoint(tmp_path: Path):
    policy = CloudMcpApprovals(delivered_dir=tmp_path)
    refusal = policy.refusal("search", "/repo/.mcp.json")
    assert "/v1/me/mcp-approvals" in refusal
    assert "search" in refusal and "/repo/.mcp.json" in refusal
