# 5_Live_Bot_Dashboard.py
# ------------------------------------------------------------
# Live Bot Dashboard (Streamlit) – שקיפות מלאה:
# - מחיר חי, בניית ORB בזמן אמת (High/Low מתעדכנים), טיימר/התקדמות
# - בתום חלון: גבולות סופיים והסבר החלטה (reason)
# - גרף 1min קצר
# - טלגרם (אופציונלי), דופק ריצה, לוג טריידים
# ------------------------------------------------------------

import sys, asyncio, os, json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

import streamlit as st

# ---- asyncio fix (Windows) ----
try:
    if sys.platform.startswith("win") and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
except Exception:
    pass
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# ---- optional autorefresh ----
_HAS_AUTO = True
try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    _HAS_AUTO = False

# ---- altair chart (optional) ----
_HAS_ALTAIR = True
try:
    import pandas as pd
    import altair as alt
except Exception:
    _HAS_ALTAIR = False

# ---- ib_insync ----
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

from importlib import import_module

# ---- load ORB entrypoint (prefers repo, fallback orb_strategy) ----
def load_orb_entrypoint():
    for mod_name in ["strategies.orb", "trade_monitor.orb", "trader_bot", "orb_strategy"]:
        try:
            mod = import_module(mod_name)
        except Exception:
            continue
        fn = getattr(mod, "run_orb_once", None)
        if callable(fn):
            return fn, mod_name + ".run_orb_once"
    return None, None

ORB_ENTRYPOINT, ORB_SOURCE = load_orb_entrypoint()

# ---- IB client cached ----
@st.cache_resource(show_spinner=False)
def get_ib_client():
    if not _HAS_IB:
        return None
    return IB()

# ---- util ----
def now_utc(): return datetime.now(timezone.utc)
def fmt_ts(ts: Optional[datetime]) -> str:
    return "—" if not ts else ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")

# ---- trades access (robust for trades() vs trades) ----
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

# ---- telegram ----
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

# ---- page ----
st.set_page_config(page_title="Live Bot Dashboard", layout="wide")
st.title("📈 Live Bot Dashboard – מצב מסחר חי")
if not _HAS_IB: st.stop()

# ---------- SIDEBAR ----------
with st.sidebar:
    st.header("🔌 חיבור וסטטוס")
    refresh_every = st.number_input("קצב רענון (שניות)", 1, 30, 3)
    auto_refresh = st.toggle("רענון אוטומטי", value=True)
    if auto_refresh and not _HAS_AUTO:
        st.info("כדי לאפשר רענון אוטומטי התקן: `pip install streamlit-autorefresh`")
    st.divider()

    st.subheader("💼 חיבור ל־IB")
    host = st.text_input("Host", value="127.0.0.1")
    port = st.number_input("Port", value=7497, step=1)
    client_id = st.number_input("Client ID", value=1101, step=1)
    connect_btn = st.button("התחבר / בדוק חיבור")
    disconnect_btn = st.button("נתק")
    manual_refresh_btn = st.button("🔄 רענן עכשיו")
    st.divider()

    st.subheader("H.N Bot Controls")
    st.session_state.setdefault("strategy_enabled", False)
    st.session_state.setdefault("strategy_config", {
        "symbol": "VIXY", "qty": 100, "timeframe": "5 mins",
        "orb_minutes": 5, "stop_value": 0.50, "tp_value": 2.00,
        "trade_direction": "Long & Short",
        "use_regime_filter": False, "use_vwap_filter": False, "use_volume_filter": False,
        "secType": "STK", "exchange": "SMART", "currency": "USD",
        "catchup": True, "catchup_window": 30
    })
    cfg = st.session_state["strategy_config"]
    ticker = st.text_input("Ticker", value=cfg.get("symbol", "VIXY"))
    timeframe = st.selectbox("Timeframe", ["1 min", "5 mins", "15 mins"], index=1)
    orb_minutes = st.number_input("ORB Minutes", 1, 60, int(cfg.get("orb_minutes", 5)), 1)
    sl_pct = st.number_input("Stop Loss (%)", 0.0, 100.0, float(cfg.get("stop_value", 0.50)), 0.1, format="%.2f")
    tp_pct = st.number_input("Take Profit (%)", 0.0, 100.0, float(cfg.get("tp_value", 2.00)), 0.1, format="%.2f")
    trade_dir = st.selectbox("Trade Direction", ["Long & Short", "Long Only", "Short Only"], index=0)

    st.markdown("**Filters**")
    use_regime = st.checkbox("Use Market Regime Filter", value=bool(cfg.get("use_regime_filter", False)))
    use_vwap   = st.checkbox("Use VWAP Filter",         value=bool(cfg.get("use_vwap_filter", False)))
    use_vol    = st.checkbox("Use Volume Filter",       value=bool(cfg.get("use_volume_filter", False)))

    st.markdown("---")
    catchup_on = st.checkbox("Enter on past breakout (Catch-Up)", value=bool(cfg.get("catchup", True)))
    catchup_win = st.number_input("Catch-Up Window (mins)", 1, 180, int(cfg.get("catchup_window", 30)), 1)

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
            "catchup": bool(catchup_on), "catchup_window": int(catchup_win)
        })
        st.session_state["strategy_config"] = cfg
        st.success("ההגדרות נשמרו.")
    if start_btn:
        st.session_state["strategy_enabled"] = True; st.success("הבוט הופעל.")
    if stop_btn:
        st.session_state["strategy_enabled"] = False; st.info("הבוט כובה.")

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
    st.session_state["tg_enabled"] = tg_enabled; st.session_state["tg_token"] = tg_token; st.session_state["tg_chat"] = tg_chat

