"""``SearchCodeResult``: the cap, the truncation flag and the timeout payload."""

from __future__ import annotations

from igni_core.backend.schemas_search import SearchCodeMatch, SearchCodeResult


def _match(n: int = 1, path: str = "a.py") -> SearchCodeMatch:
    return SearchCodeMatch(path=path, line=n, end_line=n, preview=f"line {n}")


def test_a_new_result_has_no_matches_and_is_not_cut():
    result = SearchCodeResult()
    assert result.model_dump() == {"matches": [], "truncated": False, "error": ""}


def test_appending_below_the_cap_keeps_going():
    result = SearchCodeResult()
    assert result.append(_match(1), max_results=3) is False
    assert result.append(_match(2), max_results=3) is False
    assert len(result.matches) == 2
    assert result.truncated is False


def test_the_append_that_reaches_the_cap_says_stop_and_marks_the_result_cut():
    result = SearchCodeResult()
    result.append(_match(1), max_results=2)
    assert result.append(_match(2), max_results=2) is True
    assert [m.line for m in result.matches] == [1, 2]
    assert result.truncated is True
    assert result.error == ""


def test_a_cap_of_one_cuts_at_the_first_match():
    result = SearchCodeResult()
    assert result.append(_match(), max_results=1) is True
    assert result.truncated is True


def test_a_cap_of_zero_still_keeps_the_match_it_was_handed_and_says_stop():
    result = SearchCodeResult()
    assert result.append(_match(), max_results=0) is True
    assert len(result.matches) == 1
    assert result.truncated is True


def test_a_timed_out_search_is_empty_cut_and_says_why():
    result = SearchCodeResult.timed_out()
    assert result.model_dump() == {"matches": [], "truncated": True, "error": "search timed out"}


def test_two_results_never_share_their_match_list():
    first, second = SearchCodeResult(), SearchCodeResult()
    first.append(_match(), max_results=10)
    assert second.matches == []


def test_a_match_keeps_its_unicode_path_and_preview():
    match = SearchCodeMatch(path="src/é.py", line=3, end_line=4, preview="naïve → ok")
    assert match.model_dump() == {
        "path": "src/é.py",
        "line": 3,
        "end_line": 4,
        "preview": "naïve → ok",
    }
