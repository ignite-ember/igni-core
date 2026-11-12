"""The exact text the agent reads around an extension's message and after ``/plan``."""

from __future__ import annotations

from igni_core.backend.agent_framing import ExtensionMessage, PlanResearchHint


def test_an_extension_message_says_who_posted_it_and_that_the_person_did_not_type_it():
    assert ExtensionMessage.wrap("acme.index", "reindexed 3 files") == (
        "<system-context>\n"
        "The message below was posted by the igni extension 'acme.index', "
        "not typed by the person. Answer it in the chat as you would answer them.\n"
        "</system-context>\n"
        '<extension-message from="acme.index">\nreindexed 3 files\n</extension-message>'
    )


def test_multiline_and_unicode_text_is_carried_whole():
    text = "line one\n\nligne deux, naïve 日本語"

    assert f"\n{text}\n</extension-message>" in ExtensionMessage.wrap("x", text)


def test_empty_text_still_makes_a_labelled_message():
    assert ExtensionMessage.wrap("x", "").endswith(
        '<extension-message from="x">\n\n</extension-message>'
    )


def test_the_plan_hint_goes_before_the_request_and_asks_for_enter_plan_mode():
    wrapped = PlanResearchHint.wrap("add a retry to the uploader")

    assert wrapped.endswith("</system-context>\n\nadd a retry to the uploader")
    assert wrapped.startswith("<system-context>\nPlan mode was just entered via the user's /plan")
    assert "`enter_plan_mode(task=...)`" in wrapped
    assert "BEFORE doing any other work" in wrapped


def test_the_plan_hint_keeps_the_request_exactly():
    request = "  spaced \n <tag> "

    assert PlanResearchHint.wrap(request) == PlanResearchHint.TEXT + request
