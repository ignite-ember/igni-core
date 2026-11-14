"""The plugin models: roots, source resolution, registry merge rules, wire shapes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from igni_core.core.plugins.models import (
    InstallRef,
    MarketplaceCatalog,
    MarketplaceEntry,
    MarketplaceInfo,
    MarketplacePluginEntry,
    MarketplacePluginInfo,
    MarketplaceRegistry,
    PluginDefinition,
    PluginInfo,
    PluginManifest,
    PluginRoot,
    PluginSource,
)

_MKT = "https://example.com/mkt"
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _entry(source: object = None, **extra: object) -> MarketplacePluginEntry:
    return MarketplacePluginEntry.model_validate({"name": "p", "source": source, **extra})


# ── Plugin roots and definitions ───────────────────────────────────


def test_plugin_roots_are_named_for_igni_and_claude() -> None:
    assert get_args(PluginRoot) == (
        "user-claude",
        "user-igni",
        "project-claude",
        "project-igni",
        "group-policy-igni",
        "managed-claude",
        "managed-igni",
    )
    with pytest.raises(ValidationError):
        PluginSource(root="user-ember", path=Path("."), priority=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "root, managed",
    [
        ("managed-claude", True),
        ("managed-igni", True),
        ("user-claude", False),
        ("user-igni", False),
        ("project-claude", False),
        ("project-igni", False),
        ("group-policy-igni", False),
    ],
)
def test_only_the_sysadmin_roots_are_managed(root: str, managed: bool, tmp_path: Path) -> None:
    plugin = PluginDefinition(
        manifest=PluginManifest(name="acme"),
        source=PluginSource(root=root, path=tmp_path, priority=1),  # type: ignore[arg-type]
    )
    assert (plugin.name, plugin.is_managed, plugin.root_path) == ("acme", managed, tmp_path)


def test_a_manifest_needs_only_a_name_and_keeps_unknown_fields() -> None:
    manifest = PluginManifest.model_validate({"name": "a", "futureField": [1]})
    assert manifest.version is None and manifest.author is None
    assert manifest.model_dump()["futureField"] == [1]
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({"version": "1"})


# ── Source resolution ──────────────────────────────────────────────


def test_no_source_is_unresolvable() -> None:
    assert _entry(None).resolved_source(_MKT) is None


def test_a_bare_string_is_a_url_pinned_to_the_entry_branch() -> None:
    resolved = _entry("git@h:o/r.git", branch="dev").resolved_source(_MKT)
    assert resolved is not None
    assert (resolved.kind, resolved.url, resolved.subdir, resolved.ref) == (
        "url",
        "git@h:o/r.git",
        None,
        "dev",
    )


@pytest.mark.parametrize(
    "fields, ref",
    [
        ({"sha": "abc1234", "ref": "v1"}, "abc1234"),
        ({"ref": "v1"}, "v1"),
        ({}, "dev"),
    ],
)
def test_an_object_source_prefers_sha_then_ref_then_the_entry_branch(
    fields: dict, ref: str
) -> None:
    for shape in (
        {"source": "url", "url": "https://h/r", **fields},
        {"source": "git-subdir", "url": "https://h/r", "path": "d", **fields},
        {"url": "https://h/r", **fields},  # no discriminator: the dict fallback
    ):
        resolved = _entry(shape, branch="dev").resolved_source(_MKT)
        assert resolved is not None
        assert resolved.ref == ref, shape


def test_a_dict_source_without_a_url_is_unresolvable() -> None:
    assert _entry({"source": "mystery", "path": "x"}).resolved_source(_MKT) is None
    assert _entry({"url": ""}).resolved_source(_MKT) is None


def test_the_resolved_source_is_frozen() -> None:
    resolved = _entry("https://h/r").resolved_source(_MKT)
    assert resolved is not None
    with pytest.raises(ValidationError):
        resolved.url = "x"  # type: ignore[misc]


def test_a_catalog_keeps_an_extensions_array_it_does_not_use() -> None:
    catalog = MarketplaceCatalog.model_validate(
        {"name": "m", "extensions": [{"name": "e"}], "plugins": [{"name": "p"}]}
    )
    assert catalog.model_dump()["extensions"] == [{"name": "e"}]
    assert [p.name for p in catalog.plugins] == ["p"]
    assert catalog.description is None


# ── Fetch timestamps ───────────────────────────────────────────────


def _cached(stamp: str | None) -> MarketplaceEntry:
    return MarketplaceEntry(
        name="m", url=_MKT, cached=MarketplaceCatalog(name="m"), last_fetched=stamp
    )


def test_mark_fetched_stamps_utc_to_the_second() -> None:
    entry = MarketplaceEntry(name="m", url=_MKT)
    catalog = MarketplaceCatalog(name="m", plugins=[MarketplacePluginEntry(name="p")])
    entry.mark_fetched(catalog)
    assert entry.cached is catalog and entry.plugin_count == 1
    assert entry.last_fetched is not None
    stamp = datetime.fromisoformat(entry.last_fetched)
    assert stamp.utcoffset() == timedelta(0) and stamp.microsecond == 0
    assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 60


def test_an_entry_never_fetched_has_no_plugins() -> None:
    assert MarketplaceEntry(name="m", url=_MKT).plugin_count == 0


def test_update_from_catalog_points_the_entry_at_the_new_url_and_catalog() -> None:
    entry = _cached("2000-01-01T00:00:00+00:00")
    fresh = MarketplaceCatalog(name="m", plugins=[MarketplacePluginEntry(name="p")])
    entry.update_from_catalog("https://moved/mkt", fresh)
    assert entry.url == "https://moved/mkt" and entry.cached is fresh
    assert entry.last_fetched != "2000-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    "stamp, seconds, fresh",
    [
        ("2026-01-01T11:59:00+00:00", 120, True),
        ("2026-01-01T11:59:00+00:00", 60, False),  # exactly as old as the limit: stale
        ("2026-01-01T11:59:00+00:00", 59, False),
        ("2026-01-01T11:59:00", 120, True),  # naive counts as UTC
        ("2026-01-01T13:00:00+00:00", 1, True),  # from the future
        ("2026-01-01T12:00:00+00:00", 0, False),
        ("2026-01-01T11:00:00+00:00", -5, False),
        ("not a date", 10**9, False),
        ("", 10**9, False),
        (None, 10**9, False),
    ],
)
def test_fetched_within_judges_the_stamp(stamp: str | None, seconds: float, fresh: bool) -> None:
    assert _cached(stamp).fetched_within(seconds, now=_NOW) is fresh


def test_fetched_within_is_false_without_a_cached_catalog() -> None:
    entry = MarketplaceEntry(name="m", url=_MKT, last_fetched="2026-01-01T11:59:59+00:00")
    assert entry.fetched_within(10**9, now=_NOW) is False


def test_fetched_within_defaults_to_the_real_clock() -> None:
    entry = MarketplaceEntry(name="m", url=_MKT)
    entry.mark_fetched(MarketplaceCatalog(name="m"))
    assert entry.fetched_within(3600)
    assert not entry.fetched_within(-1)


# ── Registry rules ─────────────────────────────────────────────────


def _reg_entry(url: str = _MKT, added_by: str | None = None, cached: bool = True):
    return MarketplaceEntry(
        name="m",
        url=url,
        added_by=added_by,  # type: ignore[arg-type]
        cached=MarketplaceCatalog(name="m") if cached else None,
    )


def test_adding_a_new_name_appends_it() -> None:
    registry = MarketplaceRegistry()
    entry = _reg_entry()
    assert registry.add(entry) is entry
    assert registry.find("m") is entry and registry.find("other") is None


@pytest.mark.parametrize(
    "existing, incoming, expected",
    [
        ("user", "default", "user"),  # a person's registration outlives the default's
        ("user", None, "user"),
        ("user", "user", "user"),
        ("default", "user", "user"),
        ("default", "default", "default"),
        ("default", None, "default"),
        (None, "default", "default"),
        (None, "user", "user"),
        (None, None, None),
    ],
)
def test_readding_merges_who_added_it(
    existing: str | None, incoming: str | None, expected: str | None
) -> None:
    registry = MarketplaceRegistry()
    registry.add(_reg_entry(added_by=existing))
    effective = registry.add(_reg_entry(url="https://moved/mkt", added_by=incoming))
    assert effective.added_by == expected
    assert effective.url == "https://moved/mkt"
    assert len(registry.marketplaces) == 1 and registry.marketplaces[0] is effective


def test_readding_updates_the_entry_in_place_and_refreshes_its_cache() -> None:
    registry = MarketplaceRegistry()
    first = registry.add(_reg_entry())
    registry.add(MarketplaceEntry(name="other", url="https://o"))
    first.last_fetched = "2000-01-01T00:00:00+00:00"
    again = registry.add(_reg_entry(url="https://moved/mkt"))
    assert again is first and registry.marketplaces[0] is first
    assert first.last_fetched != "2000-01-01T00:00:00+00:00"


def test_readding_without_a_cache_keeps_the_old_one() -> None:
    registry = MarketplaceRegistry()
    first = registry.add(_reg_entry())
    registry.add(_reg_entry(cached=False))
    assert first.cached is not None


def test_remove_reports_whether_anything_went() -> None:
    registry = MarketplaceRegistry()
    registry.add(_reg_entry())
    assert registry.remove("m") is True
    assert registry.remove("m") is False
    assert registry.marketplaces == []


def test_the_first_plugin_of_a_name_wins() -> None:
    registry = MarketplaceRegistry()
    catalog = MarketplaceCatalog(
        name="m",
        plugins=[
            MarketplacePluginEntry(name="p", source="https://first"),
            MarketplacePluginEntry(name="p", source="https://second"),
        ],
    )
    entry = MarketplaceEntry(name="m", url=_MKT, cached=catalog)
    registry.add(entry)
    resolved = registry.resolve_install_ref(InstallRef(marketplace="m", plugin="p"))
    assert resolved is not None and resolved[0].url == "https://first"


@pytest.mark.parametrize(
    "ref",
    ["@nope/p", "@m/missing", "@m/nosource", "@m/bad"],
)
def test_a_ref_that_cannot_be_resolved_is_none(ref: str) -> None:
    registry = MarketplaceRegistry()
    registry.add(
        MarketplaceEntry(
            name="m",
            url=_MKT,
            cached=MarketplaceCatalog(
                name="m",
                plugins=[
                    MarketplacePluginEntry(name="p", source="https://h/p"),
                    MarketplacePluginEntry(name="nosource"),
                    MarketplacePluginEntry.model_validate({"name": "bad", "source": {"x": 1}}),
                ],
            ),
        )
    )
    registry.add(MarketplaceEntry(name="uncached", url=_MKT))
    parsed = InstallRef.parse(ref)
    assert parsed is not None
    assert registry.resolve_install_ref(parsed) is None
    ok = InstallRef.parse("@m/p")
    assert ok is not None and registry.resolve_install_ref(ok) is not None
    uncached = InstallRef.parse("@uncached/p")
    assert uncached is not None and registry.resolve_install_ref(uncached) is None


@pytest.mark.parametrize(
    "ref, parsed",
    [
        ("@m/p", ("m", "p")),
        ("@m/a/b", ("m", "a/b")),
        ("@mü/pé", ("mü", "pé")),
        ("m/p", None),
        ("@m", None),
        ("@/p", None),
        ("@m/", None),
        ("@", None),
        ("", None),
        ("https://h/r", None),
    ],
)
def test_an_install_ref_is_at_marketplace_slash_plugin(
    ref: str, parsed: tuple[str, str] | None
) -> None:
    result = InstallRef.parse(ref)
    assert (result and (result.marketplace, result.plugin)) == parsed


# ── Wire shapes ────────────────────────────────────────────────────


def test_the_panel_models_describe_every_field_for_outside_developers() -> None:
    for model in (PluginInfo, MarketplacePluginInfo, MarketplaceInfo):
        for name, field in model.model_fields.items():
            assert field.description and field.description.strip(), f"{model.__name__}.{name}"


def test_plugin_info_defaults_are_empty_and_enabled() -> None:
    info = PluginInfo(name="p")
    assert (info.version, info.description, info.source_root, info.path, info.pin) == ("",) * 5
    assert info.enabled is True and info.managed is False
    assert not any(
        getattr(info, f"has_{k}")
        for k in ["skills", "agents", "hooks", "mcp", "tools", "lsp", "monitors"]
    )


@pytest.mark.parametrize(
    "source, expected",
    [
        ("https://h/r", "https://h/r"),
        (
            {"source": "git-subdir", "url": "https://h/r", "path": "sub/dir"},
            "https://h/r [sub/dir]",
        ),
        ("./plugins/x", f"{_MKT} [plugins/x]"),
        ({"source": "url", "url": "https://h/r"}, "https://h/r"),
        ({"weird": 1}, "{'weird': 1}"),
        (None, ""),
    ],
)
def test_a_listed_plugin_shows_where_it_is_fetched_from(source: object, expected: str) -> None:
    info = MarketplacePluginInfo.from_catalog_entry(
        _entry(source, description="d", version="1.2", branch="b"), _MKT
    )
    assert (info.name, info.source) == ("p", expected)
    assert (info.description, info.version, info.branch) == ("d", "1.2", "b")


def test_a_listed_plugin_without_metadata_shows_empty_strings() -> None:
    info = MarketplacePluginInfo.from_catalog_entry(_entry("https://h/r"), _MKT)
    assert (info.description, info.version, info.branch) == ("", "", "")


def test_a_marketplace_projects_its_cache_into_the_panel_shape() -> None:
    entry = MarketplaceEntry(
        name="m",
        url=_MKT,
        last_fetched="2026-01-01T00:00:00+00:00",
        cached=MarketplaceCatalog(
            name="m", plugins=[MarketplacePluginEntry(name="p", source="https://h/r")]
        ),
    )
    info = MarketplaceInfo.from_entry(entry)
    assert (info.name, info.url, info.last_fetched) == ("m", _MKT, "2026-01-01T00:00:00+00:00")
    assert [(p.name, p.source) for p in info.plugins] == [("p", "https://h/r")]


def test_a_marketplace_never_fetched_has_no_plugins_and_no_timestamp() -> None:
    info = MarketplaceInfo.from_entry(MarketplaceEntry(name="m", url=_MKT))
    assert (info.last_fetched, info.plugins) == ("", [])
