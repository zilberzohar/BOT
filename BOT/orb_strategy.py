# orb_strategy.py
# ------------------------------------------------------------
# ORB (Opening Range Breakout) עם:
# - בניה חיה של הטווח 09:30–09:30+N (NY) + טיימר/התקדמות
# - מחיר חי (reqTickers) עם fallback להיסטורית 1min
# - כניסה רק אחרי סגירת חלון ה-ORB (ברירת מחדל) + Catch-Up
# - החזרת 'phase' ו-'reason' להסבר החלטה
# - Bracket: הוראת שוק + TP/SL באחוזים
# ------------------------------------------------------------

from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

from ib_insync import IB, Stock, Contract, MarketOrder, LimitOrder, StopOrder, BarData, Ticker

NY = ZoneInfo("America/New_York")


# ---------- Contract autodetect ----------
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


# ---------- Live price ----------
def live_price(ib: IB, contract: Contract) -> Optional[float]:
    """
    מנסה להביא מחיר חי במהירות; אם אין, חוזר ל-last של היסטוריה.
    """
    try:
        ib.reqMktData(contract, "", False, False)
        ib.sleep(0.15)  # קצר כדי לא לחסום
        t: Ticker = ib.ticker(contract)
        if t is not None:
            # marketPrice() לעתים מחזיר עדכונים חיים
            mp = getattr(t, "marketPrice", None)
            if callable(mp):
                v = mp()
                if v and v == v:  # not NaN
                    return float(v)
            for fld in ("last", "close", "bid", "ask"):
                v = getattr(t, fld, None)
                if v and v == v:
                    return float(v)
    except Exception:
        pass
    return None


# ---------- Data pulls ----------
def _bars_today_1min(ib: IB, contract: Contract) -> List[BarData]:
    return ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="1 D",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=True,
        keepUpToDate=False,
    ) or []


def _bars_recent_1min(ib: IB, contract: Contract, minutes: int = 45) -> List[BarData]:
    dur = f"{max(5, int(minutes))} M"
    return ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=dur,
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=True,
        keepUpToDate=False,
    ) or []


def compute_opening_range_partial(bars: List[BarData], range_minutes: int) -> Optional[Dict[str, Any]]:
    """
    מחזיר טווח ORB גם במהלך בניה:
    {high, low, start, end, complete(bool), elapsed_sec, remaining_sec, progress (0..1)}
    """
    if not bars:
        return None
    today_ny = datetime.now(NY).date()
    start = datetime.combine(today_ny, datetime.min.time(), NY).replace(hour=9, minute=30)
    end = start + timedelta(minutes=range_minutes)

    hi = None
    lo = None
    now_ny = datetime.now(NY)
    for b in bars:
        bt = b.date
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=NY)
        bt_ny = bt.astimezone(NY)
        if start <= bt_ny < min(now_ny, end):  # עד עכשיו (או עד סוף החלון)
            hi = b.high if hi is None else max(hi, b.high)
            lo = b.low  if lo is None else min(lo, b.low)

    if hi is None or lo is None:
        # עוד לא התחיל החלון (לפני 09:30)
        complete = now_ny >= end
        elapsed = max(0, int((min(now_ny, end) - start).total_seconds()))
        remaining = max(0, int((end - min(now_ny, end)).total_seconds()))
        progress = 1.0 if complete else (elapsed / max(1, int((end - start).total_seconds())))
        return {"high": None, "low": None, "start": start, "end": end,
                "complete": complete, "elapsed_sec": elapsed, "remaining_sec": remaining, "progress": progress}

    complete = datetime.now(NY) >= end
    elapsed = max(0, int((min(datetime.now(NY), end) - start).total_seconds()))
    remaining = max(0, int((end - min(datetime.now(NY), end)).total_seconds()))
    progress = 1.0 if complete else (elapsed / max(1, int((end - start).total_seconds())))

    return {"high": hi, "low": lo, "start": start, "end": end,
            "complete": complete, "elapsed_sec": elapsed, "remaining_sec": remaining, "progress": progress}


def latest_close(ib: IB, contract: Contract) -> Optional[float]:
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr="15 M",
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=True,
        keepUpToDate=False,
    )
    if not bars:
        return None
    return float(bars[-1].close)


# ---------- State checks ----------
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


# ---------- Orders ----------
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


