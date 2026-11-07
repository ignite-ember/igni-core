"""Skills system — reusable prompted workflows via /skill-name."""

from igni_core.core.skills.executor import SkillExecutor, SkillResult
from igni_core.core.skills.loader import SkillEntry, SkillPool
from igni_core.core.skills.parser import SkillDefinition, SkillParser

__all__ = [
    "SkillPool",
    "SkillEntry",
    "SkillParser",
    "SkillDefinition",
    "SkillExecutor",
    "SkillResult",
]
