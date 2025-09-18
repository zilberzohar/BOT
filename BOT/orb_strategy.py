# orb_strategy.py
# ------------------------------------------------------------
# ORB (Opening Range Breakout) with pluggable data providers:
# - OkamiStocks (API) for market data  |  IBKR for order routing
# - Live ORB build (progress/timer), reasons, optional catch-up
# ------------------------------------------------------------

from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

# IB only for ORDERS (we try not to request market data from IB)
from ib_insync import IB, Stock, Contract, MarketOrder, LimitOrder, StopOrder, BarData

NY = ZoneInfo("America/New_York")

# ---------------------------- Okami client ----------------------------
class OkamiClient:
    BASE = "https://okamistocks.io/api"

    def __init__(self, token: str):
        self.token = (token or "").strip()
        self._last_ok = False
        self._last_ts: Optional[str] = None

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        data = {"token": self.token, **payload}
        try:
            try:
                import requests  # type: ignore
                r = requests.post(f"{self.BASE}{path}", json=data, timeout=5)
                if r.ok:
                    self._last_ok = True
                    try:
                        self._last_ts = r.json().get("timestamp")
                    except Exception:
                        pass
                    return r.json()
                self._last_ok = False
                return None
            except Exception:
                # stdlib fallback
                import json as _json
                import urllib.request
                req = urllib.request.Request(
                    f"{self.BASE}{path}",
                    data=_json.dumps(data).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                    body = resp.read().decode("utf-8")
                    import json as _json2
                    js = _json2.loads(body)
                    self._last_ok = True
                    self._last_ts = js.get("timestamp")
                    return js
        except Exception:
            self._last_ok = False
            return None

    def realtime_mid(self, ticker: str) -> Optional[float]:
        """
        /api/quote/real-time returns bid/ask; take mid if available, else any available field.
        """
        if not self.token:
            return None
        js = self._post_json("/quote/real-time", {"ticker": ticker})
        if not js:
            return None
        bid = js.get("bid_price")
        ask = js.get("ask_price")
        if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid == bid and ask == ask:
            return float((bid + ask) / 2.0)
        for fld in ("last", "minute_close_price", "bid_price", "ask_price"):
            v = js.get(fld)
            if isinstance(v, (int, float)) and v == v:
                return float(v)
        return None

    def minute_snapshot(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        /api/quote/minute returns the current minute OHLCV snapshot.
        NOTE: Okami does NOT return historical minute series—only the *current* minute.
        """
        if not self.token:
            return None
        js = self._post_json("/quote/minute", {"ticker": ticker})
        if not js:
            return None
        # Normalize field names
        snap = {
            "open": js.get("minute_open_price"),
            "high": js.get("minute_high_price"),
            "low": js.get("minute_low_price"),
            "close": js.get("minute_close_price"),
            "volume": js.get("minute_volume"),
            "timestamp": js.get("timestamp"),
        }
        return snap


# ------------------------- IB contract helper -------------------------
def autodetect_contract(ib: IB, symbol: str) -> Contract:
    symbol = symbol.strip().upper()
    con = Stock(symbol, "SMART", "USD")
    if not ib or not ib.isConnected():
        return con
    try:
        q = ib.qualifyContracts(con)[0]
        ex = getattr(q, "primaryExchange", None) or getattr(q, "exchange", None) or "SMART"
        cur = getattr(q, "currency", "USD") or "USD"
        return Stock(symbol, ex, cur)
    except Exception:
        return con


# --------- IB historical bars (used only if hybrid catch-up is enabled) ---------
def _bars_today_1min_ib(ib: IB, contract: Contract) -> List[BarData]:
    return ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=True,
        keepUpToDate=False,
    ) or []


# ---------------------------- ORB calculators ----------------------------
def _orb_window_today(range_minutes: int) -> Dict[str, Any]:
    today = datetime.now(NY).date()
    start = datetime.combine(today, datetime.min.time(), NY).replace(hour=9, minute=30)
    end = start + timedelta(minutes=range_minutes)
    return {"start": start, "end": end}

def _compute_range_from_bars(bars: List[BarData], start: datetime, end: datetime) -> Optional[Dict[str, Any]]:
    if not bars:
        return None
    hi, lo = None, None
    for b in bars:
        bt = b.date if getattr(b, "date", None) else None
        if not bt:
            continue
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=NY)
        bt_ny = bt.astimezone(NY)
        if start <= bt_ny < end:
            hi = b.high if hi is None else max(hi, b.high)
            lo = b.low  if lo is None else min(lo, b.low)
    if hi is None or lo is None:
        return None
    return {"high": float(hi), "low": float(lo), "start": start, "end": end}

def _partial_orb_build_with_okami(cache: Dict[str, Any], ok: OkamiClient, symbol: str, range_minutes: int) -> Dict[str, Any]:
    """
    Builds ORB high/low incrementally using Okami minute snapshots from the moment the bot is running.
    If the bot started after 09:30, minutes לפני־כן לא ישוחזרו (אלא אם נעשה hybrid catch-up).
    """
    win = _orb_window_today(range_minutes)
    start, end = win["start"], win["end"]
    now = datetime.now(NY)

    key = f"okami_orb_{symbol}_{start.date().isoformat()}_{range_minutes}"
    state = cache.get(key, {"high": None, "low": None, "built_from_okami": True})

    # אם עוד לא התחיל חלון ה-ORB
    if now < start:
        total = int((end - start).total_seconds())
        remaining = int((end - now).total_seconds())
        progress = max(0.0, min(1.0, (total - remaining) / max(1, total)))
        rng = {**win, "high": None, "low": None, "complete": False, "elapsed_sec": total - remaining,
               "remaining_sec": remaining, "progress": progress}
        return rng

    # אם כבר הסתיים – נחזיר מה שבנוי
    if now >= end:
        total = int((end - start).total_seconds())
        rng = {**win, "high": state["high"], "low": state["low"],
               "complete": True, "elapsed_sec": total, "remaining_sec": 0, "progress": 1.0}
        cache[key] = state
        return rng

    # בתוך חלון – נעדכן מדקה נוכחית
    snap = ok.minute_snapshot(symbol)
    if snap and isinstance(snap.get("high"), (int, float)) and isinstance(snap.get("low"), (int, float)):
        hi = float(snap["high"])
        lo = float(snap["low"])
        state["high"] = hi if state["high"] is None else max(state["high"], hi)
        state["low"]  = lo if state["low"]  is None else min(state["low"],  lo)
        cache[key] = state

    total = int((end - start).total_seconds())
    elapsed = int((min(now, end) - start).total_seconds())
    remaining = max(0, total - elapsed)
    progress = elapsed / max(1, total)

    rng = {**win, "high": state["high"], "low": state["low"],
           "complete": False, "elapsed_sec": elapsed, "remaining_sec": remaining, "progress": progress}
    return rng


# ---------------------------- Orders (IB) ----------------------------
def has_open_position(ib: IB, contract: Contract) -> int:
    try:
        for p in ib.positions():
            if p.contract.conId == contract.conId:
                return int(p.position)
    except Exception:
        pass
    return 0

def open_orders_count(ib: IB, contract: Contract) -> int:
    c = 0
    try:
        for t in ib.openTrades():
            if t.contract.conId == contract.conId:
                st = (t.orderStatus and t.orderStatus.status) or ""
                if st in {"PreSubmitted", "Submitted", "ApiPending", "PendingSubmit", "PendingCancel"}:
                    c += 1
    except Exception:
        pass
    return c

def place_bracket_market(
    ib: IB,
    contract: Contract,
    side: str,     # "BUY" / "SELL"
    qty: int,
    tp_pct: float,
    sl_pct: float,
    ref_price: float,
    round_decimals: int = 2,
) -> Dict[str, Any]:
    side = side.upper()
    qty = int(qty)

    if side == "BUY":
        tp_price = round(ref_price * (1 + tp_pct / 100.0), round_decimals)
        sl_price = round(ref_price * (1 - sl_pct / 100.0), round_decimals)
        parent = MarketOrder("BUY", qty)
        tp = LimitOrder("SELL", qty, tp_price)
        sl = StopOrder("SELL", qty, sl_price)
    else:
        tp_price = round(ref_price * (1 - tp_pct / 100.0), round_decimals)
        sl_price = round(ref_price * (1 + sl_pct / 100.0), round_decimals)
        parent = MarketOrder("SELL", qty)
        tp = LimitOrder("BUY", qty, tp_price)
        sl = StopOrder("BUY", qty, sl_price)

    parent.transmit = False
    trade = ib.placeOrder(contract, parent)

    tp.parentId = parent.orderId
    tp.transmit = False
    ib.placeOrder(contract, tp)

    sl.parentId = parent.orderId
    sl.transmit = True
    ib.placeOrder(contract, sl)

    return {
        "entry": {"side": side, "qty": qty, "price_ref": ref_price},
        "tp": {"price": tp.lmtPrice},
        "sl": {"price": sl.auxPrice},
        "orderIds": {"parent": trade.order.orderId, "tp": tp.orderId, "sl": sl.orderId},
    }


# ---------------------------- Main tick ----------------------------
def run_orb_once(
    ib: IB,
    symbol: str,
    qty: int,
    tp_pct: float,
    sl_pct: float,
    range_minutes: int = 5,
    buffer_pct: float = 0.0,
    cache: Optional[Dict[str, Any]] = None,
    # data source
    data_source: str = "okami",          # "okami" | "ib"
    okami_token: Optional[str] = None,
    # optional hybrid (single-shot) catch-up using IB bars
    hybrid_fill_with_ib: bool = False,
    # late-entry window (if missed breakout and price still outside range)
    enter_on_late_breakout: bool = True,
    late_window_minutes: int = 30,
) -> Dict[str, Any]:
    """
    Returns dict for UI and places orders via IB when signal occurs.
    Keys: phase, range{...}, last, status, reason, provider{...}
    """
    if cache is None:
        cache = {}

    contract = autodetect_contract(ib, symbol)
    win = _orb_window_today(range_minutes)
    start, end = win["start"], win["end"]
    now_ny = datetime.now(NY)

    # --------- data provider selection ---------
    provider_info = {"source": data_source}
    last_price: Optional[float] = None
    rng: Optional[Dict[str, Any]] = None

    if data_source == "okami":
        ok = OkamiClient(okami_token or "")
        # build ORB incrementally via Okami minute
        rng = _partial_orb_build_with_okami(cache, ok, symbol, range_minutes)

        # price: prefer realtime mid; fallback to minute close
        last_price = ok.realtime_mid(symbol)
        if last_price is None:
            snap = ok.minute_snapshot(symbol)
            if snap and isinstance(snap.get("close"), (int, float)):
                last_price = float(snap["close"])

        provider_info["ok"] = ok._last_ok
        provider_info["last_api_ts"] = ok._last_ts

        # hybrid catch-up: one-time IB bars to fill missing early minutes if we started late
        if hybrid_fill_with_ib and rng and not rng.get("complete", False):
            # אם התחלת אחרי 09:30 ויש לנו חוסר HL, ננסה למלא מה־IB לקריאה אחת
            if (now_ny > start) and (rng.get("high") is None or rng.get("low") is None) and ib.isConnected():
                bars = _bars_today_1min_ib(ib, contract)
                from_ib = _compute_range_from_bars(bars, start, min(now_ny, end))
                if from_ib:
                    # הזרקה למצב שנבנה מאוקאמי
                    rng["high"] = from_ib["high"] if rng["high"] is None else max(rng["high"], from_ib["high"])
                    rng["low"]  = from_ib["low"]  if rng["low"]  is None else min(rng["low"],  from_ib["low"])

    else:  # "ib" as data source (legacy)
        bars = _bars_today_1min_ib(ib, contract)
        rng = _compute_range_from_bars(bars, start, min(now_ny, end))
        # last price approx from last bar (no streaming)
        if bars:
            last_price = float(bars[-1].close)  # last 1min close
        provider_info["ok"] = ib.isConnected()

    # --------- phases ---------
    if now_ny < start:
        phase = "pre"
    elif now_ny < end:
        phase = "building"
    else:
        phase = "post"

    if rng is None:
        return {
            "status": "waiting_premarket_or_no_data",
            "phase": "pre",
            "symbol": symbol,
            "range": {"start": start, "end": end, "high": None, "low": None, "complete": now_ny >= end},
            "last": last_price,
            "provider": provider_info,
            "reason": "אין נתוני דקה/היסטוריה זמינים מהמprovider שנבחר.",
        }

    # complete flag if past end
    if now_ny >= end:
        rng["complete"] = True
        rng["remaining_sec"] = 0
        rng["progress"] = 1.0

    # --------- already in position / open orders ---------
    pos = has_open_position(ib, contract)
    oo = open_orders_count(ib, contract)
    if pos != 0 or oo > 0:
        return {
            "status": "already_in_position_or_open_orders",
            "phase": "post" if now_ny >= end else "building",
            "symbol": symbol,
            "position": pos,
            "open_orders": oo,
            "range": rng,
            "last": last_price,
            "provider": provider_info,
            "reason": "כבר קיימת פוזיציה/הזמנות פתוחות.",
        }

    high = rng.get("high")
    low  = rng.get("low")
    if high is None or low is None:
        # still building without enough info
        return {
            "status": "building_range",
            "phase": "building",
            "symbol": symbol,
            "range": rng,
            "last": last_price,
            "provider": provider_info,
            "reason": f"בונה טווח פתיחה ({range_minutes} ד׳). ייתכן שחסרות דקות מוקדמות (Okami לא מספק היסטוריית דקה).",
        }

    # --------- breakout logic ---------
    hi_buf = high * (1 + buffer_pct / 100.0)
    lo_buf = low  * (1 - buffer_pct / 100.0)

    if last_price is not None and last_price > hi_buf:
        placed = place_bracket_market(ib, contract, "BUY", qty, tp_pct, sl_pct, float(last_price))
        return {
            "status": "entered_long",
            "phase": "post",
            "symbol": symbol,
            "range": rng,
            "last": last_price,
            "provider": provider_info,
            "reason": f"פריצת HIGH: last({last_price}) > {round(hi_buf, 4)}. נפתחה עסקת LONG.",
            **placed
        }
    if last_price is not None and last_price < lo_buf:
        placed = place_bracket_market(ib, contract, "SELL", qty, tp_pct, sl_pct, float(last_price))
        return {
            "status": "entered_short",
            "phase": "post",
            "symbol": symbol,
            "range": rng,
            "last": last_price,
            "provider": provider_info,
            "reason": f"שבירת LOW: last({last_price}) < {round(lo_buf, 4)}. נפתחה עסקת SHORT.",
            **placed
        }

    # --------- late-entry (catch-up) ---------
    if enter_on_late_breakout and now_ny >= end and last_price is not None:
        # אין לנו היסטוריית דקה מאוקאמי, לכן late-entry אמין רק אם עדיין מחוץ לטווח כרגע
        if last_price > hi_buf:
            placed = place_bracket_market(ib, contract, "BUY", qty, tp_pct, sl_pct, float(last_price))
            return {
                "status": "entered_long_late",
                "phase": "post",
                "symbol": symbol,
                "range": rng,
                "last": last_price,
                "provider": provider_info,
                "reason": "כניסה מאוחרת: המחיר עדיין מעל HIGH.",
                **placed
            }
        if last_price < lo_buf:
            placed = place_bracket_market(ib, contract, "SELL", qty, tp_pct, sl_pct, float(last_price))
            return {
                "status": "entered_short_late",
                "phase": "post",
                "symbol": symbol,
                "range": rng,
                "last": last_price,
                "provider": provider_info,
                "reason": "כניסה מאוחרת: המחיר עדיין מתחת ל-LOW.",
                **placed
            }

    # --------- no signal ---------
    explain = []
    if last_price is not None:
        explain += [f"last={last_price}", f"H={round(hi_buf,4)} / L={round(lo_buf,4)}"]
        if lo_buf <= last_price <= hi_buf:
            explain.append("המחיר בתוך הטווח – אין סיגנל.")
        elif last_price <= hi_buf:
            explain.append("לא עבר את ה-HIGH.")
        elif last_price >= lo_buf:
            explain.append("לא ירד מתחת ל-LOW.")

    return {
        "status": "waiting_for_breakout",
        "phase": "post" if now_ny >= end else "building",
        "symbol": symbol,
        "range": rng,
        "last": last_price,
        "provider": provider_info,
        "reason": " | ".join(explain) if explain else "מחכה לסיגנל.",
    }


# ------------- helper for chart (if you still want a chart) -------------
def recent_bars_for_chart(ib: IB, symbol: str, minutes: int = 45) -> List[BarData]:
    """
    לגרף מהיר—נשתמש ב-IB היסטורי (אם מחוברים). אחרת החזר ריק.
    (Okami לא מספק היסטוריית דקה מלאה כרגע.)
    """
    try:
        con = autodetect_contract(ib, symbol)
        return ib.reqHistoricalData(
            con, endDateTime="", durationStr=f"{max(5, int(minutes))} M",
            barSizeSetting="1 min", whatToShow="TRADES", useRTH=True, keepUpToDate=False
        ) or []
    except Exception:
        return []
