"""Tests for ``protocol.agno_compat`` — Agno's two producer spellings.

The module reads the producer off an Agno event or run output. What is
worth pinning:

* an agent's run reads as ``kind="agent"``, a team's as ``kind="team"``;
* a source that names no producer reads as ``kind="unknown"`` with an
  empty name — never a wrong name;
* the agent spelling wins when a source carries both;
* nothing patches Agno's classes — importing the protocol package must
  not add ``agent_id`` to ``TeamRunOutput``, and must not pull Agno in.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys

from agno.run.agent import RunStartedEvent
from agno.run.team import RunStartedEvent as TeamRunStartedEvent
from agno.run.team import TeamRunOutput

from igni_core.protocol import messages as msg
from igni_core.protocol.agno_compat import RunProducer
from igni_core.protocol.serializer import serialize_event


class TestRunProducer:
    """Reading the producer off an Agno event or run output."""

    def test_agent_event_reads_as_agent(self):
        producer = RunProducer.from_agno(RunStartedEvent(agent_name="editor", run_id="r1"))
        assert producer.kind == "agent"
        assert producer.name == "editor"

    def test_team_event_reads_as_team(self):
        producer = RunProducer.from_agno(TeamRunStartedEvent(team_name="reviewers", run_id="r1"))
        assert producer.kind == "team"
        assert producer.name == "reviewers"

    def test_team_run_output_reads_as_team(self):
        run = TeamRunOutput(run_id="r1", team_id="t1", team_name="reviewers")
        producer = RunProducer.from_agno(run)
        assert producer.kind == "team"
        assert producer.name == "reviewers"

    def test_source_without_a_producer_is_unknown(self):
        producer = RunProducer.from_agno(object())
        assert producer.kind == "unknown"
        assert producer.name == ""

    def test_agent_spelling_wins_when_a_source_carries_both(self):
        class Both:
            agent_name = "editor"
            team_name = "reviewers"

        producer = RunProducer.from_agno(Both())
        assert producer.kind == "agent"
        assert producer.name == "editor"

    def test_an_empty_agent_name_falls_through_to_the_team_name(self):
        class Empty:
            agent_name = ""
            team_name = "reviewers"

        producer = RunProducer.from_agno(Empty())
        assert producer.kind == "team"
        assert producer.name == "reviewers"

    def test_a_name_that_is_not_a_string_is_read_as_text(self):
        class Odd:
            agent_name = 42

        assert RunProducer.from_agno(Odd()).name == "42"


class TestRunStartedMessage:
    """The consumer of the producer: a run-start message names its producer."""

    def test_an_agent_run_start_names_the_agent(self):
        proto = serialize_event(RunStartedEvent(agent_name="editor", run_id="r1"))
        assert isinstance(proto, msg.RunStarted)
        assert proto.agent_name == "editor"
        assert proto.run_id == "r1"

    def test_a_team_run_start_names_the_team(self):
        proto = serialize_event(TeamRunStartedEvent(team_name="reviewers", run_id="r1"))
        assert isinstance(proto, msg.RunStarted)
        assert proto.agent_name == "reviewers"
        assert proto.run_id == "r1"

    def test_a_run_start_without_a_name_is_dropped(self):
        assert serialize_event(TeamRunStartedEvent(team_name="", run_id="r1")) is None


class TestNothingIsPatched:
    """Agno's classes are read, never rewritten."""

    def test_the_team_run_output_is_not_given_an_agent_id(self):
        # Agno's ``TeamRunOutput`` names a team: ``team_id`` /
        # ``team_name``. igni reads the producer where a run is
        # consumed; it does not make a team answer to the agent
        # spelling behind every call site's back.
        native = {f.name for f in dataclasses.fields(TeamRunOutput)}
        assert "agent_id" not in TeamRunOutput.__dict__ or "agent_id" in native

    def test_importing_the_protocol_package_does_not_import_agno(self):
        # The protocol package is the FE-facing contract; pulling Agno
        # into it would make an import a side effect on a third-party
        # library for every consumer, the FE included.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, igni_core.protocol; "
                "print(sorted(m for m in sys.modules if m.split('.')[0] == 'agno'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "[]", result.stdout
