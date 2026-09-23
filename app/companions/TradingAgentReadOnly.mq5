// Trading Agent experimental local companion. Reads only; never submits orders.
#property strict
#property version "1.04"
#property description "Read-only account, quote and recent-deal snapshots to your local Trading Agent."
#define COMPANION_BUILD_ID "development"

input long ExpectedAccount = 0;
input string ExpectedServer = "";
input string QuoteSymbol = "XAUUSD";
input int ReceiverPort = 8766;
input string ReceiverToken = "";

string last_status = "";
string last_quote_status = "";
string last_reload_request = "";
const int MAX_DEALS = 500;
ulong last_snapshot_msc = 0;

void Status(const string message)
{
   if(message != last_status)
   {
      Print("Trading Agent: ", message);
      last_status = message;
   }
}

string Q(const string value)
{
   string result = "\"";
   for(int i = 0; i < StringLen(value); i++)
   {
      ushort c = StringGetCharacter(value, i);
      if(c == 34) result += "\\\"";
      else if(c == 92) result += "\\\\";
      else if(c < 32) result += StringFormat("\\u%04x", (int)c);
      else result += StringSubstr(value, i, 1);
   }
   return result + "\"";
}

string Number(const double value)
{
   // Decimal strings preserve the submitted precision across JSON runtimes.
   // Nonfinite terminal values become null and are rejected by the receiver.
   if(!MathIsValidNumber(value)) return "null";
   return Q(DoubleToString(value, 10));
}

string Ticket(const ulong value)
{
   return Q(StringFormat("%I64u", value));
}

bool PinnedAccount()
{
   return AccountInfoInteger(ACCOUNT_LOGIN) == ExpectedAccount &&
          AccountInfoString(ACCOUNT_SERVER) == ExpectedServer;
}

bool CheckLocalRefresh()
{
   // A fixed, local marker only. Never accept commands, code or paths over HTTP.
   const string marker = "TradingAgentReadOnly.reload";
   if(!FileIsExist(marker)) return false;
   int handle = FileOpen(marker, FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ);
   if(handle == INVALID_HANDLE) return false;
   if(FileSize(handle) != 65) { FileClose(handle); return false; }
   string build = FileReadString(handle);
   FileClose(handle);
   if(StringLen(build) != 64) return false;
   for(int i = 0; i < 64; i++)
   {
      ushort c = StringGetCharacter(build, i);
      if(!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
   }
   if(build == COMPANION_BUILD_ID || build == last_reload_request) return false;
   // Template application cannot expand trading permissions. Also fail closed
   // when our current instance has trading or DLL permission, even though this
   // companion contains neither trading nor DLL calls.
   if(!PinnedAccount() ||
      (MQLInfoInteger(MQL_TRADE_ALLOWED) && TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) ||
      MQLInfoInteger(MQL_DLLS_ALLOWED))
   {
      Status("Local refresh paused: account must match and trading/DLL permissions must be off.");
      return false;
   }
   // Persist the attempted build so a failed/cached reload cannot loop after
   // OnInit resets globals. Only the local updater resets this fixed marker.
   const string attempted = "TradingAgentReadOnly.reload-attempt";
   handle = FileOpen(attempted, FILE_READ|FILE_TXT|FILE_ANSI|FILE_SHARE_READ);
   if(handle != INVALID_HANDLE)
   {
      string previous = FileSize(handle) == 65 ? FileReadString(handle) : "";
      FileClose(handle);
      if(previous == build) return false;
   }
   handle = FileOpen(attempted, FILE_WRITE|FILE_BIN|FILE_ANSI);
   if(handle == INVALID_HANDLE) return false;
   uint written = FileWriteString(handle, build + "\n", 65);
   FileClose(handle);
   if(written != 65) return false;
   last_reload_request = build;
   const string reload_template = "TradingAgentReadOnly-reload.tpl";
   // Save only our own chart, including the already-paired inputs. No other
   // chart, profile, EA, account login or terminal-wide permission is changed.
   if(!ChartSaveTemplate(0, reload_template) || !ChartApplyTemplate(0, reload_template))
   {
      Status("Local refresh could not be queued; the current companion remains active.");
      return false;
   }
   Print("Trading Agent: local companion refresh queued; waiting for new-build acknowledgement.");
   return true;
}

string CaptureTime()
{
   MqlDateTime dt;
   TimeToStruct(TimeGMT(), dt);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
                       dt.year, dt.mon, dt.day, dt.hour, dt.min, dt.sec);
}

