"""Tests for the update checker's request, cache and version edges."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.activity import ActivityRegistry
from igni_core.core.config.settings import Settings
from igni_core.core.utils import update_checker as uc
from igni_core.core.utils.update_checker import (
    PackageMetadata,
    PyPIInfoResponse,
    UpdateCache,
    UpdateCacheEntry,
    UpdateChecker,
    UpdateError,
    UpdateInfo,
    Version,
)

_PKG = PackageMetadata(name="igni-probe", project_url="https://example.test/probe")


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _Pypi:
    """Stands in for ``httpx.AsyncClient``: records how it was built and
    asked, and whether the activity registry showed the call as busy."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.timeouts: list[float] = []
        self.urls: list[str] = []
        self.busy_holders: list[list[str]] = []

    def __call__(self, *, timeout: float) -> _Pypi:
        self.timeouts.append(timeout)
        return self

    async def __aenter__(self) -> _Pypi:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str) -> _Response:
        self.urls.append(url)
        self.busy_holders.append([lease.holder for lease in ActivityRegistry.shared().busy()])
        return _Response(self.payload)


@pytest.fixture
def current(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(uc, "__version__", "0.1.0")
    return "0.1.0"


def _checker(tmp_path: Path, *, ttl: int = 86400, timeout: float = 5.0) -> UpdateChecker:
    return UpdateChecker(
        settings=Settings(update_check_ttl=ttl, air_gapped=False),
        cache_path=tmp_path / ".update-check",
        timeout=timeout,
        package=_PKG,
    )


def _fake_pypi(monkeypatch: pytest.MonkeyPatch, payload: Any) -> _Pypi:
    pypi = _Pypi(payload)
    monkeypatch.setattr(uc.httpx, "AsyncClient", pypi)
    return pypi


# ── the request ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_request_goes_to_pypi_for_the_installed_package_with_the_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    pypi = _fake_pypi(monkeypatch, {"info": {"version": "0.1.0"}})

    await _checker(tmp_path, timeout=2.5).check()

    assert pypi.urls == ["https://pypi.org/pypi/igni-probe/json"]
    assert pypi.timeouts == [2.5]


@pytest.mark.asyncio
async def test_the_request_is_held_as_busy_work_and_released_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    pypi = _fake_pypi(monkeypatch, {"info": {"version": "0.1.0"}})

    await _checker(tmp_path).check()

    assert pypi.busy_holders == [["update-check"]]
    assert ActivityRegistry.shared().busy() == []


@pytest.mark.asyncio
async def test_a_newer_release_is_reported_with_the_installed_page_as_fallback_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    _fake_pypi(monkeypatch, {"info": {"version": "0.2.0", "project_url": ""}})

    info = await _checker(tmp_path).check()

    assert info.available is True
    assert info.download_url == "https://example.test/probe"
    assert info.message == (
        "Update available: v0.1.0 → v0.2.0\n  Upgrade: https://example.test/probe"
    )


@pytest.mark.asyncio
async def test_an_empty_latest_version_is_not_an_update_and_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    _fake_pypi(monkeypatch, {"info": {"version": ""}})

    info = await _checker(tmp_path).check()

    assert info.available is False
    assert info.error is None
    assert UpdateCache(tmp_path / ".update-check").read(ttl=60) is not None


@pytest.mark.asyncio
async def test_a_prerelease_on_pypi_is_never_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    _fake_pypi(monkeypatch, {"info": {"version": "9.0.0rc1"}})

    assert (await _checker(tmp_path).check()).available is False


@pytest.mark.asyncio
async def test_an_unwritable_cache_does_not_change_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    _fake_pypi(monkeypatch, {"info": {"version": "0.2.0"}})
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed")
    checker = UpdateChecker(
        settings=Settings(update_check_ttl=86400, air_gapped=False),
        cache_path=blocker / "sub" / ".update-check",
        package=_PKG,
    )

    info = await checker.check()

    assert info.available is True
    assert info.error is None


@pytest.mark.asyncio
async def test_a_negative_ttl_turns_the_check_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    pypi = _fake_pypi(monkeypatch, {"info": {"version": "9.9.9"}})
    (tmp_path / ".update-check").write_text(
        UpdateCacheEntry(latest_version="9.9.9").model_dump_json()
    )

    info = await _checker(tmp_path, ttl=-1).check()

    assert info.available is False
    assert pypi.urls == []


@pytest.mark.asyncio
async def test_the_settings_are_loaded_when_none_are_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    pypi = _fake_pypi(monkeypatch, {"info": {"version": "9.9.9"}})
    monkeypatch.setattr(
        uc.SettingsLoader, "load", classmethod(lambda cls: Settings(air_gapped=True))
    )

    info = await UpdateChecker(cache_path=tmp_path / ".update-check", package=_PKG).check()

    assert info.available is False
    assert pypi.urls == []


# ── the cache ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fresh_cache_answers_with_its_notes_and_url_and_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    pypi = _fake_pypi(monkeypatch, {"info": {"version": "9.9.9"}})
    UpdateCache(tmp_path / ".update-check").write(
        UpdateCacheEntry(
            latest_version="0.3.0", release_notes="Faster", download_url="https://x.test/dl"
        )
    )

    info = await _checker(tmp_path).check()

    assert pypi.urls == []
    assert info == UpdateInfo(
        available=True,
        latest_version="0.3.0",
        current_version="0.1.0",
        release_notes="Faster",
        download_url="https://x.test/dl",
    )
    assert info.message == (
        "Update available: v0.1.0 → v0.3.0  (Faster)\n  Upgrade: https://x.test/dl"
    )


@pytest.mark.asyncio
async def test_a_stale_cache_is_ignored_and_pypi_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    pypi = _fake_pypi(monkeypatch, {"info": {"version": "0.5.0"}})
    (tmp_path / ".update-check").write_text(
        UpdateCacheEntry(latest_version="0.3.0", checked_at=time.time() - 1000).model_dump_json()
    )

    info = await _checker(tmp_path, ttl=60).check()

    assert pypi.urls != []
    assert info.latest_version == "0.5.0"


@pytest.mark.asyncio
async def test_a_cache_that_is_not_text_is_a_miss_and_pypi_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: str
) -> None:
    pypi = _fake_pypi(monkeypatch, {"info": {"version": "0.2.0"}})
    (tmp_path / ".update-check").write_bytes(b"\xff\xfe\x00\x80 not utf-8")

    info = await _checker(tmp_path).check()

    assert info.error is None
    assert info.available is True
    assert pypi.urls != []


