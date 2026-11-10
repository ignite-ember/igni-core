"""A person knows their email address, not their server's URL.

Asking for a URL asks for something most people do not have. The
domain in the address is already the name of the organisation whose
igni they should reach, so the domain answers it — and the record
lives in **that organisation's own zone**, not in a lookup service
somebody else runs. A service in the middle would learn every user's
address before they had chosen anything, and would stand in the path
of every login including the air-gapped ones.

Whoever controls the domain decides where its people sign in. That is
the right party — and it is also why the fetch is treated as a request
to a host we were *told* about rather than one we chose.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import dns.exception
import dns.resolver
import httpx
import pytest

from igni_core.backend.group_policy_sync import GroupPolicySync
from igni_core.backend.server_auth import AuthController
from igni_core.core.auth import discovery
from igni_core.core.auth.discovery import ServerDiscovery
from igni_core.core.auth.discovery_schemas import Discovered
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.config.user_config_store import UserConfigStore

domain_of = ServerDiscovery.domain_of


class _Txt:
    """One TXT record, as dnspython hands it over."""

    def __init__(self, *strings: bytes) -> None:
        self.strings = list(strings)


class _World:
    """What answers discovery instead of the network and the DNS.

    No test reaches the real network or the real DNS. A suite that
    resolves `acme.example` for real is a suite whose results depend
    on somebody else's nameserver. By default the web root serves
    nothing and the zone has no record, the way a bare domain does.
    """

    def __init__(self) -> None:
        self.handler = lambda request: httpx.Response(404)
        self.resolver = self._refusing(None)

    def answering(self, handler) -> None:
        """Serve the well-known file through this handler."""
        self.handler = handler

    def zone(self, *records: _Txt) -> None:
        """Make the TXT lookup answer with these."""

        async def resolve(name, rdtype, **kw):
            return list(records)

        self.resolver = resolve

    def no_zone(self, exc: Exception | None = None) -> None:
        """Make the TXT lookup find nothing."""
        self.resolver = self._refusing(exc)

    @staticmethod
    def _refusing(exc: Exception | None):
        async def resolve(name, rdtype, **kw):
            raise exc or dns.resolver.NXDOMAIN()

        return resolve

    async def discover(self, email: str) -> Discovered:
        async def resolve(*a, **kw):
            return await self.resolver(*a, **kw)

        transport = httpx.MockTransport(lambda request: self.handler(request))
        return await ServerDiscovery(transport=transport, resolve=resolve).discover(email)


@pytest.fixture
def world() -> _World:
    return _World()


def _record(**fields) -> httpx.Response:
    return httpx.Response(200, content=json.dumps(fields).encode())


class TestReadingTheAddress:
    @pytest.mark.parametrize(
        "email,expected",
        [
            ("sam@acme.example", "acme.example"),
            ("  Sam@ACME.Example  ", "acme.example"),
            ("sam+tag@acme.example", "acme.example"),
            ("sam@mail.acme.co.uk", "mail.acme.co.uk"),
        ],
    )
    def test_the_domain_is_what_matters(self, email, expected):
        assert domain_of(email) == expected

    @pytest.mark.parametrize(
        "email",
        [
            "",
            "sam",
            "sam@",
            "@acme.example",
            "sam@localhost",
            "sam@acme.example/../../evil.test",
            "sam@acme example",
            "sam@acme..example",
            "sam@.acme.example",
        ],
    )
    def test_anything_that_is_not_a_domain_is_refused(self, email):
        # The value is interpolated into a URL. A domain carrying a
        # slash must not become a request somewhere else, and
        # sanitising it into something plausible would be worse than
        # refusing.
        assert domain_of(email) == ""


class TestWhatTheDomainSays:
    @pytest.mark.asyncio
    async def test_a_published_record_is_the_answer(self, world):
        seen: list[str] = []

        def handler(request):
            seen.append(str(request.url))
            return _record(api_url="https://igni.acme.example")

        world.answering(handler)

        found = await world.discover("sam@acme.example")

        assert found.found
        assert found.api_url == "https://igni.acme.example"
        assert found.source == "well-known"
        assert seen == ["https://acme.example/.well-known/igni.json"]

    @pytest.mark.asyncio
    async def test_a_portal_url_is_carried_when_given(self, world):
        world.answering(
            lambda r: _record(
                api_url="https://igni.acme.example",
                portal_url="https://portal.acme.example",
            )
        )

        found = await world.discover("sam@acme.example")

        assert found.portal_url == "https://portal.acme.example"

    @pytest.mark.asyncio
    async def test_a_trailing_slash_is_not_carried_into_the_config(self, world):
        world.answering(lambda r: _record(api_url="https://igni.acme.example/"))

        assert (await world.discover("sam@acme.example")).api_url == "https://igni.acme.example"

    @pytest.mark.asyncio
    async def test_a_domain_with_no_record_is_not_an_error(self, world):
        # The common case. The screen offers the address field; it
        # does not report a fault.
        world.answering(lambda r: httpx.Response(404))

        found = await world.discover("sam@acme.example")

        assert not found.found
        assert "no igni record" in found.reason


class TestItIsCarefulAboutWhatItIsTold:
    @pytest.mark.asyncio
    async def test_a_plaintext_server_is_refused(self, world):
        # Whoever can see the traffic would otherwise choose the server
        # that receives sign-ins for the whole company.
        world.answering(lambda r: _record(api_url="http://igni.acme.example"))

        found = await world.discover("sam@acme.example")

        assert not found.found
        assert "not an https URL" in found.reason

    @pytest.mark.asyncio
    async def test_a_local_deployment_may_speak_plaintext(self, world):
        # The one exception, for developing against your own box.
        world.answering(lambda r: _record(api_url="http://localhost:7777"))

        assert (await world.discover("sam@acme.example")).api_url == "http://localhost:7777"

    @pytest.mark.asyncio
    async def test_a_redirect_is_not_followed(self, world):
        # A redirect is a different host answering a question asked of
        # this one.
        world.answering(
            lambda r: httpx.Response(302, headers={"location": "https://evil.test/r.json"})
        )

        assert not (await world.discover("sam@acme.example")).found

    @pytest.mark.asyncio
    async def test_the_lookup_itself_is_https(self, world):
        seen: list[str] = []

        def handler(request):
            seen.append(request.url.scheme)
            return _record(api_url="https://igni.acme.example")

        world.answering(handler)
        await world.discover("sam@acme.example")

        assert seen == ["https"]

    @pytest.mark.asyncio
    async def test_an_unsafe_portal_url_does_not_sink_the_lookup(self, world):
        # The api_url is usable alone and the portal is derived when
        # absent, so one bad field is dropped rather than fatal.
        world.answering(
            lambda r: _record(api_url="https://igni.acme.example", portal_url="http://evil.test")
        )

        found = await world.discover("sam@acme.example")

        assert found.api_url == "https://igni.acme.example"
        assert found.portal_url == ""

    @pytest.mark.asyncio
    async def test_a_record_that_is_too_large_is_refused(self, world):
        world.answering(lambda r: httpx.Response(200, content=b"x" * (discovery.MAX_BYTES + 1)))

        assert not (await world.discover("sam@acme.example")).found


class TestItNeverRaises:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "boom",
        [
            httpx.ConnectError("no route"),
            httpx.ReadTimeout("slow"),
            httpx.ConnectTimeout("slow"),
        ],
    )
    async def test_a_domain_that_answers_nowhere_is_a_reason_not_a_crash(self, world, boom):
        def handler(request):
            raise boom

        world.answering(handler)

        found = await world.discover("sam@acme.example")

        assert isinstance(found, Discovered)
        assert not found.found
        assert "no igni record" in found.reason

    @pytest.mark.asyncio
    async def test_a_record_that_is_not_json_is_a_reason(self, world):
        world.answering(lambda r: httpx.Response(200, content=b"<html>nope</html>"))

        assert "not valid JSON" in (await world.discover("sam@acme.example")).reason

    @pytest.mark.asyncio
    async def test_a_record_that_is_not_an_object_is_a_reason(self, world):
        world.answering(lambda r: httpx.Response(200, content=b'["https://igni.acme.example"]'))

        assert "not an object" in (await world.discover("sam@acme.example")).reason

    @pytest.mark.asyncio
    async def test_a_record_without_an_api_url_is_a_reason(self, world):
        world.answering(lambda r: _record(something_else="x"))

        assert "does not name an api_url" in (await world.discover("sam@acme.example")).reason

    @pytest.mark.asyncio
    async def test_a_bad_address_never_reaches_the_network(self, world):
        def handler(request):
            raise AssertionError(f"asked {request.url} about a non-address")

        world.answering(handler)

        assert "does not look like an email" in (await world.discover("not-an-address")).reason


class TestTheZoneWhenThereIsNoFile:
    """The other half: a TXT record instead of a served file.

    Organisations rarely have both abilities to hand — one needs a
    file at the email domain's web root, the other needs DNS access,
    and most find one easy and the other a ticket. So both are read,
    the file first.
    """

    @pytest.mark.asyncio
    async def test_a_txt_record_answers_when_no_file_is_served(self, world):
        world.answering(lambda r: httpx.Response(404))
        world.zone(_Txt(b"api_url=https://igni.acme.example"))

        found = await world.discover("sam@acme.example")

        assert found.api_url == "https://igni.acme.example"
        assert found.source == "dns"

    @pytest.mark.asyncio
    async def test_it_is_asked_at_the_underscored_name(self, world):
        # Underscored so it cannot collide with a host somebody wants
        # to run.
        asked: list[str] = []

        async def resolve(name, rdtype, **kw):
            asked.append(f"{name}/{rdtype}")
            return [_Txt(b"api_url=https://igni.acme.example")]

        world.answering(lambda r: httpx.Response(404))
        world.resolver = resolve

        await world.discover("sam@acme.example")

        assert asked == ["_igni.acme.example/TXT"]

    @pytest.mark.asyncio
    async def test_a_portal_url_rides_along(self, world):
        world.answering(lambda r: httpx.Response(404))
        world.zone(
            _Txt(b"api_url=https://igni.acme.example portal_url=https://portal.acme.example")
        )

        found = await world.discover("sam@acme.example")

        assert found.portal_url == "https://portal.acme.example"

    @pytest.mark.asyncio
    async def test_a_value_split_across_strings_is_rejoined(self, world):
        # TXT holds character-strings of at most 255 bytes, so a long
        # URL arrives in pieces. Taking the first would truncate it.
        world.answering(lambda r: httpx.Response(404))
        world.zone(_Txt(b"api_url=https://igni.acme", b".example"))

        assert (await world.discover("sam@acme.example")).api_url == "https://igni.acme.example"

    @pytest.mark.asyncio
    async def test_an_unrelated_txt_record_is_skipped(self, world):
        # A zone has SPF, verification strings and whatever else at
        # that name; ours is the one that names an api_url.
        world.answering(lambda r: httpx.Response(404))
        world.zone(
            _Txt(b"v=spf1 include:example.net ~all"), _Txt(b"api_url=https://igni.acme.example")
        )

        assert (await world.discover("sam@acme.example")).api_url == "https://igni.acme.example"

    @pytest.mark.asyncio
    async def test_a_plaintext_server_in_dns_is_refused_too(self, world):
        # The same check as the file. How it arrived does not change
        # what it is.
        world.answering(lambda r: httpx.Response(404))
        world.zone(_Txt(b"api_url=http://igni.acme.example"))

        assert not (await world.discover("sam@acme.example")).found

    @pytest.mark.asyncio
    async def test_the_file_wins_when_both_exist(self, world):
        world.answering(lambda r: _record(api_url="https://from-the-file.acme.example"))
        world.zone(_Txt(b"api_url=https://from-the-zone.acme.example"))

        found = await world.discover("sam@acme.example")

        assert found.api_url == "https://from-the-file.acme.example"
        assert found.source == "well-known"

    @pytest.mark.asyncio
    async def test_a_served_file_that_is_wrong_is_not_papered_over_by_dns(self, world):
        # Somebody published it. Falling through to the zone would
        # send them looking in the wrong place for the mistake.
        world.answering(lambda r: _record(api_url="http://insecure.acme.example"))
        world.zone(_Txt(b"api_url=https://igni.acme.example"))

        found = await world.discover("sam@acme.example")

        assert not found.found
        assert "not an https URL" in found.reason

    @pytest.mark.asyncio
    async def test_a_zone_that_refuses_is_a_reason_not_a_crash(self, world):
        world.answering(lambda r: httpx.Response(404))
        world.no_zone(dns.exception.Timeout())

        found = await world.discover("sam@acme.example")

        assert isinstance(found, Discovered)
        assert not found.found


class TestWritingTheServerDown:
    """The fallback path: an address typed by hand.

    A deployment that publishes no record is still a deployment, so
    the screen offers a field. What arrives that way gets exactly the
    checks a discovered URL gets — "the user typed it" is not a reason
    to write a plaintext host into the file that decides where
    sign-in codes are sent.
    """

    @staticmethod
    def _controller(tmp_path):
        store = UserConfigStore(tmp_path / "config.yaml")
        settings = SimpleNamespace(
            api_url="",
            portal_url="",
            auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json")),
        )
        portal = PortalClient(api_url="")
        controller = AuthController(
            MagicMock(),
            settings,
            MagicMock(),
            portal=portal,
            group_policy=GroupPolicySync(MagicMock(), settings, portal),
            user_config_store=store,
            server_discovery=ServerDiscovery(),
        )
        return controller, store

    def test_an_https_server_is_written_down(self, tmp_path):
        controller, store = self._controller(tmp_path)

        answer = controller.set_server("https://igni.acme.example")

        assert answer.api_url == "https://igni.acme.example"
        assert store.read()["api_url"] == "https://igni.acme.example"

    def test_the_running_process_learns_too(self, tmp_path):
        # A config file the running process disagrees with is the
        # confusing half of this operation.
        controller, _ = self._controller(tmp_path)

        controller.set_server("https://igni.acme.example", "https://portal.acme.example")

        assert controller._settings.api_url == "https://igni.acme.example"
        assert controller._settings.portal_url == "https://portal.acme.example"

    def test_a_plaintext_server_is_refused(self, tmp_path):
        controller, store = self._controller(tmp_path)

        answer = controller.set_server("http://igni.acme.example")

        assert "not an https address" in answer.error
        assert store.read() == {}

    def test_nothing_typed_is_refused(self, tmp_path):
        controller, store = self._controller(tmp_path)

        assert controller.set_server("   ").error
        assert store.read() == {}

    def test_an_existing_config_keeps_its_other_keys(self, tmp_path):
        # This is somebody's config file, not ours to rewrite.
        controller, store = self._controller(tmp_path)
        store.write({"models": {"default": "MiniMax-M2.7"}})

        controller.set_server("https://igni.acme.example")

        config = store.read()
        assert config["models"] == {"default": "MiniMax-M2.7"}
        assert config["api_url"] == "https://igni.acme.example"

    def test_a_local_deployment_may_be_named(self, tmp_path):
        controller, store = self._controller(tmp_path)

        assert controller.set_server("http://127.0.0.1:7777").api_url == "http://127.0.0.1:7777"
