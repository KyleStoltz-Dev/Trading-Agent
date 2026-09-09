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


def test_dashboard_renders_verified_ohlc_as_candlesticks() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'id="candle-series"' in html
    assert 'id="chart-type"' in html
    assert "const drawCandles = (items) =>" in html
    assert "const drawLine = (items) =>" in html
    assert "openValue: Number(item.open)" in html
    assert "highValue: Number(item.high)" in html
    assert "lowValue: Number(item.low)" in html
    assert "closeValue: Number(item.close)" in html
    assert "drawCandles(state.marketCandles)" in html
    assert "OHLC candles" in html
    assert '$("chart-type").addEventListener("change", renderMarketChart)' in html


def test_dashboard_can_switch_instruments_from_the_market_card() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'id="market-symbol"' in html
    assert 'role="combobox"' in html
    assert 'id="instrument-menu" role="listbox"' in html
    assert "const instrumentCatalogByProvider = new Map();" in html
    assert "function filteredInstruments(query" in html
    assert 'new URL("/api/market-instruments", location.origin)' in html
    assert 'url.searchParams.set("limit", "50")' in html
    assert "window.setTimeout(() => loadInstrumentCatalog(query), 140)" in html
    assert "activeInstrumentQuery === query" in html
    assert "async function chooseInstrument(rawValue)" in html
    assert '$("market-symbol").addEventListener("input"' in html
    assert '$("instrument-toggle").addEventListener("click"' in html
    assert 'url.searchParams.set("instrument", state.instrument)' in html
    assert "syncInstrument(data.instrument)" in html
    assert 'symbol: "XAU_USD"' not in html


def test_dashboard_routes_natural_actions_through_the_shared_agent_gateway() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'id="agent-form"' in html
    assert 'data-agent-prompt="Give me a concise day-start brief' in html
    assert 'fetch("/api/agent/context"' in html
    assert 'fetch("/api/agent/models"' in html
    assert 'fetch("/api/agent/sessions"' in html
    assert "fetch(`/api/agent/sessions/${sessionId}/messages`" in html
    assert 'bubble.textContent = String(text)' in html
    assert "No broker credentials are sent to the browser" in html


def test_dashboard_consumes_and_immediately_removes_ephemeral_launch_fragment() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'launchFragment.get("session")' in html
    assert 'history.replaceState(null, "", `${location.pathname}${location.search}`)' in html
    assert 'fetch("/api/dashboard/session"' in html
    assert "window.setTimeout(bootstrapDashboard, 0)" in html
    assert "await loadBroker();" in html
    assert "await loadMarket();" in html


def test_dashboard_recovers_partial_connections_and_reports_offline_server() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")

    assert "Promise.allSettled([loadAgentModels(), loadStrategies()])" in html
    assert "Broker and market sync continue independently" in html
    assert "Trading-Agent server offline — run trade dashboard" in html
    assert "window.setInterval(checkDashboardServer, 15000)" in html
