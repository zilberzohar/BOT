# ib_client.py

import streamlit as st
from ib_insync import IB, Stock, util
import asyncio


class IBClient:
    def __init__(self):
        self.ib = IB()
        st.session_state.market_data = "Initializing..."

    def _on_pending_ticker(self, ticker):
        """Callback function to handle incoming ticker data."""
        if ticker:
            st.session_state.market_data = {
                "symbol": ticker.contract.symbol,
                "last_price": ticker.last if ticker.last else 'N/A',
                "bid_price": ticker.bid if ticker.bid else 'N/A',
                "ask_price": ticker.ask if ticker.ask else 'N/A',
                "time": ticker.time.strftime('%Y-%m-%d %H:%M:%S') if ticker.time else 'N/A'
            }

    def connect(self, host='127.0.0.1', port=7497, clientId=20):
        """Connects to IB TWS or Gateway."""
        if not self.ib.isConnected():
            try:
                self.ib.connect(host, port, clientId)
                return "Connection successful"
            except Exception as e:
                return f"Connection failed: {e}"
        return "Already connected"

    def subscribe_to_market_data(self, symbol='VIXY', exchange='SMART', currency='USD'):
        """Subscribes to market data for a given symbol."""
        if self.ib.isConnected():
            contract = Stock(symbol, exchange, currency)
            # --- התיקון האחרון: דילוג על הפקודה שעלולה להיתקע ---
            # self.ib.qualifyContracts(contract)

            market_data = self.ib.reqMktData(contract, '', False, False)
            market_data.updateEvent += self._on_pending_ticker
            return f"Subscribed to {symbol}"
        return "Not connected"

    def run_loop(self):
        """Runs the IB event loop."""
        try:
            self.ib.run()
        except Exception as e:
            print(f"IB loop stopped: {e}")

    def disconnect(self):
        """Disconnects from IB."""
        if self.ib.isConnected():
            self.ib.disconnect()


@st.cache_resource
def get_ib_client():
    return IBClient()