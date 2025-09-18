# -*- coding: utf-8 -*-
# 5_Live_Bot_Dashboard.py
# ------------------------------------------------------------
# Live Bot Dashboard:
# - Data: OkamiStocks (API) | IB (optional)
# - Orders: IBKR only
# - ORB live, reasons, Telegram, log
# - Tests: Okami (price), TWS Round-Trip on the selected Ticker
# - Spinner fix + historical suppression during round-trip run
# - Okami API Key auto-load (secrets/env/keyring) + optional inline edit
# ------------------------------------------------------------

import sys, asyncio, os, json, warnings
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

import streamlit as st

warnings.filterwarnings(
    "ignore",
    message=r".*pkg_resources is deprecated.*",
    category=UserWarning
)

# ---- asyncio (Windows) ----
try:
    if sys.platform.startswith("win") and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
except Exception:
    pass
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# ---- optional extras ----
_HAS_AUTO = True
try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    _HAS_AUTO = False

_HAS_ALTAIR = True
try:
    import pandas as pd
    import altair as alt
except Exception:
    _HAS_ALTAIR = False

# ---- ib_insync ----
try:
    from ib_insync import IB, Stock, MarketOrder
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

from importlib import import_module

# ---- Okami token loader ----
def get_okami_token_from_sources() -> str:
    # order: session → secrets.toml → ENV → keyring
    tok = st.session_state.get("okami_token")
    if tok:
        return tok
    try:
        sec = st.secrets.get("okami", {})
        if isinstance(sec, dict) and sec.get("token"):
            return sec["token"]
    except Exception:
        pass
    env = os.getenv("OKAMI_API_KEY")
    if env:
        return env
    try:
        import keyring  # pip install keyring
        val = keyring.get_password("okami", "token")
        if val:
            return val
    except Exception:
        pass
    return ""

# ---- load strategy helpers (from orb_strategy.py if exists) ----
OkamiClient = None
recent_bars_for_chart = None
autodetect_contract = None

def load_orb_entrypoint():
    global OkamiClient, recent_bars_for_chart, autodetect_contract
    for mod_name in ["strategies.orb", "trade_monitor.orb", "trader_bot", "orb_strategy"]:
        try:
            mod = import_module(mod_name)
        except Exception:
            continue
        fn = getattr(mod, "run_orb_once", None)
        if callable(fn):
            OkamiClient = getattr(mod, "OkamiClient", OkamiClient)
            recent_bars_for_chart = getattr(mod, "recent_bars_for_chart", recent_bars_for_chart)
            autodetect_contract = getattr(mod, "autodetect_contract", autodetect_contract)
            return fn, mod_name + ".run_orb_once"
    return None, None

ORB_ENTRYPOINT, ORB_SOURCE = load_orb_entrypoint()

@st.cache_resource(show_spinner=False)
def get_ib_client():
    if not _HAS_IB:
        return None
    return IB()

def now_utc(): return datetime.now(timezone.utc)
def fmt_ts(ts: Optional[datetime]) -> str:
    return "—" if not ts else ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")

# ---- IB trades helpers ----
def _trades_list(ib) -> list:
    try:
        tr = getattr(ib, "trades", None)
        if callable(tr): return tr() or []
        if isinstance(tr, list): return tr
    except Exception: pass
    return []

