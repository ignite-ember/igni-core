"""Tests for tips and update checker."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from igni_core.core.config.schemas import GuardrailsConfig, PermissionsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.paths import CONFIG_DIR, PROJECT_CONTEXT_FILE
from igni_core.core.utils.tips import CONTEXTUAL_TIPS, GENERAL_TIPS, get_tip, random_tip
from igni_core.core.utils.update_checker import (
    PackageMetadata,
    UpdateCache,
    UpdateCacheEntry,
    UpdateChecker,
    UpdateError,
    UpdateInfo,
    Version,
    default_cache_path,
)

# ── Tips ────────────────────────────────────────────────────────────


class TestTips:
    def test_general_tips_not_empty(self):
        assert len(GENERAL_TIPS) > 0

    def test_contextual_tips_not_empty(self):
        assert len(CONTEXTUAL_TIPS) > 0

    def test_all_general_tips_are_strings(self):
        for tip in GENERAL_TIPS:
            assert isinstance(tip, str)
            assert len(tip) > 0

    def test_random_tip_returns_string(self):
        tip = random_tip()
        assert isinstance(tip, str)
        assert tip in GENERAL_TIPS

    def test_get_tip_no_settings(self):
        """Without settings, returns a general tip."""
        tip = get_tip()
        assert isinstance(tip, str)
        assert tip in GENERAL_TIPS

    def test_get_tip_contextual_no_project_context_file(self, tmp_path):
        """When the project context file is missing, suggests creating it."""
        settings = Settings()
        tip = get_tip(settings, tmp_path)
        # Should be one of the contextual tips (the missing file is a match)
        assert isinstance(tip, str)
        assert len(tip) > 0

    def test_that_tip_names_the_file_the_check_looks_for(self, tmp_path):
        """The tip said "create an ember.md" while its predicate read
        ``settings.context.project_file`` — so a project that had
        configured a different name was told to create the wrong file,
        and the rename would have made that true for everybody. The
        sentence is built from the setting now.

        Asserted on the tip itself rather than through ``get_tip``,
        which picks at random among every tip that matches — a test
        that went through it would pass or fail depending on the draw,
        which is worse than no test.
        """
        from igni_core.core.utils.tips import NoProjectContextTip, TipContext

        settings = Settings()
        settings.context.project_file = "TEAM_CONTEXT.md"
        ctx = TipContext(settings=settings, project_dir=tmp_path)
        tip = NoProjectContextTip()

        assert tip.matches(ctx) is True
        assert "TEAM_CONTEXT.md" in tip.render(ctx)

        (tmp_path / "TEAM_CONTEXT.md").write_text("# here")
        assert tip.matches(ctx) is False

    def test_get_tip_all_features_enabled(self, tmp_path):
        """When everything is configured, falls back to general tips."""
        (tmp_path / PROJECT_CONTEXT_FILE).write_text("# test")
        (tmp_path / CONFIG_DIR / "agents").mkdir(parents=True)
        (tmp_path / CONFIG_DIR / "agents" / "custom.md").write_text("---\nname: custom\n---")
        settings = Settings(
            guardrails=GuardrailsConfig(pii_detection=True, prompt_injection=True, moderation=True),
            reasoning=Settings().reasoning.model_copy(update={"enabled": True}),
            learning=Settings().learning.model_copy(update={"enabled": True}),
            permissions=PermissionsConfig(web_search="allow"),
        )
        tip = get_tip(settings, tmp_path)
        assert isinstance(tip, str)
        assert tip in GENERAL_TIPS


# ── Version parsing ─────────────────────────────────────────────────


class TestVersionParsing:
    def test_parse_simple(self):
        assert Version.parse("1.2.3").parts == (1, 2, 3)

    def test_parse_with_v_prefix(self):
        assert Version.parse("v0.1.0").parts == (0, 1, 0)

    def test_parse_two_parts(self):
        assert Version.parse("1.0").parts == (1, 0)

    def test_parse_invalid(self):
        assert Version.parse("abc").parts == (0,)

    def test_is_newer_true(self):
        assert Version.parse("0.2.0") > Version.parse("0.1.0")

    def test_is_newer_false_same(self):
        assert not (Version.parse("0.1.0") > Version.parse("0.1.0"))

    def test_is_newer_false_older(self):
        assert not (Version.parse("0.1.0") > Version.parse("0.2.0"))

    def test_is_newer_major(self):
        assert Version.parse("1.0.0") > Version.parse("0.9.9")

    def test_is_newer_patch(self):
        assert Version.parse("0.1.1") > Version.parse("0.1.0")


# ── UpdateInfo ──────────────────────────────────────────────────────


class TestUpdateInfo:
    def test_no_update(self):
        info = UpdateInfo(available=False, current_version="0.1.0")
        assert info.message == ""

    def test_update_available(self):
        info = UpdateInfo(
            available=True,
            latest_version="0.2.0",
            current_version="0.1.0",
            release_notes="Bug fixes",
            download_url="https://example.com",
        )
        assert "0.1.0" in info.message
        assert "0.2.0" in info.message
        assert "Bug fixes" in info.message
        assert "https://example.com" in info.message

    def test_update_no_notes(self):
        info = UpdateInfo(
            available=True,
            latest_version="0.2.0",
            current_version="0.1.0",
        )
        assert "0.2.0" in info.message

    def test_error_no_message(self):
        info = UpdateInfo(error=UpdateError.NETWORK, error_detail="timeout")
        assert info.message == ""


# ── Cache ───────────────────────────────────────────────────────────


class TestCache:
    def test_write_and_read(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        cache = UpdateCache(cache_file)
        cache.write(UpdateCacheEntry(latest_version="0.2.0"))
        result = cache.read(ttl=86400)
        assert result is not None
        assert result.latest_version == "0.2.0"

    def test_expired_cache(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        data = {"latest_version": "0.2.0", "checked_at": time.time() - 86401}
        cache_file.write_text(json.dumps(data))
        cache = UpdateCache(cache_file)
        result = cache.read(ttl=86400)
        assert result is None

    def test_missing_cache(self, tmp_path):
        cache_file = tmp_path / "nonexistent"
        cache = UpdateCache(cache_file)
        result = cache.read(ttl=86400)
        assert result is None


# ── the check ──────────────────────────────────────────────────────


class TestUpdateCheckerCheck:
    @staticmethod
    def _make_mock_client(response_data):
        """Create a mock httpx.AsyncClient that returns the given JSON."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = response_data

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    @staticmethod
    def _checker(tmp_path, **kwargs):
        """A checker with its cache in the tmp dir and settings of its own."""
        return UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=False),
            cache_path=tmp_path / ".update-check",
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_no_update(self, tmp_path):
        mock_client = self._make_mock_client({"info": {"version": "0.1.0"}})
        with (
            patch("igni_core.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            info = await self._checker(tmp_path).check()
            assert info.available is False
            assert info.current_version == "0.1.0"
            assert info.error is None

    @pytest.mark.asyncio
    async def test_update_available(self, tmp_path):
        mock_client = self._make_mock_client({"info": {"version": "0.2.0"}})
        with (
            patch("igni_core.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            info = await self._checker(tmp_path).check()
            assert info.available is True
            assert info.latest_version == "0.2.0"
            assert info.current_version == "0.1.0"

    @pytest.mark.asyncio
    async def test_the_download_url_is_pypis_own_when_it_names_one(self, tmp_path):
        mock_client = self._make_mock_client(
            {"info": {"version": "0.2.0", "project_url": "https://pypi.example/project"}}
        )
        with (
            patch("igni_core.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            info = await self._checker(tmp_path).check()
            assert info.download_url == "https://pypi.example/project"

    @pytest.mark.asyncio
    async def test_the_download_url_falls_back_to_the_installed_package(self, tmp_path):
        """PyPI does not always name a project URL; the installed
        distribution's own page is the fallback."""
        mock_client = self._make_mock_client({"info": {"version": "0.2.0"}})
        checker = self._checker(
            tmp_path,
            package=PackageMetadata(name="igni-probe", project_url="https://example.test/probe"),
        )
        with (
            patch("igni_core.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            info = await checker.check()
            assert info.download_url == "https://example.test/probe"

    @pytest.mark.asyncio
    async def test_a_check_that_asked_pypi_caches_the_answer(self, tmp_path):
        """The next session reads the answer off the disk instead of
        asking PyPI again."""
        mock_client = self._make_mock_client({"info": {"version": "0.2.0"}})
        with (
            patch("igni_core.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            await self._checker(tmp_path).check()

        cached = UpdateCache(tmp_path / ".update-check").read(ttl=86400)
        assert cached is not None
        assert cached.latest_version == "0.2.0"

    @pytest.mark.asyncio
    async def test_network_error(self, tmp_path):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("timeout"))

        with (
            patch("igni_core.core.utils.update_checker.__version__", "0.1.0"),
            patch(
                "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            info = await self._checker(tmp_path).check()
            assert info.available is False
            assert info.error is not None

    @pytest.mark.asyncio
    async def test_uses_cache(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        cached_data = {
            "latest_version": "0.3.0",
            "release_notes": "Cached",
            "download_url": "",
            "checked_at": time.time(),
        }
        cache_file.write_text(json.dumps(cached_data))

        with patch("igni_core.core.utils.update_checker.__version__", "0.1.0"):
            # Should NOT hit the network — cache is fresh
            info = await self._checker(tmp_path).check()
            assert info.available is True
            assert info.latest_version == "0.3.0"


class TestUpdateCheckerBoundaries:
    """The checker's own edges: the switch that turns it off, the
    package it asks about, and the promise that it never raises."""

    @pytest.mark.asyncio
    async def test_a_ttl_of_zero_turns_the_check_off(self, tmp_path):
        checker = UpdateChecker(
            settings=Settings(update_check_ttl=0, air_gapped=False),
            cache_path=tmp_path / ".update-check",
        )
        with patch("igni_core.core.utils.update_checker.httpx.AsyncClient") as client:
            info = await checker.check()
        assert client.called is False
        assert info.available is False
        assert info.error is None

    @pytest.mark.asyncio
    async def test_an_air_gapped_install_asks_nobody(self, tmp_path):
        checker = UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=True),
            cache_path=tmp_path / ".update-check",
        )
        with patch("igni_core.core.utils.update_checker.httpx.AsyncClient") as client:
            info = await checker.check()
        assert client.called is False
        assert info.available is False

    @pytest.mark.asyncio
    async def test_the_package_asked_about_is_the_one_installed(self, tmp_path):
        checker = UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=False),
            cache_path=tmp_path / ".update-check",
            package=PackageMetadata(name="igni-core", project_url="https://example.test"),
        )
        assert checker.package.name == "igni-core"

    @pytest.mark.asyncio
    async def test_a_failure_the_check_does_not_know_comes_back_as_unknown(self, tmp_path):
        """A bug inside the check is reported, not raised: the callers
        of the banner and the RPC treat a raised check as a crash."""
        checker = UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=False),
            cache_path=tmp_path / ".update-check",
        )
        with patch.object(
            UpdateChecker,
            "_from_pypi",
            side_effect=RuntimeError("something broke"),
        ):
            info = await checker.check()
        assert info.error is UpdateError.UNKNOWN
        assert "something broke" in (info.error_detail or "")

    @pytest.mark.asyncio
    async def test_a_cancellation_is_not_a_failed_check(self, tmp_path):
        checker = UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=False),
            cache_path=tmp_path / ".update-check",
        )
        with (
            patch.object(UpdateChecker, "_from_pypi", side_effect=asyncio.CancelledError),
            pytest.raises(asyncio.CancelledError),
        ):
            await checker.check()

    @pytest.mark.asyncio
    async def test_a_cached_version_that_is_not_newer_says_nothing_is_newer(self, tmp_path):
        cache_file = tmp_path / ".update-check"
        cache_file.write_text(json.dumps({"latest_version": "0.0.1", "checked_at": time.time()}))
        checker = UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=False),
            cache_path=tmp_path / ".update-check",
        )
        with patch("igni_core.core.utils.update_checker.__version__", "0.1.0"):
            info = await checker.check()
            assert info.available is False
            assert info.error is None


class TestUpdateCheckerPyPIFailures:
    """PyPI can fail four ways. Each has its own answer, so the
    caller can tell a rate limit from a dead network."""

    @staticmethod
    def _client(response_data=None, *, raises=None, json_raises=None, status_raises=None):
        """A mock httpx client whose ``get`` behaves as asked."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(side_effect=status_raises)
        mock_response.json.side_effect = json_raises
        mock_response.json.return_value = response_data

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=raises, return_value=mock_response)
        return mock_client

    @staticmethod
    def _checker(tmp_path, **kwargs):
        return UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=False),
            cache_path=tmp_path / ".update-check",
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_an_http_status_error_is_reported_as_such(self, tmp_path):
        request = httpx.Request("GET", "https://pypi.org/pypi/igni-core/json")
        status = httpx.HTTPStatusError(
            "server said no",
            request=request,
            response=httpx.Response(503, request=request),
        )
        mock_client = self._client(status_raises=status)
        with patch(
            "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
        ):
            info = await self._checker(tmp_path).check()
        assert info.error is UpdateError.HTTP_STATUS
        assert "server said no" in (info.error_detail or "")

    @pytest.mark.asyncio
    async def test_a_network_failure_is_reported_as_a_network_failure(self, tmp_path):
        mock_client = self._client(raises=httpx.ReadTimeout("timeout"))
        with patch(
            "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
        ):
            info = await self._checker(tmp_path).check()
        assert info.error is UpdateError.NETWORK
        assert info.available is False

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_reported_as_a_parse_failure(self, tmp_path):
        mock_client = self._client(json_raises=ValueError("not json"))
        with patch(
            "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
        ):
            info = await self._checker(tmp_path).check()
        assert info.error is UpdateError.PARSE

    @pytest.mark.asyncio
    async def test_a_body_of_the_wrong_shape_is_reported_as_a_parse_failure(self, tmp_path):
        """PyPI's envelope has an ``info`` object. If that ever comes
        back as something else, the answer is a parse failure — not
        a crash out of the check."""
        mock_client = self._client({"info": "not an object"})
        with patch(
            "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=mock_client
        ):
            info = await self._checker(tmp_path).check()
        assert info.error is UpdateError.PARSE


class TestUpdateCacheFailures:
    """A broken cache is a cache miss, not a broken check."""

    def test_a_cache_file_that_cannot_be_read_is_a_miss(self, tmp_path):
        # A directory where a file is expected: the read fails.
        assert UpdateCache(tmp_path).read(ttl=60) is None

    def test_a_cache_file_that_cannot_be_parsed_is_a_miss(self, tmp_path):
        path = tmp_path / ".update-check"
        path.write_text("this is not json")
        assert UpdateCache(path).read(ttl=60) is None

    def test_a_cache_that_cannot_be_written_is_swallowed(self, tmp_path):
        """A full disk or a read-only home must not break the check."""
        UpdateCache(tmp_path).write(UpdateCacheEntry(latest_version="9.9.9"))

    def test_a_written_entry_is_fresh_however_old_it_was(self, tmp_path):
        """The TTL is measured from when the entry was written, not
        from when it was built — otherwise an entry built long ago
        and written now is born stale."""
        path = tmp_path / ".update-check"
        UpdateCache(path).write(
            UpdateCacheEntry(latest_version="9.9.9", checked_at=time.time() - 10_000)
        )
        entry = UpdateCache(path).read(ttl=60)
        assert entry is not None
        assert entry.latest_version == "9.9.9"


class TestUpdateCheckerPackage:
    """The installed package is probed once, when it is asked for."""

    def test_the_default_cache_path_is_in_the_config_directory(self):
        assert default_cache_path() == Path.home() / CONFIG_DIR / ".update-check"

    def test_the_package_is_probed_only_when_it_is_asked_for(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        calls: list[str] = []

        def _load(cls: type[PackageMetadata]) -> PackageMetadata:
            calls.append(cls.__name__)
            return PackageMetadata(name="igni-probe", project_url="https://example.test/probe")

        monkeypatch.setattr(PackageMetadata, "load", classmethod(_load))

        checker = UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=False),
            cache_path=tmp_path / ".update-check",
        )
        assert calls == []

        assert checker.package.name == "igni-probe"
        assert checker.package.name == "igni-probe"
        assert calls == ["PackageMetadata"]

    def test_an_injected_package_is_the_one_asked_about(self, tmp_path):
        checker = UpdateChecker(
            settings=Settings(update_check_ttl=86400, air_gapped=False),
            cache_path=tmp_path / ".update-check",
            package=PackageMetadata(name="igni-probe-package"),
        )
        assert checker.package.name == "igni-probe-package"