void QuoteStatus(const string message, const bool list_candidates = false)
{
   if(message == last_quote_status) return;
   Print("Trading Agent: ", message);
   last_quote_status = message;
   if(!list_candidates) return;
   int total = SymbolsTotal(false);
   int limit = total < 10000 ? total : 10000;
   int found = 0;
   for(int i = 0; i < limit && found < 20; i++)
   {
      string name = SymbolName(i, false);
      string upper = name;
      StringToUpper(upper);
      if(StringFind(upper, "XAU") < 0 && StringFind(upper, "GOLD") < 0) continue;
      bool custom = false;
      if(!SymbolExist(name, custom) || custom) continue;
      // Report broker-provided names, never silently substitute another instrument.
      Print("Trading Agent: gold symbol candidate from broker: ", Q(name));
      found++;
   }
   if(found == 0)
      Print("Trading Agent: no gold-named symbols found in the bounded broker catalog scan.");
}

string BrokerQuote()
{
   bool custom = false;
   if(!SymbolExist(QuoteSymbol, custom) || custom)
   {
      QuoteStatus("Configured quote symbol is unavailable as a broker symbol: " + Q(QuoteSymbol), true);
      return "null";
   }
   // Subscribe to the exact configured symbol. A chart on EURUSD does not
   // automatically subscribe to gold. This selects market data, not an order.
   ResetLastError();
   if(!SymbolSelect(QuoteSymbol, true))
   {
      int error = GetLastError();
      QuoteStatus("Cannot select quote symbol " + Q(QuoteSymbol) +
                  "; terminal error " + IntegerToString(error), true);
      return "null";
   }
   MqlTick tick;
   if(!SymbolInfoTick(QuoteSymbol, tick) || tick.time_msc <= 0)
   {
      QuoteStatus("Waiting for the first broker tick for " + Q(QuoteSymbol));
      return "null";
   }
   if(!MathIsValidNumber(tick.bid) || !MathIsValidNumber(tick.ask) ||
      tick.bid <= 0 || tick.ask < tick.bid)
   {
      QuoteStatus("Broker tick has no usable bid/ask for " + Q(QuoteSymbol));
      return "null";
   }
   QuoteStatus("Broker bid/ask available for " + Q(QuoteSymbol) +
               ". Tick freshness remains separate from connection health.");
   return "{\"symbol\":" + Q(QuoteSymbol) +
      ",\"bid\":" + Number(tick.bid) + ",\"ask\":" + Number(tick.ask) +
      ",\"broker_time_msc\":" + IntegerToString(tick.time_msc) + "}";
}

string BrokerPositions()
{
   ResetLastError();
   int total = PositionsTotal();
   if(total > 500 || GetLastError() != 0) return "null";
   string result = "[";
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) return "null";
      long side = PositionGetInteger(POSITION_TYPE);
      if(side != POSITION_TYPE_BUY && side != POSITION_TYPE_SELL) return "null";
      if(i > 0) result += ",";
      result += "{\"ticket\":" + Ticket(ticket) +
         ",\"symbol\":" + Q(PositionGetString(POSITION_SYMBOL)) +
         ",\"side\":" + Q(side == POSITION_TYPE_BUY ? "buy" : "sell") +
         ",\"volume_lots\":" + Number(PositionGetDouble(POSITION_VOLUME)) +
         ",\"open_price\":" + Number(PositionGetDouble(POSITION_PRICE_OPEN)) +
         ",\"unrealized_pnl\":" + Number(PositionGetDouble(POSITION_PROFIT)) + "}";
   }
   if(GetLastError() != 0 || PositionsTotal() != total) return "null";
   return result + "]";
}

string BrokerCandles(const ENUM_TIMEFRAMES timeframe, const string label)
{
   MqlRates rates[];
   // Bounded chart evidence, not a full-price-history download. CopyRates can
   // initiate loading; an unavailable series is omitted until a later timer.
   int count = CopyRates(QuoteSymbol, timeframe, 0, 50, rates);
   if(count <= 0) return "";
   string result = "{\"symbol\":" + Q(QuoteSymbol) + ",\"timeframe\":" + Q(label) + ",\"candles\":[";
   for(int i = 0; i < count; i++)
   {
      if(i > 0) result += ",";
      result += "{\"broker_time_seconds\":" + IntegerToString((long)rates[i].time) +
         ",\"open\":" + Number(rates[i].open) + ",\"high\":" + Number(rates[i].high) +
         ",\"low\":" + Number(rates[i].low) + ",\"close\":" + Number(rates[i].close) +
         ",\"tick_volume\":" + IntegerToString(rates[i].tick_volume) +
         ",\"complete\":" + (i < count - 1 ? "true" : "false") + "}";
   }
   return result + "]}";
}

