from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

RunPhase = Literal[
    "started",
    "observe",
    "analyze",
    "propose",
    "policy_check",
    "replan",
    "execute",
    "skip",
    "review",
    "completed",
    "failed",
]


@dataclass
class AgentRunContext:
    run_id: int
    settings_snapshot: dict[str, Any]
    trigger_source: str
    schedule_id: int | None
    runtime: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentPhaseTransition:
    phase: RunPhase
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
