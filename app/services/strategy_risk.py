from decimal import Decimal


def effective_strategy_risk_policy(
    definition: dict | None,
    *,
    maximum_risk_percent: Decimal,
) -> tuple[Decimal, Decimal | None]:
    risk = definition.get("risk", {}) if isinstance(definition, dict) else {}
    if not isinstance(risk, dict):
        risk = {}
    strategy_maximum = Decimal(
        str(risk.get("maximum_risk_percent", maximum_risk_percent))
    )
    minimum_planned_r = (
        Decimal(str(risk["minimum_planned_r"]))
        if risk.get("minimum_planned_r") is not None
        else None
    )
    return min(maximum_risk_percent, strategy_maximum), minimum_planned_r
