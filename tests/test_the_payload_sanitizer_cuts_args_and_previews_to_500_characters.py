"""PayloadSanitizer: what a hook subprocess is sent of a tool's args and result."""

from __future__ import annotations

from pathlib import Path

from igni_core.core.hooks.payload_sanitizer import PayloadSanitizer


def test_safe_args_of_nothing_is_empty():
    assert PayloadSanitizer.safe_args({}) == {}


def test_safe_args_keeps_a_value_of_500_characters_and_cuts_one_of_501():
    safe = PayloadSanitizer.safe_args({"a": "x" * 500, "b": "y" * 501, "c": "z" * 5000})

    assert safe == {"a": "x" * 500, "b": "y" * 500, "c": "z" * 500}


def test_safe_args_stringifies_every_value_and_keeps_the_keys():
    safe = PayloadSanitizer.safe_args(
        {"n": None, "i": 7, "p": Path("/a/b"), "d": {"k": 1}, "l": [1, 2], "b": b"hi", "t": True}
    )

    assert safe == {
        "n": "None",
        "i": "7",
        "p": "/a/b",
        "d": "{'k': 1}",
        "l": "[1, 2]",
        "b": "b'hi'",
        "t": "True",
    }


def test_safe_args_counts_characters_not_bytes():
    safe = PayloadSanitizer.safe_args({"u": "é" * 600})

    assert safe["u"] == "é" * 500


def test_safe_args_cuts_the_string_form_of_a_long_non_string():
    safe = PayloadSanitizer.safe_args({"l": list(range(1000))})

    assert safe["l"] == str(list(range(1000)))[:500]


def test_preview_of_none_is_empty_but_of_other_falsy_results_is_their_text():
    assert PayloadSanitizer.preview(None) == ""
    assert PayloadSanitizer.preview("") == ""
    assert PayloadSanitizer.preview(0) == "0"
    assert PayloadSanitizer.preview(False) == "False"
    assert PayloadSanitizer.preview([]) == "[]"


def test_preview_keeps_500_characters_and_cuts_501():
    assert PayloadSanitizer.preview("x" * 500) == "x" * 500
    assert PayloadSanitizer.preview("y" * 501) == "y" * 500
    assert PayloadSanitizer.preview("é" * 900) == "é" * 500


def test_preview_stringifies_a_structured_result():
    assert PayloadSanitizer.preview({"ok": True}) == "{'ok': True}"


def test_a_subclass_sets_its_own_limits():
    class Tight(PayloadSanitizer):
        MAX_ARG_CHARS = 3
        MAX_PREVIEW_CHARS = 4

    assert Tight.safe_args({"a": "abcdef"}) == {"a": "abc"}
    assert Tight.preview("abcdef") == "abcd"
    assert PayloadSanitizer.preview("abcdef") == "abcdef"
