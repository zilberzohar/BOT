# ==============================================================================
# File: trader_bot.py  (Stable, thread-safe, Streamlit-friendly)
# ==============================================================================
import datetime
import time
import pandas as pd
import pandas_ta as ta
import traceback
import queue
import pytz


class TradingBot:
    """
    Synchronous trading bot designed to run inside its own thread.
    We LAZY-IMPORT ib_insync INSIDE start(), after creating an event loop in this thread.
    """

    def __init__(self, params: dict, q: queue.Queue, stop_event=None):
        self.params = params
        self.q = q
        self.stop_event = stop_event

        # Will be set in start() after importing ib_insync
        self.ib = None
        self.util = None
        self.Stock = None
        self.Order = None
        self.contract = None

        # State
        self.in_position = False
        self.historical_data = pd.DataFrame()
        self.daily_data = pd.DataFrame()
        self.market_regime = "Unknown"
        self.orb_high = None
        self.orb_low = None
        self.active_trade_details = {}
        self.last_bar_timestamp = None
        self.is_new_bar_handling = False  # prevent re-entrancy

    # ---------------- Utilities ----------------
    def log_and_queue(self, msg_type: str, data):
        self.q.put({'type': msg_type, 'data': data})

    # ---------------- Lifecycle ----------------
    def start(self):
        try:
            # --- Ensure this thread has an event loop BEFORE importing ib_insync ---
            import sys, asyncio
            if sys.platform.startswith('win'):
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())
            # -----------------------------------------------------------------------

            # Now it's safe to import ib_insync/eventkit
            from ib_insync import IB, Stock, Order, util

            # Wrap the loop for GUI/threads
            util.startLoop()

            # Save references
            self.util = util
            self.Stock = Stock
            self.Order = Order
            self.ib = IB()

            # Preferred for VIXY (ETF): ARCA. (If you must use SMART, add primaryExchange='ARCA')
            self.contract = Stock(self.params['ticker'], 'ARCA', 'USD')

            self.log_and_queue('status', "Connecting to IBKR...")
            self.ib.connect(self.params['host'], self.params['port'], clientId=self.params['client_id'])
            self.log_and_queue('status', "✅ Connection successful. Initializing...")

            # If needed, force data mode:
            # self.ib.reqMarketDataType(1)  # 1=REALTIME, 3=DELAYED

            self.run_startup_sequence()

            # Events
            self.ib.barUpdateEvent += self.on_bar_update
            self.ib.orderStatusEvent += self.on_order_status

            # Streams
            self.ib.reqMktData(self.contract, '', False, False)
            self.ib.reqRealTimeBars(self.contract, 5, 'TRADES', False)

            self.log_and_queue('status', "Subscribed to real-time data. Bot is LIVE.")

            # Main loop
            while self.ib.isConnected() and not (self.stop_event and self.stop_event.is_set()):
                time.sleep(0.5)
                self.q.put({'type': 'heartbeat', 'data': datetime.datetime.now()})

        except Exception as e:
            tb_str = traceback.format_exc()
            self.log_and_queue('status', f"❌ CRITICAL ERROR: {e}")
            self.log_and_queue('log', tb_str)
        finally:
            try:
                if self.ib and self.ib.isConnected():
                    self.ib.disconnect()
            finally:
                self.log_and_queue('status', "Disconnected from IBKR.")

    # ---------------- Startup/Data Prep ----------------
    def run_startup_sequence(self):
        self.ib.qualifyContracts(self.contract)

        # Resume open position, if any
        try:
            positions = self.ib.positions()
            for pos in positions:
                if pos.contract.conId == self.contract.conId and pos.position != 0:
                    self.in_position = True
                    direction = "Long" if pos.position > 0 else "Short"
                    self.active_trade_details = {
                        'direction': direction,
                        'quantity': abs(pos.position),
                        'entry_price': pos.avgCost
                    }
                    self.log_and_queue('status', f"⚠️ Found existing position: {direction} {abs(pos.position)} shares.")
                    self.q.put({'type': 'active_trade', 'data': self.active_trade_details})
                    break
        except Exception as e:
            self.log_and_queue('log', f"Positions check failed: {e}")

        self.prepare_market_data()
        self.log_and_queue('market_regime', self.market_regime)

    # ---------------- Event Handlers ----------------
    def on_order_status(self, trade):
        if trade.orderStatus.status == 'Filled':
            if not self.in_position:
                self.in_position = True
                direction = "Long" if trade.order.action == 'BUY' else "Short"
                details = {
                    'direction': direction,
                    'quantity': trade.order.totalQuantity,
                    'entry_price': trade.orderStatus.avgFillPrice
                }
                self.active_trade_details = details
                self.log_and_queue('status', f"✅ TRADE FILLED: {direction} at {details['entry_price']}")
            else:
                self.log_and_queue('status', f"🎉 TRADE CLOSED at {trade.orderStatus.avgFillPrice}")
                self.in_position = False
                self.active_trade_details = {}
        self.q.put({'type': 'active_trade', 'data': self.active_trade_details})

    def on_bar_update(self, bars, hasNewBar: bool):
        if not hasNewBar or self.is_new_bar_handling:
            return
        try:
            self.is_new_bar_handling = True
            latest_data = self.fetch_historical_data(self.params['timeframe'], '1 D')
            if not latest_data.empty:
                if self.last_bar_timestamp is None or latest_data.index[-1] > self.last_bar_timestamp:
                    self.historical_data = latest_data
                    self.last_bar_timestamp = self.historical_data.index[-1]
                    self.calculate_indicators()
                    self.q.put({'type': 'chart_data', 'data': self.historical_data.to_json(orient='split')})
                    self.log_and_queue('log', f"New bar: {self.last_bar_timestamp}")
                    self.run_strategy_logic(self.last_bar_timestamp)
        finally:
            self.is_new_bar_handling = False

    # ---------------- Orders/Strategy ----------------
    def execute_trade(self, direction: str, price: float):
        if self.in_position:
            return
        action = 'BUY' if direction == 'Long' else 'SELL'
        quantity = self.params.get('order_quantity', 1)
        tp_pct = self.params.get('take_profit_pct', 1.0)
        sl_pct = self.params.get('stop_loss_pct', 0.5)

        tp_price = round(price * (1 + tp_pct / 100 * (1 if direction == 'Long' else -1)), 2)
        sl_price = round(price * (1 - sl_pct / 100 * (1 if direction == 'Long' else -1)), 2)

        parent = self.Order(
            orderId=self.ib.client.getReqId(),
            action=action, orderType="LMT", totalQuantity=quantity,
            lmtPrice=price, transmit=False
        )
        tp = self.Order(
            orderId=self.ib.client.getReqId(),
            action="SELL" if action == "BUY" else "BUY",
            orderType="LMT", totalQuantity=quantity,
            lmtPrice=tp_price, parentId=parent.orderId, transmit=False
        )
        sl = self.Order(
            orderId=self.ib.client.getReqId(),
            action="SELL" if action == "BUY" else "BUY",
            orderType="STP", totalQuantity=quantity,
            auxPrice=sl_price, parentId=parent.orderId, transmit=True
        )

        for o in (parent, tp, sl):
            self.ib.placeOrder(self.contract, o)

        self.log_and_queue(
            'status',
            f"Placing {direction} bracket order for {quantity} @ ~{price:.2f} (TP: {tp_price}, SL: {sl_price})"
        )

    def run_strategy_logic(self, current_time_dt: pd.Timestamp):
        ny_time = current_time_dt.tz_localize('UTC').tz_convert('America/New_York')
        market_open_time = ny_time.replace(hour=9, minute=30, second=0, microsecond=0)
        orb_end_time = market_open_time + datetime.timedelta(minutes=self.params['orb_minutes'])

        if self.orb_high is None and ny_time >= orb_end_time:
            self.calculate_orb(orb_end_time)

        if self.orb_high is not None and not self.in_position:
            self.check_breakout(self.historical_data.iloc[-1])

    def calculate_orb(self, orb_end_time: pd.Timestamp):
        df_ny = self.historical_data.tz_localize('UTC').tz_convert('America/New_York')
        orb_data = df_ny[df_ny.index < orb_end_time]
        if not orb_data.empty:
            self.orb_high = float(orb_data['high'].max())
            self.orb_low = float(orb_data['low'].min())
            self.q.put({'type': 'orb_levels', 'data': {'high': self.orb_high, 'low': self.orb_low}})
            self.log_and_queue('status', f"ORB Calculated: H={self.orb_high:.2f}, L={self.orb_low:.2f}")

    def check_breakout(self, bar: pd.Series):
        if self.orb_high is None or self.orb_low is None:
            return

        breakout_direction = None
        if bar['high'] > self.orb_high:
            breakout_direction = 'Long'
        elif bar['low'] < self.orb_low:
            breakout_direction = 'Short'
        if not breakout_direction:
            return

        price = float(bar['close'])
        vwap = bar.get('VWAP_D')
        volume = float(bar['volume'])
        vol_sma = bar.get('VOLUME_SMA_20')

        regime_ok = not self.params.get('use_market_regime_filter', True) or (
            self.market_regime == ("UPTREND" if breakout_direction == 'Long' else "DOWNTREND")
        )
        vwap_ok = not self.params.get('use_vwap_filter', True) or (
            pd.notna(vwap) and (price > vwap if breakout_direction == 'Long' else price < vwap)
        )
        volume_ok = not self.params.get('use_volume_filter', True) or (
            pd.notna(volume) and pd.notna(vol_sma) and volume > vol_sma
        )

        all_filters_passed = bool(regime_ok and vwap_ok and volume_ok)

        reasoning = {
            'timestamp': datetime.datetime.now(pytz.timezone('America/New_York')).strftime('%H:%M:%S'),
            'price': price, 'direction': breakout_direction,
            'regime_check': {'pass': regime_ok, 'actual': self.market_regime},
            'vwap_check': {'pass': vwap_ok, 'price': f"{price:.2f}",
                           'vwap': f"{float(vwap):.2f}" if pd.notna(vwap) else "N/A"},
            'volume_check': {'pass': volume_ok, 'volume': f"{volume:,.0f}",
                             'sma': f"{float(vol_sma):,.0f}" if pd.notna(vol_sma) else "N/A"},
            'final_decision': "Trade Approved" if all_filters_passed else "Trade Rejected"
        }
        self.q.put({'type': 'reasoning', 'data': reasoning})

        if all_filters_passed:
            self.execute_trade(breakout_direction, price)

    # ---------------- Data ----------------
    def prepare_market_data(self):
        # Daily for regime
        self.daily_data = self.fetch_historical_data('1 day', '1 Y')
        if not self.daily_data.empty:
            self.daily_data.ta.ema(length=50, append=True)
            if 'EMA_50' in self.daily_data:
                ema_last = self.daily_data['EMA_50'].iloc[-1]
                if pd.notna(ema_last):
                    self.market_regime = "UPTREND" if self.daily_data['close'].iloc[-1] > ema_last else "DOWNTREND"

        # Intraday matches UI timeframe
        self.historical_data = self.fetch_historical_data(self.params['timeframe'], '1 D')
        if not self.historical_data.empty:
            self.calculate_indicators()
            self.q.put({'type': 'chart_data', 'data': self.historical_data.to_json(orient='split')})
            self.last_bar_timestamp = self.historical_data.index[-1]

    def fetch_historical_data(self, bar_size: str, duration: str) -> pd.DataFrame:
        try:
            bars = self.ib.reqHistoricalData(
                self.contract,
                endDateTime='',
                durationStr=duration,
                barSizeSetting=bar_size,  # e.g., '5 mins', '1 day'
                whatToShow='TRADES',
                useRTH=True,
                formatDate=2
            )
            if not bars:
                return pd.DataFrame()
            df = self.util.df(bars)
            df.set_index('date', inplace=True)
            df.index = pd.to_datetime(df.index)
            return df
        except Exception as e:
            self.log_and_queue('log', f"❌ Exception in fetch_historical_data: {e}")
            return pd.DataFrame()

    def calculate_indicators(self):
        if self.historical_data.empty:
            return
        self.historical_data.ta.vwap(append=True)
        self.historical_data.ta.sma(
            close='volume', length=20, append=True, col_names=('VOLUME_SMA_20',)
        )
