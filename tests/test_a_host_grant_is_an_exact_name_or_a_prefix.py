"""``HostGrants``: what ``permissions.host`` and ``permissions.workspace`` reach.

A grant is an exact name or a ``*``-suffixed prefix; ``workspace`` is not
a ``permissions.host`` entry but ``permissions.workspace``; an extension's
own things (permission ``None``) are always granted.
"""

from __future__ import annotations

import pytest

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.spec import ErrorCode
from tests.host_model_helpers import manifest


def grants(**permissions: object) -> HostGrants:
    return HostGrants(manifest("acme.reviewer", **permissions))


class TestAllows:
    def test_what_is_not_a_permission_is_always_allowed(self) -> None:
        assert grants().allows(None)

    def test_an_exact_name_matches_only_itself(self) -> None:
        held = grants(host=["chat.read"])
        assert held.allows("chat.read")
        assert not held.allows("chat.read.more")
        assert not held.allows("chat")
        assert not held.allows("chat.write")

    def test_a_star_suffix_is_a_prefix_not_a_word(self) -> None:
        held = grants(host=["chat.*"])
        assert held.allows("chat.read") and held.allows("chat.write")
        assert not held.allows("chat")
        assert not held.allows("chatter.read")

    def test_a_prefix_without_a_dot_matches_the_name_itself_too(self) -> None:
        held = grants(host=["chat*"])
        assert held.allows("chat") and held.allows("chatter.read")

    def test_a_prefix_that_would_match_workspace_does_not_grant_it(self) -> None:
        held = grants(host=["work*"])
        assert held.allows("work.items")
        assert not held.allows("workspace")

    def test_the_workspace_follows_permissions_workspace_alone(self) -> None:
        assert not grants(host=["workspace"]).allows("workspace")
        assert grants(workspace="read").allows("workspace")
        assert not grants(workspace="read").allows("links")

    def test_an_extension_with_no_grants_is_allowed_nothing_named(self) -> None:
        held = grants()
        assert not held.allows("links") and not held.allows("workspace")


class TestNotifications:
    def test_every_extensions_notices_reach_only_one_granted_notifications(self) -> None:
        assert grants(host=["notifications"]).sees_all_notifications
        assert grants(host=["notif*"]).sees_all_notifications
        assert not grants(host=["links"]).sees_all_notifications
        assert not grants().sees_all_notifications


class TestWorkspaceGuards:
    def test_without_the_workspace_grant_a_request_is_refused_saying_what_to_declare(
        self,
    ) -> None:
        with pytest.raises(HostModelError) as caught:
            grants().require_workspace("host.workspace.open")
        assert caught.value.code is ErrorCode.NOT_GRANTED
        assert "Declare `permissions.workspace: read`" in caught.value.message
        assert "'acme.reviewer' is not granted 'workspace', which host.workspace.open needs" in (
            caught.value.message
        )

    def test_with_the_workspace_grant_it_passes(self) -> None:
        grants(workspace="read").require_workspace("host.workspace.open")

    def test_a_path_outside_workspace_paths_is_refused(self) -> None:
        held = grants(workspace="read", workspacePaths=["src/**"])
        held.require_workspace_path("src/a.py")
        with pytest.raises(HostModelError) as caught:
            held.require_workspace_path("notes.md")
        assert caught.value.code is ErrorCode.REFUSED
        assert caught.value.message == (
            "'notes.md' is outside what 'acme.reviewer' was granted of the workspace"
        )

    def test_without_the_workspace_grant_no_path_is_inside(self) -> None:
        with pytest.raises(HostModelError) as caught:
            grants().require_workspace_path("src/a.py")
        assert caught.value.code is ErrorCode.REFUSED


class TestGrantedForTheHello:
    def test_only_known_permissions_that_are_held_come_back_sorted(self) -> None:
        held = grants(host=["chat.*", "links", "nonsense"], workspace="read")
        known = {"workspace", "links", "chat.write", "chat.read", "notifications"}
        assert held.granted(known) == ["chat.read", "chat.write", "links", "workspace"]

    def test_nothing_known_is_nothing_granted(self) -> None:
        assert grants(host=["chat.*"]).granted(set()) == []


class TestUnknown:
    KNOWN = {"links", "chat.read", "chat.write", "workspace"}

    def test_an_exact_name_the_model_lacks_is_stray(self) -> None:
        assert HostGrants.unknown(["links", "teleport"], self.KNOWN) == ["teleport"]

    def test_a_star_that_matches_nothing_known_is_stray_one_that_matches_is_not(self) -> None:
        assert HostGrants.unknown(["chat.*", "voice.*", "*"], self.KNOWN) == ["voice.*"]

    def test_a_bare_star_is_stray_when_the_model_knows_nothing(self) -> None:
        assert HostGrants.unknown(["*"], set()) == ["*"]

    def test_workspace_is_stray_in_the_host_list_though_the_model_knows_it(self) -> None:
        assert HostGrants.unknown(["workspace", "links"], self.KNOWN) == ["workspace"]

    def test_an_empty_list_has_no_strays(self) -> None:
        assert HostGrants.unknown([], self.KNOWN) == []
