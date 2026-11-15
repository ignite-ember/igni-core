"""Fuzz the parser for permission rules such as ``Bash(rm *)``.

Rules come from settings files, an administrator's policy and the
"Always allow" button, so ``PermissionRule.parse`` sees whatever text
ended up there. It has to return a rule or ``None`` — a rule it cannot
read is skipped with a warning, never a crash that takes the deny list
down with it — and a rule it returns has to survive being written back
out (``display``) and read again, since that is how a rule is shown and
stored. Matching a parsed rule against a fuzzed command must answer
without raising. The rule is matched under its own tool name, so the
friendly-name resolver (which reads the tool registry) is never asked.
"""

from __future__ import annotations

import logging
import sys

import atheris

with atheris.instrument_imports(include=["igni_core"]):
    from igni_core.core.config.permission_eval.schemas import PermissionRule

logging.disable(logging.CRITICAL)


def _text(data: bytes) -> str | None:
    """The input as the UTF-8 text a transport or settings file would hold.

    Decoded directly rather than through ``FuzzedDataProvider`` so the seed
    corpus is read as written: the provider spends the first byte choosing
    an encoding, which turns every seed into something else.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def TestOneInput(data: bytes) -> None:
    text = _text(data)
    if text is None:
        return
    # The rule, then on the next line the argument it is matched against.
    raw, _newline, argument = text.partition("\n")

    rule = PermissionRule.parse(raw)
    assert len(PermissionRule.parse_many([raw])) == (0 if rule is None else 1)
    if rule is None:
        return

    if rule.pattern:
        assert PermissionRule.parse(rule.display()) == rule, (raw, rule)
    assert isinstance(rule.deny_reason(), str)

    for key in ("command", "file_path", "path", "url"):
        assert isinstance(rule.matches(rule.tool, {key: argument}), bool)
    assert isinstance(rule.matches(rule.tool, {"args": argument.split()}), bool)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
