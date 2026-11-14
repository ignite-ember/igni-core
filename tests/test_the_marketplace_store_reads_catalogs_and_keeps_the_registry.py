"""The marketplace store: where a catalog is read from, and what stays on disk.

Driven through a stand-in git client that writes a repository into the
clone directory, so the fetcher's own lookup (canonical location, root
fallback), its failure reasons and its cleanup are what is under test,
and through real files for the registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.plugins.git import GitError
from igni_core.core.plugins.marketplace_store import (
    DEFAULT_MARKETPLACES,
    CatalogFetchResult,
    MarketplaceRegistryStore,
)
from igni_core.core.plugins.models import MarketplaceCatalog

_CATALOG = {"name": "acme", "plugins": [{"name": "p", "source": "https://h/p"}]}


class _FakeGit:
    """Writes ``files`` into the clone directory, or fails like git does."""

    def __init__(self, files: dict[str, str] | None = None, error: Exception | None = None):
        self.files = files or {}
        self.error = error
        self.clones: list[tuple[str, Path]] = []

    def clone(self, url: str, dest: Path, **kwargs: object) -> None:
        self.clones.append((url, dest))
        if self.error is not None:
            raise self.error
        for rel, text in self.files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


def _store(tmp_path: Path, git: _FakeGit) -> MarketplaceRegistryStore:
    return MarketplaceRegistryStore(tmp_path / "data", git_client=git)  # type: ignore[arg-type]


def test_the_catalog_is_read_from_the_claude_plugin_folder_first(tmp_path: Path) -> None:
    git = _FakeGit(
        {
            ".claude-plugin/marketplace.json": json.dumps(_CATALOG),
            "marketplace.json": json.dumps({"name": "other"}),
        }
    )
    result = _store(tmp_path, git).fetch_catalog("https://h/m")
    assert result.ok and result.catalog is not None
    assert result.catalog.name == "acme"
    assert [p.name for p in result.catalog.plugins] == ["p"]


def test_the_catalog_falls_back_to_the_repository_root(tmp_path: Path) -> None:
    git = _FakeGit({"marketplace.json": json.dumps(_CATALOG)})
    result = _store(tmp_path, git).fetch_catalog("https://h/m")
    assert result.ok and result.catalog is not None and result.catalog.name == "acme"


def test_a_repository_without_a_catalog_says_where_it_looked(tmp_path: Path) -> None:
    result = _store(tmp_path, _FakeGit({"README.md": "x"})).fetch_catalog("https://h/m")
    assert not result.ok and result.catalog is None
    assert result.reason == (
        "No marketplace.json found at https://h/m — checked "
        ".claude-plugin/marketplace.json and root."
    )


@pytest.mark.parametrize(
    "text",
    ["not json", '{"description": "no name"}', '{"name": 3}'],
)
def test_an_unreadable_catalog_is_a_failed_result_with_a_reason(tmp_path: Path, text: str) -> None:
    result = _store(tmp_path, _FakeGit({"marketplace.json": text})).fetch_catalog("https://h/m")
    assert not result.ok and result.catalog is None
    assert result.reason


def test_a_catalog_that_is_not_utf8_is_a_failed_result(tmp_path: Path) -> None:
    class _Latin(_FakeGit):
        def clone(self, url: str, dest: Path, **kwargs: object) -> None:
            dest.mkdir(parents=True)
            (dest / "marketplace.json").write_bytes(b'{"name": "caf\xe9"}')

    result = _store(tmp_path, _Latin()).fetch_catalog("https://h/m")
    assert not result.ok and result.reason


def test_a_failed_clone_is_a_result_carrying_gits_message(tmp_path: Path) -> None:
    git = _FakeGit(error=GitError("fatal: repository not found"))
    result = _store(tmp_path, git).fetch_catalog("https://h/m")
    assert (result.ok, result.reason) == (False, "fatal: repository not found")


@pytest.mark.parametrize(
    "files, error",
    [
        ({"marketplace.json": json.dumps(_CATALOG)}, None),
        ({}, None),
        ({"marketplace.json": "{"}, None),
        ({}, GitError("boom")),
    ],
)
def test_the_clone_is_deleted_whatever_happened(
    tmp_path: Path, files: dict[str, str], error: Exception | None
) -> None:
    git = _FakeGit(files, error)
    _store(tmp_path, git).fetch_catalog("https://h/m")
    [(url, dest)] = git.clones
    assert url == "https://h/m"
    assert not dest.exists()
    assert not dest.parent.exists()


def test_the_clone_lives_in_a_temporary_directory_named_for_igni(tmp_path: Path) -> None:
    git = _FakeGit({"marketplace.json": json.dumps(_CATALOG)})
    _store(tmp_path, git).fetch_catalog("https://h/m")
    [(_, dest)] = git.clones
    assert dest.parent.name.startswith("igni_mkt_")
    assert dest.name == "repo"


def test_unwrap_returns_the_catalog_or_raises_the_reason() -> None:
    catalog = MarketplaceCatalog(name="acme")
    assert CatalogFetchResult(ok=True, catalog=catalog).unwrap() is catalog
    with pytest.raises(ValueError, match="no luck"):
        CatalogFetchResult(ok=False, reason="no luck").unwrap()
    with pytest.raises(ValueError, match="^catalog fetch failed$"):
        CatalogFetchResult(ok=False).unwrap()
    with pytest.raises(ValueError, match="^catalog fetch failed$"):
        CatalogFetchResult(ok=True).unwrap()


def test_the_default_marketplace_is_anthropics_official_directory() -> None:
    assert DEFAULT_MARKETPLACES == (
        (
            "claude-plugins-official",
            "https://github.com/anthropics/claude-plugins-official",
        ),
    )


# ── Registry file ──────────────────────────────────────────────────


def test_the_registry_path_follows_home_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MarketplaceRegistryStore("~/.igni-test")
    monkeypatch.setenv("HOME", str(tmp_path / "a"))
    first = store.path()
    monkeypatch.setenv("HOME", str(tmp_path / "b"))
    assert first == tmp_path / "a" / ".igni-test" / "marketplaces.json"
    assert store.path() == tmp_path / "b" / ".igni-test" / "marketplaces.json"


def test_saving_creates_the_folder_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    store = MarketplaceRegistryStore(tmp_path / "deep" / "dir")
    store.save(store.load())
    assert sorted(p.name for p in (tmp_path / "deep" / "dir").iterdir()) == ["marketplaces.json"]


def test_a_stale_temporary_file_does_not_stop_a_save(tmp_path: Path) -> None:
    (tmp_path / "marketplaces.json.tmp").write_text("garbage from a crash")
    git = _FakeGit({"marketplace.json": json.dumps(_CATALOG)})
    store = MarketplaceRegistryStore(tmp_path, git_client=git)  # type: ignore[arg-type]
    store.add("https://h/m")
    assert [m.name for m in store.load().marketplaces] == ["acme"]
    assert not (tmp_path / "marketplaces.json.tmp").exists()


@pytest.mark.parametrize("text", ["{", '{"marketplaces": 3}', "[]"])
def test_a_corrupt_registry_loads_empty_and_says_so(
    tmp_path: Path, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "marketplaces.json").write_text(text)
    with caplog.at_level("WARNING"):
        assert MarketplaceRegistryStore(tmp_path).load().marketplaces == []
    assert "Failed to parse marketplace registry" in caplog.text


# ── add / refresh ──────────────────────────────────────────────────


def test_add_names_the_entry_from_its_catalog_and_records_who_added_it(tmp_path: Path) -> None:
    git = _FakeGit({"marketplace.json": json.dumps(_CATALOG)})
    store = _store(tmp_path, git)
    entry = store.add("https://h/some-repo-name")
    assert (entry.name, entry.url, entry.added_by) == ("acme", "https://h/some-repo-name", "user")
    assert entry.plugin_count == 1 and entry.last_fetched
    assert store.load().find("acme").added_by == "user"
    assert store.add("https://h/y", added_by="default").added_by == "user"


def test_add_that_cannot_read_a_catalog_raises_and_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path, _FakeGit(error=GitError("fatal: nope")))
    with pytest.raises(ValueError, match="fatal: nope"):
        store.add("https://h/m")
    assert not store.path().exists()


def test_refresh_that_fails_raises_and_leaves_the_registry_as_it_was(tmp_path: Path) -> None:
    good = _FakeGit({"marketplace.json": json.dumps(_CATALOG)})
    _store(tmp_path, good).add("https://h/m")
    before = (tmp_path / "data" / "marketplaces.json").read_text()

    broken = _store(tmp_path, _FakeGit(error=GitError("fatal: gone")))
    with pytest.raises(ValueError, match="fatal: gone"):
        broken.refresh("acme")
    assert (tmp_path / "data" / "marketplaces.json").read_text() == before


def test_refresh_of_an_unknown_name_is_none_and_remove_of_one_is_false(tmp_path: Path) -> None:
    store = _store(tmp_path, _FakeGit())
    assert store.refresh("nope") is None
    assert store.remove("nope") is False
    assert not store.path().exists()


def test_refresh_all_keeps_what_succeeded_beside_what_failed(tmp_path: Path) -> None:
    class _PerUrl(_FakeGit):
        def clone(self, url: str, dest: Path, **kwargs: object) -> None:
            if url.endswith("/bad"):
                raise GitError("fatal: bad")
            name = "good" if url.endswith("/good") else "good2"
            (dest).mkdir(parents=True)
            (dest / "marketplace.json").write_text(
                json.dumps({"name": name, "plugins": [{"name": f"{name}-plugin"}]})
            )

    store = MarketplaceRegistryStore(tmp_path, git_client=_PerUrl())  # type: ignore[arg-type]
    store.add("https://h/good")
    store.add("https://h/good2")
    registry = store.load()
    registry.marketplaces[0].cached = None  # forces a visible change on refresh
    registry.marketplaces[0].last_fetched = None
    registry.marketplaces.insert(
        1, registry.marketplaces[0].model_copy(update={"name": "bad", "url": "https://h/bad"})
    )
    store.save(registry)

    outcomes = store.refresh_all()
    assert [(o.name, o.ok, o.reason) for o in outcomes] == [
        ("good", True, ""),
        ("bad", False, "fatal: bad"),
        ("good2", True, ""),
    ]
    saved = store.load()
    assert saved.find("good").plugin_count == 1 and saved.find("good").last_fetched
    assert saved.find("bad").url == "https://h/bad"


def test_refresh_all_on_an_empty_registry_reports_nothing(tmp_path: Path) -> None:
    assert _store(tmp_path, _FakeGit()).refresh_all() == []


def test_a_default_registration_is_recorded_as_the_defaults(tmp_path: Path) -> None:
    git = _FakeGit({"marketplace.json": json.dumps(_CATALOG)})
    store = _store(tmp_path, git)
    assert store.add("https://h/m", added_by="default").added_by == "default"
    assert store.load().find("acme").added_by == "default"
    # A person adding the same marketplace afterwards takes it over.
    assert store.add("https://h/m").added_by == "user"
    assert store.load().find("acme").added_by == "user"
