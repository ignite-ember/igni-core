"""The one failure this reports."""

from __future__ import annotations


class DistributionError(Exception):
    """One extension could not be brought up to date, and why.

    The message is for the log and for the person reading it: what
    was refused, and why. Nothing is installed and nothing is removed
    when one is raised.
    """
