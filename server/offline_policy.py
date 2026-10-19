"""Network boundary for local inference; parsing never performs DNS or I/O."""

from ipaddress import ip_address
from urllib.parse import urlsplit


def local_model_origin(value: str) -> str:
    """Return an HTTP(S) loopback origin, or refuse before opening a connection."""
    try:
        url = urlsplit(value.strip())
        host = url.hostname
        if (
            url.scheme not in {"http", "https"}
            or not host
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError
        if host != "localhost" and not ip_address(host).is_loopback:
            raise ValueError
        port = url.port  # Also validates range and syntax.
        if host != "localhost":
            host = str(ip_address(host))
        authority = f"[{host}]" if ":" in host else host
        return f"{url.scheme}://{authority}" + (f":{port}" if port is not None else "")
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Orb inference requires an HTTP(S) loopback origin") from None
