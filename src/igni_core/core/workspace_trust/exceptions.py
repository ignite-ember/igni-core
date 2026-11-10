"""The one exception the trust service raises.

A refusal is not a failure to answer: the organisation's policy says
this folder may not be trusted, and the person cannot override it. It
is the one case where recording an answer is impossible, so it is the
one case that raises.
"""

from __future__ import annotations


class TrustRefused(Exception):
    """The organisation's policy does not let this folder be trusted."""


__all__ = ["TrustRefused"]
