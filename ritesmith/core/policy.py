"""PolicyEngine — avalia se uma ação é permitida, requer aprovação ou está bloqueada.

Regras em ordem de prioridade (primeira que bate, decide):
  1. shell_script execute         → deny
  2. risk_level=critical execute  → deny
  3. risk_level=high execute      → require_approval
  4. risk_level=medium execute    → require_approval
  5. risk_level=low execute       → allow (se allow_unapproved_low_risk=True)
  6. trama_workflow delegate      → require_approval
  7. Default                      → settings.policy_default
"""
from ritesmith.config import Settings
from ritesmith.observability.metrics import policy_decisions_total
from ritesmith.schemas.artifact import RiskLevel
from ritesmith.schemas.policy import PolicyDecision, PolicyDecisionValue, PolicyEvaluationRequest


class PolicyEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate(self, req: PolicyEvaluationRequest) -> PolicyDecision:
        artifact_type = req.artifact_type or ""
        operation = req.operation or "execute"
        risk_level = req.risk_level or RiskLevel.low

        decision = self._decide(artifact_type, operation, risk_level)
        policy_decisions_total.labels(
            decision=decision.decision.value,
            rule=f"{artifact_type}.{operation}.{risk_level}",
        ).inc()
        return decision

    def _decide(self, artifact_type: str, operation: str, risk_level: str) -> PolicyDecision:
        if artifact_type == "shell_script" and operation == "execute":
            return PolicyDecision(
                decision=PolicyDecisionValue.deny,
                reason="Shell script execution is never permitted",
            )

        if operation == "execute":
            if risk_level == RiskLevel.critical:
                return PolicyDecision(
                    decision=PolicyDecisionValue.deny,
                    reason="Critical risk artifacts cannot be executed",
                )
            if risk_level == RiskLevel.high:
                return PolicyDecision(
                    decision=PolicyDecisionValue.require_approval,
                    reason="High risk artifacts require approval before execution",
                )
            if risk_level == RiskLevel.medium:
                return PolicyDecision(
                    decision=PolicyDecisionValue.require_approval,
                    reason="Medium risk artifacts require approval before execution",
                )
            if risk_level == RiskLevel.low:
                return PolicyDecision(
                    decision=PolicyDecisionValue.allow,
                    reason="Low risk artifact approved automatically",
                )

        if artifact_type == "trama_workflow" and operation == "delegate":
            return PolicyDecision(
                decision=PolicyDecisionValue.require_approval,
                reason="Workflow delegation requires approval",
            )

        default = self.settings.policy_default
        return PolicyDecision(
            decision=PolicyDecisionValue(default),
            reason=f"Default policy: {default}",
        )
