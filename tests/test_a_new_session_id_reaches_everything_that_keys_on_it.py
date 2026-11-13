"""A new session id reaches everything that keys on it, at once.

Agno keys stored history on ``team.session_id`` and igni's persistence on
its own copy, so ``/clear`` and ``/fork`` rotating only ``Session.session_id``
would write the next turn into the old session. :class:`SessionIdentity`
owns that invariant. Pinned here:

* :meth:`rotate` sets the id on the session, the live team and the
  persister — the ones current *now*, read through their refs, so a team
  rebuilt after construction still gets it — and survives either being
  absent (before start-up finished);
* :meth:`rebind` (switching to an existing session) does the same, marks
  the session as named, and reloads that session's history for this user;
* :meth:`mark_named` is what a successful auto-name flips.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.core.session.identity import SessionIdentity


class _Team:
    def __init__(self) -> None:
        self.session_id = "old"
        self.loaded: list[tuple[str, str]] = []

    async def aget_session(self, *, session_id: str, user_id: str) -> None:
        self.loaded.append((session_id, user_id))


class _Persistence:
    session_id = "old"


class _Holder:
    """Stands in for the session's slots, which are rebuilt over its life."""

    def __init__(self, team: Any = None, persistence: Any = None) -> None:
        self.team = team
        self.persistence = persistence


def _identity(holder: _Holder, named: bool = False) -> SessionIdentity:
    return SessionIdentity(
        session_id="old",
        session_named=named,
        user_id="dana",
        main_team_ref=lambda: holder.team,
        persistence_ref=lambda: holder.persistence,
    )


def test_rotate_reaches_the_session_the_team_and_the_persister():
    holder = _Holder(_Team(), _Persistence())
    identity = _identity(holder)

    identity.rotate("new")

    assert identity.session_id == holder.team.session_id == holder.persistence.session_id == "new"
    assert identity.user_id == "dana"
    assert not identity.session_named


def test_rotate_reaches_a_team_rebuilt_after_the_identity_was_made():
    holder = _Holder(_Team(), _Persistence())
    identity = _identity(holder)
    holder.team = _Team()

    identity.rotate("new")

    assert holder.team.session_id == "new"


def test_rotate_before_start_up_finished_changes_only_the_id():
    identity = _identity(_Holder())

    identity.rotate("new")

    assert identity.session_id == "new"


async def test_rebind_switches_to_the_session_and_reloads_its_history():
    holder = _Holder(_Team(), _Persistence())
    identity = _identity(holder)

    await identity.rebind("existing")

    assert identity.session_id == holder.persistence.session_id == "existing"
    assert identity.session_named
    assert holder.team.loaded == [("existing", "dana")]


async def test_rebind_without_a_team_still_switches_the_id():
    identity = _identity(_Holder(persistence=_Persistence()))

    await identity.rebind("existing")

    assert identity.session_id == "existing"
    assert identity.session_named


def test_a_successful_auto_name_marks_the_session_named():
    identity = _identity(_Holder())

    identity.mark_named()

    assert identity.session_named


def test_set_named_says_whether_the_person_named_the_session():
    identity = _identity(_Holder())

    identity.set_named(True)
    assert identity.session_named
    identity.set_named(False)
    assert not identity.session_named


def test_the_ids_are_the_ones_it_was_made_with():
    identity = _identity(_Holder(), named=True)

    assert (identity.session_id, identity.session_named, identity.user_id) == ("old", True, "dana")


async def test_rebind_asks_for_the_history_of_this_user():
    team = _Team()
    identity = _identity(_Holder(team, _Persistence()))

    await identity.rebind("existing")

    assert team.loaded == [("existing", "dana")]


async def test_a_history_that_cannot_be_loaded_leaves_the_session_as_it_was():
    class _Broken(_Team):
        async def aget_session(self, *, session_id: str, user_id: str) -> None:
            raise RuntimeError("no such session")

    team = _Broken()
    persistence = _Persistence()
    identity = _identity(_Holder(team, persistence))

    with pytest.raises(RuntimeError, match="no such session"):
        await identity.rebind("gone")

    assert identity.session_id == "old"
    assert team.session_id == "old"
    assert persistence.session_id == "old"
    assert not identity.session_named


async def test_a_failed_rebind_keeps_a_name_the_session_already_had():
    class _Broken(_Team):
        async def aget_session(self, *, session_id: str, user_id: str) -> None:
            raise RuntimeError("no such session")

    identity = _identity(_Holder(_Broken(), _Persistence()), named=True)

    with pytest.raises(RuntimeError):
        await identity.rebind("gone")

    assert identity.session_named
