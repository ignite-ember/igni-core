"""What a domain's published record is read as, and how far discovery reads it.

``test_finding_the_server_from_an_address.py`` pins the answers through
``ServerDiscovery``. This file pins the record's own reading (both
encodings parse into one :class:`PublishedRecord`), the size cap at its
edge and on a stream, and that a lookup counts as igni being busy while
it is out.
"""

from __future__ import annotations

import json

import httpx
import pytest

from igni_core.core.activity.registry import ActivityRegistry
from igni_core.core.auth import discovery
from igni_core.core.auth.discovery import ServerDiscovery
from igni_core.core.auth.discovery_schemas import Discovered, PublishedRecord


class _Txt:
    def __init__(self, *strings: bytes) -> None:
        self.strings = list(strings)


async def _no_zone(name, rdtype, **kw):
    import dns.resolver

    raise dns.resolver.NXDOMAIN()


async def _discover(handler, resolve=_no_zone, email="sam@acme.example") -> Discovered:
    transport = httpx.MockTransport(handler)
    return await ServerDiscovery(transport=transport, resolve=resolve).discover(email)


def _padded(size: int) -> bytes:
    """A valid record of exactly ``size`` bytes."""
    head = b'{"api_url": "https://igni.acme.example", "pad": "'
    tail = b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


class TestTheJsonDocument:
    def test_a_record_ignores_keys_it_does_not_know(self):
        record = PublishedRecord.from_json(b'{"api_url": "https://a.example", "other": [1]}')
        assert isinstance(record, PublishedRecord)
        assert record.api_url == "https://a.example"

    def test_whitespace_and_trailing_slashes_are_stripped_from_both_urls(self):
        record = PublishedRecord.from_json(
            b'{"api_url": "  https://a.example//  ", "portal_url": "https://p.example/ "}'
        )
        assert isinstance(record, PublishedRecord)
        assert (record.api_url, record.portal_url) == ("https://a.example", "https://p.example")

    @pytest.mark.parametrize("value", ["null", "0", "false", '""', '"   "'])
    def test_a_url_that_is_null_or_blank_is_missing(self, value):
        record = PublishedRecord.from_json(f'{{"api_url": {value}}}'.encode())
        assert isinstance(record, PublishedRecord)
        assert record.api_url == ""
        assert record.answer("well-known").reason == "The record there does not name an api_url."

    @pytest.mark.parametrize("payload", [b"[]", b'"text"', b"42", b"null", b"true"])
    def test_json_that_is_not_an_object_says_so(self, payload):
        result = PublishedRecord.from_json(payload)
        assert result == Discovered.nothing("The record there is not an object.")

    @pytest.mark.parametrize("payload", [b"", b"{", b"not json", b"\xff\xfe{}"])
    def test_a_body_that_is_not_json_says_so(self, payload):
        result = PublishedRecord.from_json(payload)
        assert result == Discovered.nothing("The record there is not valid JSON.")


class TestTheTxtRecord:
    def test_words_are_pairs_and_words_without_equals_are_skipped(self):
        record = PublishedRecord.from_txt(
            "v=1 junk api_url=https://a.example portal_url=https://p.example"
        )
        assert (record.api_url, record.portal_url) == ("https://a.example", "https://p.example")

    def test_a_bare_key_with_no_equals_does_not_blank_an_earlier_value(self):
        record = PublishedRecord.from_txt("api_url=https://a.example api_url")
        assert record.api_url == "https://a.example"

    def test_the_value_is_everything_after_the_first_equals(self):
        record = PublishedRecord.from_txt("api_url=https://a.example/x?y=1&z=2")
        assert record.api_url == "https://a.example/x?y=1&z=2"

    def test_the_last_of_a_repeated_key_wins(self):
        record = PublishedRecord.from_txt(
            "api_url=https://first.example api_url=https://second.example"
        )
        assert record.api_url == "https://second.example"

    @pytest.mark.parametrize("text", ["", "   ", "no pairs here", "a b c"])
    def test_a_record_with_no_pairs_names_nothing(self, text):
        assert PublishedRecord.from_txt(text).answer("dns").found is False