def test_a_cache_that_is_not_text_reads_as_a_miss(tmp_path: Path) -> None:
    path = tmp_path / ".update-check"
    path.write_bytes(b"\xff\xfe\x00\x80")
    assert UpdateCache(path).read(ttl=60) is None


def test_a_cache_of_the_wrong_json_shape_is_a_miss(tmp_path: Path) -> None:
    path = tmp_path / ".update-check"
    path.write_text("[1, 2, 3]")
    assert UpdateCache(path).read(ttl=60) is None


def test_writing_creates_the_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / ".update-check"
    UpdateCache(path).write(UpdateCacheEntry(latest_version="1.0.0"))
    entry = UpdateCache(path).read(ttl=60)
    assert entry is not None
    assert entry.latest_version == "1.0.0"


def test_an_entry_is_fresh_up_to_and_including_its_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    assert UpdateCacheEntry(checked_at=900.0).is_fresh(100) is True
    assert UpdateCacheEntry(checked_at=899.0).is_fresh(100) is False


def test_a_cache_entry_ignores_legacy_fields_and_defaults_the_rest() -> None:
    entry = UpdateCacheEntry.model_validate({"legacy": 1, "latest_version": "1.0.0"})
    assert entry.latest_version == "1.0.0"
    assert entry.download_url == ""
    assert entry.release_notes == ""
    assert isinstance(entry.checked_at, float)
    assert UpdateCacheEntry.model_validate({}).latest_version == ""


# ── the wire models ────────────────────────────────────────────────


def test_pypis_envelope_tolerates_missing_parts_and_extra_fields() -> None:
    assert PyPIInfoResponse.model_validate({}).info.version == ""
    empty = PyPIInfoResponse.model_validate({"info": {}})
    assert (empty.info.version, empty.info.project_url) == ("", "")
    full = PyPIInfoResponse.model_validate(
        {"info": {"version": "1.2.3", "project_url": "u", "summary": "x"}, "urls": []}
    )
    assert (full.info.version, full.info.project_url) == ("1.2.3", "u")


