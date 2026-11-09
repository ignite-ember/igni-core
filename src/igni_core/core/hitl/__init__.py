"""The pieces of the sub-agent HITL bridge.

One concern per module, light on imports:

* :mod:`.models` — what a pending question is, and the handles the
  relay passes around.
* :mod:`.registry` — the questions waiting for a person, and how they
  are shown.
* :mod:`.ledger` — the approvals' durable side (the journal).
* :mod:`.turns` — the finished calls' results a continued turn needs.
* :mod:`.relay` — who is relaying questions to a person right now.
* :mod:`.trace` — the debug trace of what was pushed.
"""
