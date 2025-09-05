# ==============================================================================
# File: trader_bot.py (Synchronous, stable version for Streamlit)
# ==============================================================================
import datetime
import pandas as pd
import pandas_ta as ta
import traceback
import queue
import pytz
from ib_insync import IB, Stock, Order, util


class TradingBot:
    def __init__(self, params, q: queue.Queue):
        self.params = params
        self.q = q
        self.ib = IB()
        self.contract = Stock(params['ticker'], 'SMART', 'USD')
        self.in_position = False
        self.historical_data = pd.DataFrame()
        self.daily_data = pd.DataFrame()
        self.market_regime = "Unknown"
        self.orb_high = None
        self.orb_low = None
        self.active_trade_details = {}
        self.last_bar_timestamp = None
        self.is_new_bar_handling = False  # Flag to prevent re-entrancy

    def log_and_queue(self, msg_type, data):
        log_msg = data
        if isinstance(data, dict): log_msg = data.get('log', str(data))
        self.q.put({'type': msg_type, 'data': data})

    def start(self):
        try:
            self.log_and_queue('status', "Connecting to IBKR...")
            # Using the synchronous connect method
            self.ib.connect(self.params['host'], self.params['port'], clientId=self.params['client_id'])
            self.log_and_queue('status', "✅ Connection successful. Initializing...")

            self.run_startup_sequence()

            self.ib.barUpdateEvent += self.on_bar_update
            self.ib.orderStatusEvent += self.on_order_status

            self.ib.reqMktData(self.contract, '', False, False)
            self.ib.reqRealTimeBars(self.contract, 5, 'TRADES', False)
            self.log_and_queue('status', "Subscribed to real-time data. Bot is LIVE.")

            while self.ib.isConnected():
                self.ib.sleep(5)  # The event loop is running in the background
                self.q.put({'type': 'heartbeat', 'data': datetime.now()})

        except Exception as e:
            tb_str = traceback.format_exc()
            self.log_and_queue('status', f"❌ CRITICAL ERROR: {e}")
        finally:
            if self.ib.isConnected():
                self.ib.disconnect()
            self.log_and_queue('status', "Disconnected from IBKR.")

    def run_startup_sequence(self):
        # Using synchronous methods
        self.ib.qualifyContracts(self.contract)

        positions = self.ib.positions()
        for pos in positions:
            if pos.contract.conId == self.contract.conId and pos.position != 0:
                self.in_position = True
                direction = "Long" if pos.position > 0 else "Short"
                self.active_trade_details = {'direction': direction, 'quantity': abs(pos.position),
                                             'entry_price': pos.avgCost}
                self.log_and_queue('status', f"⚠️ Found existing position: {direction} {abs(pos.position)} shares.")
                self.q.put({'type': 'active_trade', 'data': self.active_trade_details})
                break

        self.prepare_market_data()
        self.log_and_queue('market_regime', self.market_regime)

    def on_order_status(self, trade):
        if trade.orderStatus.status == 'Filled':
            if not self.in_position:
                self.in_position = True
                direction = "Long" if trade.order.action == 'BUY' else "Short"
                details = {'direction': direction, 'quantity': trade.order.totalQuantity,
                           'entry_price': trade.orderStatus.avgFillPrice}
                self.active_trade_details = details
                self.log_and_queue('status', f"✅ TRADE FILLED: {direction} at {details['entry_price']}")
            else:
                self.log_and_queue('status', f"🎉 TRADE CLOSED at {trade.orderStatus.avgFillPrice}")
                self.in_position = False
                self.active_trade_details = {}
        self.q.put({'type': 'active_trade', 'data': self.active_trade_details})

    def execute_trade(self, direction, price):
        if self.in_position: return
        action = 'BUY' if direction == 'Long' else 'SELL'
        quantity = self.params.get('order_quantity', 1)
        tp_pct = self.params.get('take_profit_pct', 1.0)
        sl_pct = self.params.get('stop_loss_pct', 0.5)

        tp_price = round(price * (1 + tp_pct / 100 * (1 if direction == 'Long' else -1)), 2)
        sl_price = round(price * (1 - sl_pct / 100 * (1 if direction == 'Long' else -1)), 2)

        parent = Order(orderId=self.ib.client.getReqId(), action=action, orderType="LMT", totalQuantity=quantity,
                       lmtPrice=price, transmit=False)
        tp = Order(orderId=self.ib.client.getReqId(), action="SELL" if action == "BUY" else "BUY", orderType="LMT",
                   totalQuantity=quantity, lmtPrice=tp_price, parentId=parent.orderId, transmit=False)
        sl = Order(orderId=self.ib.client.getReqId(), action="SELL" if action == "BUY" else "BUY", orderType="STP",
                   totalQuantity=quantity, auxPrice=sl_price, parentId=parent.orderId, transmit=True)

        for o in [parent, tp, sl]: self.ib.placeOrder(self.contract, o)
        self.log_and_queue('status',
                           f"Placing {direction} bracket order for {quantity} @ ~{price:.2f} (TP: {tp_price}, SL: {sl_price})")

    def prepare_market_data(self):
        self.daily_data = self.fetch_historical_data('1 day', '1 Y')
        if not self.daily_data.empty:
            self.daily_data.ta.ema(length=50, append=True)
            if pd.notna(self.daily_data['EMA_50'].iloc[-1]):
                self.market_regime = "UPTREND" if self.daily_data['close'].iloc[-1] > self.daily_data['EMA_50'].iloc[
                    -1] else "DOWNTREND"

        self.historical_data = self.fetch_historical_data(self.params['timeframe'], '1 D')
        if not self.historical_data.empty:
            self.calculate_indicators()
            self.q.put({'type': 'chart_data', 'data': self.historical_data.to_json(orient='split')})
            self.last_bar_timestamp = self.historical_data.index[-1]

    def fetch_historical_data(self, bar_size, duration):
        try:
            # Using synchronous method
            bars = self.ib.reqHistoricalData(
                self.contract, endDateTime='',
                durationStr=duration, barSizeSetting=bar_size,
                whatToShow='TRADES', useRTH=True, formatDate=2)

            if not bars: return pd.DataFrame()
            df = util.df(bars)
            df.set_index('date', inplace=True)
            df.index = pd.to_datetime(df.index)
            return df
        except Exception as e:
            self.log_and_queue('log', f"❌ Exception in fetch_historical_data: {e}")
            return pd.DataFrame()

    def calculate_indicators(self):
        if not self.historical_data.empty:
            self.historical_data.ta.vwap(append=True)
            self.historical_data.ta.sma(close='volume', length=20, append=True, col_names=('VOLUME_SMA_20',))

    def on_bar_update(self, bars, hasNewBar):
        if not hasNewBar or self.is_new_bar_handling:
            return

        try:
            self.is_new_bar_handling = True  # Set flag
            latest_data = self.fetch_historical_data(self.params['timeframe'], '1 D')
            if not latest_data.empty and self.last_bar_timestamp and latest_data.index[-1] > self.last_bar_timestamp:
                self.historical_data = latest_data
                self.last_bar_timestamp = self.historical_data.index[-1]
                self.calculate_indicators()
                self.q.put({'type': 'chart_data', 'data': self.historical_data.to_json(orient='split')})
                self.log_and_queue('log', f"New bar: {self.last_bar_timestamp}")
                self.run_strategy_logic(self.last_bar_timestamp)
        finally:
            self.is_new_bar_handling = False  # Unset flag

    def run_strategy_logic(self, current_time_dt):
        ny_time = current_time_dt.tz_localize('UTC').tz_convert('America/New_York')
        market_open_time = ny_time.replace(hour=9, minute=30, second=0, microsecond=0)
        orb_end_time = market_open_time + datetime.timedelta(minutes=self.params['orb_minutes'])
        if self.orb_high is None and ny_time >= orb_end_time: self.calculate_orb(orb_end_time)
        if self.orb_high is not None and not self.in_position: self.check_breakout(self.historical_data.iloc[-1])

    def calculate_orb(self, orb_end_time):
        df_ny = self.historical_data.tz_localize('UTC').tz_convert('America/New_York')
        orb_data = df_ny[df_ny.index < orb_end_time]
        if not orb_data.empty:
            self.orb_high = orb_data['high'].max()
            self.orb_low = orb_data['low'].min()
            self.q.put({'type': 'orb_levels', 'data': {'high': self.orb_high, 'low': self.orb_low}})
            self.log_and_queue('status', f"ORB Calculated: H={self.orb_high:.2f}, L={self.orb_low:.2f}")

    def check_breakout(self, bar):
        breakout_direction = None
        if bar['high'] > self.orb_high:
            breakout_direction = 'Long'
        elif bar['low'] < self.orb_low:
            breakout_direction = 'Short'
        if not breakout_direction: return

        price = bar['close']
        vwap = bar.get('VWAP_D')
        volume = bar['volume']
        vol_sma = bar.get('VOLUME_SMA_20')

        regime_ok = not self.params.get('use_market_regime_filter', True) or (
                    self.market_regime == ("UPTREND" if breakout_direction == 'Long' else "DOWNTREND"))
        vwap_ok = not self.params.get('use_vwap_filter', True) or (
                    pd.notna(vwap) and (price > vwap if breakout_direction == 'Long' else price < vwap))
        volume_ok = not self.params.get('use_volume_filter', True) or (
                    pd.notna(volume) and pd.notna(vol_sma) and volume > vol_sma)

        all_filters_passed = all([regime_ok, vwap_ok, volume_ok])

        reasoning = {'timestamp': datetime.now(pytz.timezone('America/New_York')).strftime('%H:%M:%S'),
                     'price': price, 'direction': breakout_direction,
                     'regime_check': {'pass': regime_ok, 'actual': self.market_regime},
                     'vwap_check': {'pass': vwap_ok, 'price': f"{price:.2f}",
                                    'vwap': f"{vwap:.2f}" if pd.notna(vwap) else "N/A"},
                     'volume_check': {'pass': volume_ok, 'volume': f"{volume:,.0f}",
                                      'sma': f"{vol_sma:,.0f}" if pd.notna(vol_sma) else "N/A"},
                     'final_decision': "Trade Approved" if all_filters_passed else "Trade Rejected"}
        self.q.put({'type': 'reasoning', 'data': reasoning})

        if all_filters_passed:
            self.execute_trade(breakout_direction, bar['close'])