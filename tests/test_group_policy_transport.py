"""The one live fetch a required policy makes: what it asks, what it stores and why it did not."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pytest

from igni_core.core.auth.schemas import SignedInSession
from igni_core.core.config.group_policy import transport as transport_module
from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.transport import (
    LiveFetchResult,
    PackResponseBody,
    SignedPackTransport,
)
from igni_core.core.config.group_policy.trust import PackTrust
from tests.group_policy_signing import ISSUER, KEY, PERSON, ServerKey

STRANGER = ServerKey()
REAL_CLIENT = transport_module.busy_http.BusyClient
TOKEN = "session-token-value"


def _pack() -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Regulated",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="agents",
                entry_name="reviewer",
                content="---\nname: reviewer\n---\nReview.",
                content_type="markdown",
            )
        ],
    )


class Server:
    """igni server in miniature: answers the pack and the keys, and remembers what it was asked."""

    def __init__(self, pack_response: httpx.Response, jwks_response: httpx.Response) -> None:
        self.pack_response = pack_response
        self.jwks_response = jwks_response
        self.requests: list[httpx.Request] = []
        self.timeouts: list[object] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/v1/portal/me/group/pack"):
            return self.pack_response
        return self.jwks_response

    @classmethod
    def answering(cls, signed_pack: str | None, jwks: object = None) -> Server:
        body = {"signed_pack": signed_pack, "group_id": "ignored-plain-field"}
        return cls(
            httpx.Response(200, json=body),
            httpx.Response(200, json=KEY.jwks if jwks is None else jwks),
        )


@pytest.fixture
def serve(monkeypatch):
    """Route the transport's HTTP client to a :class:`Server` instead of the network."""

    def install(server: Server) -> Server:
        def client(**kwargs):
            server.timeouts.append(kwargs.get("timeout"))
            return REAL_CLIENT(transport=httpx.MockTransport(server), **kwargs)

        monkeypatch.setattr(
            transport_module, "busy_http", type("H", (), {"BusyClient": staticmethod(client)})
        )
        return server

    return install


def _trust(*, api_url: str | None = ISSUER, token: str | None = TOKEN, pins=()) -> PackTrust:
    return PackTrust(
        api_url=api_url, session=SignedInSession(subject=PERSON, token=token), pins=tuple(pins)
    )


def _cache(tmp_path) -> GroupPolicyCache:
    return GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "group-policy"))


def _fetch(tmp_path, trust: PackTrust | None = None, **kwargs) -> LiveFetchResult:
    return SignedPackTransport(trust or _trust(), **kwargs).fetch_into(_cache(tmp_path))


def _nothing_stored(tmp_path) -> bool:
    return not (tmp_path / "group-policy" / GroupPolicyLayout.SIGNED_PACK_FILE).exists()


class TestWhatIsAsked:
    def test_the_pack_is_asked_for_with_the_token_then_the_keys(self, tmp_path, serve):
        server = serve(Server.answering(KEY.sign(_pack())))

        _fetch(tmp_path)

        pack_request, keys_request = server.requests
        assert str(pack_request.url) == f"{ISSUER}/v1/portal/me/group/pack"
        assert pack_request.method == "GET"
        assert pack_request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert str(keys_request.url) == f"{ISSUER}/.well-known/jwks.json"

    def test_a_trailing_slash_on_the_address_makes_no_double_slash(self, tmp_path, serve):
        server = serve(Server.answering(KEY.sign(_pack())))

        result = _fetch(tmp_path, _trust(api_url=ISSUER + "/"))

        assert result.stored is True
        assert str(server.requests[0].url) == f"{ISSUER}/v1/portal/me/group/pack"

    def test_the_timeout_is_the_one_given(self, tmp_path, serve):
        server = serve(Server.answering(KEY.sign(_pack())))
        _fetch(tmp_path, timeout=3.5)
        assert server.timeouts == [3.5]

    def test_the_default_timeout_is_ten_seconds(self, tmp_path, serve):
        server = serve(Server.answering(KEY.sign(_pack())))
        _fetch(tmp_path)
        assert server.timeouts == [10.0]

    def test_the_token_is_never_logged(self, tmp_path, serve, caplog):
        serve(Server(httpx.Response(401), httpx.Response(200, json=KEY.jwks)))
        with caplog.at_level(logging.DEBUG):
            _fetch(tmp_path)
        assert TOKEN not in caplog.text


