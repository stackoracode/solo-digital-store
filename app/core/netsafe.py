"""SSRF guard for admin-configured outbound endpoints (AI providers, supplier APIs, RPC URLs)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURLError(ValueError):
    pass


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or (isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None)
    )


def validate_outbound_url(
    url: str,
    allowlist: list[str] | None = None,
    *,
    resolve: bool = True,
    require_https: bool = True,
) -> str:
    """Return the normalized URL or raise UnsafeURLError.

    - https only (unless explicitly relaxed for local dev RPCs)
    - no credentials, no non-default ports unless https:443/http:80
    - hostname must be on the allowlist (exact or subdomain match) when one is given
    - every resolved address must be public (blocks 169.254.169.254, 10/8, ::1, ...)
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or (require_https and parts.scheme != "https"):
        raise UnsafeURLError("URL must use https")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URL are not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURLError("missing hostname")
    if parts.port not in (None, 443, 80):
        raise UnsafeURLError("non-standard port")
    if allowlist is not None and not any(host == a or host.endswith("." + a) for a in allowlist):
        raise UnsafeURLError(f"host {host} is not on the allowlist")
    try:
        ipaddress.ip_address(host)
        literal_ip = True
    except ValueError:
        literal_ip = False
    if literal_ip and not _is_public_ip(host):
        raise UnsafeURLError("private address")
    if resolve and not literal_ip:
        try:
            infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"cannot resolve {host}") from exc
        for info in infos:
            if not _is_public_ip(str(info[4][0])):
                raise UnsafeURLError(f"{host} resolves to a non-public address")
    return parts.geturl()
