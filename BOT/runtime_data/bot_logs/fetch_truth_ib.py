"""
Smart fetch from IBKR with clearer messages & longer timeout.
Usage:
  python fetch_truth_ib.py --symbol VIXY --bar_size "5 mins" --duration "1 D" --host 127.0.0.1 --port 7497 --client_id 1001 --out truth.csv
"""
from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd
from ib_insync import IB, Stock, util

def fetch(symbol: str, bar_size: str, duration: str, host: str, port: int, client_id: int) -> pd.DataFrame:
    ib = IB()
    util.startLoop()
    ib.RequestTimeout = 60  # increase handshake timeout
    print(f"Connecting to TWS @ {host}:{port} with clientId={client_id} ...")
    ib.connect(host, port, clientId=client_id)
    if not ib.isConnected():
        raise RuntimeError("Not connected. Check TWS API settings and accept the API popup.")

    print("Connected. Qualifying contract...")
    contract = Stock(symbol, "ARCA", "USD")
    ib.qualifyContracts(contract)

    print(f"Requesting historical data: {duration}, {bar_size}, RTH=True")
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=True,
        formatDate=2,
    )
    if not bars:
        ib.disconnect()
        raise RuntimeError("No bars returned. If you saw Error 162 earlier, ודא שאין חיבור אחר/‏VPN ושה־TWS מאשר את ה־API.")

    df = util.df(bars)
    if df is None or df.empty:
        ib.disconnect()
        raise RuntimeError("Empty dataframe returned from IB for historical data.")

    df.set_index("date", inplace=True)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York")
    else:
        df.index = df.index.tz_convert("America/New_York")

    ib.disconnect()
    return df[["open","high","low","close","volume"]]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="VIXY")
    ap.add_argument("--bar_size", default="5 mins")
    ap.add_argument("--duration", default="1 D")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client_id", type=int, default=1001)
    ap.add_argument("--out", default="truth.csv")
    args = ap.parse_args()

    df = fetch(args.symbol, args.bar_size, args.duration, args.host, args.port, args.client_id)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index_label="date")
    print(f"Saved {len(df)} rows -> {args.out}")

if __name__ == "__main__":
    main()
