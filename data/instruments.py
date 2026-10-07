from __future__ import annotations
from utils.clock import market_now, market_time, candle_start

from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
import zipfile
from typing import Any

import pandas as pd
import requests

# ==============================================================================
# SECTION 1: GLOBAL CONSTANTS & CONFIGURATIONS
# ==============================================================================

EXCHANGES = ("NSE", "BSE", "NFO", "BFO", "CDS", "MCX")
DERIVATIVE_EXCHANGES = {"NFO", "BFO", "CDS", "MCX"}
BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}


# ==============================================================================
# SECTION 2: PARSING HELPERS
# ==============================================================================

def trade_exchange(exchange: str, symbol: str, underlying: str) -> str:
    """Resolve the trading derivative exchange name from standard inputs."""
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


def _parse_expiry(value: Any) -> date | None:
    """Safely parse various date string representations into a Python date object."""
    if value in (None, ""):
        return None
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.date()


def _expiry_text(value: Any) -> str:
    """Convert an expiry date object or string into standardized 'DD-MM-YYYY' text format."""
    parsed = _parse_expiry(value)
    return parsed.strftime("%d-%m-%Y") if parsed else ""


def _num(value: Any) -> float | None:
    """Safely convert strings or numbers to floats, falling back to None if parsing fails."""
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ==============================================================================
# SECTION 3: INSTRUMENTMASTER CLASS & CACHE INITIALIZATION
# ==============================================================================

@dataclass
class InstrumentMaster:
    """Central manager to load, download, and index contract master files from Shoonya API."""
    root: Path
    frames: dict[str, pd.DataFrame]

    @classmethod
    def create(cls, root: Path) -> "InstrumentMaster":
        """Instantiate InstrumentMaster and load already downloaded CSV frames from cache."""
        inst = cls(root=root, frames={})
        inst.load_cached()
        return inst

    @property
    def folder(self) -> Path:
        """The absolute directory path containing local instrument symbols CSV cache."""
        return self.root / "instruments"

    def load_cached(self) -> None:
        """Scan the local directory and load existing symbols files into Pandas DataFrames."""
        self.folder.mkdir(exist_ok=True)
        for exchange in EXCHANGES:
            path = self.folder / f"{exchange}_symbols.csv"
            if path.exists():
                self.frames[exchange] = self._read_csv(path, exchange)


