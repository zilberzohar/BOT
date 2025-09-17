# 5_Live_Bot_Dashboard.py
# ------------------------------------------------------------
# Live Bot Dashboard (Streamlit)
# - H.N Bot Controls UI (Ticker/Timeframe/ORB/TP/SL/Direction + Filters)
# - Windows asyncio loop fix before ib_insync import
# - Connect IB + metrics/trades/log
# - Tries to use ORB from repo; otherwise falls back to orb_strategy.run_orb_once
# - Heartbeat + ORB Levels (High/Low/Last) display
# - Telegram notifications on entry (optional, via sidebar settings)
# ------------------------------------------------------------

# ===== asyncio event loop BEFORE ib_insync =====
import sys
import asyncio

try:
    if sys.platform.startswith("win") and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
except Exception:
    pass

try:
    asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from datetime import datetime, timedelta, timezone, time as dtime
from typing import List, Dict, Any, Optional
import os
import json

import streamlit as st

# ===== optional autorefresh =====
_HAS_AUTO = True
try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    _HAS_AUTO = False

# ===== ib_insync =====
try:
    from ib_insync import IB
    _HAS_IB = True
except ImportError:
    IB = None
    _HAS_IB = False
    st.error("❌ ib_insync לא מותקן. התקן: `pip install ib-insync`")
except Exception as e:
    IB = None
    _HAS_IB = False
    st.error("⚠️ שגיאה בזמן טעינת ib_insync:")
    st.exception(e)

# ===== config =====
IBKR_HOST = "127.0.0.1"
IBKR_PORT = 7497         # Paper: 7497, Live: 7496
IBKR_CLIENT_ID = 1101

DEFAULT_REFRESH_SEC = 3
RECENT_FILL_MINUTES = 2

# ===== time utils =====
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def fmt_ts(ts: Optional[datetime]) -> str:
    if not ts:
        return "—"
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")

# ===== try load ORB entrypoint from repo =====
from importlib import import_module

def load_orb_entrypoint():
    """
    Return (callable, kind) or (None, None).
    Tries: strategies.orb / trade_monitor.orb / orb_strategy / trader_bot
    Prefers a function `run_orb_once`.
    """
    candidates = [
        "strategies.orb",
        "trade_monitor.orb",
        "trader_bot",
        "orb_strategy",   # our fallback
    ]
    for mod_name in candidates:
        try:
            mod = import_module(mod_name)
        except Exception:
            continue

        fn = getattr(mod, "run_orb_once", None)
        if callable(fn):
            return fn, f"{mod_name}.run_orb_once"

        cls = getattr(mod, "ORBStrategy", None)
        if cls is not None:
            instance = None
            def run_once_wrapper(**kwargs):
                nonlocal instance
                if instance is None:
                    instance = cls(**kwargs)
                return instance.run_once()
            return run_once_wrapper, f"{mod_name}.ORBStrategy.run_once"

        bot_cls = getattr(mod, "TradingBot", None)
        if bot_cls is not None:
            bot = None
            def tb_wrapper(ib, symbol, qty, tp_pct, sl_pct, range_minutes=5, buffer_pct=0.0, cache=None):
                nonlocal bot
                if bot is None:
                    bot = bot_cls(ib=ib)
                cand = getattr(bot, "run_orb_once", None)
                if callable(cand):
                    return cand(symbol=symbol, qty=qty, tp_pct=tp_pct, sl_pct=sl_pct,
                                range_minutes=range_minutes, buffer_pct=buffer_pct, cache=cache)
                cand = getattr(bot, "tick", None)
                if callable(cand):
                    return cand()
                return {"status": "no_entrypoint_in_tradingbot"}
            return tb_wrapper, f"{mod_name}.TradingBot"
    return None, None

ORB_ENTRYPOINT, ORB_SOURCE = load_orb_entrypoint()

# ===== cache resources =====
@st.cache_resource(show_spinner=False)
def get_ib_client():
    if not _HAS_IB:
        return None
    return IB()

def _connect_if_needed(ib: "IB", host: str, port: int, client_id: int) -> bool:
    try:
        if not ib.isConnected():
            ib.connect(host, port, clientId=client_id, readonly=False, timeout=5)
        return ib.isConnected()
    except Exception:
        return False

# ===== robust trades accessor (fixes "'method' object is not iterable") =====
def _trades_list(ib) -> list:
    try:
        tr = getattr(ib, "trades", None)
        if callable(tr):
            return tr() or []
        if isinstance(tr, list):
            return tr
    except Exception:
        pass
    return []

