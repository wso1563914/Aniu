from __future__ import annotations

from typing import Any, Callable

from app.agent.kernel.fsm import AgentPhaseTransition, AgentRunContext


class AgentRunner:
    def __init__(self, *, emit: Callable[[str, Any], None] | None = None) -> None:
        self._emit = emit if callable(emit) else None

    def transition(
        self,
        *,
        context: AgentRunContext,
        phase: str,
        message: str,
        **payload: Any,
    ) -> AgentPhaseTransition:
        event = AgentPhaseTransition(phase=phase, message=message, payload=payload)
        context.runtime["phase"] = phase
        if self._emit is not None:
            emit_payload = dict(payload)
            stage_name = str(emit_payload.pop("stage", phase))
            self._emit(
                "stage",
                stage=stage_name,
                fsm_phase=phase,
                message=message,
                **emit_payload,
            )
        return event
