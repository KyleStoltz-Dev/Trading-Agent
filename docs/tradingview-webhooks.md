# TradingView webhook integration

TradingView is an inbound chart-alert source. It is not the authoritative broker feed and it
does not expose any order capability in Trading Agent.

## What is stored

Each accepted alert stores:

- the configured workspace and route-selected trading account;
- a trader-defined, replay-safe event ID;
- alert name, exchange, symbol, timeframe, event type, and condition;
- the UTC trigger time and app receipt time;
- optional OHLCV values;
- bounded note and scalar metadata values as untrusted evidence;
- a payload hash and verification method.

The full HTTP request, credentials, cookies, and TLS certificate are not stored. A repeated
event ID or identical payload returns the existing record rather than creating another row.

## Required production topology

TradingView requires a public webhook URL on port 80 or 443 and cancels requests that take
longer than three seconds. A local-only laptop URL cannot receive its alerts directly.

Use this boundary:

```text
TradingView
  -> public HTTPS reverse proxy
     - allow only TradingView source IPs
     - verify TradingView's TLS client certificate
     - rate-limit by source and account path
     - strip all inbound X-TradingView-* headers
     - set the three verified headers below
  -> Trading Agent bound to loopback/private network
  -> PostgreSQL
```

After the proxy has verified the request, it must set:

```text
X-TradingView-Webhook-Verified: true
X-TradingView-Rate-Limit-Verified: true
X-TradingView-Client-Identity: webhook-server@tradingview.com
X-TradingView-Source-IP: <verified original TradingView IP>
```

The app trusts those headers only when the direct network peer belongs to
`TRADINGVIEW_TRUSTED_PROXY_CIDRS`. Keep that setting narrow. Never expose the app directly
while loopback is configured as a trusted proxy, and never configure a public catch-all CIDR.

Enable the receiver only after that boundary is working:

```dotenv
TRADINGVIEW_WEBHOOK_ENABLED=true
TRADINGVIEW_WEBHOOK_MAX_REQUEST_BYTES=32768
TRADINGVIEW_WEBHOOK_REQUESTS_PER_MINUTE=60
TRADINGVIEW_WEBHOOK_MAX_DELIVERY_AGE_SECONDS=300
TRADINGVIEW_WEBHOOK_FUTURE_SKEW_SECONDS=60
TRADINGVIEW_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128
```

The guided setup can write the enable/disable flag, but it cannot configure your public
proxy:

```bash
trade setup --tradingview enabled
```

The public webhook URL is:

```text
https://your-host.example/api/webhooks/tradingview/{account_id}
```

Replace `{account_id}` with the `Current internal UUID` shown by
`trade account list`. Broker configuration also returns this value as `account_id`.
It is the `trading_accounts.id` value, not the broker login/account number. The app
resolves that UUID only inside `TRADING_WORKSPACE`; an unknown account or an account
from another workspace fails authentication.

Create the account-specific webhook secret before making the alert:

```bash
trade account tradingview-secret
```

The command displays the plaintext once. PostgreSQL stores only its SHA-256 digest.
Running it again rotates the secret and immediately invalidates the previous value.

Use a different URL for every account. Do not reuse one account's webhook URL for another
account or try to choose an account from alert JSON: the route fixes scope before the body is
parsed, and payload fields cannot override it.

Do not place the Trading Agent API key, broker token, login, password, or private journal
text in the URL or alert body. The only credential accepted in an alert is the dedicated,
account-specific `webhook_secret`; it grants only alert ingestion and must not be reused
elsewhere. The internal account UUID is an identifier, not a credential, and proxy
verification remains mandatory.

## Alert message

Create the alert in TradingView's chart UI and use valid JSON as its message:

```json
{
  "webhook_secret": "paste-the-one-time-account-secret-here",
  "sent_at": "{{timenow}}",
  "event_id": "wyckoff-spring-{{exchange}}-{{ticker}}-{{interval}}-{{time}}",
  "alert_name": "Wyckoff spring candidate",
  "exchange": "{{exchange}}",
  "symbol": "{{ticker}}",
  "timeframe": "{{interval}}",
  "event_type": "spring_candidate",
  "condition": "price reclaimed the configured range low",
  "market_time": "{{time}}",
  "open": "{{open}}",
  "high": "{{high}}",
  "low": "{{low}}",
  "close": "{{close}}",
  "volume": "{{volume}}",
  "metadata": {
    "definition_version": "wyckoff-spring-v1"
  }
}
```

Keep `event_id` unique per condition and triggered bar. `sent_at` is the alert fire time and
must remain `{{timenow}}`; the receiver rejects stale or implausibly future deliveries.
`market_time` is the timestamp of the bar that caused the alert and can legitimately be older
on higher timeframes; the app separately records when it received the request.
TradingView replaces the placeholders when the alert fires. If a market does not supply
volume, remove the `volume` field rather than sending an empty value.

Creating an alert event in Pine does not create the running alert automatically; the trader
still selects the condition, message, and webhook URL in TradingView.

## Agent behavior

The agent can retrieve recent alerts by symbol and timeframe and cites each stored alert it
uses, but only for the current workspace/account. Every result is wrapped as untrusted
external data. An alert:

- cannot change the account selected by the route;
- cannot switch the active immutable strategy;
- cannot create, modify, hedge, close, or cancel an order;
- cannot increase permitted risk;
- cannot replace a fresh broker quote, account state, fill, or firm-side rule state;
- is a condition to investigate, not a recommendation to trade.

For a pre-trade decision, pair relevant TradingView alerts with current OANDA/MT4/MT5 state,
calendar/news evidence, the exact active strategy, account restrictions, and mindset check.