def snapshot_trades(ib) -> List[Dict[str, Any]]:
    rows = []
    for t in _trades_list(ib):
        c, o, s = getattr(t, "contract", None), getattr(t, "order", None), getattr(t, "orderStatus", None)
        sym = getattr(c, "localSymbol", None) or getattr(c, "symbol", "—")
        sec = getattr(c, "secType", "")
        act = getattr(o, "action", "—")
        qty = getattr(o, "totalQuantity", "—")
        filled = getattr(s, "filled", 0.0) if s else 0.0
        remain = getattr(s, "remaining", 0.0) if s else None
        avg = getattr(s, "avgFillPrice", None) if s else None
        status = getattr(s, "status", "—") if s else "—"

        last_fill = None
        for f in getattr(t, "fills", []):
            ex = getattr(f, "execution", None)
            ts = getattr(ex, "time", None) if ex else None
            if ts and (last_fill is None or ts > last_fill):
                last_fill = ts
        rows.append({"time": last_fill, "symbol": sym, "type": sec, "action": act,
                     "qty": qty, "filled": filled, "remaining": remain, "avg_price": avg, "status": status})
    rows.sort(key=lambda r: r["time"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows

def count_open_orders(ib) -> int:
    open_statuses = {"PreSubmitted", "Submitted", "ApiPending", "PendingSubmit", "PendingCancel"}
    n = 0
    for t in _trades_list(ib):
        s = getattr(getattr(t, "orderStatus", None), "status", "")
        if s in open_statuses: n += 1
    return n

def last_fill_timestamp(rows: List[Dict[str, Any]]) -> Optional[datetime]:
    for r in rows:
        if r["time"]: return r["time"]
    return None

def derive_bot_state(enabled: bool, open_orders: int, last_fill: Optional[datetime]) -> str:
    if enabled and open_orders > 0: return "Placing / Managing"
    if last_fill and (now_utc() - last_fill <= timedelta(minutes=2)): return "Executed (recent)"
    if enabled: return "Waiting for signal"
    return "Idle"

# ---- Telegram ----
def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    if not bot_token or not chat_id: return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        try:
            import requests  # type: ignore
            r = requests.post(url, json=payload, timeout=5)
            return r.ok
        except Exception:
            import urllib.request, urllib.error
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                return resp.status == 200
    except Exception:
        return False

# ---- Session flags ----
st.session_state.setdefault("suppress_hist_until_rerun", False)  # lock history/chart during round-trip run

# ---- UI ----
st.set_page_config(page_title="Live Bot Dashboard", layout="wide")
st.title("📈 Live Bot Dashboard – מצב מסחר חי")
if not _HAS_IB: st.stop()

with st.sidebar:
    st.header("🔌 חיבור וסטטוס")
    refresh_every = st.number_input("קצב רענון (שניות)", 1, 30, 2)
    auto_refresh = st.toggle("רענון אוטומטי", value=True,
                             help='ב־Okami Std מותר כ־60 קריאות בדקה. השאר ≥ 1 שנ׳.')
    if auto_refresh and not _HAS_AUTO:
        st.info("כדי לאפשר רענון אוטומטי התקן: `pip install streamlit-autorefresh`")
    st.divider()

    st.subheader("💼 חיבור ל־IB (הוראות בלבד)")
    host = st.text_input("Host", value="127.0.0.1")
    port = st.number_input("Port", value=7497, step=1)
    client_id = st.number_input("Client ID", value=1101, step=1)
    connect_btn = st.button("התחבר / בדוק חיבור")
    disconnect_btn = st.button("נתק")
    manual_refresh_btn = st.button("🔄 רענן עכשיו")
    st.caption("הדשבורד נמנע ממשיכת דאטה דרך IB כדי לא לנתק DATA באפליקציה.")

    # --- Round-Trip options ---
    st.markdown("**TWS Round-Trip Test**")
    paper_only = st.checkbox("Run only on Paper (port 7497)", value=True,
                             help="הגנה בסיסית: אם תחובר ל־7496 (LIVE), הבדיקה לא תרוץ.")
    test_tws_btn = st.button("🧪 בדיקת TWS (Round-Trip 1 יחידה)")

    st.divider()
    st.subheader("📡 Data Source")
    st.session_state.setdefault("data_source", "okami")
    data_source = st.radio(
        "בחר מקור דאטה",
        ["okami", "ib"],
        index=0,
        help='okami – דאטה דרך OkamiStocks API. ib – דאטה דרך IB (בדרך כלל אין צורך).'
    )
    st.session_state["data_source"] = data_source

    # Okami token auto-load + optional inline edit
    st.session_state.setdefault("okami_token", "")
    _loaded_okami = get_okami_token_from_sources()
    if _loaded_okami:
        okami_token = _loaded_okami
        st.caption("🔑 Okami API Key נטען אוטומטית (secrets/env/keyring).")
        if st.toggle("ערוך מפתח Okami", value=False):
            okami_token = st.text_input("Okami API Key", value=okami_token, type="password")
            st.session_state["okami_token"] = okami_token
        else:
            st.session_state["okami_token"] = okami_token
    else:
        okami_token = st.text_input("Okami API Key", value="", type="password",
                                    help="שמור בקובץ .streamlit/secrets.toml או במשתנה סביבה OKAMI_API_KEY לטעינה אוטומטית.")
        st.session_state["okami_token"] = okami_token

    st.info("Okami endpoints: real-time & minute snapshot. ללא היסטוריית דקה מלאה.")
    test_okami_btn = st.button("🧪 בדיקת Okami (שער נוכחי)")

    hybrid = st.toggle(
        "Hybrid one-time catch-up via IB",
        value=False,
        help="חד־פעמי להשלים דקות חסרות בתחילת היום (דורש היסטוריית IB)."
    ) if data_source == "okami" else False

    st.divider()
    st.subheader("H.N Bot Controls")
    st.session_state.setdefault("strategy_enabled", False)
    st.session_state.setdefault("strategy_config", {
        "symbol": "VIXY", "qty": 100, "timeframe": "1 min",
        "orb_minutes": 5, "stop_value": 0.50, "tp_value": 2.00,
        "trade_direction": "Long & Short",
        "use_regime_filter": False, "use_vwap_filter": False, "use_volume_filter": False,
        "secType": "STK", "exchange": "SMART", "currency": "USD",
        "catchup": True, "catchup_window": 30
    })
    cfg = st.session_state["strategy_config"]
    ticker = st.text_input("Ticker", value=cfg.get("symbol", "VIXY"))
    timeframe = st.selectbox("Timeframe", ["1 min", "5 mins", "15 mins"], index=0)
    orb_minutes = st.number_input("ORB Minutes", 1, 60, int(cfg.get("orb_minutes", 5)), 1)
    sl_pct = st.number_input("Stop Loss (%)", 0.0, 100.0, float(cfg.get("stop_value", 0.50)), 0.1, format="%.2f")
    tp_pct = st.number_input("Take Profit (%)", 0.0, 100.0, float(cfg.get("tp_value", 2.00)), 0.1, format="%.2f")
    trade_dir = st.selectbox("Trade Direction", ["Long & Short", "Long Only", "Short Only"], index=0)

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
            "symbol": ticker.strip().upper(), "timeframe": timeframe, "orb_minutes": int(orb_minutes),
            "stop_value": float(sl_pct), "tp_value": float(tp_pct), "trade_direction": trade_dir,
            "use_regime_filter": bool(use_regime), "use_vwap_filter": bool(use_vwap), "use_volume_filter": bool(use_vol),
        })
        st.session_state["strategy_config"] = cfg
        st.success("ההגדרות נשמרו.")
    if start_btn:
        st.session_state["strategy_enabled"] = True; st.success("הבוט הופעל.")
    if stop_btn:
        st.session_state["strategy_enabled"] = False; st.info("הבוט כובה.")

    st.divider()
    st.subheader("🔔 Notifications")
    st.session_state.setdefault(
        "tg_token",
        (st.secrets.get("telegram", {}).get("bot_token") if "telegram" in st.secrets else os.getenv("TELEGRAM_BOT_TOKEN", ""))
    )
    st.session_state.setdefault(
        "tg_chat",
        (st.secrets.get("telegram", {}).get("chat_id") if "telegram" in st.secrets else os.getenv("TELEGRAM_CHAT_ID", ""))
    )
    tg_enabled = st.toggle("Enable Telegram alerts", value=st.session_state.get("tg_enabled", False))
    tg_token = st.text_input("Bot Token", value=st.session_state["tg_token"], type="password")
    tg_chat  = st.text_input("Chat ID",  value=st.session_state["tg_chat"])
    if st.button("שלח הודעת בדיקה"):
        ok = send_telegram(tg_token, tg_chat, "✅ Live Bot Dashboard — Test message")
        st.success("נשלח!") if ok else st.error("נכשל לשלוח (בדוק Token/Chat ID)")
    st.session_state["tg_enabled"] = tg_enabled; st.session_state["tg_token"] = tg_token; st.session_state["tg_chat"] = tg_chat

