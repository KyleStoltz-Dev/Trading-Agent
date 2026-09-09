import json

import pytest

from app.schemas import DashboardCustomizeRequest, DashboardLayoutSpec
from app.services.dashboard_customization import (
    DashboardCustomizationError,
    customize_dashboard_layout,
)


def _layout() -> dict:
    return {
        "version": 1,
        "name": "Trading Command Center",
        "accent": "teal",
        "density": "comfortable",
        "width": "wide",
        "cardStyle": "rounded",
        "widgets": {
            "balance": True,
            "equity": True,
            "profit-target": True,
            "win-rate": True,
            "drawdown": True,
            "market-chart": True,
            "risk-distribution": True,
            "connections": True,
            "journal": True,
            "position-sizing": True,
            "positions": True,
            "calendar": True,
        },
        "sectionOrder": ["metrics", "market", "workflow", "context"],
    }


class _Provider:
    name = "test-provider"
    model = "test-model"

    def __init__(self, response: dict | str) -> None:
        self.response = response
        self.call = None

    def complete(self, **kwargs) -> str:
        self.call = kwargs
        return self.response if isinstance(self.response, str) else json.dumps(self.response)


def test_customize_dashboard_accepts_only_validated_layout_output() -> None:
    changed = _layout()
    changed.update(
        name="Focused journal",
        accent="blue",
        density="compact",
        sectionOrder=["workflow", "metrics", "market", "context"],
    )
    changed["widgets"]["calendar"] = False
    provider = _Provider({"spec": changed, "summary": "Focused the dashboard on journal work."})
    request = DashboardCustomizeRequest(
        request="Make the journal first, blue, and compact. Hide calendar.",
        current=DashboardLayoutSpec.model_validate(_layout()),
    )

    spec, summary = customize_dashboard_layout(provider, request)

    assert spec.name == "Focused journal"
    assert spec.accent == "blue"
    assert spec.widgets["calendar"] is False
    assert spec.section_order[0] == "workflow"
    assert summary == "Focused the dashboard on journal work."
    assert provider.call["tools"] == []
    assert provider.call["max_tool_rounds"] == 1


def test_customize_dashboard_rejects_model_output_with_unknown_widget() -> None:
    changed = _layout()
    changed["widgets"]["invented-profit"] = True
    provider = _Provider({"spec": changed, "summary": "Added a profit forecast."})
    request = DashboardCustomizeRequest(
        request="Add a profit forecast.",
        current=DashboardLayoutSpec.model_validate(_layout()),
    )

    with pytest.raises(DashboardCustomizationError, match="no changes were applied"):
        customize_dashboard_layout(provider, request)


def test_dashboard_layout_requires_every_section_exactly_once() -> None:
    changed = _layout()
    changed["sectionOrder"] = ["metrics", "market", "workflow", "workflow"]

    with pytest.raises(ValueError, match="every dashboard section"):
        DashboardLayoutSpec.model_validate(changed)
