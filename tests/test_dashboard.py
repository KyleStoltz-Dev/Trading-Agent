from __future__ import annotations

from pathlib import Path

DASHBOARD = Path(__file__).parents[1] / "app" / "static" / "index.html"


def test_dashboard_starts_without_fabricated_trading_evidence() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    for fabricated_value in (
        "$150,500.00",
        "$150,742.80",
        "68.4%",
        "US Nonfarm Payrolls",
        "FOMC meeting minutes",
        "POI reversal",
        "Redistribution",
        "2 preview",
    ):
        assert fabricated_value not in html
    assert "Data not loaded" in html
    assert "No live data loaded" in html
    assert "decision support only · no order execution" in html


def test_dashboard_escapes_api_and_journal_text_before_html_rendering() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    assert "const escapeHtml" in html
    assert "escapeHtml(position.instrument)" in html
    assert "escapeHtml(trade.instrument" in html
    assert "replaceChildren(dot, document.createTextNode(String(label)))" in html


def test_dashboard_can_send_the_required_journal_scope() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'id="workspace-id"' in html
    assert 'id="account-id"' in html
    assert 'headers["X-Workspace-ID"] = state.workspaceId' in html
    assert 'headers["X-Account-ID"] = state.accountId' in html
    assert "requireBrokerScope" in html
    assert "if (!requireKey() || !requireBrokerScope()) return;" in html
    assert "requireJournalScope" in html


def test_dashboard_grid_children_allow_mobile_table_scrolling() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    assert ".dashboard-grid > *, .bottom-grid > *, .stack > * { min-width: 0; }" in html