# ==============================================================================
# SECTION 4: DOWNLOAD & SHAPE NORMALIZATION UTILITIES
# ==============================================================================

    def ensure_daily(self, exchanges: tuple[str, ...] = EXCHANGES) -> None:
        """Download missing or outdated symbols files from Shoonya API for current day."""
        self.folder.mkdir(exist_ok=True)
        today = market_now().date()
        for exchange in exchanges:
            path = self.folder / f"{exchange}_symbols.csv"
            if path.exists() and datetime.fromtimestamp(path.stat().st_mtime, market_now().tzinfo).date() == today:
                if exchange not in self.frames:
                    self.frames[exchange] = self._read_csv(path, exchange)
                continue
            self.frames[exchange] = self._download_exchange(exchange, path)

    def _download_exchange(self, exchange: str, path: Path) -> pd.DataFrame:
        """Fetch zip symbols txt from api.shoonya.com, extract, and write CSV cache locally."""
        url = f"https://api.shoonya.com/{exchange}_symbols.txt.zip"
        response = requests.get(url, allow_redirects=True, timeout=30)
        response.raise_for_status()
        
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            name = archive.namelist()[0]
            with archive.open(name) as handle:
                df = pd.read_csv(handle)
                
        df = self._normalize(df, exchange)
        df.to_csv(path, index=False)
        return df

    def _read_csv(self, path: Path, exchange: str) -> pd.DataFrame:
        """Read standard columns from local CSV cache file."""
        return self._normalize(pd.read_csv(path), exchange)

    def _normalize(self, df: pd.DataFrame, exchange: str) -> pd.DataFrame:
        """Clean and map inconsistent header fields in Shoonya API raw contract lists."""
        df = df.copy()
        rename = {
            "TradingSy": "TradingSymbol",
            "TradingSym": "TradingSymbol",
            "Trading Symbol": "TradingSymbol",
            "OptionTyp": "OptionType",
            "Option Type": "OptionType",
            "StrikePric": "StrikePrice",
            "Strike Price": "StrikePrice",
            "TickSiz": "TickSize",
            "Tick Size": "TickSize",
            "Lotsize": "LotSize",
            "Lot Size": "LotSize",
        }
        df.rename(columns={k: v for k, v in rename.items() if k in df.columns}, inplace=True)
        
        if "Exchange" not in df.columns:
            df.insert(0, "Exchange", exchange)
        if "Symbol" not in df.columns and "TradingSymbol" in df.columns and exchange == "BFO":
            symbols = df["TradingSymbol"].astype(str).str.extract(r"([A-Z]+)", expand=False).fillna("")
            df.insert(3, "Symbol", symbols)
            
        if "Symbol" in df.columns:
            df["Symbol"] = df["Symbol"].astype(str).str.upper().str.strip()
        if "OptionType" in df.columns:
            df["OptionType"] = df["OptionType"].astype(str).str.upper().str.strip()
        if "Instrument" in df.columns:
            df["Instrument"] = df["Instrument"].astype(str).str.upper().str.strip()
            
        if "Expiry" in df.columns:
            expiry_dates = pd.to_datetime(df["Expiry"], errors="coerce", format="mixed", dayfirst=True)
            df["ExpiryText"] = expiry_dates.dt.strftime("%d-%m-%Y").fillna("")
            df["_ExpiryDate"] = expiry_dates
        else:
            df["ExpiryText"] = ""
            df["_ExpiryDate"] = pd.NaT
            
        if "StrikePrice" in df.columns:
            df["_Strike"] = pd.to_numeric(df["StrikePrice"], errors="coerce")
        else:
            df["_Strike"] = pd.NA
            
        return df


# ==============================================================================
# SECTION 5: METADATA & NEAREST EXPIRY SELECTOR
# ==============================================================================

    def metadata(self, exchange: str, symbol: str, underlying: str) -> dict[str, Any]:
        """Compile lot size, strike gap, and sorted expiries list for client dropdown forms."""
        exch = trade_exchange(exchange, symbol, underlying)
        df = self._filtered(exch, symbol, underlying)
        if df.empty:
            return {
                "exchange": exch,
                "underlying": underlying.upper(),
                "expiries": [],
                "lot_size": None,
                "strike_gap": None,
                "tick_size": None
            }
            
        # Extract unique expiries and sort by date chronologically (nearest first)
        raw_expiries = [x for x in df["ExpiryText"].dropna().unique().tolist() if x]
        def _sort_key(text: str):
            try:
                return datetime.strptime(text, "%d-%m-%Y")
            except ValueError:
                return datetime.max
        expiries = sorted(raw_expiries, key=_sort_key)
        
        # Select the nearest upcoming expiry (or fallback to soonest available)
        today = market_now().date()
        upcoming = [e for e in expiries if _sort_key(e).date() >= today]
        default_expiry = upcoming[0] if upcoming else (expiries[0] if expiries else "")
        
        lot_size = self._first_number(df, "LotSize")
        tick_size = self._first_number(df, "TickSize")
        strike_gap = self._strike_gap(df)
        
        return {
            "exchange": exch,
            "underlying": underlying.upper(),
            "expiries": expiries,
            "default_expiry": default_expiry,
            "lot_size": int(lot_size) if lot_size and lot_size.is_integer() else lot_size,
            "strike_gap": int(strike_gap) if strike_gap and strike_gap.is_integer() else strike_gap,
            "tick_size": tick_size,
        }