string CandleEvidence()
{
   bool custom = false;
   if(!SymbolExist(QuoteSymbol, custom) || custom) return "[]";
   ENUM_TIMEFRAMES periods[4] = {PERIOD_H4, PERIOD_M15, PERIOD_M5, PERIOD_M1};
   string labels[4] = {"H4", "M15", "M5", "M1"};
   string result = "[";
   for(int i = 0; i < 4; i++)
   {
      string series = BrokerCandles(periods[i], labels[i]);
      if(series == "") continue;
      if(StringLen(result) > 1) result += ",";
      result += series;
   }
   return result + "]";
}

bool Snapshot(string &body)
{
   if(!TerminalInfoInteger(TERMINAL_CONNECTED) || !PinnedAccount())
   {
      Status("Paused: terminal disconnected or account/server changed. No data sent.");
      return false;
   }
   ResetLastError();
   string captured = CaptureTime();
   string account = "{\"currency\":" + Q(AccountInfoString(ACCOUNT_CURRENCY)) +
      ",\"balance\":" + Number(AccountInfoDouble(ACCOUNT_BALANCE)) +
      ",\"equity\":" + Number(AccountInfoDouble(ACCOUNT_EQUITY)) +
      ",\"margin_used\":" + Number(AccountInfoDouble(ACCOUNT_MARGIN)) +
      ",\"margin_available\":" + Number(AccountInfoDouble(ACCOUNT_MARGIN_FREE)) + "}";
   if(GetLastError() != 0)
   {
      Status("Account read failed; no data sent.");
      return false;
   }

   string quote = BrokerQuote();
   string positions = BrokerPositions();
   string candles = CandleEvidence();

   // MT5 history selection uses server time. Keep it raw; never guess a UTC offset.
   ResetLastError();
   datetime until = TimeCurrent();
   datetime since = until - 7 * 86400;
   if(until <= 0 || !HistorySelect(since, until))
   {
      Status("Recent history unavailable; no data sent. The receiver will mark old data stale.");
      return false;
   }
   int total = HistoryDealsTotal();
   int first = total > MAX_DEALS ? total - MAX_DEALS : 0;
   string deals = "[";
   for(int i = first; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
      {
         Status("History changed or could not be read; waiting for the next snapshot.");
         return false;
      }
      if(i > first) deals += ",";
      // Preserve all deal types, including cash movements. These are not all fills.
      // Cancelled/rejected orders are not deals and need a separate order-history phase.
      deals += "{\"ticket\":" + Ticket(ticket) +
         ",\"order_ticket\":" + Ticket((ulong)HistoryDealGetInteger(ticket, DEAL_ORDER)) +
         ",\"position_id\":" + Ticket((ulong)HistoryDealGetInteger(ticket, DEAL_POSITION_ID)) +
         ",\"symbol\":" + Q(HistoryDealGetString(ticket, DEAL_SYMBOL)) +
         ",\"deal_type\":" + IntegerToString(HistoryDealGetInteger(ticket, DEAL_TYPE)) +
         ",\"entry_type\":" + IntegerToString(HistoryDealGetInteger(ticket, DEAL_ENTRY)) +
         ",\"broker_time_msc\":" + IntegerToString(HistoryDealGetInteger(ticket, DEAL_TIME_MSC)) +
         ",\"volume_lots\":" + Number(HistoryDealGetDouble(ticket, DEAL_VOLUME)) +
         ",\"price\":" + Number(HistoryDealGetDouble(ticket, DEAL_PRICE)) +
         ",\"profit\":" + Number(HistoryDealGetDouble(ticket, DEAL_PROFIT)) +
         ",\"commission\":" + Number(HistoryDealGetDouble(ticket, DEAL_COMMISSION)) +
         ",\"swap\":" + Number(HistoryDealGetDouble(ticket, DEAL_SWAP)) +
         ",\"fee\":" + Number(HistoryDealGetDouble(ticket, DEAL_FEE)) + "}";
   }
   deals += "]";
   if(GetLastError() != 0 || !TerminalInfoInteger(TERMINAL_CONNECTED) || !PinnedAccount())
   {
      Status("Snapshot changed or read failed; no data sent.");
      return false;
   }
   // A terminal-uptime sequence survives EA reattachment/recompilation without
   // weakening receiver replay checks. Restart pairing after a Wine/OS restart.
   long sequence = (long)GetTickCount64();
   body = "{\"schema_version\":1,\"companion_build\":" + Q(COMPANION_BUILD_ID) +
      ",\"platform\":\"mt5\",\"read_only\":true," +
      "\"terminal_connected\":true,\"account_id\":" + Q(IntegerToString(ExpectedAccount)) +
      ",\"broker_server\":" + Q(ExpectedServer) +
      ",\"sequence\":" + IntegerToString(sequence) +
      ",\"captured_at\":" + Q(captured) +
      ",\"market_time_basis\":\"broker_server_unconverted\",\"account\":" + account +
      ",\"quote\":" + quote + ",\"positions\":" + positions +
      ",\"candle_series\":" + candles + ",\"history\":{\"coverage\":\"recent_window\"," +
      "\"broker_from_seconds\":" + IntegerToString((long)since) +
      ",\"broker_to_seconds\":" + IntegerToString((long)until) +
      ",\"total_deals\":" + IntegerToString(total) +
      ",\"truncated\":" + (total > MAX_DEALS ? "true" : "false") +
      ",\"deals\":" + deals + "}}";
   return true;
}

