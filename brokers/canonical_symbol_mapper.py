# brokers/canonical_symbol_mapper.py
# Centralizes symbol mapping logic across all brokers.
# Maps user-facing canonical names (e.g., NIFTY, BANKNIFTY) to broker-specific
# token IDs and API symbol strings.

from typing import Any

# ---------------------------------------------------------------------------
# Index Tokens (Shoonya/Zebu/FlatTrade standard)
# ---------------------------------------------------------------------------
INDEX_TOKENS: dict[str, str] = {
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
# Index data symbol normalization
# ---------------------------------------------------------------------------
INDEX_DATA_SYMBOLS: dict[str, str] = {
    "NIFTY": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY50-INDEX": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
}

BSE_UNDERLYINGS: set[str] = {"SENSEX", "BANKEX", "SENSEX50"}
DERIVATIVE_EXCHANGES: set[str] = {"NFO", "BFO", "CDS", "MCX"}


def canonical_to_token(symbol: str) -> str | None:
    """Return the broker token ID for a canonical index symbol, or None if not found."""
    return INDEX_TOKENS.get(symbol.upper().strip())


def canonical_to_data_symbol(symbol: str) -> str:
    """Normalize a user-supplied symbol to the canonical data symbol for bar fetching."""
    return INDEX_DATA_SYMBOLS.get(symbol.upper().strip(), symbol.upper().strip())


def canonical_exchange(exchange: str, underlying: str) -> str:
    """Resolve the data exchange for an index underlying."""
    clean = underlying.upper().strip()
    if clean in BSE_UNDERLYINGS:
        return "BSE"
    clean_ex = exchange.upper().strip()
    if clean_ex in DERIVATIVE_EXCHANGES:
        return "NSE"
    return clean_ex


def trade_exchange(exchange: str, symbol: str, underlying: str) -> str:
    """Resolve the derivative trading exchange from user-supplied parameters."""
    clean_exchange = exchange.strip().upper()
    clean_symbol = symbol.strip()
    clean_underlying = underlying.strip().upper()

    if clean_symbol == "Spot":
        if clean_exchange in {"BFO", "BSE"} or clean_underlying in BSE_UNDERLYINGS:
            return "BSE"
        if clean_exchange in {"NFO", "NSE", "INDICES"}:
            return "NSE"
        return clean_exchange

    if clean_exchange in DERIVATIVE_EXCHANGES:
        return clean_exchange
    if clean_exchange == "BSE" or clean_underlying in BSE_UNDERLYINGS:
        return "BFO"
    return "NFO"
