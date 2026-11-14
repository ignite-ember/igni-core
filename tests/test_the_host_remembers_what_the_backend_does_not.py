"""``HostState`` and ``ViewOwners``: what the host has observed and keeps.

The last update check, and which extension appended which view. Each host
model has its own; nothing is shared between two.
"""

from __future__ import annotations

from igni_core.extensions.host.host_state import HostState, ViewOwners
from igni_core.extensions.host.values import UpdateInfo

NEWER = UpdateInfo(current_version="0.7.0", latest_version="0.8.0", download_url="https://x/y")


class TestViewOwners:
    def test_a_view_nobody_appended_has_no_owner(self) -> None:
        assert ViewOwners().owner_of("v-1") is None

    def test_the_extension_that_appended_a_view_owns_it(self) -> None:
        owners = ViewOwners()
        owners.own("v-1", "acme.one")
        owners.own("v-2", "acme.two")
        assert (owners.owner_of("v-1"), owners.owner_of("v-2")) == ("acme.one", "acme.two")

    def test_appending_a_view_again_gives_it_to_the_latest(self) -> None:
        owners = ViewOwners()
        owners.own("v-1", "acme.one")
        owners.own("v-1", "acme.two")
        assert owners.owner_of("v-1") == "acme.two"


class TestHostState:
    def test_no_update_check_has_answered_at_first(self) -> None:
        assert HostState().update is None

    def test_an_update_check_is_recorded_and_a_later_none_clears_it(self) -> None:
        state = HostState()
        state.record_update(NEWER)
        assert state.update == NEWER
        state.record_update(None)
        assert state.update is None

    def test_two_states_share_nothing(self) -> None:
        first, second = HostState(), HostState()
        first.record_update(NEWER)
        first.view_owners.own("v-1", "acme.one")
        assert second.update is None
        assert second.view_owners.owner_of("v-1") is None
        assert first.approvals is not second.approvals
        assert first.runs is not second.runs
        assert first.status is not second.status