# ---- autorefresh ----
if 'auto_refresh' in locals() and auto_refresh and _HAS_AUTO:
    st_autorefresh(interval=int(refresh_every) * 1000, key="auto_refresh_key")

# ---- connect/disconnect ----
ib = get_ib_client()
if ib is None: st.error("❌ לא ניתן ליצור חיבור IB."); st.stop()

if 'connect_btn' in locals() and connect_btn:
    try:
        if not ib.isConnected():
            ib.connect(host, int(port), clientId=int(client_id), readonly=False, timeout=5)
        if ib.isConnected():
            st.sidebar.success(f"מחובר ל־IB ({host}:{port}, clientId={client_id})")
        else:
            st.sidebar.error("נכשל להתחבר.")
    except Exception as e:
        st.sidebar.error(f"שגיאת חיבור: {e}")

if 'disconnect_btn' in locals() and disconnect_btn:
    try:
        if ib.isConnected(): ib.disconnect()
        st.sidebar.info("נותק.")
    except Exception as e:
        st.sidebar.error(f"שגיאת ניתוק: {e}")

if 'manual_refresh_btn' in locals() and manual_refresh_btn:
    st.rerun()

# ---- Okami test ----
def run_okami_test(symbol: str, token: str):
    try:
        if not token:
            st.sidebar.error("נא להזין Okami API Key.")
            return
        if OkamiClient is not None:
            oc = OkamiClient(token)
            price = oc.realtime_mid(symbol)
            if price is None:
                snap = oc.minute_snapshot(symbol)
                price = float(snap["close"]) if snap and isinstance(snap.get("close"), (int, float)) else None
            if price is not None:
                st.sidebar.success(f"Okami OK — {symbol} price: {price}")
            else:
                st.sidebar.warning("Okami מחובר אך לא הוחזר מחיר (בדוק סימול/הודעות מערכת).")
            return
        import requests  # type: ignore
        r = requests.post(
            "https://okamistocks.io/api/quote/real-time",
            json={"token": token, "ticker": symbol},
            timeout=5
        )
        if r.ok:
            js = r.json()
            price = None
            bid, ask = js.get("bid_price"), js.get("ask_price")
            if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
                price = (bid + ask) / 2.0
            else:
                for fld in ("last", "minute_close_price", "bid_price", "ask_price"):
                    v = js.get(fld)
                    if isinstance(v, (int, float)): price = float(v); break
            st.sidebar.success(f"Okami OK — {symbol} price: {price}") if price is not None \
                else st.sidebar.warning("Okami OK אך לא זוהה שדה מחיר.")
        else:
            st.sidebar.error(f"Okami כשל (HTTP {r.status_code})")
    except Exception as e:
        st.sidebar.error(f"שגיאת Okami: {e}")

