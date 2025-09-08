# Live Trade Monitor (Events + Dashboard)

This package gives you:
- Structured **events** (DATA / SIGNAL / BLOCK / ORDER / FILL / STATE / PNL)
- Storage in **SQLite** (WAL) and **JSONL**
- A real-time **Streamlit** dashboard with:
  - status lights (IB connected, data fresh)
  - "Why not?" panel (BLOCK reasons)
  - timeline and per-symbol metrics
  - price chart with SIGNAL/ORDER/FILL markers

## Install

```bash
python -m pip install -r requirements.txt
```

## Run the dashboard

```bash
streamlit run monitor_app.py
```

For LAN access (tablet/phone):

```bash
streamlit run monitor_app.py --server.address 0.0.0.0 --server.port 8501
```

## Integrate with your bot

Emit events at key checkpoints:

```python
from monitor import info, warn, error

# New price/bar
info("DATA", symbol=symbol, price=float(last_price), details={"bar_time": str(bar_time)})

# State changes
info("STATE", symbol=symbol, details={"orb": "start", "open_range": [o, h, l]})
info("STATE", symbol=symbol, details={"vwap": float(vwap)})

# Signals
info("SIGNAL", symbol=symbol, side=direction, price=float(trigger_price), details={"logic":"ORB+VWAP"})

# Blocks (why the trade didn't fire)
warn("BLOCK", symbol=symbol, side=direction, reason="Direction filter (Long Only)")
warn("BLOCK", symbol=symbol, reason="VWAP check failed")
warn("BLOCK", symbol=symbol, reason="Insufficient cash")

# Orders & fills
info("ORDER", symbol=symbol, side="BUY", price=float(limit_price), details={"orderId": oid, "qty": qty})
info("FILL",  symbol=symbol, side="BUY", price=float(fill_price),  details={"orderId": oid, "filled": filled})
```

> The database uses WAL + busy_timeout, so your bot (writer) and dashboard (reader) won't collide.

## Export

```bash
python export_events.py
```
Saves a CSV under `runtime_data/`.
