from __future__ import annotations

from dataclasses import asdict, dataclass
from dataclasses import replace
from typing import Any, Literal


TradeAction = Literal["BUY", "SELL", "CANCEL", "MANAGE_SELF_SELECT"]


@dataclass
class TradeExecutionIntent:
    symbol: str
    action: TradeAction
    quantity: int
    price_type: str
    price: float | None = None
    name: str | None = None
    reason: str = ""
    status: str = "submitted"
    response: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeProposal:
    symbol: str
    action: TradeAction
    quantity: int
    price_type: str
    price: float | None = None
    name: str | None = None
    reason: str = ""
    response: dict[str, Any] | None = None

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


PolicyDecisionType = Literal["approved", "revise", "rejected"]


@dataclass
class PolicyDecision:
    decision: PolicyDecisionType
    proposal: TradeProposal
    message: str = ""
    retryable: bool = False
    revised_proposal: TradeProposal | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "message": self.message,
            "retryable": self.retryable,
            "proposal": self.proposal.to_record(),
            "revised_proposal": self.revised_proposal.to_record()
            if self.revised_proposal is not None
            else None,
        }


def intent_from_record(record: dict[str, Any]) -> TradeExecutionIntent | None:
    if not isinstance(record, dict):
        return None
    action = str(record.get("action") or "").upper()
    if action not in {"BUY", "SELL", "CANCEL", "MANAGE_SELF_SELECT"}:
        return None
    return TradeExecutionIntent(
        symbol=str(record.get("symbol") or "").strip(),
        name=str(record.get("name") or "").strip() or None,
        action=action,  # type: ignore[arg-type]
        quantity=int(record.get("quantity") or 0),
        price_type=str(record.get("price_type") or "MARKET"),
        price=_parse_float(record.get("price")),
        reason=str(record.get("reason") or "").strip(),
        status=str(record.get("status") or "submitted"),
        response=record.get("response") if isinstance(record.get("response"), dict) else None,
    )


def proposal_from_record(record: dict[str, Any]) -> TradeProposal | None:
    if not isinstance(record, dict):
        return None
    action = str(record.get("action") or "").upper()
    if action not in {"BUY", "SELL", "CANCEL", "MANAGE_SELF_SELECT"}:
        return None
    return TradeProposal(
        symbol=str(record.get("symbol") or "").strip(),
        name=str(record.get("name") or "").strip() or None,
        action=action,  # type: ignore[arg-type]
        quantity=int(record.get("quantity") or 0),
        price_type=str(record.get("price_type") or "MARKET"),
        price=_parse_float(record.get("price")),
        reason=str(record.get("reason") or "").strip(),
        response=record.get("response") if isinstance(record.get("response"), dict) else None,
    )


def proposals_from_records(records: Any) -> list[TradeProposal]:
    if not isinstance(records, list):
        return []
    proposals: list[TradeProposal] = []
    for record in records:
        proposal = proposal_from_record(record)
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def proposals_to_records(proposals: list[TradeProposal]) -> list[dict[str, Any]]:
    return [proposal.to_record() for proposal in proposals]


def default_policy_decisions(proposals: list[TradeProposal]) -> list[PolicyDecision]:
    return [
        PolicyDecision(
            decision="approved",
            proposal=proposal,
            message="default approval",
            retryable=False,
        )
        for proposal in proposals
    ]


def intents_from_proposals(decisions: list[PolicyDecision]) -> list[TradeExecutionIntent]:
    intents: list[TradeExecutionIntent] = []
    for decision in decisions:
        if decision.decision not in {"approved", "revise"}:
            continue
        proposal = decision.revised_proposal or decision.proposal
        intents.append(
            TradeExecutionIntent(
                symbol=proposal.symbol,
                name=proposal.name,
                action=proposal.action,
                quantity=proposal.quantity,
                price_type=proposal.price_type,
                price=proposal.price,
                reason=proposal.reason,
                status="submitted",
                response=proposal.response,
            )
        )
    return intents


def intents_from_records(records: Any) -> list[TradeExecutionIntent]:
    if not isinstance(records, list):
        return []
    intents: list[TradeExecutionIntent] = []
    for record in records:
        intent = intent_from_record(record)
        if intent is not None:
            intents.append(intent)
    return intents


def intents_to_records(intents: list[TradeExecutionIntent]) -> list[dict[str, Any]]:
    return [intent.to_record() for intent in intents]


def _parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def with_revised_price_type(proposal: TradeProposal, price_type: str) -> TradeProposal:
    return replace(proposal, price_type=price_type)
