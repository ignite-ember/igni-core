"""What ``MessageSanitizer`` hands a model that cannot see images.

A session that used a vision model leaves messages whose content is a list
of parts (text, image_url, file). A model without vision gets the text of
those parts, joined by newlines; a vision model gets them untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

from igni_core.core.config.message_sanitizer import MessageSanitizer

IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def _sanitize(messages, *rest, vision=False):
    return MessageSanitizer().sanitize_if_needed((messages, *rest), vision=vision)


def test_a_vision_model_sees_the_content_as_it_is() -> None:
    parts = [{"type": "text", "text": "look"}, IMAGE]
    messages = [{"role": "user", "content": parts}]

    out = _sanitize(messages, vision=True)

    assert out[0] is messages
    assert messages[0]["content"] is parts


def test_no_arguments_come_back_as_no_arguments() -> None:
    assert MessageSanitizer().sanitize_if_needed((), vision=False) == ()


def test_the_text_parts_are_joined_by_newlines_and_the_images_dropped() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                IMAGE,
                {"type": "file", "file": {"file_id": "f"}},
                {"type": "text", "text": "second"},
            ],
        }
    ]

    _sanitize(messages)

    assert messages[0]["content"] == "first\nsecond"


def test_bare_strings_count_as_text_and_other_parts_are_ignored() -> None:
    messages = [{"content": ["plain", {"type": "text", "text": "dict"}, 7, None]}]

    _sanitize(messages)

    assert messages[0]["content"] == "plain\ndict"


def test_a_message_with_no_text_at_all_becomes_the_empty_string() -> None:
    messages = [
        {"content": [IMAGE]},
        {"content": []},
        {"content": [{"type": "text"}]},
    ]

    _sanitize(messages)

    assert [m["content"] for m in messages] == ["", "", ""]


def test_content_that_is_not_a_list_is_left_alone() -> None:
    messages = [{"content": "already text"}, {"content": None}, {"role": "user"}]

    _sanitize(messages)

    assert messages == [{"content": "already text"}, {"content": None}, {"role": "user"}]


def test_message_objects_are_rewritten_through_their_content_attribute() -> None:
    message = SimpleNamespace(role="user", content=[{"type": "text", "text": "hi"}, IMAGE])
    no_content = SimpleNamespace(role="system")

    _sanitize([message, no_content])

    assert message.content == "hi"
    assert not hasattr(no_content, "content")


def test_the_same_list_comes_back_rewritten_in_place_with_the_other_arguments_kept() -> None:
    messages = [{"content": [{"type": "text", "text": "é ✓"}]}]

    out = _sanitize(messages, "tools", {"k": 1})

    assert out == (messages, "tools", {"k": 1})
    assert out[0] is messages
    assert messages[0]["content"] == "é ✓"


def test_an_empty_conversation_is_returned_empty() -> None:
    assert _sanitize([]) == ([],)
