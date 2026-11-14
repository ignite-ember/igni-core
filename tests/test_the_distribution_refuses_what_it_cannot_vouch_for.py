"""The distribution package, one piece at a time: archive, client, one-shot sync.

``test_extensions_arrive_from_their_services`` drives a whole sync. This
module pins the edges the whole sync cannot reach cheaply: the size and
corruption limits of an archive, what each call does when the server
answers badly, why a one-shot sync is skipped, and what a sync does when
the world around it misbehaves. Each refusal leaves the machine as it was.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from igni_core.extensions.assets import AssetCache, AssetPlatform, ExtensionAsset
from igni_core.extensions.distribution import bundle as bundle_module
from igni_core.extensions.distribution import client as client_module
from igni_core.extensions.distribution.bundle import BundleArchive, BundleVerifier
from igni_core.extensions.distribution.client import DistributionClient
from igni_core.extensions.distribution.errors import DistributionError
from igni_core.extensions.distribution.once import OneShotSync
from igni_core.extensions.distribution.schemas import (
    DistributedExtension,
    DistributionListing,
    DownloadedBundle,
    PublisherJwkSet,
    SyncReport,
)
from igni_core.extensions.distribution.sync import ExtensionDistribution
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.service_token import IgniSession
from igni_core.extensions.store import ExtensionStore
from tests.extension_distribution_helpers import (
    API_URL,
    SESSION,
    FakeIgni,
    Listed,
    Publisher,
    asset,
    bundle_archive,
    manifest,
)

PUBLISHER = Publisher("RSA")


def _entry(archive: bytes, **changes: Any) -> DistributedExtension:
    return DistributedExtension.model_validate(
        Listed(archive, PUBLISHER, **changes).row()  # type: ignore[arg-type]
    )


def _downloaded(archive: bytes, **changes: Any) -> DownloadedBundle:
    listed = Listed(archive, PUBLISHER, **changes)
    return DownloadedBundle(
        archive=archive, signature=listed.signature, sha256=listed.header_sha256
    )


def _session() -> IgniSession:
    return IgniSession(access_token=SESSION)


# ── The archive ────────────────────────────────────────────────────


def test_an_archives_digest_is_the_sha256_of_its_bytes():
    assert BundleArchive(b"abc").sha256 == hashlib.sha256(b"abc").hexdigest()


def test_an_archive_unpacks_into_a_target_that_does_not_exist_yet(tmp_path):
    target = tmp_path / "deep" / "bundle"
    BundleArchive(bundle_archive(manifest(), {"wheels/a.txt": b"x"})).extract(target)
    assert (target / "wheels" / "a.txt").read_bytes() == b"x"
    assert (target / "manifest.json").is_file()


def test_an_archive_that_unpacks_to_more_than_allowed_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle_module, "MAX_UNPACKED_BYTES", 10)
    archive = bundle_archive(manifest(), {"big.bin": b"x" * 100})
    with pytest.raises(DistributionError, match="unpacks to more than igni allows"):
        BundleArchive(archive).extract(tmp_path / "out")
    assert not (tmp_path / "out" / "big.bin").exists()


def test_an_archive_exactly_at_the_unpacked_limit_is_accepted(tmp_path, monkeypatch):
    raw = manifest()
    manifest_size = len(json.dumps(raw).encode())
    archive = bundle_archive(raw, {"big.bin": b"x" * 100})
    monkeypatch.setattr(bundle_module, "MAX_UNPACKED_BYTES", manifest_size + 100)
    BundleArchive(archive).extract(tmp_path / "out")
    assert (tmp_path / "out" / "big.bin").is_file()
    monkeypatch.setattr(bundle_module, "MAX_UNPACKED_BYTES", manifest_size + 99)
    with pytest.raises(DistributionError, match="unpacks to more than igni allows"):
        BundleArchive(archive).extract(tmp_path / "again")


@pytest.mark.parametrize(
    "archive",
    [
        b"",
        b"this is not an archive",
        bundle_archive(manifest(), {"a.bin": b"x" * 5000})[:60],
        gzip.compress(b"not a tar inside"),
    ],
    ids=["empty", "not-gzip", "truncated", "gzip-of-junk"],
)
def test_a_corrupt_archive_is_a_refusal_not_a_traceback(tmp_path, archive):
    with pytest.raises(DistributionError, match="could not be unpacked"):
        BundleArchive(archive).extract(tmp_path / "out")


def test_a_device_file_in_an_archive_is_refused(tmp_path):
    device = tarfile.TarInfo("null")
    device.type = tarfile.CHRTYPE
    device.devmajor, device.devminor = 1, 3
    archive = bundle_archive(manifest(), extra_members=[(device, None)])
    with pytest.raises(DistributionError, match="could not be unpacked"):
        BundleArchive(archive).extract(tmp_path / "out")


def test_an_archive_reaching_outside_the_target_writes_nothing_there(tmp_path):
    escape = tarfile.TarInfo("../outside.txt")
    escape.size = 4
    archive = bundle_archive(manifest(), extra_members=[(escape, b"evil")])
    with pytest.raises(DistributionError, match="could not be unpacked"):
        BundleArchive(archive).extract(tmp_path / "out")
    assert not (tmp_path / "outside.txt").exists()


# ── The checks between a download and the store ───────────────────


def test_a_verified_archive_is_returned_with_its_bytes():
    archive = bundle_archive(manifest())
    verified = BundleVerifier.verify(_entry(archive), _downloaded(archive))
    assert verified.archive == archive


def test_a_digest_in_upper_case_is_the_same_digest():
    archive = bundle_archive(manifest())
    sha = hashlib.sha256(archive).hexdigest().upper()
    BundleVerifier.verify(
        _entry(archive, listed_sha256=sha), _downloaded(archive, header_sha256=sha)
    )


def test_a_download_without_a_digest_header_is_still_held_to_the_listed_digest():
    archive = bundle_archive(manifest())
    downloaded = _downloaded(archive).model_copy(update={"sha256": ""})
    BundleVerifier.verify(_entry(archive), downloaded)
    wrong = _entry(archive, listed_sha256="0" * 64)
    with pytest.raises(DistributionError, match="not the one the listing describes"):
        BundleVerifier.verify(wrong, downloaded)


def test_a_bundle_without_a_signature_is_refused_by_name():
    archive = bundle_archive(manifest())
    downloaded = _downloaded(archive).model_copy(update={"signature": ""})
    with pytest.raises(DistributionError, match="came without X-Igni-Bundle-Signature"):
        BundleVerifier.verify(_entry(archive), downloaded)


def test_a_signature_by_another_key_is_a_distribution_error():
    archive = bundle_archive(manifest())
    stranger = Publisher("RSA", kid=PUBLISHER.kid)
    signature = stranger.sign_bundle("acme.notes", "1.0.0", archive)
    with pytest.raises(DistributionError, match="not signed by its publisher"):
        BundleVerifier.verify(_entry(archive), _downloaded(archive, signature=signature))


def test_a_signature_over_other_bytes_is_refused():
    archive = bundle_archive(manifest())
    signature = PUBLISHER.sign_bundle("acme.notes", "1.0.0", b"other bytes")
    with pytest.raises(DistributionError, match="different bytes"):
        BundleVerifier.verify(_entry(archive), _downloaded(archive, signature=signature))


@pytest.mark.parametrize(
    ("extension", "version"), [("acme.other", "1.0.0"), ("acme.notes", "2.0.0")]
)
def test_a_signature_for_another_extension_or_version_is_refused(extension, version):
    archive = bundle_archive(manifest())
    signature = PUBLISHER.sign_bundle(extension, version, archive)
    with pytest.raises(DistributionError, match=f"signature is for {extension} {version}"):
        BundleVerifier.verify(_entry(archive), _downloaded(archive, signature=signature))


# ── The wire models ────────────────────────────────────────────────


def test_a_key_keeps_what_igni_does_not_know_and_drops_what_is_absent():
    jwks = PublisherJwkSet.model_validate(
        {"keys": [{"kid": "k", "kty": "RSA", "n": "AQAB", "use": "sig"}], "extra": 1}
    ).as_jwks()
    assert jwks == {"keys": [{"kid": "k", "kty": "RSA", "n": "AQAB", "use": "sig"}], "extra": 1}


def test_a_key_with_no_members_arrives_empty_and_is_not_rejected():
    assert PublisherJwkSet.model_validate({"keys": [{}]}).as_jwks() == {
        "keys": [{"kid": "", "kty": ""}]
    }
    assert PublisherJwkSet.model_validate({}).as_jwks() == {"keys": []}


def _listing(**changes: Any) -> DistributionListing:
    row = Listed(bundle_archive(manifest()), PUBLISHER).row()
    return DistributionListing.model_validate({"extensions": [row], **changes})


def test_a_listing_names_an_extension_only_for_a_group_it_is_on_for():
    assert _listing(group="g-legal").entitled()[0].extension_id == "acme.notes"
    assert _listing(group="g-other").entitled() == []
    assert _listing(group=None).entitled() == []
    assert _listing().entitled() == []


def test_an_extension_on_for_several_groups_reaches_each_of_them():
    row = Listed(bundle_archive(manifest()), PUBLISHER, groups=["g-a", "g-b"]).row()
    for group, expected in [("g-a", 1), ("g-b", 1), ("g-c", 0)]:
        listing = DistributionListing.model_validate({"group": group, "extensions": [row]})
        assert len(listing.entitled()) == expected


def test_a_listing_from_an_older_server_has_no_group_name_and_ignores_extras():
    listing = _listing(group="g-legal", brand_new_field=True)
    assert listing.group_name is None
    assert listing.priority == []


def test_the_wire_limits_are_the_ones_igni_server_and_the_host_agree_on():
    from igni_core.extensions.distribution import constants

    assert constants.MAX_ARCHIVE_BYTES == 400 * 1024 * 1024
    assert constants.MAX_UNPACKED_BYTES == 1024 * 1024 * 1024
    assert constants.SIGNATURE_HEADER == "X-Igni-Bundle-Signature"
    assert constants.SHA256_HEADER == "X-Igni-Bundle-Sha256"


def test_a_sync_report_starts_empty():
    report = SyncReport()
    assert report.model_dump() == {
        "skipped": None,
        "installed": [],
        "removed": [],
        "unchanged": [],
        "development": [],
        "priority_changed": False,
        "failed": {},
    }


# ── The client ─────────────────────────────────────────────────────


def _client(handler, **kwargs: Any) -> DistributionClient:
    return DistributionClient(
        API_URL + "/", _session(), transport=httpx.MockTransport(handler), **kwargs
    )


async def test_the_listing_is_asked_for_at_the_servers_path_whatever_the_trailing_slash():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"group": "g", "extensions": []})

    listing = await _client(handler).listing()
    assert listing.group == "g"
    assert str(seen[0].url) == f"{API_URL}/v1/extensions"
    assert seen[0].headers["authorization"] == f"Bearer {SESSION}"


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_a_listing_that_is_not_200_says_the_status(status):
    client = _client(lambda request: httpx.Response(status))
    with pytest.raises(DistributionError, match=rf"refused the extension listing \({status}\)"):
        await client.listing()


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"extensions": [{"version": "1"}]}', b'{"extensions": 5}'],
    ids=["not-json", "missing-fields", "wrong-type"],
)
async def test_a_malformed_listing_is_a_distribution_error(body):
    client = _client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(DistributionError, match="listing is malformed"):
        await client.listing()


async def test_a_listing_missing_its_digest_or_keys_is_malformed():
    row = Listed(bundle_archive(manifest()), PUBLISHER).row()
    for dropped in ("sha256", "publisher_jwks"):
        broken = {k: v for k, v in row.items() if k != dropped}
        client = _client(lambda request, b=broken: httpx.Response(200, json={"extensions": [b]}))
        with pytest.raises(DistributionError, match="listing is malformed"):
            await client.listing()


async def test_an_unreachable_server_is_a_distribution_error_for_the_listing():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    with pytest.raises(DistributionError, match="could not be reached"):
        await _client(handler).listing()


async def test_a_bundle_comes_with_its_signature_and_digest():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/extensions/acme.notes/bundle"
        return httpx.Response(
            200,
            content=b"archive",
            headers={"X-Igni-Bundle-Signature": "sig", "X-Igni-Bundle-Sha256": "abc"},
        )

    got = await _client(handler).bundle("acme.notes")
    assert (got.archive, got.signature, got.sha256) == (b"archive", "sig", "abc")


async def test_a_bundle_without_the_headers_has_empty_ones():
    got = await _client(lambda request: httpx.Response(200, content=b"x")).bundle("acme.notes")
    assert (got.signature, got.sha256) == ("", "")


async def test_a_bundle_the_server_refuses_is_a_distribution_error():
    client = _client(lambda request: httpx.Response(403))
    with pytest.raises(DistributionError, match=r"refused the bundle download \(403\)"):
        await client.bundle("acme.notes")


async def test_a_bundle_larger_than_any_igni_accepts_is_refused(monkeypatch):
    monkeypatch.setattr(client_module, "MAX_ARCHIVE_BYTES", 10)
    client = _client(lambda request: httpx.Response(200, content=b"x" * 11))
    with pytest.raises(DistributionError, match="larger than any igni accepts"):
        await client.bundle("acme.notes")
    ok = _client(lambda request: httpx.Response(200, content=b"x" * 10))
    assert (await ok.bundle("acme.notes")).archive == b"x" * 10


async def test_a_bundle_that_breaks_off_mid_download_is_a_distribution_error():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"half"
            raise httpx.ReadError("connection reset")

    client = _client(lambda request: httpx.Response(200, stream=Broken()))
    with pytest.raises(DistributionError, match="bundle download failed"):
        await client.bundle("acme.notes")


def _declared(body: bytes, **changes: Any) -> ExtensionAsset:
    raw = {**asset("jre", "any", body, file="jre.bin"), **changes}
    return ExtensionManifest.model_validate(manifest(assets=[raw])).assets[0]


def _partials(cache: AssetCache) -> list[str]:
    return [p.name for p in cache.root.iterdir()] if cache.root.is_dir() else []


async def test_an_asset_is_adopted_into_the_cache_by_its_digest(tmp_path):
    body = b"jvm bytes"
    declared = _declared(body)
    cache = AssetCache(tmp_path / "cache")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body)

    path = await _client(handler).asset("acme.notes", declared, cache)
    assert path == cache.path(declared)
    assert path.read_bytes() == body
    assert seen[0].url.path == f"/v1/extensions/acme.notes/assets/{declared.sha256}"
    assert seen[0].headers["authorization"] == f"Bearer {SESSION}"
    assert _partials(cache) == [declared.sha256]


async def test_an_asset_the_server_refuses_leaves_no_partial_file(tmp_path):
    declared = _declared(b"jvm bytes")
    cache = AssetCache(tmp_path / "cache")
    with pytest.raises(DistributionError, match=r"refused asset jre \(any\): 404"):
        await _client(lambda request: httpx.Response(404)).asset("acme.notes", declared, cache)
    assert _partials(cache) == []


async def test_an_asset_larger_than_its_manifest_pins_is_refused(tmp_path):
    declared = _declared(b"jvm bytes")
    cache = AssetCache(tmp_path / "cache")
    client = _client(lambda request: httpx.Response(200, content=b"x" * (declared.size + 1)))
    with pytest.raises(DistributionError, match="larger than its manifest pins"):
        await client.asset("acme.notes", declared, cache)
    assert _partials(cache) == []


async def test_an_asset_with_the_wrong_bytes_is_not_adopted(tmp_path):
    declared = _declared(b"jvm bytes")
    cache = AssetCache(tmp_path / "cache")
    client = _client(lambda request: httpx.Response(200, content=b"x" * declared.size))
    with pytest.raises(DistributionError, match="its manifest pins"):
        await client.asset("acme.notes", declared, cache)
    assert _partials(cache) == []
    assert not cache.holds(declared)


async def test_an_asset_that_breaks_off_leaves_no_partial_file(tmp_path):
    declared = _declared(b"jvm bytes")
    cache = AssetCache(tmp_path / "cache")

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"jvm"
            raise httpx.ReadError("connection reset")

    client = _client(lambda request: httpx.Response(200, stream=Broken()))
    with pytest.raises(DistributionError, match="asset jre could not be downloaded"):
        await client.asset("acme.notes", declared, cache)
    assert _partials(cache) == []


async def test_the_clients_timeouts_are_the_ones_it_was_given(monkeypatch):
    timeouts: list[float | None] = []
    real = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        timeouts.append(kwargs["timeout"])
        return real(*args, **kwargs)

    monkeypatch.setattr(client_module.httpx, "AsyncClient", spy)
    client = _client(
        lambda request: httpx.Response(200, json={}), timeout=3.0, download_timeout=7.0
    )
    await client.listing()
    await client.bundle("acme.notes")
    assert timeouts == [3.0, 7.0]


async def test_an_asset_may_take_as_long_as_the_asset_timeout(tmp_path, monkeypatch):
    timeouts: list[float | None] = []
    real = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        timeouts.append(kwargs["timeout"])
        return real(*args, **kwargs)

    monkeypatch.setattr(client_module.httpx, "AsyncClient", spy)
    body = b"jvm bytes"
    client = _client(lambda request: httpx.Response(200, content=body), timeout=3.0)
    await client.asset("acme.notes", _declared(body), AssetCache(tmp_path / "cache"))
    assert timeouts == [client_module.ASSET_DOWNLOAD_TIMEOUT]
    assert client_module.ASSET_DOWNLOAD_TIMEOUT > 300.0


# ── The one-shot sync ──────────────────────────────────────────────


class Settings:
    def __init__(self, tmp_path: Path, **changes: Any) -> None:
        self.api_url: Any = API_URL
        self.portal_url = ""
        self.air_gapped = False
        self.auth = SimpleNamespace(credentials_file=str(tmp_path / "credentials.json"))
        self.storage = SimpleNamespace(data_dir=str(tmp_path / "data"))
        self.group_policy = SimpleNamespace(trusted_keys=[], required=False)
        for name, value in changes.items():
            setattr(self, name, value)


def _one_shot(tmp_path: Path, igni: FakeIgni | None = None, **changes: Any) -> OneShotSync:
    igni = igni or FakeIgni()
    return OneShotSync(
        ExtensionStore(tmp_path / "store"),
        Settings(tmp_path, **changes),  # type: ignore[arg-type]
        transport=igni.transport(),
        session=_session,
    )


@pytest.mark.parametrize("api_url", [None, "", "   ", 42, ["https://igni.test"]])
def test_a_server_that_is_not_a_string_is_no_server(tmp_path, api_url):
    once = _one_shot(tmp_path, api_url=api_url)
    assert once.api_url is None or isinstance(once.api_url, str)
    assert once.why_not() == "no igni server is configured (set api_url in your igni settings)"


# The configured server's own host is always inside the network, so only
# a server address with no usable host can be outside it.
UNUSABLE_SERVER = "http://[not-a-host"


def test_an_air_gapped_network_that_excludes_the_server_says_how_to_allow_it(tmp_path):
    once = _one_shot(tmp_path, api_url=UNUSABLE_SERVER, air_gapped=True)
    reason = once.why_not()
    assert reason is not None
    assert f"{UNUSABLE_SERVER} is outside this network and igni is air-gapped" in reason
    assert "so igni server was not contacted" in reason
    assert "network.internal_hosts" in reason


def test_an_air_gapped_network_still_reaches_the_configured_server(tmp_path):
    assert _one_shot(tmp_path, air_gapped=True).why_not() is None


async def test_a_sync_that_is_not_attempted_asks_igni_nothing(tmp_path):
    igni = FakeIgni()
    once = _one_shot(tmp_path, igni, api_url=UNUSABLE_SERVER, air_gapped=True)
    report = await once.run()
    assert report.skipped is not None and "air-gapped" in report.skipped
    assert igni.requests == []


def test_a_corrupt_credentials_file_is_nobody_signed_in(tmp_path):
    (tmp_path / "credentials.json").write_text("{not json")
    once = OneShotSync(
        ExtensionStore(tmp_path / "store"),
        Settings(tmp_path),  # type: ignore[arg-type]
        transport=FakeIgni().transport(),
        session=lambda: None,
    )
    assert once.why_not() == f"nobody is signed in to {API_URL}: run 'igni login'"


def test_a_sync_that_would_run_has_no_reason_not_to(tmp_path):
    assert _one_shot(tmp_path).why_not() is None


async def test_a_sync_that_does_not_finish_in_time_is_skipped_with_the_seconds(tmp_path):
    once = _one_shot(tmp_path)

    async def never(self: ExtensionDistribution) -> SyncReport:
        await asyncio.sleep(30)
        return SyncReport()

    ExtensionDistribution.sync, original = never, ExtensionDistribution.sync  # type: ignore[method-assign]
    try:
        report = await once.run(timeout=0.05)
    finally:
        ExtensionDistribution.sync = original  # type: ignore[method-assign]
    assert report.skipped == "igni server did not finish answering within 0.05s"


async def test_an_unexpected_failure_is_reported_and_never_raised(tmp_path, caplog):
    once = _one_shot(tmp_path)

    async def boom(self: ExtensionDistribution) -> SyncReport:
        raise RuntimeError("disk on fire")

    original = ExtensionDistribution.sync
    ExtensionDistribution.sync = boom  # type: ignore[method-assign]
    try:
        report = await once.run()
    finally:
        ExtensionDistribution.sync = original  # type: ignore[method-assign]
    assert report.skipped == "the sync failed: disk on fire"
    assert "extension sync failed" in caplog.text


# ── The sync, when the world misbehaves ───────────────────────────


class Recorded:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def activate(self, extension_id: str, bundle: Path) -> None:
        self.calls.append(("activate", extension_id))

    async def deactivate(self, extension_id: str) -> None:
        self.calls.append(("deactivate", extension_id))


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.igni = FakeIgni()
        self.store = ExtensionStore(tmp_path / "store")
        self.lifecycle = Recorded()
        self.distribution = ExtensionDistribution(
            self.store,
            api_url=lambda: API_URL,
            session=_session,
            lifecycle=self.lifecycle,
            transport=self.igni.transport(),
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


async def test_a_service_that_moved_restarts_its_extension_without_a_download(world):
    archive = bundle_archive(manifest(), {"wheels/placeholder.txt": b"x"})
    world.igni.publish(Listed(archive, PUBLISHER, service_url="https://old.example.com"))
    await world.distribution.sync()
    world.lifecycle.calls.clear()

    world.igni.publish(Listed(archive, PUBLISHER, service_url="https://new.example.com"))
    report = await world.distribution.sync()

    assert report.unchanged == ["acme.notes"] and report.installed == []
    assert len(world.igni.downloads()) == 1
    assert world.store.provenance.of("acme.notes").service_url == "https://new.example.com"
    assert world.lifecycle.calls == [("deactivate", "acme.notes"), ("activate", "acme.notes")]


async def test_a_machine_igni_has_no_assets_for_refuses_an_extension_that_needs_them(
    world, monkeypatch
):
    monkeypatch.setattr(AssetPlatform, "current", classmethod(lambda cls: None))
    raw = manifest(assets=[asset("jre", "any", b"x", file="jre.bin")])
    world.igni.publish(Listed(bundle_archive(raw, {"wheels/p.txt": b"x"}), PUBLISHER))

    report = await world.distribution.sync()

    assert "none of the platforms igni runs on" in report.failed["acme.notes"]
    assert report.installed == []
    assert not world.store.path_for("acme.notes").exists()
    assert world.lifecycle.calls == []


async def test_an_extension_id_the_store_cannot_name_is_a_failure_not_a_crash(world):
    good = Listed(bundle_archive(manifest(), {"wheels/p.txt": b"x"}), PUBLISHER)
    evil_raw = manifest(extension_id="../evil")
    evil = Listed(bundle_archive(evil_raw), PUBLISHER, extension_id="../evil")
    world.igni.publish(evil)
    world.igni.publish(good)

    report = await world.distribution.sync()

    assert "../evil" in report.failed
    assert report.installed == ["acme.notes"]


async def test_two_syncs_at_once_run_one_after_the_other(world):
    world.igni.publish(Listed(bundle_archive(manifest(), {"wheels/p.txt": b"x"}), PUBLISHER))
    running = 0
    overlap = 0
    original = DistributionClient.listing

    async def watched(self: DistributionClient) -> DistributionListing:
        nonlocal running, overlap
        running += 1
        overlap = max(overlap, running)
        try:
            await asyncio.sleep(0.02)
            return await original(self)
        finally:
            running -= 1

    DistributionClient.listing = watched  # type: ignore[method-assign]
    try:
        first, second = await asyncio.gather(world.distribution.sync(), world.distribution.sync())
    finally:
        DistributionClient.listing = original  # type: ignore[method-assign]

    assert overlap == 1
    assert first.installed == ["acme.notes"]
    assert second.unchanged == ["acme.notes"]


async def test_stopping_cancels_a_sync_in_flight(world):
    started = asyncio.Event()
    original = DistributionClient.listing

    async def hangs(self: DistributionClient) -> DistributionListing:
        started.set()
        await asyncio.sleep(3)
        return await original(self)

    DistributionClient.listing = hangs  # type: ignore[method-assign]
    try:
        task = world.distribution.schedule()
        await started.wait()
        begun = time.monotonic()
        await world.distribution.stop()
        waited = time.monotonic() - begun
    finally:
        DistributionClient.listing = original  # type: ignore[method-assign]
    assert task.cancelled()
    assert waited < 1.5, "stop() waited for the sync instead of cancelling it"


async def test_a_background_sync_that_fails_unexpectedly_returns_nothing_and_says_so(world, caplog):
    original = DistributionClient.listing

    async def explodes(self: DistributionClient) -> DistributionListing:
        raise RuntimeError("surprise")

    DistributionClient.listing = explodes  # type: ignore[method-assign]
    try:
        assert await world.distribution.schedule() is None
    finally:
        DistributionClient.listing = original  # type: ignore[method-assign]
    assert "extension distribution sync failed" in caplog.text


async def test_a_sync_works_without_a_lifecycle_or_a_priority_listener(tmp_path):
    igni = FakeIgni()
    igni.priority = ["a.b"]
    igni.publish(Listed(bundle_archive(manifest(), {"wheels/p.txt": b"x"}), PUBLISHER))
    store = ExtensionStore(tmp_path / "store")
    distribution = ExtensionDistribution(
        store, api_url=lambda: API_URL, session=_session, transport=igni.transport()
    )
    report = await distribution.sync()
    assert report.installed == ["acme.notes"] and report.priority_changed
    igni.withdraw("acme.notes")
    assert (await distribution.sync()).removed == ["acme.notes"]


async def test_an_unlisted_extension_is_stopped_before_it_is_removed(world):
    world.igni.publish(Listed(bundle_archive(manifest(), {"wheels/p.txt": b"x"}), PUBLISHER))
    await world.distribution.sync()
    world.lifecycle.calls.clear()
    world.igni.withdraw("acme.notes")

    report = await world.distribution.sync()

    assert report.removed == ["acme.notes"]
    assert world.lifecycle.calls == [("deactivate", "acme.notes")]
    assert not world.store.path_for("acme.notes").exists()


async def test_assets_no_installed_extension_declares_are_pruned_after_a_sync(world):
    cache = world.store.asset_cache
    stale = cache.root / ("0" * 64)
    cache.root.mkdir(parents=True)
    stale.write_bytes(b"old")
    world.igni.publish(Listed(bundle_archive(manifest(), {"wheels/p.txt": b"x"}), PUBLISHER))

    await world.distribution.sync()

    assert not stale.exists()
