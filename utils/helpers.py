from utils.clock import market_time, EXCHANGE_TZ
from datetime import datetime, time, timedelta
from typing import Any

from config.constants import (
    NSE_OPEN, NSE_CLOSE, INDEX_DATA_SYMBOLS, DERIVATIVE_EXCHANGES, BSE_UNDERLYINGS,
    OPEN_ORDER_STATUSES, ACTIVE_ORDER_STATUSES, FINAL_ORDER_STATUSES,
    PRODUCT_TYPE_MAP, TERMINAL_STATUS_SECONDS,
)


def _adjust_strike(base: int, option_type: str, mode: str, steps: int, interval: int) -> int:
    """Adjust option strike price based on Moneyness offset and underlying direction."""
    if mode == "ATM" or steps == 0:
        return base
    direction = 1 if option_type == "CE" else -1
    if mode == "ITM":
        direction *= -1
    return base + direction * steps * interval


def _strike_mode_from_moneyness(value: int) -> tuple[str, int]:
    """Parse integer moneyness offset into Strike Mode string and Step count."""
    if value == 0:
        return "ATM", 0
    return ("OTM" if value > 0 else "ITM"), abs(value)


def _parse_clock(value: str, fallback: time) -> time:
    """Safely parse clock string into Python time object with format try-fallbacks."""
    cleaned = value.strip().upper().replace(".", ":")
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    return fallback


def _data_symbol(underlying: str) -> str:
    """Map user-friendly underlying to corresponding cash-index symbol for candle fetching."""
    clean = underlying.strip().upper()
    return INDEX_DATA_SYMBOLS.get(clean, underlying.strip())


def _data_exchange(exchange: str, underlying: str) -> str:
    """Resolve standard cash exchange name for underlying data feed lookup."""
    clean_exchange = exchange.strip().upper()
    clean_underlying = underlying.strip().upper()
    if clean_exchange in {"BFO", "BSE"} or clean_underlying in BSE_UNDERLYINGS:
        return "BSE"
    if clean_exchange in {"NFO", "NSE", "INDICES"}:
        return "NSE"
    return clean_exchange


def _trade_exchange(exchange: str, symbol: str, underlying: str) -> str:
    """Resolve derivative exchange name for option/future contract trading lookup."""
    clean_exchange = exchange.strip().upper()
    clean_symbol = symbol.strip()
    clean_underlying = underlying.strip().upper()
    if clean_symbol == "Spot":
        return _data_exchange(clean_exchange, clean_underlying)
    if clean_exchange in DERIVATIVE_EXCHANGES:
        return clean_exchange
    if clean_exchange == "BSE" or clean_underlying in BSE_UNDERLYINGS:
        return "BFO"
    return "NFO"


def _today_session_start(now: datetime, start: time = NSE_OPEN) -> datetime:
    """Combine today's date with configured session start time."""
    return datetime.combine(market_time(now).date(), start, EXCHANGE_TZ)


def _is_market_time(now: datetime, start: time = NSE_OPEN, end: time = NSE_CLOSE) -> bool:
    """Check if current time falls within configured trading hours."""
    now = market_time(now)
    return now.weekday() < 5 and start <= now.time() <= end


def _contract_name(contract: dict[str, Any] | None) -> str:
    """Extract trading symbol or fallback identifier from Zebu resolved contract dict."""
    if not contract:
        return ""
    return str(contract.get("tradingsymbol") or contract.get("token") or "")


def _clean_order_id(value: Any) -> str:
    """Normalize Zebu/Mynt order ids that may arrive as strings, numbers, or Excel-style floats."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "-"}:
        return ""
    if "e" in text.lower():
        try:
            text = str(int(float(text)))
        except (TypeError, ValueError, OverflowError):
            pass
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _broker_value(response: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-empty broker value, case-insensitive across known field names."""
    lowered = {str(key).lower(): value for key, value in response.items()}
    for key in keys:
        if key in response and response[key] not in (None, ""):
            return response[key]
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _broker_order_id(response: dict[str, Any] | None) -> str | None:
    """Safely extract Zebu/Mynt order id from placement, history, or order-book rows."""
    if not response:
        return None
    value = _broker_value(
        response,
        (
            "norenordno",
            "orderno",
            "order_no",
            "order_id",
            "orderid",
            "Order No",
            "NOrdNo",
            "exchordid",
        ),
    )
    cleaned = _clean_order_id(value)
    return cleaned or None


def _broker_message(response: Any) -> str | None:
    """Safely extract broker status/rejection text from Zebu client response."""
    if not response:
        return None
    if isinstance(response, dict):
        message = _broker_value(
            response,
            (
                "rejreason",
                "Order rejection reason",
                "text",
                "emsg",
                "message",
                "reason",
                "remarks",
                "Remarks",
                "dsc",
                "status",
                "Status",
                "stat",
            ),
        )
        return str(message).strip() if message not in (None, "") else None
    return str(response)


def _broker_avg_price(response: Any) -> float | None:
    if not isinstance(response, dict):
        return None
    value = _broker_value(response, ("avgprc", "avg_price", "avgprice", "Executed Price", "avg. price"))
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _broker_filled_qty(response: Any) -> int:
    if not isinstance(response, dict):
        return 0
    value = _broker_value(response, ("fillshares", "filledqty", "cumqty", "filled_qty", "flqty"))
    try:
        return int(float(value)) if value not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _flatten_broker_orders(raw: Any) -> list[dict[str, Any]]:
    """Normalize Zebu order book/history responses into row dictionaries."""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    for key in ("orders", "values", "data", "result"):
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [raw] if raw else []


def _resolved_option_type(signal_side: str, option_strategy: str) -> str:
    """Determine the option contract type (CE/PE) to trade based on signal side and option strategy."""
    if signal_side.strip().upper() == "BUY":
        return "CE" if option_strategy.strip().upper() == "BUY" else "PE"
    else:
        return "PE" if option_strategy.strip().upper() == "BUY" else "CE"
