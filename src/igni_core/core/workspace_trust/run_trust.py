"""The trust decisions that last for this process only.

"This time only" and "not this time" are answers a person gives at a
prompt, and they belong to the run they gave them in: the next process
asks again. They are kept here, keyed by the folder's identity key.

There is one answer per folder. A folder that was refused and then
trusted is trusted, and the other way round — the newest answer is the
one that counts, and a folder cannot hold two answers at once.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: The two answers a person can give for one run.
RunAnswer = Literal["trusted", "untrusted"]


class RunTrust(BaseModel):
    """Which folders this process trusts or refuses, and for this run only."""

    #: Identity key → the answer given for it in this process.
    answers: dict[str, RunAnswer] = Field(default_factory=dict)

    def grant(self, key: str) -> None:
        """Trust ``key`` for this run, whatever this run said before."""
        self.answers[key] = "trusted"

    def refuse(self, key: str) -> None:
        """Refuse ``key`` for this run, whatever this run said before."""
        self.answers[key] = "untrusted"

    def forget(self, key: str) -> None:
        """Drop the run-scoped answer about ``key``."""
        self.answers.pop(key, None)

    def answer(self, key: str) -> RunAnswer | None:
        """The answer given for ``key`` in this run, if there is one."""
        return self.answers.get(key)

    def granted(self, key: str) -> bool:
        """Whether ``key`` is trusted for this run."""
        return self.answers.get(key) == "trusted"

    def refused(self, key: str) -> bool:
        """Whether ``key`` is refused for this run."""
        return self.answers.get(key) == "untrusted"


__all__ = ["RunAnswer", "RunTrust"]
