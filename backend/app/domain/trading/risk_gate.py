from __future__ import annotations

from typing import Any

from app.domain.trading.intents import (
    PolicyDecision,
    TradeProposal,
    with_revised_price_type,
)


class RiskGate:
    def evaluate(
        self,
        *,
        proposal: TradeProposal,
        run_type: str,
        trade_enabled: bool,
        enforce_trade_run_type: bool = True,
    ) -> PolicyDecision:
        normalized_run_type = str(run_type or "analysis").strip().lower()
        normalized_action = str(proposal.action or "").upper()

        if normalized_action in {"BUY", "SELL", "CANCEL"} and not trade_enabled:
            return PolicyDecision(
                decision="rejected",
                proposal=proposal,
                message="trade disabled by settings",
                retryable=False,
            )

        if (
            enforce_trade_run_type
            and normalized_run_type != "trade"
            and normalized_action in {"BUY", "SELL", "CANCEL"}
        ):
            return PolicyDecision(
                decision="rejected",
                proposal=proposal,
                message="trade actions require trade run type",
                retryable=False,
            )

        if normalized_action in {"BUY", "SELL"}:
            if int(proposal.quantity or 0) <= 0:
                return PolicyDecision(
                    decision="rejected",
                    proposal=proposal,
                    message="trade quantity must be positive",
                    retryable=False,
                )
            if not str(proposal.symbol or "").strip():
                return PolicyDecision(
                    decision="rejected",
                    proposal=proposal,
                    message="trade symbol is required",
                    retryable=False,
                )
            normalized_price_type = str(proposal.price_type or "MARKET").upper()
            if normalized_price_type not in {"MARKET", "LIMIT"}:
                return PolicyDecision(
                    decision="revise",
                    proposal=proposal,
                    revised_proposal=with_revised_price_type(proposal, "MARKET"),
                    message="unsupported price_type revised to MARKET",
                    retryable=True,
                )

        if normalized_action == "CANCEL" and not str(proposal.symbol or "").strip():
            return PolicyDecision(
                decision="rejected",
                proposal=proposal,
                message="cancel symbol is required",
                retryable=False,
            )

        return PolicyDecision(
            decision="approved",
            proposal=proposal,
            message="approved by risk gate",
            retryable=False,
        )


risk_gate = RiskGate()