if 'test_okami_btn' in locals() and test_okami_btn:
    run_okami_test(st.session_state["strategy_config"]["symbol"].strip().upper(), st.session_state.get("okami_token","").strip())

# ---- TWS round-trip (on selected ticker) ----
def build_stock_contract(symbol: str):
    # Force SMART to avoid getting stuck on BATS
    sym = symbol.strip().upper()
    try:
        if autodetect_contract:
            c = autodetect_contract(ib, sym)
            return Stock(sym, "SMART", getattr(c, "currency", "USD") or "USD")
    except Exception:
        pass
    return Stock(sym, "SMART", "USD")

def run_tws_round_trip(ib: IB, symbol: str, qty: int = 1, timeout_s: int = 30) -> (bool, str):
    try:
        if not ib.isConnected():
            return False, "IB לא מחובר."
        con = build_stock_contract(symbol)
        buy = MarketOrder("BUY", qty)
        t_buy = ib.placeOrder(con, buy)
        end = now_utc() + timedelta(seconds=timeout_s)
        while (now_utc() < end) and (getattr(t_buy.orderStatus, "filled", 0) < qty):
            ib.sleep(0.25)
        if getattr(t_buy.orderStatus, "filled", 0) < qty:
            return False, "קניה לא מולאה בזמן שהוגדר."
        sell = MarketOrder("SELL", qty)
        t_sell = ib.placeOrder(con, sell)
        end = now_utc() + timedelta(seconds=timeout_s)
        while (now_utc() < end) and (getattr(t_sell.orderStatus, "filled", 0) < qty):
            ib.sleep(0.25)
        if getattr(t_sell.orderStatus, "filled", 0) < qty:
            return False, "מכירה לא מולאה בזמן שהוגדר."
        avg_buy = getattr(t_buy.orderStatus, "avgFillPrice", None)
        avg_sell = getattr(t_sell.orderStatus, "avgFillPrice", None)
        return True, f"הושלם Round-Trip על {symbol}: קניה {qty} @ {avg_buy}, מכירה {qty} @ {avg_sell}"
    except Exception as e:
        return False, f"שגיאה: {e}"

