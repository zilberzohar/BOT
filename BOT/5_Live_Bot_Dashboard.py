# ==============================================================================
# File: 5_Live_Bot_Dashboard.py  (Streamlit UI for TradingBot)
# ==============================================================================
import sys
import asyncio
import streamlit as st
import queue
import threading
import time
from datetime import datetime

# Windows + Python 3.12: selector policy
if sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# VERY IMPORTANT: do NOT import ib_insync here
from trader_bot import TradingBot
import config_live

st.set_page_config(layout="wide", page_title="H.N Bot Dashboard")

# Session state
if 'bot_thread' not in st.session_state: st.session_state.bot_thread = None
if 'bot_running' not in st.session_state: st.session_state.bot_running = False
if 'connection_status' not in st.session_state: st.session_state.connection_status = "Disconnected"
if 'log_messages' not in st.session_state: st.session_state.log_messages = []
if 'reasoning' not in st.session_state: st.session_state.reasoning = {}
if 'orb_levels' not in st.session_state: st.session_state.orb_levels = {}
if 'active_trade' not in st.session_state: st.session_state.active_trade = {}
if 'q' not in st.session_state: st.session_state.q = queue.Queue()
if 'stop_event' not in st.session_state: st.session_state.stop_event = threading.Event()

def run_bot(params, q, stop_event):
    try:
        bot = TradingBot(params=params, q=q, stop_event=stop_event)
        bot.start()
    except Exception as e:
        q.put({'type': 'status', 'data': f"❌ CRITICAL THREAD ERROR: {e}"})


# ---------------- Sidebar ----------------
with st.sidebar:
    st.header("H.N Bot Controls")

    st.subheader("Strategy Parameters")
    ticker = st.text_input("Ticker", value=getattr(config_live, "STRATEGY_TICKER", "VIXY"))
    timeframe = st.selectbox("Timeframe", ['1 min', '5 mins', '15 mins', '1 hour', '1 day'], index=1)
    orb_minutes = st.number_input("ORB Minutes", min_value=1, value=getattr(config_live, "STRATEGY_ORB_MINUTES", 15))
    stop_loss_pct = st.number_input("Stop Loss (%)", min_value=0.1, value=getattr(config_live, "STRATEGY_STOP_LOSS_PCT", 0.5),
                                    step=0.1, format="%.2f")
    take_profit_pct = st.number_input("Take Profit (%)", min_value=0.1, value=getattr(config_live, "STRATEGY_TAKE_PROFIT_PCT", 2.0),
                                      step=0.1, format="%.2f")
    trade_direction = st.selectbox("Trade Direction", ['Long & Short', 'Long Only', 'Short Only'],
                                   index=['Long & Short', 'Long Only', 'Short Only'].index(
                                       getattr(config_live, "STRATEGY_TRADE_DIRECTION", "Long & Short")
                                   ))

    st.subheader("Filters")
    use_market_regime_filter = st.checkbox("Use Market Regime Filter", value=getattr(config_live, "USE_MARKET_REGIME_FILTER", False))
    use_vwap_filter = st.checkbox("Use VWAP Filter", value=getattr(config_live, "USE_VWAP_FILTER", False))
    use_volume_filter = st.checkbox("Use Volume Filter", value=getattr(config_live, "USE_VOLUME_FILTER", False))

    st.divider()

    if st.button("Start Bot", type="primary", use_container_width=True, disabled=st.session_state.bot_running):
        params = {
            'host': getattr(config_live, "IBKR_HOST", "127.0.0.1"),
            'port': getattr(config_live, "IBKR_PORT", 7497),
            'client_id': getattr(config_live, "IBKR_CLIENT_ID", 1),
            'ticker': ticker,
            'timeframe': timeframe,
            'orb_minutes': orb_minutes,
            'stop_loss_pct': stop_loss_pct,
            'take_profit_pct': take_profit_pct,
            'trade_direction': trade_direction,
            'use_market_regime_filter': use_market_regime_filter,
            'use_vwap_filter': use_vwap_filter,
            'use_volume_filter': use_volume_filter,
            'order_quantity': getattr(config_live, "ORDER_QUANTITY", 1),
        }
        st.session_state.stop_event.clear()
        st.session_state.q = queue.Queue()
        st.session_state.bot_thread = threading.Thread(
            target=run_bot,
            args=(params, st.session_state.q, st.session_state.stop_event),
            daemon=True
        )
        st.session_state.bot_thread.start()
        st.session_state.bot_running = True
        st.toast("Bot is starting...", icon="🚀")
        time.sleep(0.8)
        st.rerun()

    if st.button("Stop Bot", use_container_width=True, disabled=not st.session_state.bot_running):
        st.session_state.stop_event.set()
        st.session_state.bot_running = False
        st.toast("Bot stopping...", icon="🛑")
        time.sleep(0.8)
        st.session_state.log_messages = []
        st.session_state.reasoning = {}
        st.session_state.orb_levels = {}
        st.session_state.active_trade = {}
        st.rerun()


