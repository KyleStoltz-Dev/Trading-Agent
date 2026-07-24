from decimal import ROUND_DOWN, Decimal

from app.models import InstrumentSpecification
from app.schemas import (
    BrokerPositionSizeRequest,
    BrokerPositionSizeResult,
    PositionSizeRequest,
    PositionSizeResult,
)


def calculate_position_size(request: PositionSizeRequest) -> PositionSizeResult:
    risk_amount = request.account_equity * request.risk_percent / Decimal("100")
    stop_distance = abs(request.entry - request.stop)
    raw_quantity = risk_amount / (stop_distance * request.value_per_price_unit)
    quantity = raw_quantity.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

    planned_r = None
    if request.target is not None:
        planned_r = (abs(request.target - request.entry) / stop_distance).quantize(
            Decimal("0.0001")
        )

    return PositionSizeResult(
        risk_amount=risk_amount.quantize(Decimal("0.01")),
        stop_distance=stop_distance,
        quantity=quantity,
        planned_r=planned_r,
    )


def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return steps * step


def calculate_broker_position_size(
    request: BrokerPositionSizeRequest,
    specification: InstrumentSpecification,
) -> BrokerPositionSizeResult:
    risk_budget = request.account_equity * request.risk_percent / Decimal("100")
    stop_distance = abs(request.entry - request.stop)
    stop_ticks = stop_distance / specification.tick_size
    stop_loss_per_quantity = (
        stop_ticks
        * specification.tick_value_per_quantity_unit
        * request.conversion_rate_to_account
    )
    spread = specification.estimated_spread or Decimal("0")
    spread_cost = (
        spread
        / specification.tick_size
        * specification.tick_value_per_quantity_unit
        * request.conversion_rate_to_account
    )
    slippage_cost = (
        request.estimated_slippage
        / specification.tick_size
        * specification.tick_value_per_quantity_unit
        * request.conversion_rate_to_account
    )
    commission = (
        specification.commission_per_quantity
        * Decimal("2")
        * request.conversion_rate_to_account
    )
    cost_per_quantity = spread_cost + slippage_cost + commission
    total_loss_per_quantity = stop_loss_per_quantity + cost_per_quantity
    if total_loss_per_quantity <= 0:
        raise ValueError("calculated loss per quantity must be positive")

    raw_quantity = risk_budget / total_loss_per_quantity
    quantity = _round_down_to_step(raw_quantity, specification.quantity_step)
    limited_by = None
    if quantity > specification.maximum_quantity:
        quantity = _round_down_to_step(
            specification.maximum_quantity, specification.quantity_step
        )
        limited_by = "broker_maximum_quantity"
    if quantity < specification.minimum_quantity:
        raise ValueError(
            "risk budget is too small for the broker minimum quantity at this stop"
        )

    estimated_margin = None
    if specification.margin_rate is not None:
        estimated_margin = (
            abs(request.entry)
            * specification.contract_size
            * quantity
            * specification.margin_rate
            * request.conversion_rate_to_account
        )
        if request.available_margin is not None and estimated_margin > request.available_margin:
            margin_quantity = (
                request.available_margin
                / (
                    abs(request.entry)
                    * specification.contract_size
                    * specification.margin_rate
                    * request.conversion_rate_to_account
                )
            )
            quantity = _round_down_to_step(
                margin_quantity, specification.quantity_step
            )
            if quantity < specification.minimum_quantity:
                raise ValueError("available margin is below the broker minimum quantity")
            limited_by = "available_margin"
            estimated_margin = (
                abs(request.entry)
                * specification.contract_size
                * quantity
                * specification.margin_rate
                * request.conversion_rate_to_account
            )

    estimated_loss = stop_loss_per_quantity * quantity
    estimated_costs = cost_per_quantity * quantity
    planned_r = None
    if request.target is not None:
        reward_ticks = abs(request.target - request.entry) / specification.tick_size
        gross_reward = (
            reward_ticks
            * specification.tick_value_per_quantity_unit
            * request.conversion_rate_to_account
            * quantity
        )
        total_risk = estimated_loss + estimated_costs
        planned_r = ((gross_reward - estimated_costs) / total_risk).quantize(
            Decimal("0.0001")
        )

    return BrokerPositionSizeResult(
        quantity=quantity,
        risk_budget=risk_budget.quantize(Decimal("0.01")),
        estimated_loss_at_stop=estimated_loss.quantize(Decimal("0.01")),
        estimated_costs=estimated_costs.quantize(Decimal("0.01")),
        estimated_margin=(
            estimated_margin.quantize(Decimal("0.01"))
            if estimated_margin is not None
            else None
        ),
        stop_ticks=stop_ticks,
        planned_r=planned_r,
        limited_by=limited_by,
    )