# ---------- Catch-up helper ----------
def _first_breakout_after_end(bars: List[BarData], rng: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not bars or not rng:
        return None
    high = rng["high"]; low = rng["low"]; end = rng["end"]
    for b in bars:
        bt = b.date
        if bt.tzinfo is None:
            bt = bt.replace(tzinfo=NY)
        bt_ny = bt.astimezone(NY)
        if bt_ny >= end:
            if b.close > high:
                return {"side": "BUY", "time": bt_ny, "close": float(b.close)}
            if b.close < low:
                return {"side": "SELL", "time": bt_ny, "close": float(b.close)}
    return None


# ---------- Main tick ----------
def run_orb_once(
    ib: IB,
    symbol: str,
    qty: int,
    tp_pct: float,
    sl_pct: float,
    range_minutes: int = 5,
    buffer_pct: float = 0.0,
    cache: Optional[Dict[str, Any]] = None,
    enter_only_after_close: bool = True,
    enter_on_late_breakout: bool = True,
    late_window_minutes: int = 30,
) -> Dict[str, Any]:
    """
    מחזיר מידע מלא ל-UI + מבצע הוראה כשיש סיגנל.
    שדות עיקריים בתוצאה:
      - phase: "pre", "building", "ready" (נגמר ה-ORB), "post"
      - range: {high, low, start, end, complete, elapsed_sec, remaining_sec, progress}
      - last: מחיר חי (אם זמין) או אחרון היסטורי
      - status: entered_long / entered_short / waiting_for_breakout / already_in_position_or_open_orders / ...
      - reason: טקסט הסבר קצר
    """
    if cache is None:
        cache = {}

    contract = autodetect_contract(ib, symbol)

    # טווח היום (גם חלקי)
    bars_today = _bars_today_1min(ib, contract)
    rng = compute_opening_range_partial(bars_today, range_minutes)
    if rng is None:
        return {"status": "waiting_premarket_or_no_data", "symbol": symbol, "phase": "pre", "reason": "אין נתוני 1-דקה עדיין."}

    now_ny = datetime.now(NY)
    phase = "pre" if now_ny < rng["start"] else ("building" if not rng["complete"] else "ready")

    # מחיר חי
    last = live_price(ib, contract)
    if last is None:
        last = latest_close(ib, contract)

    # בזמן בניית הטווח – רק מציגים התקדמות
    if phase == "building":
        hi = rng["high"]; lo = rng["low"]
        return {
            "status": "building_range",
            "phase": phase,
            "symbol": symbol,
            "range": rng,
            "last": last,
            "reason": f"בונה טווח פתיחה ({range_minutes} ד׳): High={hi if hi is not None else '—'}, Low={lo if lo is not None else '—'}, נותר {rng['remaining_sec']} שנ׳."
        }

    # אם נגמר החלון – יש לנו גבולות סופיים
    high = rng["high"]; low = rng["low"]
    if high is None or low is None:
        return {"status": "no_orb_range", "phase": phase, "symbol": symbol, "range": rng, "last": last,
                "reason": "הטווח לא חושב (חוסר בנתונים)."}

    # בדיקת מצב קיים
    pos = has_open_position(ib, contract)
    oo = open_orders_count(ib, contract)
    if pos != 0 or oo > 0:
        return {
            "status": "already_in_position_or_open_orders",
            "phase": "post",
            "symbol": symbol,
            "position": pos,
            "open_orders": oo,
            "range": rng,
            "last": last,
            "reason": "כבר קיימת פוזיציה/הזמנות פתוחות."
        }

    # אם הוגדר להיכנס רק אחרי סגירת החלון — זה המצב כבר (phase ready)
    # החלטת סיגנל בזמן אמת
    hi_buf = high * (1 + buffer_pct / 100.0)
    lo_buf =  low * (1 - buffer_pct / 100.0)

    # פריצה חיה
    if last is not None and last > hi_buf:
        placed = place_bracket_market(ib, contract, "BUY", qty, tp_pct, sl_pct, float(last))
        return {"status": "entered_long", "phase": "post", "symbol": symbol, "range": rng, "last": last,
                "reason": f"פריצת HIGH: last({last}) > {round(hi_buf, 4)}. נפתחה עסקת LONG.",
                **placed}
    if last is not None and last < lo_buf:
        placed = place_bracket_market(ib, contract, "SELL", qty, tp_pct, sl_pct, float(last))
        return {"status": "entered_short", "phase": "post", "symbol": symbol, "range": rng, "last": last,
                "reason": f"שבירת LOW: last({last}) < {round(lo_buf, 4)}. נפתחה עסקת SHORT.",
                **placed}

    # Catch-Up: אם כבר הייתה פריצה מוקדמת והבוט הופעל עכשיו
    if enter_on_late_breakout and now_ny >= rng["end"]:
        first_bo = _first_breakout_after_end(bars_today, rng)
        if first_bo:
            too_old = (now_ny - first_bo["time"]) > timedelta(minutes=late_window_minutes)
            still_outside = (last > hi_buf) if first_bo["side"] == "BUY" else (last < lo_buf)
            if not too_old and still_outside:
                placed = place_bracket_market(ib, contract, first_bo["side"], qty, tp_pct, sl_pct, float(last))
                label = "entered_long_late" if first_bo["side"] == "BUY" else "entered_short_late"
                return {"status": label, "phase": "post", "symbol": symbol, "range": rng, "last": last,
                        "reason": f"כניסת Catch-Up: הייתה פריצה {first_bo['side']} ב-{first_bo['time'].strftime('%H:%M')} והמחיר עדיין מחוץ לטווח.",
                        "first_breakout": first_bo, **placed}

    # ללא כניסה
    explain = []
    if last is not None:
        explain.append(f"last={last}")
        explain.append(f"H={round(hi_buf,4)} / L={round(lo_buf,4)}")
        if last <= hi_buf and last >= lo_buf:
            explain.append("המחיר בתוך הטווח – אין סיגנל.")
        elif last <= hi_buf:
            explain.append("לא עבר את ה-HIGH.")
        elif last >= lo_buf:
            explain.append("לא ירד מתחת ל-LOW.")
    return {
        "status": "waiting_for_breakout",
        "phase": "post",
        "symbol": symbol,
        "range": rng,
        "last": last,
        "reason": " | ".join(explain) if explain else "מחכה לסיגנל.",
    }


# כלי עזר לשרטוט/דשבורד
def recent_bars_for_chart(ib: IB, symbol: str, minutes: int = 45) -> List[BarData]:
    con = autodetect_contract(ib, symbol)
    return _bars_recent_1min(ib, con, minutes=minutes)