bool ReadTimeframe(const string label, ENUM_TIMEFRAMES &period)
{
   string labels[21] = {"M1","M2","M3","M4","M5","M6","M10","M12","M15","M20","M30",
                        "H1","H2","H3","H4","H6","H8","H12","D1","W1","MN1"};
   ENUM_TIMEFRAMES periods[21] = {PERIOD_M1,PERIOD_M2,PERIOD_M3,PERIOD_M4,PERIOD_M5,PERIOD_M6,
      PERIOD_M10,PERIOD_M12,PERIOD_M15,PERIOD_M20,PERIOD_M30,PERIOD_H1,PERIOD_H2,PERIOD_H3,
      PERIOD_H4,PERIOD_H6,PERIOD_H8,PERIOD_H12,PERIOD_D1,PERIOD_W1,PERIOD_MN1};
   for(int i = 0; i < 21; i++)
      if(label == labels[i]) { period = periods[i]; return true; }
   return false;
}

bool DigitsOnly(const string value, const int max_length)
{
   if(StringLen(value) < 1 || StringLen(value) > max_length) return false;
   for(int i = 0; i < StringLen(value); i++)
   {
      ushort c = StringGetCharacter(value, i);
      if(c < '0' || c > '9') return false;
   }
   return true;
}

void HandleCandleRead()
{
   if(!PinnedAccount() || !TerminalInfoInteger(TERMINAL_CONNECTED)) return;
   string base = "http://127.0.0.1:" + IntegerToString(ReceiverPort);
   string headers = "Authorization: Bearer " + ReceiverToken + "\r\n";
   char empty[], response[];
   string response_headers;
   int code = WebRequest("GET", base + "/v1/companion/candle-request", headers,
                         1000, empty, response, response_headers);
   if(code != 200 || ArraySize(response) > 128) return;
   string fields[];
   string wire = CharArrayToString(response, 0, WHOLE_ARRAY, CP_UTF8);
   if(StringSplit(wire, '|', fields) != 4 || StringLen(fields[0]) != 32) return;
   for(int i = 0; i < 32; i++)
   {
      ushort c = StringGetCharacter(fields[0], i);
      if(!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return;
   }
   ENUM_TIMEFRAMES period;
   if(!ReadTimeframe(fields[1], period) || !DigitsOnly(fields[2], 4) ||
      !DigitsOnly(fields[3], 10)) return;
   int requested = (int)StringToInteger(fields[2]);
   long before = StringToInteger(fields[3]);
   if(requested < 1 || requested > 5000 || before < 0 || before > 4102444800) return;
   bool custom = false;
   if(!SymbolExist(QuoteSymbol, custom) || custom || !SymbolSelect(QuoteSymbol, true)) return;
   MqlRates rates[];
   int count = before == 0 ? CopyRates(QuoteSymbol, period, 0, requested, rates) :
      CopyRates(QuoteSymbol, period, (datetime)(before - 1), requested, rates);
   datetime current_bar = iTime(QuoteSymbol, period, 0);
   if(current_bar <= 0 || count > requested) count = -1;
   string candles = "[";
   for(int i = 0; i < count; i++)
   {
      if(i > 0) candles += ",";
      candles += "{\"broker_time_seconds\":" + IntegerToString((long)rates[i].time) +
         ",\"open\":" + Number(rates[i].open) + ",\"high\":" + Number(rates[i].high) +
         ",\"low\":" + Number(rates[i].low) + ",\"close\":" + Number(rates[i].close) +
         ",\"tick_volume\":" + IntegerToString(rates[i].tick_volume) +
         ",\"complete\":" + (rates[i].time < current_bar ? "true" : "false") + "}";
   }
   candles += "]";
   if(!PinnedAccount() || !TerminalInfoInteger(TERMINAL_CONNECTED)) return;
   string body = "{\"request_id\":" + Q(fields[0]) +
      ",\"account_id\":" + Q(IntegerToString(ExpectedAccount)) +
      ",\"broker_server\":" + Q(ExpectedServer) + ",\"symbol\":" + Q(QuoteSymbol) +
      ",\"request\":{\"timeframe\":" + Q(fields[1]) + ",\"count\":" +
      IntegerToString(requested) + ",\"before\":" + IntegerToString(before) + "}" +
      ",\"captured_at\":" + Q(CaptureTime()) + ",\"status\":" +
      Q(count > 0 ? "ok" : "unavailable") + ",\"candles\":" + candles + "}";
   char payload[];
   StringToCharArray(body, payload, 0, WHOLE_ARRAY, CP_UTF8);
   ArrayResize(payload, ArraySize(payload) - 1);
   headers += "Content-Type: application/json\r\n";
   // A fixed candle-only response, never orders, scripts, URLs, or terminal settings.
   WebRequest("POST", base + "/v1/companion/candle-result", headers, 3000,
              payload, response, response_headers);
}

int OnInit()
{
   if(MQLInfoInteger(MQL_TESTER))
   {
      Print("Trading Agent: WebRequest is unavailable in Strategy Tester.");
      return INIT_FAILED;
   }
   if(ExpectedAccount <= 0 || ExpectedServer == "" || QuoteSymbol == "" ||
      ReceiverPort < 1024 || ReceiverPort > 65535 ||
      StringLen(ReceiverToken) < 32 || StringLen(ReceiverToken) > 128)
   {
      Print("Trading Agent: enter the receiver's account, server, symbol, port and private token.");
      return INIT_PARAMETERS_INCORRECT;
   }
   for(int i = 0; i < StringLen(ReceiverToken); i++)
   {
      ushort c = StringGetCharacter(ReceiverToken, i);
      if(!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '-' || c == '_'))
         return INIT_PARAMETERS_INCORRECT;
   }
   if(!PinnedAccount())
   {
      Print("Trading Agent: login/server mismatch. No data will be sent.");
      return INIT_FAILED;
   }
   if(!EventSetTimer(1)) return INIT_FAILED;
   Print("Trading Agent: read-only companion started. No orders can be submitted by this EA.");
   return INIT_SUCCEEDED;
}

