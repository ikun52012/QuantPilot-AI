"""
Request helpers for proxy-aware public URLs.
"""
from fastapi import Request

from core.config import settings


def _first_header_value(value: str) -> str:
    return str(value or "").split(",", 1)[0].strip()


def _cf_scheme(request: Request) -> str:
    value = request.headers.get("cf-visitor", "")
    return "https" if '"scheme":"https"' in value.lower() else ""


def public_base_url(request: Request) -> str:
    """
    Build the externally visible base URL.

    Starlette's request.base_url reflects the direct ASGI connection unless
    the server is launched with trusted proxy headers. This helper honors the
    common proxy/CDN headers used by Cloudflare, Nginx, and Docker deployments.
    """
    configured = str(settings.server.public_base_url or "").strip().rstrip("/")
    if configured and "your-domain" not in configured.lower():
        return configured

    proto = (
        _first_header_value(request.headers.get("x-forwarded-proto", ""))
        or _cf_scheme(request)
        or request.url.scheme
        or "http"
    ).lower()
    host = (
        _first_header_value(request.headers.get("x-forwarded-host", ""))
        or request.headers.get("host", "")
        or request.url.netloc
    ).strip()
    port = _first_header_value(request.headers.get("x-forwarded-port", ""))

    if port and ":" not in host and not ((proto == "https" and port == "443") or (proto == "http" and port == "80")):
        host = f"{host}:{port}"

    return f"{proto}://{host}".rstrip("/")