class TestWhenNobodyCanBeAsked:
    @pytest.mark.parametrize("token", [None, ""])
    def test_without_a_token_nothing_is_requested(self, tmp_path, serve, token):
        server = serve(Server.answering(KEY.sign(_pack())))

        result = _fetch(tmp_path, _trust(token=token))

        assert result == LiveFetchResult(stored=False, reason="nobody is signed in")
        assert server.requests == []

    @pytest.mark.parametrize("api_url", [None, ""])
    def test_without_an_address_nothing_is_requested(self, tmp_path, serve, api_url):
        server = serve(Server.answering(KEY.sign(_pack())))

        result = _fetch(tmp_path, _trust(api_url=api_url))

        assert result == LiveFetchResult(stored=False, reason="nobody is signed in")
        assert server.requests == []

    def test_plain_http_with_no_pin_is_not_asked_and_says_so(self, tmp_path, serve, caplog):
        server = serve(Server.answering(KEY.sign(_pack())))

        with caplog.at_level(logging.WARNING):
            result = _fetch(tmp_path, _trust(api_url="http://igni.corp.invalid"))

        assert result == LiveFetchResult(
            stored=False, reason="http://igni.corp.invalid is not HTTPS"
        )
        assert server.requests == []
        assert "no key is pinned" in caplog.text

    def test_plain_http_with_a_pin_is_asked(self, tmp_path, serve):
        iss = "http://igni.corp.invalid"
        server = serve(Server.answering(KEY.sign(_pack(), iss=iss)))

        result = _fetch(tmp_path, _trust(api_url=iss, pins=[KEY.thumbprint]))

        assert result.stored is True
        assert len(server.requests) == 2

    def test_plain_http_to_loopback_is_asked_without_a_pin(self, tmp_path, serve):
        iss = "http://localhost:8000"
        serve(Server.answering(KEY.sign(_pack(), iss=iss)))

        assert _fetch(tmp_path, _trust(api_url=iss)).stored is True


class TestWhenTheServerCannotBeUsed:
    @pytest.mark.parametrize("status", [401, 404, 500, 204])
    def test_a_status_other_than_200_stores_nothing_and_says_which(
        self, tmp_path, serve, caplog, status
    ):
        server = serve(Server(httpx.Response(status), httpx.Response(200, json=KEY.jwks)))

        with caplog.at_level(logging.WARNING):
            result = _fetch(tmp_path)

        assert result == LiveFetchResult(stored=False, reason=f"igni server answered {status}")
        assert _nothing_stored(tmp_path)
        assert len(server.requests) == 1
        assert f"answered {status}" in caplog.text

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            ConnectionResetError("reset"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_a_network_failure_is_reported_not_raised(self, tmp_path, monkeypatch, error):
        def boom(request):
            raise error

        monkeypatch.setattr(
            transport_module,
            "busy_http",
            type(
                "H",
                (),
                {
                    "BusyClient": staticmethod(
                        lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(boom), **kw)
                    )
                },
            ),
        )

        result = _fetch(tmp_path)

        assert result.stored is False
        assert result.reason.startswith("could not reach igni server (")
        assert _nothing_stored(tmp_path)

    def test_an_answer_that_is_not_json_is_reported_not_raised(self, tmp_path, serve):
        serve(Server(httpx.Response(200, text="<html>"), httpx.Response(200, json=KEY.jwks)))

        result = _fetch(tmp_path)

        assert result.stored is False
        assert result.reason.startswith("could not reach igni server")

    def test_keys_that_cannot_be_fetched_after_the_pack_store_nothing(self, tmp_path, serve):
        serve(
            Server(
                httpx.Response(200, json={"signed_pack": KEY.sign(_pack())}),
                httpx.Response(200, text="no"),
            )
        )

        result = _fetch(tmp_path)

        assert result.stored is False
        assert result.reason.startswith("could not reach igni server")
        assert _nothing_stored(tmp_path)


