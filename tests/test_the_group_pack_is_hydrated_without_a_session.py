"""Fetching, verifying and storing the group pack needs no running session.

``igni login`` in a terminal has a token and no session: it uses
:class:`GroupPackHydrator` by itself. A running session wraps the same
object in :class:`GroupPolicySync`, which adds the cold start, the timer
and the reload of the session's group agents. These pin the hydrator on
its own and the session's half where it adds to it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend.group_pack_hydrator import GroupPackHydrator
from igni_core.backend.group_policy_sync import GroupPolicySync
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from tests.group_policy_signing import ISSUER, KEY, session_token

TOKEN = session_token()


def _settings(tmp_path: Path, *, poll_seconds: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        api_url=ISSUER,
        storage=SimpleNamespace(data_dir=str(tmp_path)),
        auth=SimpleNamespace(credentials_file=str(tmp_path / "credentials.json")),
        group_policy=SimpleNamespace(trusted_keys=[], required=False, poll_seconds=poll_seconds),
    )


def _portal(pack: GroupPolicyPack | None = None) -> MagicMock:
    portal = MagicMock()
    portal.fetch_group_pack = AsyncMock(return_value=pack)
    portal.fetch_jwks = AsyncMock(return_value=KEY.key_set)
    return portal


def _pack(name: str = "reviewer") -> GroupPolicyPack:
    return KEY.signed(
        GroupPolicyPack(
            group_id="g-1",
            group_name="Engineering",
            fetched_at=datetime.now(timezone.utc),
            entries=[
                GroupPolicyEntry(
                    kind="agents",
                    entry_name=name,
                    content=f"---\nname: {name}\n---\nBody.",
                    content_type="markdown",
                    enabled=True,
                )
            ],
        )
    )


def _agent(tmp_path: Path, name: str = "reviewer") -> Path:
    return tmp_path / "group-policy" / "agents" / f"{name}.md"


class TestTheHydratorByItself:
    @pytest.mark.asyncio
    async def test_a_pack_for_a_token_is_written_and_reported(self, tmp_path: Path):
        hydrator = GroupPackHydrator(_settings(tmp_path), _portal(_pack()))

        assert await hydrator.hydrate(TOKEN) is True
        assert _agent(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_no_token_asks_nobody(self, tmp_path: Path, caplog):
        portal = _portal(_pack())
        hydrator = GroupPackHydrator(_settings(tmp_path), portal)

        with caplog.at_level(logging.INFO, logger="igni_core.backend.group_pack_hydrator"):
            assert await hydrator.hydrate("") is False

        portal.fetch_group_pack.assert_not_awaited()
        portal.fetch_jwks.assert_not_awaited()
        assert "no bearer token" in caplog.text

    @pytest.mark.asyncio
    async def test_a_token_that_is_not_a_jwt_cannot_vouch_for_a_pack(self, tmp_path: Path):
        """The pack is bound to the person the token names; a token that
        names no one verifies nothing, and nothing is written."""
        hydrator = GroupPackHydrator(_settings(tmp_path), _portal(_pack()))

        assert await hydrator.hydrate("not-a-jwt") is False
        assert not _agent(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_a_pack_signed_by_somebody_else_is_not_written(self, tmp_path: Path):
        from tests.group_policy_signing import ServerKey

        stranger = ServerKey()
        unsigned = _pack().model_copy(update={"signature": None})
        hydrator = GroupPackHydrator(_settings(tmp_path), _portal(stranger.signed(unsigned)))

        assert await hydrator.hydrate(TOKEN) is False
        assert not _agent(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_a_cache_record_that_no_longer_parses_is_replaced_by_the_fetched_pack(
        self, tmp_path: Path
    ):
        meta = GroupPolicyLayout.for_data_dir(tmp_path).file(GroupPolicyLayout.META_FILE)
        meta.parent.mkdir(parents=True)
        meta.write_text("{ this is not json")
        hydrator = GroupPackHydrator(_settings(tmp_path), _portal(_pack()))

        assert await hydrator.hydrate(TOKEN) is True
        assert _agent(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_a_cache_that_cannot_be_parsed_is_a_warning_and_no_pack_landed(
        self, tmp_path: Path, caplog, monkeypatch
    ):
        from igni_core.core.config.group_policy import cache as cache_module

        def unparsable(*_a, **_k):
            raise ValueError("cache entry is not valid")

        monkeypatch.setattr(cache_module.GroupPolicyCache, "for_data_dir", unparsable)
        hydrator = GroupPackHydrator(_settings(tmp_path), _portal(_pack()))

        with caplog.at_level(logging.WARNING, logger="igni_core.backend.group_pack_hydrator"):
            assert await hydrator.hydrate(TOKEN) is False

        assert "cache entry is not valid" in caplog.text

    @pytest.mark.asyncio
    async def test_a_disk_that_cannot_be_written_is_a_warning(
        self, tmp_path: Path, caplog, monkeypatch
    ):
        from igni_core.core.config.group_policy import cache as cache_module

        def full(*_a, **_k):
            raise OSError("no space left on device")

        monkeypatch.setattr(cache_module.GroupPolicyCache, "materialize", full)
        hydrator = GroupPackHydrator(_settings(tmp_path), _portal(_pack()))

        with caplog.at_level(logging.WARNING, logger="igni_core.backend.group_pack_hydrator"):
            assert await hydrator.hydrate(TOKEN) is False

        assert "no space left on device" in caplog.text

    @pytest.mark.asyncio
    async def test_a_chosen_server_is_asked_from_then_on(self, tmp_path: Path):
        first, second = _portal(None), _portal(_pack())
        hydrator = GroupPackHydrator(_settings(tmp_path), first)

        await hydrator.hydrate(TOKEN, force=True)
        hydrator.use_portal(second)
        landed = await hydrator.hydrate(TOKEN, force=True)

        assert first.fetch_group_pack.await_count == 1
        assert second.fetch_group_pack.await_count == 1
        assert landed is True

    @pytest.mark.asyncio
    async def test_one_hydrator_serves_successive_event_loops(self, tmp_path: Path):
        """A terminal login runs its own loop each time it is used."""
        hydrator = GroupPackHydrator(_settings(tmp_path), _portal(None))
        loop_result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: asyncio.run(hydrator.hydrate(TOKEN, force=True))
        )

        assert loop_result is False
        assert await hydrator.hydrate(TOKEN, force=True) is False


class TestTheSessionsHalf:
    @pytest.mark.asyncio
    async def test_a_cold_start_with_a_stored_token_fetches_forced_and_starts_the_timer(
        self, tmp_path: Path
    ):
        portal = _portal(_pack())
        sync = GroupPolicySync(
            session=MagicMock(cloud_auth=MagicMock(access_token=TOKEN)),
            settings=_settings(tmp_path, poll_seconds=3600),
            portal=portal,
        )
        try:
            sync.start()
            assert sync.polling
            for _ in range(200):
                if _agent(tmp_path).exists():
                    break
                await asyncio.sleep(0.01)
        finally:
            sync.stop_polling()

        portal.fetch_group_pack.assert_awaited_once_with(TOKEN, None)
        assert _agent(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_a_cold_start_asks_even_when_the_cached_pack_is_fresh(self, tmp_path: Path):
        """Fresh by age is not current: an admin may have changed it since."""
        from igni_core.core.config.group_policy.cache import GroupPolicyCache

        GroupPolicyCache(GroupPolicyLayout.for_data_dir(tmp_path)).materialize(_pack("old"))
        portal = _portal(None)
        sync = GroupPolicySync(
            session=MagicMock(cloud_auth=MagicMock(access_token=TOKEN)),
            settings=_settings(tmp_path),
            portal=portal,
        )

        sync.start()
        for _ in range(200):
            if portal.fetch_group_pack.await_count:
                break
            await asyncio.sleep(0.01)

        portal.fetch_group_pack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_cold_start_with_no_token_does_nothing(self, tmp_path: Path):
        portal = _portal(_pack())
        sync = GroupPolicySync(
            session=MagicMock(cloud_auth=MagicMock(access_token=None)),
            settings=_settings(tmp_path, poll_seconds=3600),
            portal=portal,
        )

        sync.start()
        await asyncio.sleep(0.05)

        assert not sync.polling
        portal.fetch_group_pack.assert_not_awaited()

    def test_a_cold_start_with_no_running_loop_schedules_nothing(self, tmp_path: Path):
        portal = _portal(_pack())
        sync = GroupPolicySync(
            session=MagicMock(cloud_auth=MagicMock(access_token=TOKEN)),
            settings=_settings(tmp_path, poll_seconds=3600),
            portal=portal,
        )

        sync.start()

        assert not sync.polling
        portal.fetch_group_pack.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_poll_tick_without_a_token_asks_nobody(self, tmp_path: Path, monkeypatch):
        portal = _portal(_pack())
        session = MagicMock(cloud_auth=MagicMock(access_token=None))
        sync = GroupPolicySync(session, _settings(tmp_path, poll_seconds=1), portal)
        real_sleep = asyncio.sleep
        ticks = 0

        async def quick(_seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", quick)
        sync.start_polling()
        try:
            while ticks < 3:
                await real_sleep(0)
        finally:
            sync.stop_polling()

        portal.fetch_group_pack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_server_the_person_chose_is_the_one_asked(self, tmp_path: Path):
        first, second = _portal(None), _portal(_pack())
        sync = GroupPolicySync(MagicMock(), _settings(tmp_path), first)

        sync.use_portal(second)
        await sync.hydrate(TOKEN, force=True)

        first.fetch_group_pack.assert_not_awaited()
        second.fetch_group_pack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_session_s_group_agents_reload_only_when_a_pack_landed(self, tmp_path: Path):
        session = MagicMock()
        portal = _portal(_pack())
        sync = GroupPolicySync(session, _settings(tmp_path), portal)

        assert await sync.hydrate(TOKEN, force=True) is True
        session.reload_group_agents.assert_called_once_with()

        session.reload_group_agents.reset_mock()
        portal.fetch_group_pack.return_value = None
        assert await sync.hydrate(TOKEN, force=True) is False
        session.reload_group_agents.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_landed_pack_is_reported_even_when_the_agents_did_not_reload(
        self, tmp_path: Path
    ):
        session = MagicMock()
        session.reload_group_agents.return_value = False
        sync = GroupPolicySync(session, _settings(tmp_path), _portal(_pack()))

        assert await sync.hydrate(TOKEN, force=True) is True
