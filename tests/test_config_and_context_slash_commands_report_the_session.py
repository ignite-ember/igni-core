"""``/config``, ``/compact`` and ``/ctx``: what each puts in its result.

``/config`` reads a real ``Settings``; ``/compact`` and ``/ctx`` ask the
session's compaction coordinator, which is replaced by a stub answering
with the real ``CompactResult`` / ``ContextBreakdown`` shapes. What is
asserted is the result the client receives: its kind, action and text.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from igni_core.backend.cmd_config import ConfigCommand, cmd_config
from igni_core.backend.cmd_context import ContextCommand, cmd_compact, cmd_ctx, cmd_output_style
from igni_core.core.session.schemas import CompactResult, ContextBreakdown
from igni_core.protocol.messages import CommandAction, CommandResultKind


@pytest.fixture(autouse=True)
def _no_saved_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


# ── /config ───────────────────────────────────────────────────────


def _config_session(settings, session_id="sess-42"):
    return SimpleNamespace(settings=settings, session_id=session_id)


async def test_config_is_a_markdown_card_naming_the_model_and_the_session(settings):
    settings.models.default = "my-model"

    result = await cmd_config(SimpleNamespace(session=_config_session(settings)), "")

    assert result.kind == CommandResultKind.MARKDOWN
    assert result.content.startswith("## Configuration\n")
    assert "- **Model:** my-model\n" in result.content
    assert "- **Auth:** not logged in\n" in result.content
    assert "- **Session:** sess-42\n" in result.content


async def test_config_ignores_its_arguments(settings):
    handler = SimpleNamespace(session=_config_session(settings))

    assert (await cmd_config(handler, "")).content == (
        await cmd_config(handler, "anything")
    ).content


def test_config_with_no_default_model_still_renders(settings):
    settings.models.default = ""

    result = ConfigCommand(_config_session(settings)).show()

    assert result.kind == CommandResultKind.MARKDOWN
    assert "- **Model:** \n" in result.content


# ── /compact and /ctx ─────────────────────────────────────────────


class _Compaction:
    def __init__(self, result=None, breakdown=None):
        self._result, self._breakdown = result, breakdown
        self.forced = 0

    async def force_compact(self):
        self.forced += 1
        return self._result

    async def context_breakdown(self):
        return self._breakdown


def _ctx_session(**kw):
    return SimpleNamespace(compaction=_Compaction(**kw))


async def test_compact_returns_the_status_as_content_and_the_summary_for_the_card():
    session = _ctx_session(
        result=CompactResult(ok=True, status="Context compacted.", summary="We fixed the bug.")
    )

    result = await cmd_compact(SimpleNamespace(session=session))

    assert result.kind == CommandResultKind.ACTION
    assert result.action == CommandAction.COMPACT
    assert result.content == "Context compacted."
    assert result.display_content == "We fixed the bug."
    assert session.compaction.forced == 1


async def test_compact_with_nothing_to_compact_says_so():
    session = _ctx_session(
        result=CompactResult(ok=False, status="Nothing to compact — no conversation history.")
    )

    result = await ContextCommand(session).compact()

    assert result.action == CommandAction.COMPACT
    assert result.content == "Nothing to compact — no conversation history."
    assert not result.display_content


async def test_ctx_carries_the_breakdown_text_and_opens_the_page():
    session = _ctx_session(breakdown=ContextBreakdown.from_totals(total=12_000, runs=3_000))

    result = await cmd_ctx(SimpleNamespace(session=session))

    assert result.action == CommandAction.CTX
    assert result.kind == CommandResultKind.MARKDOWN, "kind-based renderers still print it"
    assert "- **Total:** 12,000 tokens" in result.content
    assert "- **Conversation (runs):** 3,000 tokens (25.0% of total)" in result.content
    assert "**Floor (system + tools + rules + memories + summary):** 9,000 tokens" in result.content


async def test_ctx_of_an_empty_context_is_all_zero():
    session = _ctx_session(breakdown=ContextBreakdown.from_totals(total=0, runs=0))

    result = await ContextCommand(session).ctx()

    assert "- **Total:** 0 tokens" in result.content
    assert result.action == CommandAction.CTX


# ── /output-style verb routing ────────────────────────────────────


class _Mode:
    def __init__(self):
        self.asked: list[str] = []

    def set_output_style(self, name):
        self.asked.append(name)
        refused = name == "nope"
        return SimpleNamespace(refused=refused, text=f"{'refused' if refused else 'set'}:{name}")


def _style_session():
    return SimpleNamespace(output_style=SimpleNamespace(styles={}, active="default"), mode=_Mode())


@pytest.mark.parametrize("args", ["set", "set   "])
async def test_output_style_set_with_no_name_is_a_usage_error_and_sets_nothing(args):
    session = _style_session()
    result = await cmd_output_style(SimpleNamespace(session=session), args)

    assert result.kind == CommandResultKind.ERROR
    assert result.content.startswith("Usage: /output-style <name>")
    assert session.mode.asked == []


async def test_a_blank_output_style_argument_lists_the_styles():
    session = _style_session()
    result = await cmd_output_style(SimpleNamespace(session=session), "  ")

    assert result.kind != CommandResultKind.ERROR
    assert session.mode.asked == []


async def test_output_style_list_with_junk_goes_to_the_set_path_whole():
    session = _style_session()

    result = await ContextCommand(session).output_style("list junk")

    assert session.mode.asked == ["list junk"]
    assert result.content == "set:list junk"


@pytest.mark.parametrize("verb", ["SET", "Set"])
async def test_the_output_style_verb_is_case_insensitive(verb):
    session = _style_session()

    await ContextCommand(session).output_style(f"{verb} wordy")

    assert session.mode.asked == ["wordy"]


@pytest.mark.parametrize("verb", ["STATUS", "Show"])
async def test_status_and_show_report_the_active_style_in_any_case(verb):
    session = _style_session()

    result = await ContextCommand(session).output_style(verb)

    assert "default" in result.content
    assert session.mode.asked == []


async def test_a_refused_style_is_an_error_carrying_the_reason():
    result = await ContextCommand(_style_session()).output_style("nope")

    assert result.kind == CommandResultKind.ERROR
    assert result.content == "refused:nope"
