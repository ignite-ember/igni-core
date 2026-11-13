"""``HostModelError``: how the host says no, to an extension's author.

A refusal carries a code for the extension's logic and a sentence for the
person who writes it; the sentence names what to add to the manifest.
"""

from __future__ import annotations

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.wire import ErrorBody


class TestWhatAnErrorCarries:
    def test_the_code_and_the_sentence_are_what_the_reply_body_carries(self) -> None:
        error = HostModelError(ErrorCode.REFUSED, "no, thank you")
        assert (error.code, error.message, str(error)) == (
            ErrorCode.REFUSED,
            "no, thank you",
            "no, thank you",
        )
        assert error.body() == ErrorBody(code=ErrorCode.REFUSED, message="no, thank you")


class TestNotGranted:
    def test_a_host_permission_is_added_to_permissions_host(self) -> None:
        error = HostModelError.not_granted("acme.reviewer", "host.chat.post", "chat.write")
        assert error.code is ErrorCode.NOT_GRANTED
        assert error.message == (
            "'acme.reviewer' is not granted 'chat.write', which host.chat.post needs. "
            "Add 'chat.write' to `permissions.host` in its manifest and reinstall."
        )

    def test_the_workspace_is_declared_in_permissions_workspace(self) -> None:
        error = HostModelError.not_granted("acme.reviewer", "host.workspace.open", "workspace")
        assert error.code is ErrorCode.NOT_GRANTED
        assert error.message == (
            "'acme.reviewer' is not granted 'workspace', which host.workspace.open needs. "
            "Declare `permissions.workspace: read` in its manifest and reinstall."
        )

    def test_odd_characters_are_written_as_they_came(self) -> None:
        error = HostModelError.not_granted("a'b.é", "x\ty", "p*q")
        assert "'a'b.é' is not granted 'p*q', which x\ty needs." in error.message


class TestStageTwo:
    def test_a_member_not_built_yet_says_so_and_to_write_against_it(self) -> None:
        error = HostModelError.stage_two("chat.fork")
        assert error.code is ErrorCode.STAGE_2
        assert error.message == (
            "'chat.fork' is defined but not yet available: igni has not built what is "
            "behind it yet (docs/HOST_OBJECT_MODEL.md). Write against it; it will start "
            "answering without a change to your code."
        )
