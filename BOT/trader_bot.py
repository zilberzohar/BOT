# ============================================================================
# File: trader_bot.py  (Stable, thread-safe, Streamlit-friendly)
# Fixes: NY timezone for ORB, monitor hooks, direction filter, bracket orders
# Logs: BOT/runtime_data/bot_logs/events.jsonl  +  bot.log
# ============================================================================
from __future__ import annotations
import datetime, time, json, traceback, queue
from pathlib import Path
import pandas as pd
import pandas_ta as ta  # noqa: F401
import pytz

# --- disk logging ---
LOG_DIR = Path(__file__).resolve().parent / "runtime_data" / "bot_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_JSONL = LOG_DIR / "events.jsonl"
LOG_TXT   = LOG_DIR / "bot.log"
def _now_ny_iso(): return pd.Timestamp.now(tz="America/New_York").isoformat()
def _append_jsonl(rec: dict):
    try:
        with LOG_JSONL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception: pass
def _append_log(line: str):
    try:
        with LOG_TXT.open("a", encoding="utf-8") as f:
            f.write(f"[{_now_ny_iso()}] {line}\n")
    except Exception: pass

# --- optional monitor hooks (no-op if module missing) ---
mon_info = mon_warn = mon_error = lambda *a, **k: None
for _mod in ("trade_monitor.monitor", "monitor"):
    try:
        _m = __import__(_mod, fromlist=["info","warn","error"])  # type: ignore
        mon_info = getattr(_m, "info", mon_info)
        mon_warn = getattr(_m, "warn", mon_warn)
        mon_error = getattr(_m, "error", mon_error)
        break
    except Exception:
        pass


