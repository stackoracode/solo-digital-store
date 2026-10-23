"""Edge protections: trusted client IP, host allowlist, body size limit, security headers, access log."""

from __future__ import annotations

import ipaddress
import logging
import time
import uuid
from collections.abc import Callable

import anyio
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings

log = logging.getLogger("app.access")

CSP = (
    "default-src 'self'; "
    "img-src 'self' https: data:; "
    "script-src 'self' https://challenges.cloudflare.com https://telegram.org; "
    "frame-src https://challenges.cloudflare.com https://oauth.telegram.org; "
    "style-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    # Payment redirects after POST are subject to form-action in some browsers.
    "form-action 'self' https://*.waffo.ai"
)


def client_ip_from(scope: Scope, settings: Settings, networks: list) -> str:
    peer = (scope.get("client") or ("0.0.0.0", 0))[0]  # noqa: S104 - placeholder for missing peer
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in net for net in networks):
        return peer  # direct connection: never trust forwarded headers
    headers = Headers(scope=scope)
    if settings.trust_cf_connecting_ip and headers.get("cf-connecting-ip"):
        return headers["cf-connecting-ip"].strip()[:64]
    xff = headers.get("x-forwarded-for")
    if xff:
        # Right-most address not belonging to a trusted proxy.
        for candidate in reversed([p.strip() for p in xff.split(",") if p.strip()]):
            try:
                if not any(ipaddress.ip_address(candidate) in net for net in networks):
                    return candidate[:64]
            except ValueError:
                return peer
    return peer


_DOMAIN_CACHE: dict[str, object] = {"at": 0.0, "domains": set()}


def invalidate_domain_cache() -> None:
    """Call after verifying/disabling a StoreDomain so routing changes apply immediately."""
    _DOMAIN_CACHE["at"] = 0.0


class EdgeMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        domain_loader: Callable[[], set[str]] | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.networks = [ipaddress.ip_network(c, strict=False) for c in settings.trusted_proxy_cidrs]
        self.primary_hosts = {h.lower() for h in settings.primary_hosts}
        self.domain_loader = domain_loader

    async def _known_domains(self) -> set[str]:
        loaded_at = float(_DOMAIN_CACHE["at"])  # type: ignore[arg-type]
        if self.domain_loader and (loaded_at == 0.0 or time.monotonic() - loaded_at > 30):
            try:
                _DOMAIN_CACHE["domains"] = await anyio.to_thread.run_sync(self.domain_loader)
            except Exception:  # noqa: BLE001 - keep serving primary hosts if DB is briefly unavailable
                log.exception("failed to refresh store domains")
            _DOMAIN_CACHE["at"] = time.monotonic()
        return _DOMAIN_CACHE["domains"]  # type: ignore[return-value]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        headers = Headers(scope=scope)
        request_id = uuid.uuid4().hex[:16]
        ip = client_ip_from(scope, self.settings, self.networks)
        host = (headers.get("host") or "").split(":")[0].lower().rstrip(".")
        state = scope.setdefault("state", {})
        state.update({"request_id": request_id, "client_ip": ip, "host": host})
        status_holder = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw = list(message.get("headers", []))
                existing = {k.lower() for k, _ in raw}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"strict-origin-when-cross-origin",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), payment=()",
                    b"cross-origin-opener-policy": b"same-origin",
                    b"content-security-policy": CSP.encode(),
                    b"x-request-id": request_id.encode(),
                }
                if self.settings.cookie_secure:
                    extra[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                for k, v in extra.items():
                    if k not in existing:
                        raw.append((k, v))
                message["headers"] = raw
            await send(message)

        async def reject(status: int, body: bytes) -> None:
            await send_wrapper(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": body})

        path = scope.get("path", "")
        if path not in ("/healthz",) and host not in self.primary_hosts:
            if host not in await self._known_domains():
                await reject(421, b"Unknown host")
                self._log(scope, ip, host, 421, start, request_id)
                return

        limit = self.settings.max_upload_bytes if path.startswith("/admin") else self.settings.max_body_bytes
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await reject(413, b"Request body too large")
                    self._log(scope, ip, host, 413, start, request_id)
                    return
            except ValueError:
                await reject(400, b"Bad Content-Length")
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except BodyTooLarge:
            if status_holder["status"] == 0:
                await reject(413, b"Request body too large")
            status_holder["status"] = 413
        finally:
            self._log(scope, ip, host, status_holder["status"], start, request_id)

    @staticmethod
    def _log(scope: Scope, ip: str, host: str, status: int, start: float, request_id: str) -> None:
        headers = Headers(scope=scope)
        log.info(
            "request",
            extra={
                "request_id": request_id,
                "ip": ip,
                "host": host,
                "method": scope.get("method"),
                "path": scope.get("path", "")
                + ("?" + scope["query_string"].decode() if scope.get("query_string") else ""),
                "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                "ua": (headers.get("user-agent") or "")[:200],
            },
        )


class BodyTooLarge(Exception):
    pass
