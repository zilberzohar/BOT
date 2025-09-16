"""
Compare the bot's logged DATA bars (events.jsonl) vs a market CSV to validate
that what the bot saw matches the truth.

CSV schema expected: date,open,high,low,close,volume
The script aligns by timestamp; use --round-minutes to align to 1/5/15m bars.

Usage:
  python log_compare.py --events BOT/runtime_data/bot_logs/events.jsonl ^
                        --market truth.csv ^
                        --out comparison_report.csv --round-minutes 5
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import pandas as pd


def load_events(jsonl_path: Path) -> pd.DataFrame:
    rows = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("kind") != "DATA":
                continue
            try:
                rows.append(
                    {
                        "ts": pd.to_datetime(rec["bar_time"]),
                        "open": float(rec["open"]),
                        "high": float(rec["high"]),
                        "low": float(rec["low"]),
                        "close": float(rec["close"]),
                        "volume": float(rec["volume"]),
                    }
                )
            except Exception:
                # skip malformed row
                continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates(subset=["ts"]).set_index("ts").sort_index()
    # assume America/New_York if tz-naive
    if df.index.tz is None:
        df.index = df.index.tz_localize("America/New_York")
    else:
        df.index = df.index.tz_convert("America/New_York")
    return df


def load_market_csv(csv_path: Path, tz: str = "America/New_York") -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    ts_col = "date" if "date" in df.columns else ("ts" if "ts" in df.columns else None)
    if ts_col is None:
        raise ValueError('CSV must include a "date" or "ts" column')
    df[ts_col] = pd.to_datetime(df[ts_col])
    df = df.rename(columns={ts_col: "ts"}).set_index("ts").sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize(tz)
    else:
        df.index = df.index.tz_convert(tz)
    return df[["open", "high", "low", "close", "volume"]]


def maybe_round_index(df: pd.DataFrame, minutes: int | None) -> pd.DataFrame:
    if minutes is None:
        return df
    # take last value per time bucket
    return df.groupby(pd.Grouper(freq=f"{minutes}T")).last().dropna(how="all")


def compare(bot_df: pd.DataFrame, mkt_df: pd.DataFrame) -> pd.DataFrame:
    all_idx = bot_df.index.union(mkt_df.index)
    bot = bot_df.reindex(all_idx)
    mkt = mkt_df.reindex(all_idx)
    out = pd.DataFrame(index=all_idx)
    for col in ["open", "high", "low", "close", "volume"]:
        out[f"bot_{col}"] = bot[col]
        out[f"mkt_{col}"] = mkt[col]
        out[f"diff_{col}"] = bot[col] - mkt[col]
        out[f"rel_{col}"] = (out[f"diff_{col}"] / mkt[col]).abs()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True, help="Path to events.jsonl")
    ap.add_argument("--market", required=True, help="Path to market CSV")
    ap.add_argument("--out", default="comparison_report.csv")
    ap.add_argument("--round-minutes", type=int, default=None)
    args = ap.parse_args()

    bot = load_events(Path(args.events))
    if bot.empty:
        raise SystemExit("No DATA rows found in events.jsonl")

    mkt = load_market_csv(Path(args.market))

    bot = maybe_round_index(bot, args.round_minutes)
    mkt = maybe_round_index(mkt, args.round_minutes)

    rep = compare(bot, mkt)
    rep.to_csv(args.out, index_label="ts")

    rel_cols = [c for c in rep.columns if c.startswith("rel_")]
    summary = rep[rel_cols].describe(percentiles=[0.5, 0.9, 0.99])

    print("\nRelative error summary (abs):")
    print(summary)
    print(f"\nSaved detailed report -> {args.out}")


if __name__ == "__main__":
    main()
