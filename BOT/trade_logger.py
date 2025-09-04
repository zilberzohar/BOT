# ==============================================================================
# File: trade_logger.py (Updated)
# ==============================================================================
import pandas as pd
import os
from datetime import datetime

LOG_FILE = 'trades_log.csv'
# Define columns to ensure consistent order in the CSV file
COLUMNS = [
    'timestamp', 'ticker', 'direction', 'quantity', 'entry_price',
    'exit_price', 'trade_value', 'pnl_amount', 'pnl_percent', 'exit_reason'
]


def log_trade(trade_summary: dict):
    """
    Appends a completed trade to the CSV log file with a consistent column order.

    Args:
        trade_summary (dict): A dictionary containing all details of the trade.
    """
    try:
        # Ensure all standard columns are present, filling missing ones with None
        for col in COLUMNS:
            trade_summary.setdefault(col, None)

        df = pd.DataFrame([trade_summary])
        df = df[COLUMNS]  # Enforce the column order

        file_exists = os.path.isfile(LOG_FILE)

        df.to_csv(LOG_FILE, mode='a', header=not file_exists, index=False)
        print(f"Successfully logged trade to {LOG_FILE}")

    except Exception as e:
        print(f"Error logging trade to file: {e}")