# ==============================================================================
# SECTION 6: CONTRACT RESOLUTION API
# ==============================================================================

    def resolve_future(self, underlying: str, exchange: str, expiry: str = "CURRENT_MONTH") -> dict[str, Any] | None:
        """Find the matching future contract row from cached exchange masters."""
        df = self._filtered(exchange, "Future", underlying, expiry)
        if "Instrument" in df.columns:
            df = df[df["Instrument"].isin(["FUTIDX", "FUTSTK", "FUTCUR", "FUTCOM"])]
        row = self._first_expiry_row(df)
        return self._row_contract(row, exchange) if row is not None else None

    def resolve_option(self, underlying: str, strike: int, option_type: str, expiry: str, exchange: str) -> dict[str, Any] | None:
        """Find the matching option contract row from cached exchange masters."""
        df = self._filtered(exchange, "Option", underlying, expiry)
        if "OptionType" in df.columns:
            df = df[df["OptionType"] == option_type.upper()]
        if "_Strike" in df.columns:
            df = df[df["_Strike"] == float(strike)]
        row = self._first_expiry_row(df)
        return self._row_contract(row, exchange) if row is not None else None


# ==============================================================================
# SECTION 7: SEARCH FILTERS & EXTRACTION HELPERS
# ==============================================================================

    def _filtered(self, exchange: str, symbol: str, underlying: str, expiry: str | None = None) -> pd.DataFrame:
        """Utility query to subset exchange master frames by underlying, type, and expiry."""
        df = self.frames.get(exchange)
        if df is None or df.empty:
            return pd.DataFrame()
        result = df.copy()
        if "_ExpiryDate" in result and symbol in {"Option", "Future"}:
            result = result[result["_ExpiryDate"].dt.date >= market_now().date()]
        clean = underlying.strip().upper()
        
        if "Symbol" in result.columns:
            result = result[result["Symbol"] == clean]
        if symbol == "Option" and "Instrument" in result.columns:
            result = result[result["Instrument"].isin(["OPTIDX", "OPTSTK", "OPTCUR", "OPTFUT", "OPTCOM"])]
        if symbol == "Future" and "Instrument" in result.columns:
            result = result[result["Instrument"].isin(["FUTIDX", "FUTSTK", "FUTCUR", "FUTCOM"])]
            
        if expiry and expiry not in {"CURRENT_WEEK", "CURRENT_MONTH"}:
            wanted = _expiry_text(expiry)
            if not wanted:
                raise ValueError("Invalid expiry date")
            result = result[result["ExpiryText"] == wanted]
        return result

    def _first_expiry_row(self, df: pd.DataFrame) -> pd.Series | None:
        """Sort rows chronologically by expiry date and return the first matching contract row."""
        if df.empty:
            return None
        sort_cols = [col for col in ["_ExpiryDate", "_Strike"] if col in df.columns]
        if sort_cols:
            df = df.sort_values(sort_cols)
        return df.iloc[0]

    def _row_contract(self, row: pd.Series, exchange: str) -> dict[str, Any]:
        """Map standard pandas series record values into Zebu contract dictionary layout."""
        return {
            "exchange": exchange,
            "tradingsymbol": str(row.get("TradingSymbol") or row.get("Symbol") or ""),
            "token": str(row.get("Token") or ""),
            "expiry": str(row.get("ExpiryText") or ""),
            "lot_size": _num(row.get("LotSize")),
            "tick_size": _num(row.get("TickSize")) or 0.05,
            "strike": _num(row.get("StrikePrice")),
            "raw": row.drop(labels=[c for c in ("_ExpiryDate", "_Strike") if c in row.index]).to_dict(),
        }

    def _first_number(self, df: pd.DataFrame, column: str) -> float | None:
        """Find the first valid positive numeric value from a specified column."""
        if column not in df.columns:
            return None
        for value in df[column].dropna().tolist():
            number = _num(value)
            if number is not None and number > 0:
                return number
        return None

    def _strike_gap(self, df: pd.DataFrame) -> float | None:
        """Find the minimum strike increment gap by analyzing adjacent strike intervals."""
        if "_Strike" not in df.columns:
            return None
        strikes = sorted(set(float(x) for x in df["_Strike"].dropna().tolist() if float(x) > 0))
        diffs = [round(b - a, 6) for a, b in zip(strikes, strikes[1:]) if b > a]
        return min(diffs) if diffs else None