void OnTimer()
{
   if(CheckLocalRefresh()) return;
   if(GetTickCount64() - last_snapshot_msc < 10000)
   {
      HandleCandleRead();
      return;
   }
   last_snapshot_msc = GetTickCount64();
   string body;
   if(!Snapshot(body)) return;
   char payload[], response[];
   StringToCharArray(body, payload, 0, WHOLE_ARRAY, CP_UTF8);
   ArrayResize(payload, ArraySize(payload) - 1); // omit terminating NUL
   string headers = "Content-Type: application/json\r\nAuthorization: Bearer " + ReceiverToken + "\r\n";
   string response_headers;
   // Hard-coded IPv4 loopback: never send account data to arbitrary hosts.
   string url = "http://127.0.0.1:" + IntegerToString(ReceiverPort) + "/v1/companion/snapshot";
   ResetLastError();
   int code = WebRequest("POST", url, headers, 3000, payload, response, response_headers);
   // Snapshot acknowledgements are never evaluated as commands. Do not log private data.
   if(code == 200)
      Status("Receiving confirmed. Read-only snapshot accepted; journal unchanged.");
   else if(code == -1)
      Status("Receiver unreachable. Check the local receiver and WebRequest URL allowlist.");
   else if(code == 401)
      Status("Token mismatch. Copy the current receiver token into the EA.");
   else if(code == 409)
      Status("Snapshot refused: check clock, account/server/symbol; restart receiver and EA together.");
   else
      Status("Snapshot refused (HTTP " + IntegerToString(code) + "). Journal unchanged.");
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}
