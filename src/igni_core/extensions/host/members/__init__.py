"""Every built member of the host object model, against igni's backend.

One module per object; :mod:`.base`, :mod:`.call` and :mod:`.tag` hold
what they share.
Each member reaches igni the way igni's own UI does: the RPC handler a
panel calls, the action a window sends, the store a window writes.
Nothing here re-implements what igni does; it translates between the
model's promise and igni's internal wire, which is free to change.
"""

from __future__ import annotations

from igni_core.extensions.host.members.account import AccountMembers
from igni_core.extensions.host.members.agents import AgentMembers
from igni_core.extensions.host.members.app import AppMembers, AppStateMembers, WindowMembers
from igni_core.extensions.host.members.approvals import ApprovalMembers
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.chat import ChatReadMembers, ChatWriteMembers
from igni_core.extensions.host.members.chat_views import ChatViewMembers
from igni_core.extensions.host.members.commands import CommandMembers
from igni_core.extensions.host.members.decide import DecisionMembers
from igni_core.extensions.host.members.extension import ExtensionMembers, SelfMembers
from igni_core.extensions.host.members.hooks import HookMembers
from igni_core.extensions.host.members.loop import LoopMembers
from igni_core.extensions.host.members.mcp import McpMembers
from igni_core.extensions.host.members.models import ModelMembers
from igni_core.extensions.host.members.notifications import NotificationMembers
from igni_core.extensions.host.members.permissions import PermissionMembers
from igni_core.extensions.host.members.plan import PlanMembers
from igni_core.extensions.host.members.plugins import PluginMembers
from igni_core.extensions.host.members.policy import PolicyMembers
from igni_core.extensions.host.members.processes import ProcessMembers
from igni_core.extensions.host.members.scheduler import SchedulerMembers
from igni_core.extensions.host.members.sessions import SessionMembers
from igni_core.extensions.host.members.tasks import TaskMembers
from igni_core.extensions.host.members.ui import (
    UiContentMembers,
    UiNavigationMembers,
    UiNoticeMembers,
)
from igni_core.extensions.host.members.workflows import WorkflowMembers
from igni_core.extensions.host.members.workspace import WorkspaceMembers

ALL_MEMBERS: tuple[type[Members], ...] = (
    AppMembers,
    AppStateMembers,
    WindowMembers,
    UiNavigationMembers,
    UiContentMembers,
    UiNoticeMembers,
    SessionMembers,
    ChatReadMembers,
    ChatWriteMembers,
    ChatViewMembers,
    ApprovalMembers,
    PlanMembers,
    TaskMembers,
    LoopMembers,
    CommandMembers,
    AgentMembers,
    WorkflowMembers,
    SchedulerMembers,
    ProcessMembers,
    WorkspaceMembers,
    ModelMembers,
    McpMembers,
    PluginMembers,
    ExtensionMembers,
    SelfMembers,
    HookMembers,
    PermissionMembers,
    PolicyMembers,
    AccountMembers,
    NotificationMembers,
    DecisionMembers,
)

__all__ = ["ALL_MEMBERS"]