if 'test_tws_btn' in locals() and test_tws_btn:
    if paper_only and int(port) != 7497:
        st.sidebar.error("הבדיקה נעצרת: 'Paper only' מסומן אך החיבור אינו ל-7497.")
    else:
        st.session_state["suppress_hist_until_rerun"] = True  # lock history/chart this run
        with st.spinner(f"מבצע Round-Trip של 1 יחידה ב־{st.session_state['strategy_config']['symbol'].strip().upper()}..."):
            ok, msg = run_tws_round_trip(ib, st.session_state["strategy_config"]["symbol"].strip().upper(), qty=1)
        (st.sidebar.success if ok else st.sidebar.error)(msg)
        st.session_state["last_tws_result"] = (ok, msg, now_utc())

# ---------- HEADER ----------
col1, col2, col3, col4 = st.columns([2, 1, 1, 2])
with col1:
    if ib.isConnected():
        st.success(f"IB Ready (orders) ✅  ({host}:{port}, clientId={client_id})")
    else:
        st.error("IB לא מחובר ❌")

trade_rows, open_orders, last_fill = [], 0, None
enabled = bool(st.session_state.get("strategy_enabled", False))
if ib.isConnected():
    try:
        ib.reqOpenOrders(); _ = ib.openTrades(); _ = ib.fills()
        trade_rows = snapshot_trades(ib); open_orders = count_open_orders(ib); last_fill = last_fill_timestamp(trade_rows)
    except Exception as e:
        st.warning(f"שגיאה בשליפת סטטוס טריידים: {e}")

state = derive_bot_state(enabled, open_orders, last_fill)
with col2: st.metric("סטטוס בוט", state)
with col3: st.metric("הזמנות פתוחות", open_orders)
with col4: st.metric("מילוי אחרון", fmt_ts(last_fill))

if "last_tws_result" in st.session_state:
    ok, msg, t = st.session_state["last_tws_result"]
    (st.success if ok else st.error)(f"{fmt_ts(t)} · {msg}")

st.divider()

# ---------- BODY ----------
left, right = st.columns([3, 2])

with left:
    st.subheader("🧾 עסקאות אחרונות (IB Trades)")
    if trade_rows:
        def row(r): return {"Time": fmt_ts(r["time"]), "Symbol": r["symbol"], "Type": r["type"],
                            "Action": r["action"], "Qty": r["qty"], "Filled": r["filled"],
                            "Remaining": r["remaining"], "Avg Price": r["avg_price"], "Status": r["status"]}
        st.dataframe([row(r) for r in trade_rows], use_container_width=True, height=350)
    else:
        st.write("אין טריידים להצגה עדיין.")

with right:
    st.subheader("📡 ניטור חי")
    st.write(f"⏱️ עכשיו: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**")
    st.write("🔗 IB (Orders): **Connected**" if ib.isConnected() else "🔴 IB: **Disconnected**")
    ds = st.session_state.get("data_source", "okami")
    st.write(f"🛰️ Data Source: **{ds.upper()}**")
    if ds == "okami":
        st.caption("Rate limit (Std): ~60 קריאות/דקה. קצב הרענון ≥ 1 שנ׳.")
    st.markdown("---")
    st.subheader("📐 ORB – מצב חי")

# ---------- STRATEGY TICK ----------
orb_levels, last_price_val, reason_text, phase = None, None, None, None
decision_status = None
provider = {}

SUPPRESS_THIS_RUN = bool(st.session_state.get("suppress_hist_until_rerun"))