class TradingBot:
    def __init__(self, params: dict, q: queue.Queue, stop_event=None):
        self.params = params
        self.q = q
        self.stop_event = stop_event
        # set later
        self.ib = self.util = self.Stock = self.Order = self.contract = None
        # state
        self.in_position = False
        self.historical_data = pd.DataFrame()
        self.daily_data = pd.DataFrame()
        self.market_regime = "Unknown"
        self.orb_high = None
        self.orb_low = None
        self.active_trade_details = {}
        self.last_bar_timestamp: pd.Timestamp | None = None
        self.is_new_bar_handling = False

    # ---------- utils ----------
    def _log_event(self, kind: str, **payload):
        rec = {"ts": _now_ny_iso(), "kind": kind, **payload}
        _append_jsonl(rec)
        _append_log(f"{kind} | {payload}")
    def log_and_queue(self, t: str, data): self.q.put({'type': t, 'data': data})

    # ---------- lifecycle ----------
    def start(self):
        try:
            # prepare event loop for ib_insync AFTER thread starts
            import sys, asyncio
            if sys.platform.startswith('win'):
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            try: asyncio.get_running_loop()
            except RuntimeError: asyncio.set_event_loop(asyncio.new_event_loop())

            from ib_insync import IB, Stock, Order, util
            util.startLoop()

            self.util, self.Stock, self.Order = util, Stock, Order
            self.ib = IB()
            self.contract = Stock(self.params['ticker'], 'ARCA', 'USD')

            self.log_and_queue('status', "Connecting to IBKR...")
            self.ib.connect(self.params['host'], self.params['port'], clientId=self.params['client_id'])
            self.log_and_queue('status', "✅ Connection successful. Initializing...")
            mon_info("STATE", details={"ib_connected": True}); self._log_event("STATE", ib_connected=True)

            self.run_startup_sequence()

            self.ib.barUpdateEvent += self.on_bar_update
            self.ib.orderStatusEvent += self.on_order_status
            self.ib.reqMktData(self.contract, '', False, False)
            self.ib.reqRealTimeBars(self.contract, 5, 'TRADES', False)
            self.log_and_queue('status', "Subscribed to real-time data. Bot is LIVE.")
            self._log_event("STATUS", msg="bot_live")

            while self.ib.isConnected() and not (self.stop_event and self.stop_event.is_set()):
                time.sleep(0.5)
                self.q.put({'type': 'heartbeat', 'data': datetime.datetime.now()})
        except Exception as e:
            tb = traceback.format_exc()
            self.log_and_queue('status', f"❌ CRITICAL ERROR: {e}")
            self.log_and_queue('log', tb)
            mon_error("ERROR", reason="CRITICAL", details={"error": str(e)})
            self._log_event("ERROR", error=str(e))
        finally:
            try:
                if self.ib and self.ib.isConnected(): self.ib.disconnect()
            finally:
                self.log_and_queue('status', "Disconnected from IBKR.")
                mon_info("STATE", details={"ib_connected": False})
                self._log_event("STATE", ib_connected=False)

    def run_startup_sequence(self):
        self.ib.qualifyContracts(self.contract)
        try:
            for pos in self.ib.positions():
                if pos.contract.conId == self.contract.conId and pos.position != 0:
                    self.in_position = True
                    direction = "Long" if pos.position > 0 else "Short"
                    self.active_trade_details = {'direction': direction, 'quantity': abs(pos.position), 'entry_price': pos.avgCost}
                    self.log_and_queue('status', f"⚠️ Found existing position: {direction} {abs(pos.position)} shares.")
                    self.q.put({'type':'active_trade','data': self.active_trade_details})
                    self._log_event("POSITION_RESUME", **self.active_trade_details)
                    break
        except Exception as e:
            self.log_and_queue('log', f"Positions check failed: {e}")

        self.prepare_market_data()
        self.log_and_queue('market_regime', self.market_regime)
        self._log_event("REGIME", regime=self.market_regime)

    # ---------- events ----------
    def on_order_status(self, trade):
        if trade.orderStatus.status == 'Filled':
            if not self.in_position:
                self.in_position = True
                direction = "Long" if trade.order.action == 'BUY' else "Short"
                details = {'direction': direction, 'quantity': trade.order.totalQuantity, 'entry_price': trade.orderStatus.avgFillPrice}
                self.active_trade_details = details
                self.log_and_queue('status', f"✅ TRADE FILLED: {direction} at {details['entry_price']}")
                mon_info("FILL", symbol=self.params['ticker'], side=direction, price=float(details['entry_price']))
                self._log_event("FILL", side=direction, price=float(details['entry_price']), qty=details['quantity'])
            else:
                self.log_and_queue('status', f"🎉 TRADE CLOSED at {trade.orderStatus.avgFillPrice}")
                self._log_event("CLOSE", price=float(trade.orderStatus.avgFillPrice))
                self.in_position = False
                self.active_trade_details = {}
        self.q.put({'type':'active_trade','data': self.active_trade_details})

    def on_bar_update(self, bars, hasNewBar: bool):
        if not hasNewBar or self.is_new_bar_handling: return
        try:
            self.is_new_bar_handling = True
            latest = self.fetch_historical_data(self.params['timeframe'], '1 D')
            if not latest.empty and (self.last_bar_timestamp is None or latest.index[-1] > self.last_bar_timestamp):
                self.historical_data = latest
                self.last_bar_timestamp = latest.index[-1]
                self.calculate_indicators()
                self.q.put({'type':'chart_data','data': self.historical_data.to_json(orient='split')})
                self.log_and_queue('log', f"New bar: {self.last_bar_timestamp}")
                try:
                    bar = self.historical_data.iloc[-1]
                    self._log_event("DATA", bar_time=str(self.last_bar_timestamp),
                                    open=float(bar['open']), high=float(bar['high']),
                                    low=float(bar['low']), close=float(bar['close']),
                                    volume=int(bar['volume']))
                    mon_info("DATA", symbol=self.params['ticker'], price=float(bar['close']),
                             details={"bar_time": str(self.last_bar_timestamp)})
                except Exception: pass
                self.run_strategy_logic(self.last_bar_timestamp)
        finally:
            self.is_new_bar_handling = False

    # ---------- orders/strategy ----------
    def execute_trade(self, direction: str, price: float):
        if self.in_position: return
        action = 'BUY' if direction == 'Long' else 'SELL'
        qty = self.params.get('order_quantity', 1)
        tp_pct = self.params.get('take_profit_pct', 1.0)
        sl_pct = self.params.get('stop_loss_pct', 0.5)
        tp_price = round(price * (1 + tp_pct/100 * (1 if direction=='Long' else -1)), 2)
        sl_price = round(price * (1 - sl_pct/100 * (1 if direction=='Long' else -1)), 2)

        from ib_insync import Order  # type: ignore  # (safe here; loop is ready)
        parent = Order(orderId=self.ib.client.getReqId(), action=action, orderType="LMT", totalQuantity=qty, lmtPrice=price, transmit=False)
        tp     = Order(orderId=self.ib.client.getReqId(), action=("SELL" if action=="BUY" else "BUY"), orderType="LMT", totalQuantity=qty, lmtPrice=tp_price, parentId=parent.orderId, transmit=False)
        sl     = Order(orderId=self.ib.client.getReqId(), action=("SELL" if action=="BUY" else "BUY"), orderType="STP", totalQuantity=qty, auxPrice=sl_price, parentId=parent.orderId, transmit=True)
        for o in (parent, tp, sl): self.ib.placeOrder(self.contract, o)

        self.log_and_queue('status', f"Placing {direction} bracket order for {qty} @ ~{price:.2f} (TP: {tp_price}, SL: {sl_price})")
        mon_info("ORDER", symbol=self.params['ticker'], side=direction, price=float(price), details={"qty": qty, "tp": tp_price, "sl": sl_price})
        self._log_event("ORDER", side=direction, price=float(price), qty=qty, tp=tp_price, sl=sl_price)

    def run_strategy_logic(self, current_time_dt: pd.Timestamp):
        ny_time = current_time_dt.tz_localize('America/New_York') if current_time_dt.tzinfo is None else current_time_dt.tz_convert('America/New_York')
        market_open = ny_time.replace(hour=9, minute=30, second=0, microsecond=0)
        orb_end = market_open + datetime.timedelta(minutes=self.params['orb_minutes'])

        self.log_and_queue('log', f"[ORB] now={ny_time:%Y-%m-%d %H:%M} | open=09:30 | end={orb_end:%H:%M} | ready={ny_time >= orb_end}")
        self.publish_diagnostics(current_time_dt)

        if self.orb_high is None and ny_time >= orb_end:
            self.calculate_orb(market_open, orb_end)
        if self.orb_high is not None and not self.in_position:
            self.check_breakout(self.historical_data.iloc[-1])

    def calculate_orb(self, orb_start_time: pd.Timestamp, orb_end_time: pd.Timestamp):
        if self.historical_data.empty: return
        df_ny = self.historical_data.copy()
        df_ny.index = (df_ny.index.tz_localize('America/New_York') if df_ny.index.tz is None else df_ny.index.tz_convert('America/New_York'))
        mask = (df_ny.index >= orb_start_time) & (df_ny.index <= orb_end_time)  # inclusive end
        orb_data = df_ny.loc[mask]
        if orb_data.empty:
            day_start = orb_start_time.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end   = orb_start_time.replace(hour=23, minute=59, second=59, microsecond=0)
            day_data = df_ny[(df_ny.index >= day_start) & (df_ny.index <= day_end)]
            orb_data = day_data[day_data.index <= orb_end_time]
        self.log_and_queue('log', f"[ORB] window {orb_start_time:%Y-%m-%d %H:%M}→{orb_end_time:%H:%M} | bars={len(orb_data)}")
        if not orb_data.empty:
            self.orb_high = float(orb_data['high'].max()); self.orb_low = float(orb_data['low'].min())
            self.q.put({'type':'orb_levels','data': {'high': self.orb_high, 'low': self.orb_low}})
            self.log_and_queue('status', f"ORB Calculated: H={self.orb_high:.2f}, L={self.orb_low:.2f}")
            try: mon_info("STATE", details={"orb_high": self.orb_high, "orb_low": self.orb_low})
            except Exception: pass
            self._log_event("ORB", high=self.orb_high, low=self.orb_low, bars=int(len(orb_data)))

    def check_breakout(self, bar: pd.Series):
        if self.orb_high is None or self.orb_low is None: return
        breakout = 'Long' if bar['high'] > self.orb_high else ('Short' if bar['low'] < self.orb_low else None)
        if not breakout: return
        td = self.params.get('trade_direction','Long & Short')
        if td == 'Long Only' and breakout == 'Short':
            self.log_and_queue('status','Blocked: Trade direction is Long Only'); mon_warn("BLOCK", symbol=self.params['ticker'], reason="Direction filter (Long Only)"); self._log_event("BLOCK", reason="direction_long_only"); return
        if td == 'Short Only' and breakout == 'Long':
            self.log_and_queue('status','Blocked: Trade direction is Short Only'); mon_warn("BLOCK", symbol=self.params['ticker'], reason="Direction filter (Short Only)"); self._log_event("BLOCK", reason="direction_short_only"); return
        price = float(bar['close']); vwap = bar.get('VWAP_D'); vol = float(bar['volume']); vol_sma = bar.get('VOLUME_SMA_20')
        regime_ok = not self.params.get('use_market_regime_filter', True) or (self.market_regime == ("UPTREND" if breakout=='Long' else "DOWNTREND"))
        vwap_ok   = not self.params.get('use_vwap_filter', True) or (pd.notna(vwap) and (price > vwap if breakout=='Long' else price < vwap))
        volume_ok = not self.params.get('use_volume_filter', True) or (pd.notna(vol) and pd.notna(vol_sma) and vol > vol_sma)
        all_ok = bool(regime_ok and vwap_ok and volume_ok)
        reasoning = {
            'timestamp': pd.Timestamp.now(tz='America/New_York').strftime('%H:%M:%S'),
            'price': price, 'direction': breakout,
            'regime_check': {'pass': regime_ok, 'actual': self.market_regime},
            'vwap_check':   {'pass': vwap_ok, 'price': f"{price:.2f}", 'vwap': f"{float(vwap):.2f}" if pd.notna(vwap) else "N/A"},
            'volume_check': {'pass': volume_ok, 'volume': f"{vol:,.0f}", 'sma': f"{float(vol_sma):,.0f}" if pd.notna(vol_sma) else "N/A"},
            'final_decision': "Trade Approved" if all_ok else "Trade Rejected"
        }
        self.q.put({'type':'reasoning','data': reasoning})
        if not all_ok:
            if not regime_ok: mon_warn("BLOCK", symbol=self.params['ticker'], reason="Regime filter failed", details={"regime": self.market_regime}); self._log_event("BLOCK", reason="regime")
            if not vwap_ok:   mon_warn("BLOCK", symbol=self.params['ticker'], reason="VWAP check failed", details={"price": price, "vwap": (float(vwap) if pd.notna(vwap) else None)}); self._log_event("BLOCK", reason="vwap")
            if not volume_ok: mon_warn("BLOCK", symbol=self.params['ticker'], reason="Volume filter failed", details={"volume": vol, "sma20": (float(vol_sma) if pd.notna(vol_sma) else None)}); self._log_event("BLOCK", reason="volume")
            return
        mon_info("SIGNAL", symbol=self.params['ticker'], side=breakout, price=price); self._log_event("SIGNAL", side=breakout, price=price)
        self.execute_trade(breakout, price)

    # ---------- data ----------
    def prepare_market_data(self):
        self.daily_data = self.fetch_historical_data('1 day', '1 Y')
        if not self.daily_data.empty:
            self.daily_data.ta.ema(length=50, append=True)
            if 'EMA_50' in self.daily_data:
                ema_last = self.daily_data['EMA_50'].iloc[-1]
                if pd.notna(ema_last):
                    self.market_regime = "UPTREND" if self.daily_data['close'].iloc[-1] > ema_last else "DOWNTREND"
        self.historical_data = self.fetch_historical_data(self.params['timeframe'], '1 D')
        if not self.historical_data.empty:
            self.calculate_indicators()
            self.q.put({'type':'chart_data','data': self.historical_data.to_json(orient='split')})
            self.last_bar_timestamp = self.historical_data.index[-1]

    def fetch_historical_data(self, bar_size: str, duration: str) -> pd.DataFrame:
        try:
            bars = self.ib.reqHistoricalData(self.contract, endDateTime='', durationStr=duration,
                                             barSizeSetting=bar_size, whatToShow='TRADES', useRTH=True, formatDate=2)
            if not bars: return pd.DataFrame()
            df = self.util.df(bars)
            df.set_index('date', inplace=True)
            df.index = pd.to_datetime(df.index)
            if df.index.tz is None: df.index = df.index.tz_localize('America/New_York')
            else:                   df.index = df.index.tz_convert('America/New_York')
            return df
        except Exception as e:
            self.log_and_queue('log', f"❌ Exception in fetch_historical_data: {e}")
            return pd.DataFrame()

    def calculate_indicators(self):
        if self.historical_data.empty: return
        self.historical_data.ta.vwap(append=True)
        self.historical_data.ta.sma(close='volume', length=20, append=True, col_names=('VOLUME_SMA_20',))
