"""What ``MessageSanitizer`` hands a model that cannot see images.

A session that used a vision model leaves messages whose content is a list
of parts (text, image_url, file). A model without vision gets the text of
those parts, joined by newlines; a vision model gets them untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agno.models.message import Message

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

    (out,) = _sanitize(messages)

    assert out[0]["content"] == "first\nsecond"


def test_bare_strings_count_as_text_and_other_parts_are_ignored() -> None:
    messages = [{"content": ["plain", {"type": "text", "text": "dict"}, 7, None]}]

    (out,) = _sanitize(messages)

    assert out[0]["content"] == "plain\ndict"


def test_a_message_with_no_text_at_all_becomes_the_empty_string() -> None:
    messages = [
        {"content": [IMAGE]},
        {"content": []},
        {"content": [{"type": "text"}]},
    ]

    (out,) = _sanitize(messages)

    assert [m["content"] for m in out] == ["", "", ""]


def test_content_that_is_not_a_list_is_left_alone() -> None:
    messages = [{"content": "already text"}, {"content": None}, {"role": "user"}]

    (out,) = _sanitize(messages)

    assert out == [{"content": "already text"}, {"content": None}, {"role": "user"}]
    assert all(o is m for o, m in zip(out, messages, strict=True))


def test_message_objects_are_rewritten_through_their_content_attribute() -> None:
    message = SimpleNamespace(role="user", content=[{"type": "text", "text": "hi"}, IMAGE])
    no_content = SimpleNamespace(role="system")

    (out,) = _sanitize([message, no_content])

    assert out[0].content == "hi"
    assert out[0].role == "user"
    assert out[1] is no_content
    assert not hasattr(no_content, "content")


def test_the_other_arguments_are_kept_and_the_history_handed_in_is_not_changed() -> None:
    """The messages are the run's own history, stored with the session: flattening them in
    place would lose a vision session's images for good."""
    parts = [{"type": "text", "text": "é ✓"}, IMAGE]
    message = SimpleNamespace(content=parts)
    messages = [{"content": parts}, message]

    out = _sanitize(messages, "tools", {"k": 1})

    assert out[1:] == ("tools", {"k": 1})
    assert [m["content"] if isinstance(m, dict) else m.content for m in out[0]] == ["é ✓", "é ✓"]
    assert messages[0]["content"] is parts
    assert message.content is parts
    assert out[0] is not messages


def test_an_empty_conversation_is_returned_empty() -> None:
    assert _sanitize([]) == ([],)


# ── How Agno calls a model: keywords only ──────────────────────────────


def test_messages_passed_by_keyword_are_flattened_and_the_rest_kept() -> None:
    messages = [Message(role="user", content=[{"type": "text", "text": "look"}, IMAGE])]
    kwargs = {"messages": messages, "assistant_message": "a", "tools": None}

    args, out = MessageSanitizer().sanitize_call((), kwargs, vision=False)

    assert args == ()
    assert out["messages"][0].content == "look"
    assert out["messages"][0].role == "user"
    assert {k: v for k, v in out.items() if k != "messages"} == {
        "assistant_message": "a",
        "tools": None,
    }
    assert messages[0].content == [{"type": "text", "text": "look"}, IMAGE]
    assert kwargs["messages"] is messages


def test_a_vision_model_gets_the_keywords_as_they_are() -> None:
    kwargs: dict[str, Any] = {"messages": [{"content": [IMAGE]}]}

    args, out = MessageSanitizer().sanitize_call(("x",), kwargs, vision=True)

    assert (args, out) == (("x",), kwargs)
    assert out is kwargs


def test_without_a_messages_keyword_the_first_argument_is_read() -> None:
    args, out = MessageSanitizer().sanitize_call(
        ([{"content": [{"type": "text", "text": "t"}]}],), {"tools": None}, vision=False
    )

    assert args == ([{"content": "t"}],)
    assert out == {"tools": None}
