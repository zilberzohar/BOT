import time, json
from pathlib import Path
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from monitor_settings import SQLITE_PATH, DASH_REFRESH_SECS

st.set_page_config(page_title="Live Trade Monitor", layout="wide")
st.title("📈 Live Trade Monitor — Why did (or didn’t) we trade?")

@st.cache_resource
def get_engine():
    return create_engine(f"sqlite:///{SQLITE_PATH}", connect_args={"check_same_thread": False})

engine = get_engine()

# Ensure schema exists if the bot hasn't written any events yet
with engine.begin() as conn:
    conn.exec_driver_sql(
        """        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts REAL,
          iso TEXT,
          level TEXT,
          kind TEXT,
          symbol TEXT,
          price REAL,
          size INTEGER,
          side TEXT,
          reason TEXT,
          details TEXT,
          mode TEXT
        )
        """    )
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
    conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_events_symbol_ts ON events(symbol, ts)")

@st.cache_data(ttl=1.0)
def read_events(limit:int=50000, symbol:str|None=None, mode:str|None=None):
    q = "SELECT * FROM events"
    where = []
    params = {}
    if symbol:
        where.append("symbol = :s")
        params["s"] = symbol
    if mode:
        where.append("mode = :m")
        params["m"] = mode
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC LIMIT :n"
    params["n"] = limit
    with engine.begin() as conn:
        df = pd.read_sql(q, conn, params=params)
    df = df.sort_values("ts")
    # Parse details JSON
    def parse_details(x):
        try:
            return json.loads(x) if isinstance(x, str) else (x or {})
        except Exception:
            return {}
    if "details" in df.columns:
        df["details_json"] = df["details"].apply(parse_details)
    else:
        df["details_json"] = {}
    return df

# Controls
colA, colB, colC, colD, colE = st.columns([2,2,2,2,2])
with colA:
    symbol = st.text_input("Symbol filter (optional)", value="").strip() or None
with colB:
    mode = st.selectbox("Mode", ["any","live","backtest"], index=0)
    mode = None if mode=="any" else mode
with colC:
    window = st.slider("Rows window", 1000, 100000, 20000, step=1000)
with colD:
    auto = st.toggle("Auto-refresh", value=True)
with colE:
    refresh = st.number_input("Refresh secs", min_value=0.5, max_value=10.0, value=float(DASH_REFRESH_SECS), step=0.5)

# Data
df = read_events(limit=window, symbol=symbol, mode=mode)

# Status lights
def light(ok: bool):
    return "🟢" if ok else "🔴"

st.subheader("Status")
cols = st.columns(5)
with cols[0]:
    state_rows = df[(df["kind"]=="STATE")]
    ib_ok = False
    if not state_rows.empty:
        last_state = state_rows.iloc[-1]
        details = last_state.get("details_json", {}) or {}
        ib_ok = bool(details.get("ib_connected", False))
    st.metric("IB/TWS", light(ib_ok))
with cols[1]:
    fresh = (time.time() - float(df["ts"].iloc[-1])) < (2*refresh) if len(df) else False
    st.metric("Data fresh", light(fresh))
with cols[2]:
    t0 = time.time()-300
    blocks_recent = len(df[(df["kind"]=="BLOCK") & (df["ts"] > t0)])
    st.metric("Blocks (5m)", blocks_recent)
with cols[3]:
    orders_recent = len(df[(df["kind"]=="ORDER") & (df["ts"] > t0)])
    st.metric("Orders (5m)", orders_recent)
with cols[4]:
    fills_recent = len(df[(df["kind"]=="FILL") & (df["ts"] > t0)])
    st.metric("Fills (5m)", fills_recent)

# Why-not panel
st.subheader("Why not? — last blocked reasons")
blocks = df[df["kind"]=="BLOCK"].copy()
if not blocks.empty:
    blocks["t"] = pd.to_datetime(blocks["iso"])  # utc
    blocks_view = blocks[["t", "symbol", "side", "reason", "details"]].sort_values("t", ascending=False).head(50)
    st.dataframe(blocks_view, width='stretch')
else:
    st.info("No BLOCK events yet.")

# Event timeline
st.subheader("Timeline (last 1000)")
view = df[["iso","kind","symbol","side","price","reason","details"]].copy().tail(1000)
st.dataframe(view, width='stretch')

# Per-symbol breakdown
st.subheader("Per-symbol metrics (session)")
if "symbol" in df.columns and not df.empty:
    agg = df.groupby(["symbol","kind"]).size().unstack(fill_value=0)
    st.dataframe(agg, width='stretch')

# Price chart with markers (Plotly)
try:
    import plotly.graph_objects as go
    price_df = df[df.price.notna() & (df.kind=="DATA")][["ts","price","symbol"]]
    if not price_df.empty:
        fig = go.Figure()
        fig.add_scatter(x=price_df["ts"], y=price_df["price"], mode="lines", name="price")
        for k, marker in [("SIGNAL","diamond"),("ORDER","circle"),("FILL","square")]:
            sub = df[(df.kind==k) & df.price.notna()]
            if not sub.empty:
                fig.add_scatter(x=sub["ts"], y=sub["price"], mode="markers", name=k, marker_symbol=marker)
        fig.update_layout(height=420, xaxis_title="ts", yaxis_title="price")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No price events yet.")
except Exception as e:
    st.warning(f"Chart error: {e}")

# Auto refresh loop
if auto:
    time.sleep(float(refresh))
    st.rerun()
