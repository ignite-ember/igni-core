"""What the tool-result detector flags as failed, rejected or neither, and how it is configured."""

from __future__ import annotations

import re

import pytest

from igni_core.protocol.tool_error_conventions import ToolResultErrorDetector


@pytest.fixture
def detector() -> ToolResultErrorDetector:
    return ToolResultErrorDetector.default()


@pytest.mark.parametrize("result", [None, ""])
def test_nothing_is_neither_failed_nor_rejected(detector, result) -> None:
    assert detector.is_error(result) is False
    assert detector.is_rejected(result) is False


@pytest.mark.parametrize(
    "result",
    [
        "\n  Error: nope",
        "\t\nCommand exited with code 3:\nboom",
        "   [Exited with code 2 after 1s]",
        "Command exited with code 99999999999999999999999999",
        "Error: ünïcode ✗\nsecond line",
    ],
)
def test_leading_whitespace_is_ignored_when_flagging(detector, result) -> None:
    assert detector.is_error(result) is True


@pytest.mark.parametrize(
    "result",
    [
        "Command exited with code 0:\n",
        "Command exited with code 00:\n",
        "Background process exited with code 000:",
        "[Exited with code 0 after 3s]",
        "Command exited with code " + "0" * 5000,
    ],
)
def test_an_exit_code_that_is_zero_however_it_is_written_is_not_a_failure(detector, result) -> None:
    assert detector.is_error(result) is False


def test_a_long_exit_code_with_a_non_zero_digit_is_a_failure(detector) -> None:
    assert detector.is_error("Command exited with code " + "0" * 5000 + "1") is True


@pytest.mark.parametrize(
    "result",
    [
        "line one\nError: inside a file",
        "see: Command exited with code 1",
        "output\n[Exited with code 1]",
        "Warning: Error: not first",
    ],
)
def test_a_phrase_that_is_not_at_the_start_is_not_a_failure(detector, result) -> None:
    assert detector.is_error(result) is False


def test_a_call_that_was_not_run_is_rejected_and_not_failed(detector) -> None:
    result = "Not run: the person said no"

    assert detector.is_rejected(result) is True
    assert detector.is_error(result) is False


@pytest.mark.parametrize("result", ["\n \tNot run: x", "Not run:"])
def test_leading_whitespace_is_ignored_when_rejecting(detector, result) -> None:
    assert detector.is_rejected(result) is True


@pytest.mark.parametrize("result", ["it was Not run: here", "Error: boom", "not run: lower"])
def test_only_the_not_run_prefix_at_the_start_is_rejected(detector, result) -> None:
    assert detector.is_rejected(result) is False


def test_a_failure_is_not_a_rejection_and_the_other_way_round(detector) -> None:
    assert detector.is_rejected("Error: x") is False
    assert detector.is_error("Command exited with code 1:") is True
    assert detector.is_rejected("Command exited with code 1:") is False


def test_the_default_detector_is_the_plain_constructor(detector) -> None:
    plain = ToolResultErrorDetector()
    samples = [
        "Error: x",
        "Not run: x",
        "Command exited with code 1",
        "Command exited with code 0",
        "ok",
        "",
    ]

    for sample in samples:
        assert detector.is_error(sample) is plain.is_error(sample)
        assert detector.is_rejected(sample) is plain.is_rejected(sample)


def test_each_default_call_gives_its_own_detector() -> None:
    assert ToolResultErrorDetector.default() is not ToolResultErrorDetector.default()


def test_a_detector_with_its_own_rules_replaces_the_defaults() -> None:
    custom = ToolResultErrorDetector(
        error_prefixes=["FAIL "],
        failure_regexes=[re.compile(r"^status=(\d+)")],
        rejected_prefixes=["Skipped:"],
    )

    assert custom.is_error("FAIL here") is True
    assert custom.is_error("status=2") is True
    assert custom.is_error("status=0") is False
    assert custom.is_rejected("Skipped: x") is True
    # What the defaults flagged is no longer flagged.
    assert custom.is_error("Error: x") is False
    assert custom.is_rejected("Not run: x") is False


def test_a_regex_without_a_group_flags_on_any_match() -> None:
    custom = ToolResultErrorDetector(
        error_prefixes=[], failure_regexes=[re.compile(r"^panic")], rejected_prefixes=[]
    )

    assert custom.is_error("panic: oh no") is True
    assert custom.is_error("calm panic") is False
