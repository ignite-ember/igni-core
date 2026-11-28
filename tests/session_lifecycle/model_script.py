"""What the scripted model answers, chosen from the request it was sent."""

from __future__ import annotations

from tests.session_lifecycle.schemas.model_turn import ModelTurn
from tests.session_lifecycle.schemas.recorded_message import RecordedMessage
from tests.session_lifecycle.schemas.script_rule import ScriptRule


class ModelScript:
    """Rules keyed by what the latest user message says."""

    def __init__(
        self, rules: list[ScriptRule] | None = None, fallback: ModelTurn | None = None
    ) -> None:
        self.rules: list[ScriptRule] = list(rules or [])
        self.fallback = fallback or ModelTurn(text="Untitled chat", delay_before=1.0)

    def add(self, when: str, *turns: ModelTurn, then: ModelTurn | None = None) -> ModelScript:
        """Answer ``turns`` in order after a user message containing ``when``."""
        rule = ScriptRule(when=when, turns=list(turns))
        if then is not None:
            rule.then = then
        self.rules.append(rule)
        return self

    def choose(
        self, messages: list[RecordedMessage], has_tools: bool = True
    ) -> tuple[str, int, ModelTurn]:
        """The rule's key, the step, and the turn to answer ``messages`` with."""
        if not has_tools:
            return "", 0, self.fallback
        last_user = max((i for i, m in enumerate(messages) if m.role == "user"), default=-1)
        if last_user < 0:
            return "", 0, self.fallback
        text = messages[last_user].text
        step = sum(1 for m in messages[last_user + 1 :] if m.role == "assistant")
        # The rightmost match: a recap igni puts before the person's text names older prompts.
        found = [(text.rfind(rule.when), rule) for rule in self.rules if rule.when in text]
        if not found:
            return "", step, self.fallback
        rule = max(found, key=lambda pair: (pair[0], len(pair[1].when)))[1]
        turn = rule.turns[step] if step < len(rule.turns) else rule.then
        return rule.when, step, turn
