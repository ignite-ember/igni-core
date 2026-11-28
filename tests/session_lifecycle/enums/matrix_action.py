"""The actions a person takes in each state."""

from __future__ import annotations

from enum import StrEnum


class MatrixAction(StrEnum):
    """What the person does."""

    FOLLOW_UP = "follow_up"
    TWO_FOLLOW_UPS = "two_follow_ups"
    CANCEL = "cancel"
    CANCEL_TWICE = "cancel_twice"
    APPROVE = "approve"
    DENY = "deny"
    DENY_FEEDBACK = "deny_feedback"
    SWITCH_MODEL = "switch_model"
    RECONNECT = "reconnect"
    RELOAD = "reload"
    SECOND_WINDOW = "second_window"
    FORK = "fork"
    CLEAR = "clear"
    RESTART = "restart"
