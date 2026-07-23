from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import PositionSizeRequest, TradePlanCreate
from app.services.risk import calculate_position_size


def test_position_size_and_planned_r() -> None:
    result = calculate_position_size(
        PositionSizeRequest(
            account_equity="10000",
            risk_percent="1",
            entry="2000",
            stop="1990",
            target="2040",
            value_per_price_unit="1",
        )
    )

    assert result.risk_amount == Decimal("100.00")
    assert result.stop_distance == Decimal("10")
    assert result.quantity == Decimal("10.00000000")
    assert result.planned_r == Decimal("4.0000")


def test_position_size_accounts_for_contract_value() -> None:
    result = calculate_position_size(
        PositionSizeRequest(
            account_equity="25000",
            risk_percent="0.5",
            entry="2325",
            stop="2320",
            value_per_price_unit="100",
        )
    )

    assert result.risk_amount == Decimal("125.00")
    assert result.quantity == Decimal("0.25000000")


def test_long_trade_rejects_stop_above_entry() -> None:
    with pytest.raises(ValidationError):
        TradePlanCreate(
            account_equity="10000",
            risk_percent="1",
            entry="100",
            stop="101",
            target="110",
            value_per_price_unit="1",
            instrument="XAUUSD",
            direction="long",
            setup_name="test",
            context_timeframe="4h",
            trigger_timeframe="5m",
            thesis="test thesis",
            invalidation="test invalidation",
        )

