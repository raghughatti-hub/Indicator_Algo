from functools import lru_cache
from pathlib import Path
import os
import json
import tempfile
import threading

from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parents[1]
LEGACY_WORKING_DIR = BASE_DIR.parent


def _parse_key_value_file(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not path.exists():
        return parsed
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
        elif "=" in line:
            key, value = line.split("=", 1)
        elif "\t" in line:
            key, value = line.split("\t", 1)
        else:
            continue
        clean_key = key.replace(":", "").strip().lower().replace(" ", "_")
        clean_val = value.strip().lstrip(":").strip()
        parsed[clean_key] = clean_val
    return parsed


def _load_dotenv() -> dict[str, str]:
    """Read local fallback values; never override process-injected configuration."""
    from dotenv import dotenv_values
    return {k:v for k,v in dotenv_values(BASE_DIR / ".env").items() if v is not None}


_token_lock = threading.RLock()


def _session_tokens() -> dict[str, str]:
    from utils.security import runtime_dir
    path = runtime_dir() / "session_tokens.json"
    if not path.exists():
        return {}
    with _token_lock:
        return json.loads(path.read_text())


class Settings(BaseModel):
    active_broker: str = "zebu"  # "zebu", "flattrade", or "upstox"
    
    # Zebu Credentials
    zebu_client_id: str = ""
    zebu_api_secret: str = ""
    zebu_user_id: str = ""
    zebu_password: str = ""
    zebu_totp_secret: str = ""
    zebu_redirect_url: str = ""
    zebu_access_token: str = ""
    
    # FlatTrade Credentials
    flattrade_user_id: str = ""
    flattrade_password: str = ""
    flattrade_totp_secret: str = ""
    flattrade_api_key: str = ""
    flattrade_api_secret: str = ""
    flattrade_redirect_url: str = ""
    flattrade_access_token: str = ""
    flattrade_suser_token: str = ""

    # Upstox Credentials
    upstox_user_id: str = ""
    upstox_password: str = ""
    upstox_pin: str = ""
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_totp_secret: str = ""
    upstox_redirect_url: str = ""
    upstox_mobile: str = ""
    upstox_access_token: str = ""
    
    live_trading_enabled: bool = False
    default_product_type: str = "M"
    default_exchange: str = "NSE"
    default_quantity: int = 1
    creds_txt_loaded: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    dotenv = _load_dotenv()
    tokens = _session_tokens()
    def setting(name,default=""):
        return os.environ.get(name,tokens.get(name,dotenv.get(name,default)))
    
    # Read active broker from env (fallback to zebu)
    active = setting("ACTIVE_BROKER", "zebu").lower()
    
    # Load Zebu creds from app/zebu/creds.txt or root creds.txt
    zebu_path = BASE_DIR / "app" / "zebu" / "creds.txt"
    if not zebu_path.exists():
        zebu_path = BASE_DIR / "creds.txt"
        if not zebu_path.exists():
            zebu_path = LEGACY_WORKING_DIR / "creds.txt"
    zebu_txt = _parse_key_value_file(zebu_path) if zebu_path.exists() else {}

    # Load FlatTrade creds from app/flattrade/creds.txt or Flattrade/creds.txt
    flattrade_path = BASE_DIR / "app" / "flattrade" / "creds.txt"
    if not flattrade_path.exists():
        flattrade_path = BASE_DIR / "Flattrade" / "creds.txt"
    flattrade_txt = _parse_key_value_file(flattrade_path) if flattrade_path.exists() else {}

    # Load Upstox creds from app/upstox/creds.txt or Upstox/Upstox_Creds.txt
    upstox_path = BASE_DIR / "app" / "upstox" / "creds.txt"
    if not upstox_path.exists():
        upstox_path = BASE_DIR / "Upstox" / "Upstox_Creds.txt"
    upstox_txt = _parse_key_value_file(upstox_path) if upstox_path.exists() else {}
    
    creds_txt_loaded = bool(zebu_txt) or bool(flattrade_txt) or bool(upstox_txt)

    return Settings(
        active_broker=active,
        
        # Zebu
        zebu_client_id=setting("ZEBU_CLIENT_ID", "") or zebu_txt.get("client_id", ""),
        zebu_api_secret=setting("ZEBU_API_SECRET", "") or zebu_txt.get("api_secret", ""),
        zebu_user_id=setting("ZEBU_USER_ID", "") or zebu_txt.get("vendor_code", ""),
        zebu_password=setting("ZEBU_PASSWORD", "") or zebu_txt.get("password", ""),
        zebu_totp_secret=setting("ZEBU_TOTP_SECRET", "") or zebu_txt.get("totp", "") or zebu_txt.get("totp_", ""),
        zebu_redirect_url=setting("ZEBU_REDIRECT_URL", "") or zebu_txt.get("redirecturl", ""),
        zebu_access_token=setting("ZEBU_ACCESS_TOKEN", ""),
        
        # FlatTrade
        flattrade_user_id=setting("FLATTRADE_USER_ID", "") or flattrade_txt.get("vendor_code", ""),
        flattrade_password=setting("FLATTRADE_PASSWORD", "") or flattrade_txt.get("password", ""),
        flattrade_totp_secret=setting("FLATTRADE_TOTP_SECRET", "") or flattrade_txt.get("totp", "") or flattrade_txt.get("totp_", ""),
        flattrade_api_key=setting("FLATTRADE_API_KEY", "") or flattrade_txt.get("api_key", ""),
        flattrade_api_secret=setting("FLATTRADE_API_SECRET", "") or flattrade_txt.get("api_secret", ""),
        flattrade_redirect_url=setting("FLATTRADE_REDIRECT_URL", "") or flattrade_txt.get("redirecturl", ""),
        flattrade_access_token=setting("FLATTRADE_ACCESS_TOKEN", ""),
        flattrade_suser_token=setting("FLATTRADE_SUSER_TOKEN", ""),

        # Upstox
        upstox_user_id=setting("UPSTOX_USER_ID", "") or upstox_txt.get("user_code", ""),
        upstox_password=setting("UPSTOX_PASSWORD", "") or upstox_txt.get("password", ""),
        upstox_pin=setting("UPSTOX_PIN", "") or upstox_txt.get("pin", ""),
        upstox_api_key=setting("UPSTOX_API_KEY", "") or upstox_txt.get("api_key", ""),
        upstox_api_secret=setting("UPSTOX_API_SECRET", "") or upstox_txt.get("api_secret", ""),
        upstox_totp_secret=setting("UPSTOX_TOTP_SECRET", "") or upstox_txt.get("totp", ""),
        upstox_redirect_url=setting("UPSTOX_REDIRECT_URL", "") or upstox_txt.get("redirect_url", ""),
        upstox_mobile=setting("UPSTOX_MOBILE", "") or upstox_txt.get("mobile", ""),
        upstox_access_token=setting("UPSTOX_ACCESS_TOKEN", ""),
        
        # General Settings
        live_trading_enabled=setting("LIVE_TRADING_ENABLED", "false").lower() == "true",
        default_product_type=setting("DEFAULT_PRODUCT_TYPE", "M"),
        default_exchange=setting("DEFAULT_EXCHANGE", "NSE"),
        default_quantity=int(setting("DEFAULT_QUANTITY", "1")),
        creds_txt_loaded=creds_txt_loaded,
    )


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def save_token_to_env(token: str = "", broker: str = "zebu", suser_token: str = "") -> None:
    """Persist sessions atomically in ignored runtime storage, not credential files."""
    from utils.security import runtime_dir
    updates = {"ACTIVE_BROKER":broker}
    if token:
        updates[f"{broker.upper()}_ACCESS_TOKEN"] = token
        if broker == "flattrade":
            updates["FLATTRADE_SUSER_TOKEN"] = suser_token or token
    with _token_lock:
        folder = runtime_dir()
        folder.mkdir(parents=True,exist_ok=True)
        path = folder / "session_tokens.json"
        values = _session_tokens()
        values.update(updates)
        fd, temporary = tempfile.mkstemp(dir=folder,prefix="session-")
        try:
            with os.fdopen(fd,"w") as handle:
                json.dump(values,handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary,path)
        except Exception:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
        get_settings.cache_clear()
