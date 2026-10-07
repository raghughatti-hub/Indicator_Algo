from functools import lru_cache
from pathlib import Path
import os

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


def _load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


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
    _load_dotenv()
    
    # Read active broker from env (fallback to zebu)
    active = os.getenv("ACTIVE_BROKER", "zebu").lower()
    
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
        zebu_client_id=os.getenv("ZEBU_CLIENT_ID", "") or zebu_txt.get("client_id", ""),
        zebu_api_secret=os.getenv("ZEBU_API_SECRET", "") or zebu_txt.get("api_secret", ""),
        zebu_user_id=os.getenv("ZEBU_USER_ID", "") or zebu_txt.get("vendor_code", ""),
        zebu_password=os.getenv("ZEBU_PASSWORD", "") or zebu_txt.get("password", ""),
        zebu_totp_secret=os.getenv("ZEBU_TOTP_SECRET", "") or zebu_txt.get("totp", "") or zebu_txt.get("totp_", ""),
        zebu_redirect_url=os.getenv("ZEBU_REDIRECT_URL", "") or zebu_txt.get("redirecturl", ""),
        zebu_access_token=os.getenv("ZEBU_ACCESS_TOKEN", ""),
        
        # FlatTrade
        flattrade_user_id=os.getenv("FLATTRADE_USER_ID", "") or flattrade_txt.get("vendor_code", ""),
        flattrade_password=os.getenv("FLATTRADE_PASSWORD", "") or flattrade_txt.get("password", ""),
        flattrade_totp_secret=os.getenv("FLATTRADE_TOTP_SECRET", "") or flattrade_txt.get("totp", "") or flattrade_txt.get("totp_", ""),
        flattrade_api_key=os.getenv("FLATTRADE_API_KEY", "") or flattrade_txt.get("api_key", ""),
        flattrade_api_secret=os.getenv("FLATTRADE_API_SECRET", "") or flattrade_txt.get("api_secret", ""),
        flattrade_redirect_url=os.getenv("FLATTRADE_REDIRECT_URL", "") or flattrade_txt.get("redirecturl", ""),
        flattrade_access_token=os.getenv("FLATTRADE_ACCESS_TOKEN", ""),
        flattrade_suser_token=os.getenv("FLATTRADE_SUSER_TOKEN", ""),

        # Upstox
        upstox_user_id=os.getenv("UPSTOX_USER_ID", "") or upstox_txt.get("user_code", ""),
        upstox_password=os.getenv("UPSTOX_PASSWORD", "") or upstox_txt.get("password", ""),
        upstox_pin=os.getenv("UPSTOX_PIN", "") or upstox_txt.get("pin", ""),
        upstox_api_key=os.getenv("UPSTOX_API_KEY", "") or upstox_txt.get("api_key", ""),
        upstox_api_secret=os.getenv("UPSTOX_API_SECRET", "") or upstox_txt.get("api_secret", ""),
        upstox_totp_secret=os.getenv("UPSTOX_TOTP_SECRET", "") or upstox_txt.get("totp", ""),
        upstox_redirect_url=os.getenv("UPSTOX_REDIRECT_URL", "") or upstox_txt.get("redirect_url", ""),
        upstox_mobile=os.getenv("UPSTOX_MOBILE", "") or upstox_txt.get("mobile", ""),
        upstox_access_token=os.getenv("UPSTOX_ACCESS_TOKEN", ""),
        
        # General Settings
        live_trading_enabled=os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true",
        default_product_type=os.getenv("DEFAULT_PRODUCT_TYPE", "M"),
        default_exchange=os.getenv("DEFAULT_EXCHANGE", "NSE"),
        default_quantity=int(os.getenv("DEFAULT_QUANTITY", "1")),
        creds_txt_loaded=creds_txt_loaded,
    )


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def save_token_to_env(token: str = "", broker: str = "zebu", suser_token: str = "") -> None:
    """Write or update session tokens and ACTIVE_BROKER in local .env file."""
    env_path = BASE_DIR / ".env"
    
    # Decide which keys to write
    updates = {"ACTIVE_BROKER": broker}
    if token:
        if broker == "zebu":
            updates["ZEBU_ACCESS_TOKEN"] = token
        elif broker == "flattrade":
            updates["FLATTRADE_ACCESS_TOKEN"] = token
            updates["FLATTRADE_SUSER_TOKEN"] = suser_token or token
        elif broker == "upstox":
            updates["UPSTOX_ACCESS_TOKEN"] = token

    # Read existing lines
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    for key, val in updates.items():
        new_line = f"{key}={val}"
        updated = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped == key:
                lines[i] = new_line
                updated = True
                break
        if not updated:
            lines.append(new_line)
            
        # Update current environment
        os.environ[key] = val

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