def test_error_kinds_are_plain_strings_on_the_wire() -> None:
    assert {e.name: e.value for e in UpdateError} == {
        "NETWORK": "network",
        "HTTP_STATUS": "http_status",
        "PARSE": "parse",
        "CACHE_IO": "cache_io",
        "UNKNOWN": "unknown",
    }
    dumped = UpdateInfo(error=UpdateError.NETWORK, error_detail="x").model_dump(mode="json")
    assert dumped["error"] == "network"


def test_the_message_with_a_url_but_no_notes_and_with_an_error_set() -> None:
    info = UpdateInfo(
        available=True, latest_version="2", current_version="1", download_url="https://u"
    )
    assert info.message == "Update available: v1 → v2\n  Upgrade: https://u"
    broken = info.model_copy(update={"error": UpdateError.PARSE})
    assert broken.message == ""


# ── versions ───────────────────────────────────────────────────────


def test_versions_compare_with_less_equal_and_greater_equal() -> None:
    a, b = Version.parse("1.2.0"), Version.parse("1.10.0")
    assert a < b and a <= b and b > a and b >= a
    assert a <= Version.parse("1.2.0") and a >= Version.parse("1.2.0")
    assert not (b <= a) and not (a >= b)


def test_versions_compare_numerically_not_as_text() -> None:
    assert Version.parse("0.10.0") > Version.parse("0.9.0")


def test_a_shorter_version_is_older_than_the_same_with_a_zero_added() -> None:
    assert Version.parse("1.2") < Version.parse("1.2.0")


def test_whitespace_and_a_v_prefix_are_stripped() -> None:
    assert Version.parse("  v1.2.3\n").parts == (1, 2, 3)


@pytest.mark.parametrize("raw", ["", "1.2.3rc1", "1.0.0.dev", "1..2", "1.x", "v", "latest"])
def test_what_is_not_all_integers_collapses_to_zero(raw: str) -> None:
    assert Version.parse(raw).parts == (0,)


def test_a_non_string_collapses_to_zero() -> None:
    assert Version.parse(None).parts == (0,)  # type: ignore[arg-type]
    assert Version.parse(12).parts == (0,)  # type: ignore[arg-type]


def test_garbage_is_never_newer_than_a_release() -> None:
    assert not (Version.parse("garbage") > Version.parse("0.0.1"))


# ── the installed package ──────────────────────────────────────────


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: str) -> None:
    dist = tmp_path / "site" / "igni_probe-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(metadata)
    monkeypatch.syspath_prepend(str(tmp_path / "site"))


def test_the_package_name_and_first_homepage_or_repository_url_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        tmp_path,
        monkeypatch,
        "Metadata-Version: 2.1\nName: igni-probe\nVersion: 1.0\n"
        "Project-URL: Documentation, https://docs.test\n"
        "Project-URL: Repository, https://repo.test/x\n"
        "Project-URL: Homepage, https://home.test\n",
    )
    pkg = PackageMetadata.load("igni-probe")
    assert (pkg.name, pkg.project_url) == ("igni-probe", "https://repo.test/x")


def test_a_package_with_no_homepage_has_an_empty_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        tmp_path,
        monkeypatch,
        "Metadata-Version: 2.1\nName: igni-probe\nVersion: 1.0\n"
        "Project-URL: Documentation, https://docs.test\n",
    )
    assert PackageMetadata.load("igni-probe").project_url == ""


def test_a_url_entry_without_a_comma_is_taken_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(
        tmp_path,
        monkeypatch,
        "Metadata-Version: 2.1\nName: igni-probe\nVersion: 1.0\nProject-URL: Homepage\n",
    )
    assert PackageMetadata.load("igni-probe").project_url == "Homepage"


def test_a_distribution_that_is_not_installed_raises_when_probed() -> None:
    from importlib.metadata import PackageNotFoundError

    with pytest.raises(PackageNotFoundError):
        PackageMetadata.load("igni-no-such-distribution")


def test_the_default_probe_is_the_igni_core_distribution() -> None:
    assert PackageMetadata.load().name == "igni-core"
