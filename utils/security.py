"""Local control authentication and cross-origin protection."""
import hmac
import ipaddress
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

_token_lock = threading.Lock()


def runtime_dir() -> Path:
    from config.settings import BASE_DIR
    return Path(os.getenv('ALGO_RUNTIME_DIR', str(BASE_DIR / '.runtime')))


def control_token() -> str:
    configured = os.getenv('CONTROL_API_TOKEN')
    if configured:
        if len(configured) < 32:
            raise RuntimeError('CONTROL_API_TOKEN must contain at least 32 characters')
        return configured
    with _token_lock:
        folder = runtime_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'control_api_token'
        if not path.exists():
            try:
                with path.open('x') as handle:
                    os.chmod(path,0o600)
                    handle.write(secrets.token_urlsafe(32))
            except FileExistsError:
                pass
        return path.read_text().strip()


def local_browser(request) -> bool:
    try:
        peer = ipaddress.ip_address(request.client.host)
        return peer.is_loopback and request.url.hostname in {'127.0.0.1','localhost','::1'}
    except (ValueError, AttributeError):
        return False


def same_origin(request) -> bool:
    if request.headers.get('sec-fetch-site') == 'cross-site':
        return False
    origin = request.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        return parsed.scheme == request.url.scheme and parsed.netloc == request.url.netloc
    return True


def authorized(request) -> bool:
    if not same_origin(request):
        return False
    bearer = request.headers.get('authorization','')
    supplied = bearer[7:] if bearer.startswith('Bearer ') else request.cookies.get('algo_session','')
    if not local_browser(request) and not os.getenv('CONTROL_API_TOKEN'):
        return False
    return bool(supplied) and hmac.compare_digest(supplied,control_token())


def acquire_engine_lock():
    folder = runtime_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / "engine.lock").open("a+")
    os.chmod(handle.name, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            handle.write("0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("Another trading engine owns this runtime directory") from exc
    return handle