def snapshot_trades(ib) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for t in _trades_list(ib):
        contract = getattr(t, "contract", None)
        order = getattr(t, "order", None)
        status = getattr(t, "orderStatus", None)

        symbol = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "—")
        sec_type = getattr(contract, "secType", "")
        action = getattr(order, "action", "—")
        total_qty = getattr(order, "totalQuantity", "—")

        filled = getattr(status, "filled", 0.0) if status else 0.0
        remaining = getattr(status, "remaining", 0.0) if status else None
        avg_fill_price = getattr(status, "avgFillPrice", None) if status else None
        status_txt = getattr(status, "status", "—") if status else "—"

        last_fill_time = None
        for f in getattr(t, "fills", []):
            f_exec = getattr(f, "execution", None)
            ts = getattr(f_exec, "time", None) if f_exec else None
            if ts and (last_fill_time is None or ts > last_fill_time):
                last_fill_time = ts

        rows.append({
            "time": last_fill_time,
            "symbol": symbol,
            "type": sec_type,
            "action": action,
            "qty": total_qty,
            "filled": filled,
            "remaining": remaining,
            "avg_price": avg_fill_price,
            "status": status_txt,
        })
    rows.sort(key=lambda r: r["time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows

def count_open_orders(ib) -> int:
    open_statuses = {"PreSubmitted", "Submitted", "ApiPending", "PendingSubmit", "PendingCancel"}
    n = 0
    for t in _trades_list(ib):
        s = getattr(getattr(t, "orderStatus", None), "status", "")
        if s in open_statuses:
            n += 1
    return n

def last_fill_timestamp(trade_rows: List[Dict[str, Any]]) -> Optional[datetime]:
    for r in trade_rows:
        if r["time"]:
            return r["time"]
    return None

def derive_bot_state(strategy_enabled: bool, open_orders: int, last_fill: Optional[datetime]) -> str:
    if strategy_enabled and open_orders > 0:
        return "Placing / Managing"
    if last_fill and (now_utc() - last_fill <= timedelta(minutes=RECENT_FILL_MINUTES)):
        return "Executed (recent)"
    if strategy_enabled:
        return "Waiting for signal"
    return "Idle"

# ===== ORB helpers for display (from fallback module) =====
try:
    from orb_strategy import autodetect_contract, _bars_today_1min, compute_opening_range, latest_close  # type: ignore
    _HAS_ORB_HELPERS = True
except Exception:
    _HAS_ORB_HELPERS = False

# ===== Telegram notify =====
def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        try:
            import requests  # type: ignore
            r = requests.post(url, json=payload, timeout=5)
            return r.ok
        except Exception:
            # fallback stdlib
            import urllib.request, urllib.error
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                return resp.status == 200
    except Exception:
        return False

# ===== UI =====
st.set_page_config(page_title="Live Bot Dashboard", layout="wide")
st.title("📈 Live Bot Dashboard – מצב מסחר חי")

if not _HAS_IB:
    st.stop()

# ---------- SIDEBAR ----------
with st.sidebar:
    st.header("🔌 חיבור וסטטוס")
    refresh_every = st.number_input("קצב רענון (שניות)", min_value=1, max_value=30,
                                    value=DEFAULT_REFRESH_SEC, step=1)
    auto_refresh = st.toggle("רענון אוטומטי", value=True,
                             help="מרענן את המסך כל N שניות כדי למשוך סטטוס/טריידים מעודכנים.")
    if auto_refresh and not _HAS_AUTO:
        st.info("כדי לאפשר רענון אוטומטי התקן: `pip install streamlit-autorefresh`")
    st.divider()

    st.subheader("💼 חיבור ל־IB")
    host = st.text_input("Host", value=IBKR_HOST)
    port = st.number_input("Port", value=IBKR_PORT, step=1)
    client_id = st.number_input("Client ID", value=IBKR_CLIENT_ID, step=1)

    connect_btn = st.button("התחבר / בדוק חיבור")
    disconnect_btn = st.button("נתק")
    manual_refresh_btn = st.button("🔄 רענן עכשיו")

    st.divider()

    # ---------- H.N Bot Controls ----------
    st.subheader("H.N Bot Controls")

    st.session_state.setdefault("strategy_enabled", False)
    st.session_state.setdefault("strategy_config", {
        "symbol": "VIXY",
        "qty": 100,
        "timeframe": "5 mins",
        "orb_minutes": 15,
        "stop_value": 0.50,
        "tp_value": 2.00,
        "trade_direction": "Long & Short",
        "use_regime_filter": False,
        "use_vwap_filter": False,
        "use_volume_filter": False,
        "secType": "STK",
        "exchange": "SMART",
        "currency": "USD",
    })
    cfg = st.session_state["strategy_config"]

    st.markdown("**Strategy Parameters**")
    ticker = st.text_input("Ticker", value=cfg.get("symbol", "VIXY"))
    timeframe = st.selectbox("Timeframe", ["1 min", "5 mins", "15 mins"],
                             index={"1 min":0,"5 mins":1,"15 mins":2}.get(cfg.get("timeframe","5 mins"),1))
    orb_minutes = st.number_input("ORB Minutes", min_value=1, max_value=60,
                                  value=int(cfg.get("orb_minutes", 15)), step=1)
    sl_pct = st.number_input("Stop Loss (%)", min_value=0.0, max_value=100.0,
                             value=float(cfg.get("stop_value", 0.50)), step=0.1, format="%.2f")
    tp_pct = st.number_input("Take Profit (%)", min_value=0.0, max_value=100.0,
                             value=float(cfg.get("tp_value", 2.00)), step=0.1, format="%.2f")
    trade_dir = st.selectbox("Trade Direction",
                             ["Long & Short", "Long Only", "Short Only"],
                             index={"Long & Short":0,"Long Only":1,"Short Only":2}.get(cfg.get("trade_direction","Long & Short"),0))

    st.markdown("**Filters**")
    use_regime = st.checkbox("Use Market Regime Filter", value=bool(cfg.get("use_regime_filter", False)))
    use_vwap   = st.checkbox("Use VWAP Filter",         value=bool(cfg.get("use_vwap_filter", False)))
    use_vol    = st.checkbox("Use Volume Filter",       value=bool(cfg.get("use_volume_filter", False)))

    st.markdown("---")
    c1, c2, c3 = st.columns(3)
    save_btn  = c1.button("💾 Save")
    start_btn = c2.button("▶️ Start Bot")
    stop_btn  = c3.button("⏹️ Stop Bot")

    if save_btn or start_btn:
        cfg.update({
            "symbol": ticker.strip().upper(),
            "timeframe": timeframe,
            "orb_minutes": int(orb_minutes),
            "stop_value": float(sl_pct),
            "tp_value": float(tp_pct),
            "trade_direction": trade_dir,
            "use_regime_filter": bool(use_regime),
            "use_vwap_filter": bool(use_vwap),
            "use_volume_filter": bool(use_vol),
        })
        st.session_state["strategy_config"] = cfg
        st.success("ההגדרות נשמרו.")

    if start_btn:
        st.session_state["strategy_enabled"] = True
        st.success("הבוט הופעל.")
    if stop_btn:
        st.session_state["strategy_enabled"] = False
        st.info("הבוט כובה.")

    # ---------- Notifications (Telegram) ----------
    st.divider()
    st.subheader("🔔 Notifications")
    st.session_state.setdefault("tg_enabled", False)
    st.session_state.setdefault("tg_token", os.getenv("TELEGRAM_BOT_TOKEN", ""))
    st.session_state.setdefault("tg_chat", os.getenv("TELEGRAM_CHAT_ID", ""))

    tg_enabled = st.toggle("Enable Telegram alerts", value=st.session_state["tg_enabled"])
    tg_token = st.text_input("Bot Token", value=st.session_state["tg_token"], type="password")
    tg_chat  = st.text_input("Chat ID",  value=st.session_state["tg_chat"])
    if st.button("שלח הודעת בדיקה"):
        ok = send_telegram(tg_token, tg_chat, "✅ Live Bot Dashboard — Test message")
        st.success("נשלח!") if ok else st.error("נכשל לשלוח (בדוק Token/Chat ID)")

    st.session_state["tg_enabled"] = tg_enabled
    st.session_state["tg_token"] = tg_token
    st.session_state["tg_chat"] = tg_chat

# autorefresh
if auto_refresh and _HAS_AUTO:
    st_autorefresh(interval=int(refresh_every) * 1000, key="auto_refresh_key")

# IB client
ib = get_ib_client()
if ib is None:
    st.error("❌ לא ניתן ליצור חיבור IB (ib_insync לא נטען).")
    st.stop()
st.session_state['ib'] = ib

# connect/disconnect
if connect_btn:
    ok = _connect_if_needed(ib, host, int(port), int(client_id))
    if ok:
        st.sidebar.success(f"מחובר ל־IB ({host}:{port}, clientId={client_id})")
    else:
        st.sidebar.error("נכשל להתחבר. בדוק TWS/Gateway, יציאות והרשאות API.")
if disconnect_btn:
    try:
        if ib.isConnected():
            ib.disconnect()
        st.sidebar.info("נותק ממערכת IB.")
    except Exception as e:
        st.sidebar.error(f"שגיאה בניתוק: {e}")
if manual_refresh_btn:
    st.rerun()

# ---------- HEADER METRICS ----------
col1, col2, col3, col4 = st.columns([2, 1, 1, 2])
with col1:
    if ib.isConnected():
        st.success(f"מחובר ל־IB ✅  ({host}:{port}, clientId={client_id})")
    else:
        st.error("לא מחובר ל־IB ❌")

trade_rows: List[Dict[str, Any]] = []
open_orders = 0
last_fill: Optional[datetime] = None
strategy_enabled = bool(st.session_state.get("strategy_enabled", False))

if ib.isConnected():
    try:
        ib.reqOpenOrders()
        _ = ib.openTrades()
        _ = ib.fills()
        trade_rows = snapshot_trades(ib)
        open_orders = count_open_orders(ib)
        last_fill = last_fill_timestamp(trade_rows)
    except Exception as e:
        st.warning(f"לא הצלחתי למשוך סטטוס טריידים: {e}")

state = derive_bot_state(strategy_enabled, open_orders, last_fill)
with col2:
    st.metric("סטטוס בוט", state)
with col3:
    st.metric("הזמנות פתוחות", open_orders)
with col4:
    st.metric("מילוי אחרון", fmt_ts(last_fill))

# heartbeat indicators
if "last_strategy_tick" not in st.session_state:
    st.session_state["last_strategy_tick"] = None
col5, col6 = st.columns([1, 1])
with col5:
    st.metric("Strategy", "ON" if strategy_enabled else "OFF")
with col6:
    st.metric("Last Strategy Tick", fmt_ts(st.session_state.get("last_strategy_tick")))

st.divider()

# ---------- LIVE STATE STRIP ----------
if state.startswith("Placing"):
    with st.status("🟡 הבוט בביצוע/ניהול הזמנה...", state="running"):
        st.write("יש כרגע הזמנה/ות בתהליך.")
        if open_orders:
            st.write(f"מספר הזמנות פתוחות: **{open_orders}**")
elif state.startswith("Executed"):
    with st.status("✅ בוצעה עסקה לאחרונה", state="complete"):
        st.write(f"זמן מילוי אחרון: **{fmt_ts(last_fill)}**")
elif state == "Waiting for signal":
    st.info("🔵 ממתין לאות מסחר (Waiting for signal)\n\nאין הזמנות פתוחות כרגע. האסטרטגיה מופעלת וממתינה לסט־אפ.")
else:
    st.info("הבוט במצב Idle (האסטרטגיה כבויה או לא מחובר).")

# ---------- BODY ----------
left, right = st.columns([3, 2])

with left:
    st.subheader("🧾 עסקאות אחרונות (IB Trades)")
    if trade_rows:
        def displayable(r: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "Time": fmt_ts(r["time"]),
                "Symbol": r["symbol"],
                "Type": r["type"],
                "Action": r["action"],
                "Qty": r["qty"],
                "Filled": r["filled"],
                "Remaining": r["remaining"],
                "Avg Price": r["avg_price"],
                "Status": r["status"],
            }
        st.dataframe([displayable(r) for r in trade_rows], use_container_width=True, height=350)
    else:
        st.write("אין טריידים להצגה עדיין.")

with right:
    st.subheader("📡 ניטור חי")
    st.write(f"⏱️ עכשיו: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**")
    st.write("🔗 סטטוס חיבור: **Connected**" if ib.isConnected() else "🔴 סטטוס חיבור: **Disconnected**")
    st.markdown("---")
    st.subheader("🧩 תצורת אסטרטגיה פעילה")
    cfg_view = st.session_state.get("strategy_config", {})
    st.json(cfg_view or {})
    st.markdown("---")
    st.subheader("📐 ORB Levels")
    # נמלא אחרי הרצת האסטרטגיה (למטה) – או נחשב ידנית אם לא קיבלנו מהתוצאה

st.divider()

# ---------- STRATEGY TICK: ORB ----------
orb_levels = None
last_price_val = None
entry_status = None

if ib.isConnected() and strategy_enabled:
    cfg = st.session_state.get("strategy_config", {})
    symbol = cfg.get("symbol", "VIXY")
    qty = int(cfg.get("qty", 100))
    tp_pct = float(cfg.get("tp_value", 2.0))
    sl_pct = float(cfg.get("stop_value", 0.5))
    orb_min = int(cfg.get("orb_minutes", 15))

    result = None
    try:
        if ORB_ENTRYPOINT is not None:
            result = ORB_ENTRYPOINT(
                ib=ib, symbol=symbol, qty=qty,
                tp_pct=tp_pct, sl_pct=sl_pct,
                range_minutes=orb_min,
                buffer_pct=0.0,
                cache=st.session_state
            )
        else:
            from orb_strategy import run_orb_once as _fallback
            result = _fallback(
                ib=ib, symbol=symbol, qty=qty,
                tp_pct=tp_pct, sl_pct=sl_pct,
                range_minutes=orb_min,
                buffer_pct=0.0,
                cache=st.session_state
            )
        st.session_state["last_strategy_tick"] = datetime.now(timezone.utc)
    except Exception as e:
        st.error("שגיאה בהרצת האסטרטגיה:")
        st.exception(e)
        result = {"status": "error", "error": str(e)}

    # notifications on entry
    if isinstance(result, dict):
        entry_status = result.get("status", "")
        if st.session_state.get("tg_enabled", False) and entry_status in ("entered_long", "entered_short"):
            side = "Long" if entry_status.endswith("long") else "Short"
            rng = result.get("range") or {}
            hi, lo = rng.get("high"), rng.get("low")
            last_px = result.get("last")
            msg = f"✅ ORB Entry: {side}\nSymbol: {symbol}\nLast: {last_px}\nHigh: {hi}\nLow: {lo}\nTP: {tp_pct}% | SL: {sl_pct}%"
            _ = send_telegram(st.session_state.get("tg_token", ""), st.session_state.get("tg_chat", ""), msg)

        # show ORB widget
        with st.expander("📐 ORB – מצב נוכחי", expanded=True):
            st.write(f"Source: {ORB_SOURCE or 'fallback'}")
            st.json(result)

        # for levels panel
        rng = result.get("range")
        if rng:
            orb_levels = {"high": rng.get("high"), "low": rng.get("low")}
        last_price_val = result.get("last")

# Fill ORB Levels panel (right column)
with right:
    try:
        if orb_levels is None or last_price_val is None:
            # compute if not provided (using fallback helpers)
            if _HAS_ORB_HELPERS and ib.isConnected():
                symbol = st.session_state["strategy_config"]["symbol"]
                orb_min = int(st.session_state["strategy_config"]["orb_minutes"])
                contract = autodetect_contract(ib, symbol)
                bars = _bars_today_1min(ib, contract)
                rng = compute_opening_range(bars, orb_min)
                if rng:
                    orb_levels = {"high": rng["high"], "low": rng["low"]}
                last_price_val = latest_close(ib, contract)
        if orb_levels or last_price_val is not None:
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("ORB High", f"{(orb_levels or {}).get('high', '—')}")
            with c2:
                st.metric("Last Price", f"{last_price_val if last_price_val is not None else '—'}")
            with c3:
                st.metric("ORB Low", f"{(orb_levels or {}).get('low', '—')}")
        else:
            st.write("אין נתוני ORB זמינים עדיין (לפני/סמוך לפתיחה?).")
    except Exception as e:
        st.write(f"לא ניתן לחשב ORB Levels: {e}")

# ---------- LOG ----------
st.subheader("🪵 יומן אירועים")
log_lines = []
for r in trade_rows[:20]:
    when = fmt_ts(r["time"])
    line = f"{when} | {r['symbol']:>6} | {r['action']:^4} | qty={r['qty']} | filled={r['filled']} | status={r['status']} | avg={r['avg_price']}"
    log_lines.append(line)

if log_lines:
    st.code("\n".join(log_lines), language="text")
else:
    st.write("היומן ריק כרגע.")

st.caption("© Live Bot Dashboard — H.N Bot Controls, ORB Levels, Heartbeat, Telegram alerts.")