class TestAPackThatIsRefused:
    def test_a_pack_signed_by_a_stranger_stores_nothing_and_says_why(self, tmp_path, serve, caplog):
        serve(Server.answering(STRANGER.sign(_pack())))

        with caplog.at_level(logging.WARNING):
            result = _fetch(tmp_path)

        assert result.stored is False
        assert "does not verify" in result.reason
        assert _nothing_stored(tmp_path)
        assert "was refused" in caplog.text

    @pytest.mark.parametrize("signed", [None, "", 7, ["a", "b"], {"x": 1}])
    def test_an_unsigned_answer_stores_nothing(self, tmp_path, serve, signed):
        serve(Server.answering(signed))

        result = _fetch(tmp_path)

        assert result.stored is False
        assert "compact JWS" in result.reason
        assert _nothing_stored(tmp_path)

    @pytest.mark.parametrize("body", [b"[]", b'"text"', b"4", b"null"])
    def test_an_answer_that_is_not_an_object_is_unsigned(self, tmp_path, serve, body):
        serve(Server(httpx.Response(200, content=body), httpx.Response(200, json=KEY.jwks)))

        result = _fetch(tmp_path)

        assert result.stored is False
        assert "compact JWS" in result.reason

    @pytest.mark.parametrize("jwks", [[], "keys", None, {"keys": []}])
    def test_keys_that_are_not_a_key_set_store_nothing(self, tmp_path, serve, jwks):
        serve(
            Server(
                httpx.Response(200, json={"signed_pack": KEY.sign(_pack())}),
                httpx.Response(200, json=jwks),
            )
        )

        result = _fetch(tmp_path)

        assert result.stored is False
        assert result.reason
        assert _nothing_stored(tmp_path)

    def test_a_pack_for_somebody_else_stores_nothing(self, tmp_path, serve):
        serve(Server.answering(KEY.sign(_pack(), sub="someone-else")))
        assert _fetch(tmp_path).stored is False
        assert _nothing_stored(tmp_path)

    def test_a_pack_from_another_server_stores_nothing(self, tmp_path, serve):
        serve(Server.answering(KEY.sign(_pack(), iss="https://elsewhere.invalid")))
        assert _fetch(tmp_path).stored is False
        assert _nothing_stored(tmp_path)

    def test_a_pinned_install_refuses_keys_that_are_not_pinned(self, tmp_path, serve):
        serve(Server.answering(STRANGER.sign(_pack()), jwks=STRANGER.jwks))

        result = _fetch(tmp_path, _trust(pins=[KEY.thumbprint]))

        assert result.stored is False
        assert "pinned" in result.reason
        assert _nothing_stored(tmp_path)


class TestAPackThatIsStored:
    def test_it_is_stored_as_received_with_its_keys_and_version(self, tmp_path, serve):
        compact = KEY.sign(_pack(), version=11)
        serve(Server.answering(compact))

        result = _fetch(tmp_path)

        assert result == LiveFetchResult(stored=True, reason="")
        cache = _cache(tmp_path)
        assert cache.signed.signed_text() == compact
        assert cache.signed.jwks().as_mapping() == KEY.jwks
        assert cache.signed.applied_version() == 11
        assert cache.signed.read(_trust()).pack.group_name == "Regulated"

    def test_it_replaces_a_newer_stored_version_with_the_live_ones(self, tmp_path, serve):
        cache = _cache(tmp_path)
        serve(Server.answering(KEY.sign(_pack(), version=50)))
        _fetch(tmp_path)
        serve(Server.answering(KEY.sign(_pack(), version=20)))

        assert _fetch(tmp_path).stored is True
        assert cache.signed.applied_version() == 20

    def test_a_signed_absence_is_stored_and_what_was_derived_is_cleared(self, tmp_path, serve):
        cache = _cache(tmp_path)
        cache.materialize(_pack())
        agent_file = cache.layout.dir_for("agents") / "reviewer.md"
        assert agent_file.exists()
        absence = KEY.sign_absence(version=5)
        serve(Server.answering(absence))

        result = _fetch(tmp_path)

        assert result.stored is True
        assert not agent_file.exists()
        assert cache.signed.signed_text() == absence
        assert cache.signed.applied_version() == 5
        assert cache.signed.read(_trust()).status == "absent"

    def test_a_pack_leaves_what_was_derived_alone(self, tmp_path, serve):
        cache = _cache(tmp_path)
        cache.materialize(_pack())
        serve(Server.answering(KEY.sign(_pack())))

        _fetch(tmp_path)

        assert (cache.layout.dir_for("agents") / "reviewer.md").exists()


class TestTheBodies:
    def test_only_the_signed_pack_is_read_from_an_answer(self):
        body = PackResponseBody.model_validate({"signed_pack": "a.b.c", "entries": [1], "x": 2})
        assert body.signed_pack == "a.b.c"
        assert not hasattr(body, "entries")

    def test_a_result_is_frozen(self):
        with pytest.raises(ValueError):
            LiveFetchResult(stored=True).stored = False  # type: ignore[misc]
