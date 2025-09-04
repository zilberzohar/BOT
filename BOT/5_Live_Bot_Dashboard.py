# 5_Live_Bot_Dashboard.py

# --- התיקון לבעיית ה-Event Loop ---
import asyncio

try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
# ------------------------------------

import streamlit as st
import time
import threading
from ib_client import get_ib_client

st.set_page_config(layout="wide")

st.title("📈 H.N Bot Live Trading Dashboard")

ib_client = get_ib_client()

# --- הגדרת ה-Layout של העמוד ---
col_status, col_heartbeat, col_regime, col_ticker = st.columns(4)

# --- Sidebar ---
with st.sidebar:
    st.header("Bot Controls")

    if st.button("Connect to Broker"):
        with st.spinner("Connecting..."):
            status_message = ib_client.connect()
            st.success(status_message)
            st.rerun()

    if st.button("Start Bot", key="start_bot"):
        if ib_client.ib.isConnected():
            if 'bot_thread' not in st.session_state or not st.session_state.bot_thread.is_alive():
                with st.spinner("Starting bot and subscribing to data..."):
                    st.session_state.subscription_status = ib_client.subscribe_to_market_data('VIXY')

                    bot_thread = threading.Thread(target=ib_client.run_loop, daemon=True)
                    bot_thread.start()
                    st.session_state.bot_thread = bot_thread
                    st.session_state.bot_running = True
                    st.success("Bot started and listening for data!")
                    time.sleep(1)
                    st.rerun()
            else:
                st.warning("Bot is already running.")
        else:
            st.error("Must connect to broker before starting the bot.")

    if st.button("Stop Bot"):
        st.session_state.bot_running = False
        ib_client.disconnect()
        st.info("Bot stopped and disconnected.")
        time.sleep(1)
        st.rerun()

# --- תצוגת הנתונים הראשית ---
with col_status:
    st.metric("Connection Status", "Connected" if ib_client.ib.isConnected() else "Disconnected")
with col_ticker:
    st.metric("Ticker", "VIXY")

st.subheader("Live Market Data")
data_placeholder = st.empty()

# --- לולאת העדכון של ה-UI ---
if st.session_state.get('bot_running', False):
    market_data = st.session_state.get('market_data', {})
    if isinstance(market_data, dict):
        with data_placeholder.container():
            price_col, bid_col, ask_col, time_col = st.columns(4)
            price_col.metric("Last Price", f"${market_data.get('last_price', 'N/A')}")
            bid_col.metric("Bid", f"${market_data.get('bid_price', 'N/A')}")
            ask_col.metric("Ask", f"${market_data.get('ask_price', 'N/A')}")
            time_col.metric("Last Update", market_data.get('time', 'N/A'))
    else:
        data_placeholder.info(market_data)

    time.sleep(1)
    st.rerun()
else:
    data_placeholder.info("Bot is not running. Connect and start the bot to see live data.")