# autorefresh
if auto_refresh and _HAS_AUTO:
    st_autorefresh(interval=int(refresh_every) * 1000, key="auto_refresh_key")

# IB connect/disconnect
ib = get_ib_client()
if ib is None: st.error("❌ לא ניתן ליצור חיבור IB."); st.stop()
if connect_btn:
    try:
        if not ib.isConnected():
            ib.connect(host, int(port), clientId=int(client_id), readonly=False, timeout=5)
        st.sidebar.success(f"מחובר ל־IB ({host}:{port}, clientId={client_id})") if ib.isConnected() else st.sidebar.error("נכשל להתחבר.")
    except Exception as e:
        st.sidebar.error(f"שגיאת חיבור: {e}")
if disconnect_btn:
    try:
        if ib.isConnected(): ib.disconnect()
        st.sidebar.info("נותק.")
    except Exception as e:
        st.sidebar.error(f"שגיאת ניתוק: {e}")
if manual_refresh_btn: st.rerun()

# ---------- HEADER METRICS ----------
col1, col2, col3, col4 = st.columns([2, 1, 1, 2])
with col1:
    st.success(f"מחובר ל־IB ✅  ({host}:{port}, clientId={client_id})") if ib.isConnected() else st.error("לא מחובר ל־IB ❌")

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

if "last_strategy_tick" not in st.session_state: st.session_state["last_strategy_tick"] = None
col5, col6 = st.columns([1, 1])
with col5: st.metric("Strategy", "ON" if enabled else "OFF")
with col6: st.metric("Last Strategy Tick", fmt_ts(st.session_state.get("last_strategy_tick")))
st.divider()

# ---------- BODY ----------
left, right = st.columns([3, 2])

# ---- Trades table ----
with left:
    st.subheader("🧾 עסקאות אחרונות (IB Trades)")
    if trade_rows:
        def row(r): return {"Time": fmt_ts(r["time"]), "Symbol": r["symbol"], "Type": r["type"],
                            "Action": r["action"], "Qty": r["qty"], "Filled": r["filled"],
                            "Remaining": r["remaining"], "Avg Price": r["avg_price"], "Status": r["status"]}
        st.dataframe([row(r) for r in trade_rows], use_container_width=True, height=350)
    else:
        st.write("אין טריידים להצגה עדיין.")

# right panel header
with right:
    st.subheader("📡 ניטור חי")
    st.write(f"⏱️ עכשיו: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**")
    st.write("🔗 סטטוס חיבור: **Connected**" if ib.isConnected() else "🔴 סטטוס חיבור: **Disconnected**")
    st.markdown("---")
    st.subheader("📐 ORB – מצב חיי")

# ---------- STRATEGY TICK ----------
orb_levels, last_price_val, reason_text, phase = None, None, None, None
decision_status = None

