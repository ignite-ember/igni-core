"""The search cache keeps at most its size, drops the least recently read first,
and a read counts as use."""

from __future__ import annotations

from igni_core.backend.schemas_search import SearchCodeMatch, SearchCodeResult
from igni_core.backend.search_cache import LruSearchCache


def _result(tag: str) -> SearchCodeResult:
    return SearchCodeResult(matches=[SearchCodeMatch(path=tag, line=1, end_line=1, preview=tag)])


def test_a_key_never_stored_is_a_miss() -> None:
    assert LruSearchCache().get("nothing") is None


def test_a_stored_result_is_given_back() -> None:
    cache = LruSearchCache()
    stored = _result("a")

    cache.put("a", stored)

    assert cache.get("a") is stored


def test_the_oldest_entry_goes_first_when_the_cache_is_full() -> None:
    cache = LruSearchCache(max_entries=2)
    for key in ("a", "b", "c"):
        cache.put(key, _result(key))

    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None


def test_reading_an_entry_saves_it_from_the_next_eviction() -> None:
    cache = LruSearchCache(max_entries=2)
    cache.put("a", _result("a"))
    cache.put("b", _result("b"))

    cache.get("a")
    cache.put("c", _result("c"))

    assert cache.get("a") is not None
    assert cache.get("b") is None


def test_writing_an_entry_again_saves_it_from_the_next_eviction() -> None:
    """A fresh value is the newest entry, not the first to be dropped."""
    cache = LruSearchCache(max_entries=2)
    cache.put("a", _result("a"))
    cache.put("b", _result("b"))

    cache.put("a", _result("a2"))
    cache.put("c", _result("c"))

    assert cache.get("a") is not None
    assert cache.get("b") is None


def test_a_cache_of_one_keeps_only_the_latest() -> None:
    cache = LruSearchCache(max_entries=1)
    cache.put("a", _result("a"))
    cache.put("b", _result("b"))

    assert cache.get("a") is None
    assert cache.get("b") is not None


def test_a_cache_of_none_remembers_nothing() -> None:
    cache = LruSearchCache(max_entries=0)
    cache.put("a", _result("a"))

    assert cache.get("a") is None


def test_the_default_holds_sixty_four_entries() -> None:
    cache = LruSearchCache()
    for i in range(65):
        cache.put(str(i), _result(str(i)))

    assert cache.get("0") is None
    assert all(cache.get(str(i)) is not None for i in range(1, 65))


def test_putting_a_key_again_replaces_its_result_and_evicts_nobody() -> None:
    cache = LruSearchCache(max_entries=2)
    cache.put("a", _result("old"))
    cache.put("b", _result("b"))
    fresh = _result("new")

    cache.put("a", fresh)

    assert cache.get("a") is fresh
    assert cache.get("b") is not None
