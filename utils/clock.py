"""Exchange-local timestamps. Naive broker timestamps are interpreted as IST."""
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo('Asia/Kolkata')


def market_now() -> datetime:
    return datetime.now(EXCHANGE_TZ)


def market_time(value) -> datetime:
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=EXCHANGE_TZ) if value.tzinfo is None else value.astimezone(EXCHANGE_TZ)


def candle_start(value: datetime, minutes: int) -> datetime:
    value = market_time(value)
    anchor = datetime.combine(value.date(), time(9, 15), EXCHANGE_TZ)
    return anchor + timedelta(minutes=int((value-anchor).total_seconds() // (60*minutes))*minutes)
