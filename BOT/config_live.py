# ==============================================================================
# File: config_live.py
# ==============================================================================
# This file centralizes all the default configurations for the live trading bot.
# These values will be loaded into the dashboard but can be changed from the UI.

# --- IBKR Connection Settings ---
IBKR_CLIENT_ID = 1101
IBKR_HOST = '127.0.0.1'
IBKR_PORT = 7497  # 7497 for Paper Trading TWS, 7496 for Live TWS
IBKR_CLIENT_ID = 1

# --- Strategy Parameters (Default Values) ---
STRATEGY_TICKER = 'VIXY'
# !!! IMPORTANT: Use exact IBKR format. Examples: '1 min', '5 mins', '15 mins', '1 hour', '1 day'
STRATEGY_TIMEFRAME = '5 mins'
STRATEGY_ORB_MINUTES = 15
STRATEGY_STOP_LOSS_PCT = 0.5
STRATEGY_TAKE_PROFIT_PCT = 2.0
STRATEGY_TRADE_DIRECTION = 'Long & Short' # 'Long Only', 'Short Only', or 'Long & Short'

# --- Filters (Default Values) ---
USE_MARKET_REGIME_FILTER = True
USE_VWAP_FILTER = True
USE_VOLUME_FILTER = True

# --- Email Alert Settings ---
EMAIL_SENDER = 'zilber.zohar@gmail.com'
# !!! IMPORTANT: Use an "App Password" for security, not your main password !!!
EMAIL_PASSWORD = 'xizy ufzv qwrq rxsf ' # <--- REPLACE WITH YOUR GMAIL APP PASSWORD
EMAIL_RECEIVER = 'zilber.zohar@gmail.com'
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587