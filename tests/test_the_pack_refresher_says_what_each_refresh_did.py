"""``PackRefresher``: fetch when stale, verify, store, materialise, and say what happened.

The fetcher is the boundary here (a caller-supplied callable); the cache,
the verifier and the files are real. Every outcome is a ``RefreshResult``
value; nothing a fetcher or the disk does is allowed to raise out of it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from igni_core.core.auth.schemas import SignedInSession
from igni_core.core.config.group_policy.answers import PACK_UNCHANGED, SignedAbsence
from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.meta import PackMeta
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.refresher import (
    PackRefresher,
    RefreshResult,
    RefreshStatus,
)
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.config.group_policy.verifier import PackVerifier
from tests.group_policy_signing import ISSUER, KEY, PERSON, ServerKey

TOKEN = "bearer-token"  # noqa: S105 — a test value, not a credential
PLUGIN_URL = "https://git.example.invalid/org/tool.git"


def _entry(kind="agents", name="reviewer", **extra) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind=kind, entry_name=name, content="body", content_type="markdown", **extra
    )


def _pack(*entries: GroupPolicyEntry, etag: str | None = "etag-1") -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        entries=list(entries) or [_entry()],
        etag=etag,
    )


@pytest.fixture
def cache(tmp_path: Path) -> GroupPolicyCache:
    return GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "group-policy"))


def _verifier() -> PackVerifier:
    trust = PackTrust(api_url=ISSUER, session=SignedInSession(subject=PERSON))

    async def jwks():
        return KEY.key_set

    return PackVerifier(trust, jwks)


class _Fetcher:
    """What igni server answers, and the tags it was asked with."""

    def __init__(self, answer=None, *, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.asked: list[tuple[str, str | None]] = []

    async def __call__(self, token: str, etag: str | None):
        self.asked.append((token, etag))
        if self.raises is not None:
            raise self.raises
        return self.answer


def _stale_meta(cache: GroupPolicyCache, **fields) -> None:
    cache.meta.save(PackMeta(fetched_at=datetime.now(timezone.utc) - timedelta(hours=1), **fields))


class TestWhenItDoesNotAsk:
    async def test_no_token_means_no_request(self, cache):
        fetcher = _Fetcher(_pack())

        result = await PackRefresher(cache).refresh_if_stale("", fetcher)

        assert result == RefreshResult(status=RefreshStatus.no_token)
        assert fetcher.asked == []

    async def test_a_fresh_cache_is_not_asked(self, cache):
        cache.meta.save(PackMeta(fetched_at=datetime.now(timezone.utc), etag="e"))
        fetcher = _Fetcher(_pack())

        result = await PackRefresher(cache).refresh_if_stale(TOKEN, fetcher)

        assert result.status is RefreshStatus.fresh
        assert fetcher.asked == []

    async def test_force_asks_even_when_fresh_and_still_sends_the_tag(self, cache):
        cache.meta.save(PackMeta(fetched_at=datetime.now(timezone.utc), etag="e-held"))
        fetcher = _Fetcher(PACK_UNCHANGED)

        result = await PackRefresher(cache).refresh_if_stale(TOKEN, fetcher, force=True)

        assert result.status is RefreshStatus.unchanged
        assert fetcher.asked == [(TOKEN, "e-held")]


class TestAFetcherThatFails:
    @pytest.mark.parametrize(
        "error", [RuntimeError("portal down"), TimeoutError("hung"), ValueError("bad body")]
    )
    async def test_it_is_a_fetch_failed_result_with_the_reason_and_a_warning(
        self, cache, caplog, error
    ):
        caplog.set_level(logging.WARNING)

        result = await PackRefresher(cache).refresh_if_stale(TOKEN, _Fetcher(raises=error))

        assert result == RefreshResult(status=RefreshStatus.fetch_failed, reason=str(error))
        assert not result.ok
        assert any(
            "group-policy fetch failed" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )


class TestTheTagItSends:
    async def test_without_a_verifier_the_stored_tag_is_sent(self, cache):
        _stale_meta(cache, etag="e-held")
        fetcher = _Fetcher(None)

        await PackRefresher(cache).refresh_if_stale(TOKEN, fetcher)

        assert fetcher.asked == [(TOKEN, "e-held")]

    async def test_with_a_verifier_and_no_signed_pack_no_tag_is_sent(self, cache):
        _stale_meta(cache, etag="e-held")
        fetcher = _Fetcher(None)

        await PackRefresher(cache, _verifier()).refresh_if_stale(TOKEN, fetcher)

        assert fetcher.asked == [(TOKEN, None)]

    async def test_with_a_verifier_and_a_whole_signed_pack_the_tag_is_sent(self, cache):
        refresher = PackRefresher(cache, _verifier())
        await refresher.refresh_if_stale(TOKEN, _Fetcher(KEY.signed(_pack(etag="e-1"))))
        _stale_meta(cache, etag="e-1")
        fetcher = _Fetcher(PACK_UNCHANGED)

        await refresher.refresh_if_stale(TOKEN, fetcher)

        assert fetcher.asked == [(TOKEN, "e-1")]

    async def test_a_signed_plugin_missing_from_disk_withholds_the_tag(self, cache, tmp_path):
        plugin = _entry("plugins", "tool", source_url=PLUGIN_URL)
        signed = KEY.signed(_pack(plugin, etag="e-1"))
        refresher = PackRefresher(cache, _verifier())
        await refresher.refresh_if_stale(TOKEN, _Fetcher(signed))
        _stale_meta(cache, etag="e-1")
        assert cache.plugins.directory.joinpath("tool").exists() is False

        withheld = _Fetcher(PACK_UNCHANGED)
        await refresher.refresh_if_stale(TOKEN, withheld)
        (cache.plugins.directory / "tool").mkdir(parents=True)
        _stale_meta(cache, etag="e-1")
        sent = _Fetcher(PACK_UNCHANGED)
        await refresher.refresh_if_stale(TOKEN, sent)

        assert withheld.asked == [(TOKEN, None)]
        assert sent.asked == [(TOKEN, "e-1")]


class TestAnswersThatChangeNothing:
    async def test_unchanged_stamps_the_stored_meta_so_the_next_poll_finds_it_fresh(self, cache):
        _stale_meta(cache, etag="e", group_id="g-1")

        result = await PackRefresher(cache).refresh_if_stale(TOKEN, _Fetcher(PACK_UNCHANGED))

        assert result == RefreshResult(status=RefreshStatus.unchanged)
        assert not cache.meta.is_stale()
        assert cache.meta.load().group_id == "g-1"

    async def test_no_answer_keeps_the_cache_as_it_is(self, cache):
        _stale_meta(cache, etag="e")

        result = await PackRefresher(cache).refresh_if_stale(TOKEN, _Fetcher(None))

        assert result == RefreshResult(status=RefreshStatus.no_answer)
        assert cache.meta.is_stale()


class TestWithoutAVerifier:
    async def test_a_pack_is_materialised_and_no_signed_pack_is_stored(self, cache):
        result = await PackRefresher(cache).refresh_if_stale(TOKEN, _Fetcher(_pack()))

        assert result == RefreshResult(status=RefreshStatus.applied)
        assert (cache.layout.dir_for("agents") / "reviewer.md").read_text() == "body"
        assert cache.signed.signed_text() is None

    async def test_a_signed_absence_cannot_be_checked_and_clears_nothing(self, cache):
        await PackRefresher(cache).refresh_if_stale(TOKEN, _Fetcher(_pack()))

        result = await PackRefresher(cache).refresh_if_stale(
            TOKEN, _Fetcher(SignedAbsence(signed_pack="a.b.c", etag="e")), force=True
        )

        assert result == RefreshResult(status=RefreshStatus.unverifiable_absence)
        assert not result.ok
        assert (cache.layout.dir_for("agents") / "reviewer.md").is_file()


class TestWithAVerifier:
    async def test_an_accepted_pack_is_stored_signed_and_materialised(self, cache):
        signed = KEY.signed(_pack(etag="e-1"))

        result = await PackRefresher(cache, _verifier()).refresh_if_stale(TOKEN, _Fetcher(signed))

        assert result == RefreshResult(status=RefreshStatus.applied)
        assert result.ok
        assert cache.signed.signed_text() == signed.signed_pack
        assert (cache.layout.dir_for("agents") / "reviewer.md").is_file()
        assert cache.meta.stored_etag() == "e-1"

    async def test_the_signed_pack_is_stored_before_anything_is_materialised(self, cache):
        seen: list[str | None] = []
        real = cache.materialize

        def spy(pack, **kwargs):
            seen.append(cache.signed.signed_text())
            return real(pack, **kwargs)

        cache.materialize = spy  # type: ignore[method-assign]
        signed = KEY.signed(_pack())

        await PackRefresher(cache, _verifier()).refresh_if_stale(TOKEN, _Fetcher(signed))

        assert seen == [signed.signed_pack]

    async def test_a_pack_signed_by_a_stranger_is_refused_and_applies_nothing(self, cache, caplog):
        caplog.set_level(logging.WARNING)
        forged = ServerKey().signed(_pack())

        result = await PackRefresher(cache, _verifier()).refresh_if_stale(TOKEN, _Fetcher(forged))

        assert result.status is RefreshStatus.rejected
        assert "does not verify" in result.reason
        assert not result.ok
        assert not cache.layout.root.exists()
        assert any("was refused" in r.getMessage() for r in caplog.records)

    async def test_an_unsigned_pack_is_refused(self, cache):
        result = await PackRefresher(cache, _verifier()).refresh_if_stale(TOKEN, _Fetcher(_pack()))

        assert result == RefreshResult(
            status=RefreshStatus.rejected, reason="igni server sent the pack unsigned"
        )

    async def test_a_signed_absence_clears_what_the_pack_made_and_keeps_the_tag(self, cache):
        refresher = PackRefresher(cache, _verifier())
        await refresher.refresh_if_stale(TOKEN, _Fetcher(KEY.signed(_pack())))
        assert (cache.layout.dir_for("agents") / "reviewer.md").is_file()
        absence = SignedAbsence(signed_pack=KEY.sign_absence(), etag="e-absent")

        result = await refresher.refresh_if_stale(TOKEN, _Fetcher(absence), force=True)

        assert result == RefreshResult(status=RefreshStatus.withdrawn)
        assert result.ok
        assert not cache.layout.dir_for("agents").exists()
        assert cache.signed.signed_text() == absence.signed_pack
        meta = cache.meta.load()
        assert meta == PackMeta(etag="e-absent", fetched_at=meta.fetched_at, entry_count=0)
        assert not cache.meta.is_stale()

    async def test_a_materialise_that_fails_is_a_result_with_its_reason(
        self, cache, caplog, monkeypatch
    ):
        caplog.set_level(logging.WARNING)

        def fail(pack, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(cache, "materialize", fail)

        result = await PackRefresher(cache, _verifier()).refresh_if_stale(
            TOKEN, _Fetcher(KEY.signed(_pack()))
        )

        assert result == RefreshResult(status=RefreshStatus.materialize_failed, reason="disk full")
        assert not result.ok
        assert any("materialize failed: disk full" in r.getMessage() for r in caplog.records)

    async def test_a_value_error_while_materialising_is_the_same_result(self, cache, monkeypatch):
        def fail(pack, **kwargs):
            raise ValueError("bad entry")

        monkeypatch.setattr(cache, "materialize", fail)

        result = await PackRefresher(cache).refresh_if_stale(TOKEN, _Fetcher(_pack()))

        assert result == RefreshResult(status=RefreshStatus.materialize_failed, reason="bad entry")


class TestRefreshResult:
    @pytest.mark.parametrize(
        ("status", "ok"),
        [
            (RefreshStatus.applied, True),
            (RefreshStatus.withdrawn, True),
            (RefreshStatus.fresh, False),
            (RefreshStatus.no_token, False),
            (RefreshStatus.fetch_failed, False),
            (RefreshStatus.unchanged, False),
            (RefreshStatus.no_answer, False),
            (RefreshStatus.unverifiable_absence, False),
            (RefreshStatus.rejected, False),
            (RefreshStatus.materialize_failed, False),
        ],
    )
    def test_ok_is_true_only_for_a_pack_written_or_withdrawn(self, status, ok):
        assert RefreshResult(status=status).ok is ok

    def test_the_status_values_are_their_names(self):
        assert {s.value for s in RefreshStatus} == {
            "fresh",
            "no_token",
            "fetch_failed",
            "unchanged",
            "no_answer",
            "unverifiable_absence",
            "rejected",
            "materialize_failed",
            "withdrawn",
            "applied",
        }

    def test_a_result_is_frozen(self):
        with pytest.raises(Exception):  # noqa: B017, PT011 — pydantic's frozen error
            RefreshResult(status=RefreshStatus.fresh).reason = "x"
