"""The locator holds the one supervisor a backend process shares."""

from __future__ import annotations

from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator


def test_a_locator_with_nothing_given_builds_a_working_supervisor_of_its_own():
    first, second = SupervisorLocator(), SupervisorLocator()

    assert isinstance(first.supervisor, ProcessSupervisor)
    assert first.supervisor.foreground_process is None
    assert first.supervisor.registry.all_known() == []
    assert first.supervisor is not second.supervisor
    assert first.supervisor.registry is not second.supervisor.registry


def test_the_supervisor_it_was_given_is_the_one_it_keeps():
    given = ProcessSupervisor()
    locator = SupervisorLocator(given)

    assert locator.supervisor is given
    assert locator.supervisor is given


def test_the_supervisor_it_built_is_the_same_on_every_read():
    locator = SupervisorLocator()

    assert locator.supervisor is locator.supervisor


def test_a_supervisor_that_is_falsy_is_still_adopted():
    class Falsy(ProcessSupervisor):
        def __bool__(self) -> bool:
            return False

    given = Falsy()

    assert SupervisorLocator(given).supervisor is given
