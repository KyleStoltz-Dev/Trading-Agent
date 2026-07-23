from decimal import ROUND_DOWN, Decimal

from app.schemas import PositionSizeRequest, PositionSizeResult


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
