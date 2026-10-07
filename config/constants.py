# config/constants.py
# Shared application-level constants. Grouped here for a single authoritative source.
# Compatibility helpers import these values.
from datetime import time

# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------
NSE_OPEN = time(9, 15)
NSE_CLOSE = time(15, 30)

# ---------------------------------------------------------------------------
# Index symbol mapping
# ---------------------------------------------------------------------------
INDEX_DATA_SYMBOLS = {
    "NIFTY": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY50-INDEX": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
}

# Broker token IDs for major indices used in BaseBrokerClient quote fetching
INDEX_TOKENS = {
    "NIFTY": "26000",
    "NIFTY50": "26000",
    "NIFTY50-INDEX": "26000",
    "BANKNIFTY": "26009",
    "FINNIFTY": "26037",
    "MIDCPNIFTY": "26074",
    "INDIAVIX": "26017",
    "SENSEX": "1",
    "BANKEX": "12",
}

# ---------------------------------------------------------------------------
# Exchange sets
# ---------------------------------------------------------------------------
DERIVATIVE_EXCHANGES = {"NFO", "BFO", "MCX", "CDS"}
BSE_UNDERLYINGS = {"SENSEX", "BANKEX"}

# ---------------------------------------------------------------------------
# Order status sets
# ---------------------------------------------------------------------------
OPEN_ORDER_STATUSES = {"Idle", "Entry_Submitting", "Entry_Pending", "Active", "Exit_Submitting", "Exit_Pending", "Recovery_Required"}
ACTIVE_ORDER_STATUSES = {"Active", "Exit_Pending"}
FINAL_ORDER_STATUSES = {"Entry_Rejected", "Closed", "QUOTE_ERROR"}

# ---------------------------------------------------------------------------
# Product type mapping (UI value -> Broker API code)
# ---------------------------------------------------------------------------
PRODUCT_TYPE_MAP = {"MIS": "I", "NRML": "M"}

# ---------------------------------------------------------------------------
# Diagnostic / logging
# ---------------------------------------------------------------------------
TERMINAL_STATUS_SECONDS = 15
