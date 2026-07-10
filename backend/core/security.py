from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def is_local_http_request(client_host: str | None, host_header: str | None) -> bool:
    return _is_loopback(client_host) and _is_loopback(_hostname(host_header))


def is_allowed_websocket(
    client_host: str | None,
    host_header: str | None,
    origin_header: str | None,
) -> bool:
    if not is_local_http_request(client_host, host_header):
        return False
    if not origin_header:
        return True
    try:
        origin = urlsplit(origin_header)
    except ValueError:
        return False
    if origin.scheme not in {"http", "https"} or not _is_loopback(origin.hostname):
        return False
    host = urlsplit(f"//{host_header}")
    return origin.hostname == host.hostname and _effective_port(origin) == _effective_port(host)


def _hostname(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return urlsplit(f"//{value}").hostname
    except ValueError:
        return None


def _is_loopback(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _effective_port(parts) -> int | None:
    try:
        if parts.port is not None:
            return parts.port
    except ValueError:
        return None
    return 443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None