class TestTheAnswer:
    def test_it_is_frozen(self):
        found = PublishedRecord(api_url="https://a.example").answer("dns")
        assert found.found
        with pytest.raises(Exception):  # noqa: B017, PT011 — pydantic's frozen-instance error
            found.api_url = "https://elsewhere.example"  # type: ignore[misc]

    def test_the_source_it_was_found_by_rides_along(self):
        record = PublishedRecord(api_url="https://a.example")
        assert record.answer("dns").source == "dns"
        assert record.answer("well-known").source == "well-known"

    def test_nothing_found_has_no_source_and_carries_its_reason(self):
        nothing = Discovered.nothing("why")
        assert (nothing.found, nothing.source, nothing.reason, nothing.api_url) == (
            False,
            "",
            "why",
            "",
        )

    def test_an_unsafe_portal_url_is_dropped_and_the_api_url_kept(self):
        found = PublishedRecord(
            api_url="https://a.example", portal_url="http://portal.example"
        ).answer("well-known")
        assert (found.api_url, found.portal_url) == ("https://a.example", "")

    def test_an_unsafe_api_url_is_refused_by_name(self):
        refused = PublishedRecord(api_url="http://a.example").answer("dns")
        assert not refused.found
        assert (
            refused.reason
            == "The record there names 'http://a.example', which is not an https URL."
        )


class TestHowMuchIsRead:
    async def test_a_record_of_exactly_the_cap_is_read(self):
        found = await _discover(lambda r: httpx.Response(200, content=_padded(discovery.MAX_BYTES)))
        assert found.api_url == "https://igni.acme.example"

    async def test_one_byte_over_the_cap_is_refused_without_asking_the_zone(self):
        asked: list[str] = []

        async def resolve(name, rdtype, **kw):
            asked.append(name)
            return [_Txt(b"api_url=https://dns.example")]

        found = await _discover(
            lambda r: httpx.Response(200, content=_padded(discovery.MAX_BYTES + 1)), resolve
        )
        assert not found.found
        assert found.reason == "The record at acme.example is too large to be one."
        assert asked == []

    async def test_a_stream_is_stopped_at_the_cap_not_read_to_its_end(self):
        pulled = 0

        class Endless(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal pulled
                for _ in range(10_000):
                    pulled += 1
                    yield b"x" * 1024

        found = await _discover(lambda r: httpx.Response(200, stream=Endless()))
        assert found.reason == "The record at acme.example is too large to be one."
        assert pulled <= discovery.MAX_BYTES // 1024 + 2

    async def test_a_body_that_is_not_read_when_the_answer_is_not_a_200(self):
        class Boom(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise AssertionError("the body of a 404 must not be read")
                yield b""

        async def resolve(name, rdtype, **kw):
            return [_Txt(b"api_url=https://dns.example")]

        found = await _discover(lambda r: httpx.Response(404, stream=Boom()), resolve)
        assert (found.api_url, found.source) == ("https://dns.example", "dns")


class TestTheZoneRecords:
    async def test_the_first_record_that_yields_a_safe_url_wins(self):
        async def resolve(name, rdtype, **kw):
            return [
                _Txt(b"unrelated words"),
                _Txt(b"api_url=http://plain.example"),
                _Txt(b"api_url=https://good.example/"),
                _Txt(b"api_url=https://later.example"),
            ]

        found = await _discover(lambda r: httpx.Response(404), resolve)
        assert (found.api_url, found.source) == ("https://good.example", "dns")

    async def test_bytes_that_are_not_utf8_do_not_crash_the_lookup(self):
        async def resolve(name, rdtype, **kw):
            return [_Txt(b"api_url=https://ok.example \xff\xfe")]

        found = await _discover(lambda r: httpx.Response(404), resolve)
        assert found.api_url == "https://ok.example"

    async def test_a_zone_with_only_unusable_records_says_it_has_none(self):
        async def resolve(name, rdtype, **kw):
            return [_Txt(b"api_url=http://plain.example")]

        found = await _discover(lambda r: httpx.Response(404), resolve)
        assert found.reason == "acme.example has no igni record published."


class TestTheRequestItself:
    async def test_the_well_known_file_is_asked_for_as_json_without_following_redirects(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                200, content=json.dumps({"api_url": "https://a.example"}).encode()
            )

        await _discover(handler, email="Sam@ACME.Example")
        assert [str(r.url) for r in seen] == ["https://acme.example/.well-known/igni.json"]
        assert seen[0].headers["Accept"] == "application/json"

    async def test_a_redirect_to_a_host_with_a_record_is_not_followed(self):
        def handler(request):
            if request.url.host == "acme.example":
                return httpx.Response(302, headers={"Location": "https://evil.example/igni.json"})
            return httpx.Response(200, content=b'{"api_url": "https://evil.example"}')

        found = await _discover(handler)
        assert not found.found
        assert found.reason == "acme.example has no igni record published."

    async def test_a_lookup_counts_as_busy_for_igni_server_only_while_it_is_out(self):
        registry = ActivityRegistry.shared()
        during: list[tuple[str, str]] = []

        def handler(request):
            during.extend((lease.holder, str(lease.kind)) for lease in registry.busy())
            return httpx.Response(200, content=b'{"api_url": "https://a.example"}')

        assert not registry.is_busy()
        await _discover(handler)
        assert ("igni-server", "http_call") in during
        assert not registry.is_busy()
