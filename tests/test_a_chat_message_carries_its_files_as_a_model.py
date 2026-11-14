"""``ChatAttachments``: the files a chat message carries, path to content."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.attachments import ChatAttachments


def test_a_message_with_no_files_carries_none() -> None:
    attachments = ChatAttachments()

    assert attachments.files == {}
    assert not attachments.has_files()


def test_a_message_with_a_file_carries_files() -> None:
    assert ChatAttachments(files={"notes/a.md": "text"}).has_files()


def test_a_file_with_empty_content_is_still_a_file() -> None:
    assert ChatAttachments(files={"empty.txt": ""}).has_files()


def test_each_message_has_its_own_files() -> None:
    first, second = ChatAttachments(), ChatAttachments()

    first.files["a.txt"] = "x"

    assert second.files == {} and not second.has_files()


@pytest.mark.parametrize("content", [None, 3, ["x"], {"a": 1}])
def test_content_that_is_not_text_is_refused(content: object) -> None:
    with pytest.raises(ValidationError):
        ChatAttachments(files={"a.txt": content})  # type: ignore[dict-item]