if ib.isConnected() and enabled and ORB_ENTRYPOINT is not None:
    cfg = st.session_state["strategy_config"]
    symbol = cfg["symbol"]; qty = int(cfg.get("qty", 100))
    tp = float(cfg["tp_value"]); sl = float(cfg["stop_value"]); orb_min = int(cfg["orb_minutes"])
    catchup = bool(cfg.get("catchup", True)); catchup_win = int(cfg.get("catchup_window", 30))

    kwargs = dict(ib=ib, symbol=symbol, qty=qty, tp_pct=tp, sl_pct=sl,
                  range_minutes=orb_min, buffer_pct=0.0, cache=st.session_state,
                  enter_only_after_close=True, enter_on_late_breakout=catchup, late_window_minutes=catchup_win)

    try:
        result = ORB_ENTRYPOINT(**kwargs)  # מחזיר dict עם phase/range/last/status/reason
        st.session_state["last_strategy_tick"] = now_utc()
    except TypeError:
        # אם המימוש שלך לא מכיר חלק מהפרמטרים – נוריד אותם
        kwargs = {k: v for k, v in kwargs.items() if k in ("ib","symbol","qty","tp_pct","sl_pct","range_minutes","buffer_pct","cache")}
        result = ORB_ENTRYPOINT(**kwargs)
        st.session_state["last_strategy_tick"] = now_utc()
    except Exception as e:
        result = {"status": "error", "error": str(e)}

    # ----- parse result -----
    if isinstance(result, dict):
        decision_status = result.get("status", "")
        phase = result.get("phase")
        rng = result.get("range")
        if rng:
            orb_levels = {"high": rng.get("high"), "low": rng.get("low"),
                          "progress": rng.get("progress"), "remaining_sec": rng.get("remaining_sec"),
                          "complete": rng.get("complete"), "start": rng.get("start"), "end": rng.get("end")}
        last_price_val = result.get("last")
        reason_text = result.get("reason")

        # notify on entries
        if st.session_state.get("tg_enabled", False) and decision_status in ("entered_long","entered_short","entered_long_late","entered_short_late"):
            side = "Long" if "long" in decision_status else "Short"
            msg = f"✅ ORB Entry {side}\nSymbol: {symbol}\nLast: {last_price_val}\nH/L: {orb_levels.get('high')} / {orb_levels.get('low')}\nTP {tp}% | SL {sl}%"
            send_telegram(st.session_state["tg_token"], st.session_state["tg_chat"], msg)

# ---------- ORB live panel (right) ----------
with right:
    # פרוגרס/טיימר
    if orb_levels:
        p = orb_levels.get("progress")
        rem = orb_levels.get("remaining_sec")
        compl = orb_levels.get("complete")
        if compl:
            st.success("🎯 חלון ה-ORB הסתיים – גבולות סופיים נעולים.")
        else:
            st.info(f"⏳ בונה טווח ORB — נותר {rem if rem is not None else '?'} שנ׳")
            try:
                st.progress(min(1.0, max(0.0, float(p))))  # 0..1
            except Exception:
                pass

    # מדדים
    c1, c2, c3 = st.columns(3)
    with c1: st.metric("ORB High", f"{(orb_levels or {}).get('high', '—')}")
    with c2: st.metric("Last Price", f"{last_price_val if last_price_val is not None else '—'}")
    with c3: st.metric("ORB Low", f"{(orb_levels or {}).get('low', '—')}")

    # הסבר החלטה
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

    # גרף קצר
    try:
        from orb_strategy import recent_bars_for_chart  # להשתמש בעזר שלנו
        if _HAS_ALTAIR:
            bars = recent_bars_for_chart(ib, st.session_state["strategy_config"]["symbol"], minutes=45)
            if bars:
                df = pd.DataFrame([{"t": b.date, "close": float(b.close)} for b in bars])
                ch = alt.Chart(df).mark_line().encode(x="t:T", y="close:Q").properties(height=220)
                st.altair_chart(ch, use_container_width=True)
    except Exception:
        pass

st.divider()

# ---------- LOG ----------
st.subheader("🪵 יומן אירועים")
log_lines = []
for r in (snapshot_trades(ib) if ib.isConnected() else [])[:20]:
    when = fmt_ts(r["time"])
    line = f"{when} | {r['symbol']:>6} | {r['action']:^4} | qty={r['qty']} | filled={r['filled']} | status={r['status']} | avg={r['avg_price']}"
    log_lines.append(line)
st.code("\n".join(log_lines) if log_lines else "היומן ריק כרגע.", language="text")

st.caption("© Live Bot Dashboard — ORB live builder, reasons, chart, Telegram alerts.")