if (not SUPPRESS_THIS_RUN) and ib.isConnected() and enabled and ORB_ENTRYPOINT is not None:
    cfg = st.session_state["strategy_config"]
    symbol = cfg["symbol"]; qty = int(cfg.get("qty", 100))
    tp = float(cfg["tp_value"]); sl = float(cfg["stop_value"]); orb_min = int(cfg["orb_minutes"])

    kwargs = dict(
        ib=ib, symbol=symbol, qty=qty, tp_pct=tp, sl_pct=sl,
        range_minutes=orb_min, buffer_pct=0.0, cache=st.session_state,
    )
    if st.session_state["data_source"] == "okami":
        kwargs.update(data_source="okami",
                      okami_token=st.session_state.get("okami_token", ""),
                      hybrid_fill_with_ib=bool(hybrid),
                      enter_on_late_breakout=True)
    else:
        kwargs.update(data_source="ib")

    try:
        result = ORB_ENTRYPOINT(**kwargs)
        st.session_state["last_strategy_tick"] = now_utc()
    except TypeError:
        keep = ("ib","symbol","qty","tp_pct","sl_pct","range_minutes","buffer_pct","cache")
        result = ORB_ENTRYPOINT(**{k: v for k, v in kwargs.items() if k in keep})
        st.session_state["last_strategy_tick"] = now_utc()
    except Exception as e:
        result = {"status": "error", "reason": str(e)}

    if isinstance(result, dict):
        decision_status = result.get("status", "")
        phase = result.get("phase")
        provider = result.get("provider", {})
        rng = result.get("range")
        if rng:
            orb_levels = {"high": rng.get("high"), "low": rng.get("low"),
                          "progress": rng.get("progress"), "remaining_sec": rng.get("remaining_sec"),
                          "complete": rng.get("complete"), "start": rng.get("start"), "end": rng.get("end")}
        last_price_val = result.get("last")
        reason_text = result.get("reason")

with right:
    if st.session_state["data_source"] == "okami":
        ok = provider.get("ok")
        ts = provider.get("last_api_ts")
        if ok:
            st.success(f"Okami Status: OK  ·  last_ts={ts or '—'}")
        else:
            st.warning("Okami Status: לא התקבלה תשובה לאחרונה (ייבדק בטיק הבא).")

    if orb_levels:
        p = orb_levels.get("progress")
        rem = orb_levels.get("remaining_sec")
        compl = orb_levels.get("complete")
        if compl:
            st.success("🎯 חלון ה-ORB הסתיים – גבולות סופיים נעולים.")
        else:
            st.info(f"⏳ בונה טווח ORB — נותר {rem if rem is not None else '?'} שנ׳")
            try:
                st.progress(min(1.0, max(0.0, float(p))))
            except Exception:
                pass

    c1, c2, c3 = st.columns(3)
    with c1: st.metric("ORB High", f"{(orb_levels or {}).get('high', '—')}")
    with c2: st.metric("Last Price", f"{last_price_val if last_price_val is not None else '—'}")
    with c3: st.metric("ORB Low", f"{(orb_levels or {}).get('low', '—')}")

    if decision_status == "building_range":
        st.info(reason_text or "בונה טווח פתיחה…")
    elif decision_status in ("waiting_for_breakout", "already_in_position_or_open_orders"):
        st.warning(reason_text or decision_status)
    elif decision_status and decision_status.startswith("entered_"):
        st.success(reason_text or decision_status)
    elif decision_status == "error":
        st.error(reason_text or "שגיאה באסטרטגיה")
    elif decision_status:
        st.write(decision_status)

    # Chart (disabled when SUPPRESS_THIS_RUN)
    try:
        if _HAS_ALTAIR and ib.isConnected() and recent_bars_for_chart and (not SUPPRESS_THIS_RUN):
            bars = recent_bars_for_chart(ib, st.session_state["strategy_config"]["symbol"], minutes=45)
            if bars:
                df = pd.DataFrame([{"t": b.date, "close": float(b.close)} for b in bars])
                ch = alt.Chart(df).mark_line().encode(x="t:T", y="close:Q").properties(height=220)
                st.altair_chart(ch, use_container_width=True)
    except Exception:
        pass

st.divider()

# ---- LOG ----
st.subheader("🪵 יומן אירועים")
log_lines = []
for r in (snapshot_trades(ib) if ib.isConnected() else [])[:20]:
    when = fmt_ts(r["time"])
    line = f"{when} | {r['symbol']:>6} | {r['action']:^4} | qty={r['qty']} | filled={r['filled']} | status={r['status']} | avg={r['avg_price']}"
    log_lines.append(line)
st.code("\n".join(log_lines) if log_lines else "היומן ריק כרגע.", language="text")

st.caption("© Live Bot Dashboard — Data by OkamiStocks (optional), Orders via IBKR. ORB live builder, reasons, Telegram alerts. • Round-trip uses selected Ticker and is blocked on LIVE if 'Paper only' checked.")

# reset suppression flag after this run so next refresh returns to normal
if st.session_state.get("suppress_hist_until_rerun"):
    st.session_state["suppress_hist_until_rerun"] = False