# ---------------- Main ----------------
st.title("📈 H.N Bot Live Trading Dashboard")

col1, col2, col3 = st.columns(3)
col1.metric("Bot Status", "Running" if st.session_state.bot_running else "Stopped")
col2.metric("Broker Connection", st.session_state.connection_status)
if st.session_state.active_trade:
    trade_dir = st.session_state.active_trade.get('direction', 'N/A')
    trade_qty = st.session_state.active_trade.get('quantity', 'N/A')
    trade_entry = st.session_state.active_trade.get('entry_price', 'N/A')
    col3.metric("Active Trade", f"{trade_dir} {trade_qty} @ {trade_entry}")
else:
    col3.metric("Active Trade", "None")

st.divider()

col_left, col_right = st.columns([1, 2])

with col_left:
    st.subheader("🧠 Bot Reasoning")
    st.info("Shows the logic for the LAST trade decision.")
    reasoning_data = st.session_state.reasoning
    if not reasoning_data:
        st.json({"status": "Waiting for first trade signal..."})
    else:
        decision = reasoning_data.get('final_decision', 'N/A')
        color = "green" if "Approved" in decision else "red"
        st.markdown(f"**Final Decision:** <span style='color:{color};'>{decision}</span>", unsafe_allow_html=True)
        with st.expander("Show Full Reasoning Details"):
            st.json(reasoning_data)

    st.subheader("🎯 ORB Levels")
    orb_data = st.session_state.orb_levels
    if not orb_data:
        st.info("ORB levels will be calculated after market open.")
    else:
        st.metric("ORB High (Long Trigger)", f"${orb_data.get('high', 0):.2f}")
        st.metric("ORB Low (Short Trigger)", f"${orb_data.get('low', 0):.2f}")

with col_right:
    st.subheader("📜 Live Log")
    log_placeholder = st.empty()
    log_html = "<div style='background-color:#f0f2f6; color:black; border-radius:5px; padding:10px; height:400px; overflow-y:scroll; font-family:monospace; font-size:12px;'>"
    for msg in reversed(st.session_state.log_messages):
        log_html += f"{msg}<br>"
    log_html += "</div>"
    log_placeholder.markdown(log_html, unsafe_allow_html=True)


# ------------- Pump queue → UI -------------
def drain_queue():
    drained = False
    while not st.session_state.q.empty():
        drained = True
        message = st.session_state.q.get_nowait()
        msg_type, msg_data = message.get('type'), message.get('data')
        if msg_type == 'status':
            st.session_state.connection_status = msg_data
            st.session_state.log_messages.append(f"[{datetime.now().strftime('%H:%M:%S')}] [STATUS] {msg_data}")
        elif msg_type == 'log':
            st.session_state.log_messages.append(f"[{datetime.now().strftime('%H:%M:%S')}] [BOT] {msg_data}")
        elif msg_type == 'reasoning':
            st.session_state.reasoning = msg_data
        elif msg_type == 'orb_levels':
            st.session_state.orb_levels = msg_data
        elif msg_type == 'active_trade':
            st.session_state.active_trade = msg_data

    if drained:
        st.session_state.log_messages = st.session_state.log_messages[-100:]


if st.session_state.bot_running:
    drain_queue()
    time.sleep(1.0)
    st.rerun()
else:
    drain_queue()
