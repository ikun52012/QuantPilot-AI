"""
QuantPilot AI - Application Factory
Creates and configures the FastAPI application instance.
"""
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from core.cache import cache
from core.config import settings
from core.database import db_manager
from core.lifespan import lifespan
from core.metrics import metrics_endpoint
from core.middleware import setup_middleware
from routers.admin import router as admin_router
from routers.ai_config import router as ai_config_router
from routers.auth import router as auth_router
from routers.backtest import router as backtest_router
from routers.chart import router as chart_router
from routers.i18n import router as i18n_router
from routers.social import router as social_router
from routers.strategies import router as strategies_router
from routers.strategy_editor import router as strategy_editor_router
from routers.subscription import router as subscription_router
from routers.user import router as user_router

# Import routers
from routers.webhook import router as webhook_router
from routers.websocket import router as websocket_router

STATIC_DIR = Path(__file__).parent.parent / "static"

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "Vary": "Cookie",
}


def _apply_no_store_headers(response):
    for key, value in _NO_STORE_HEADERS.items():
        response.headers[key] = value
    return response


def _forced_login_requested(request: Request) -> bool:
    return any(key in request.query_params for key in ("expired", "logout", "force"))


def _login_response(request: Request):
    from core.auth import clear_auth_cookie
    response = FileResponse(STATIC_DIR / "login.html")
    clear_auth_cookie(response, request)
    return _apply_no_store_headers(response)


def _redirect_no_store(url: str):
    return _apply_no_store_headers(RedirectResponse(url=url, status_code=303))


def _redirect_dashboard_fragment(fragment: str, params: dict[str, str] | None = None):
    from urllib.parse import urlencode

    filtered_params = {key: value for key, value in (params or {}).items() if value}
    query = f"?{urlencode(filtered_params)}" if filtered_params else ""
    return _redirect_no_store(f"/dashboard{query}#{fragment}")


def _mount_v1_aliases(app: FastAPI, source_router):
    """Mount /api/v1/* aliases for backward compatibility."""
    from fastapi import APIRouter
    from fastapi.routing import APIRoute

    v1_router = APIRouter(prefix="/api/v1", tags=["v1"])

    for route in source_router.routes:
        if not isinstance(route, APIRoute):
            continue

        original_path = route.path
        if original_path.startswith("/api/"):
            alias_path = original_path.replace("/api", "", 1)
        elif original_path.startswith("/webhook"):
            alias_path = original_path
        else:
            continue

        route_kwargs: dict[str, Any] = {
            "methods": list(route.methods or []),
            "name": f"v1_{route.name}",
            "response_model": getattr(route, "response_model", None),
            "status_code": getattr(route, "status_code", None),
            "tags": list(getattr(route, "tags", []) or []),
            "dependencies": list(getattr(route, "dependencies", []) or []),
            "summary": getattr(route, "summary", None),
            "description": getattr(route, "description", None),
        }
        response_class = getattr(route, "response_class", None)
        if response_class is not None:
            route_kwargs["response_class"] = response_class

        v1_router.add_api_route(alias_path, route.endpoint, **cast(dict[str, Any], route_kwargs))

    app.include_router(v1_router)


def _setup_page_routes(app: FastAPI):
    """Setup HTML page routes."""
    from core.auth import get_optional_user

    @app.get("/", response_class=HTMLResponse)
    async def homepage(request: Request):
        async with db_manager.async_session_factory() as db:
            user = await get_optional_user(request, db)
            if user:
                return _redirect_no_store("/dashboard")
        return _apply_no_store_headers(FileResponse(STATIC_DIR / "home.html"))

    @app.get("/dashboard")
    async def dashboard(request: Request):
        async with db_manager.async_session_factory() as db:
            user = await get_optional_user(request, db)
            if not user:
                return _redirect_no_store("/login?expired=1")
        return _apply_no_store_headers(FileResponse(STATIC_DIR / "index.html"))

    @app.get("/login")
    async def login_page(request: Request):
        if _forced_login_requested(request):
            return _login_response(request)
        async with db_manager.async_session_factory() as db:
            user = await get_optional_user(request, db)
            if user:
                return _redirect_no_store("/dashboard")
        return _login_response(request)

    @app.get("/register")
    async def register_page(request: Request):
        async with db_manager.async_session_factory() as db:
            user = await get_optional_user(request, db)
            if user:
                return _redirect_no_store("/dashboard")
        return _apply_no_store_headers(FileResponse(STATIC_DIR / "register.html"))

    @app.get("/sw.js")
    async def service_worker():
        response = FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript")
        response.headers["Service-Worker-Allowed"] = "/"
        # Always fetch the worker script fresh so updates are picked up reliably.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    @app.api_route("/share", methods=["GET", "POST"])
    async def share_target_handler(request: Request):
        """Handle PWA share-target launches by redirecting into the dashboard."""
        if request.method == "GET":
            title = request.query_params.get("title", "")
            text = request.query_params.get("text", "")
            url = request.query_params.get("url", "")
        else:
            title = ""
            text = ""
            url = ""
            content_type = request.headers.get("content-type", "").lower()
            if "application/json" in content_type:
                try:
                    payload = await request.json()
                except Exception:
                    payload = {}
                if isinstance(payload, dict):
                    title = str(payload.get("title") or "")
                    text = str(payload.get("text") or "")
                    url = str(payload.get("url") or "")
            elif "application/x-www-form-urlencoded" in content_type:
                body = (await request.body()).decode("utf-8", errors="ignore")
                from urllib.parse import parse_qs

                parsed = parse_qs(body)
                title = (parsed.get("title") or [""])[0]
                text = (parsed.get("text") or [""])[0]
                url = (parsed.get("url") or [""])[0]

        return _redirect_dashboard_fragment("social", {
            "title": title,
            "text": text,
            "url": url,
        })

    @app.get("/signal")
    async def protocol_signal_handler(request: Request):
        """Handle protocol-launch redirects by forwarding payload into the dashboard."""
        data = request.query_params.get("data", "")
        return _redirect_dashboard_fragment("dashboard", {"data": data})


def _setup_utility_routes(app: FastAPI):
    """Setup health, metrics, and stats endpoints."""

    @app.get("/health")
    async def health_check():
        from sqlalchemy import text
        checks = {
            "status": "healthy",
            "version": settings.app_version,
            "database": "ok" if db_manager.engine else "not initialized",
            "cache": "ok" if cache._initialized else "not initialized",
        }
        try:
            async with db_manager.async_session_factory() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as e:
            checks["database"] = f"error: {str(e)[:50]}"
            checks["status"] = "unhealthy"

        status_code = 200 if checks["status"] == "healthy" else 503
        return JSONResponse(content=checks, status_code=status_code)

    @app.get("/metrics")
    async def metrics():
        return await metrics_endpoint()

    @app.get("/stats")
    async def get_stats():
        from sqlalchemy import select

        from core.database import TradeModel, WebhookEventModel
        from core.utils.datetime import utcnow

        today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        async with db_manager.async_session_factory() as session:
            events = await session.execute(
                select(WebhookEventModel).where(WebhookEventModel.created_at >= today_start)
            )
            trades = await session.execute(
                select(TradeModel).where(TradeModel.timestamp >= today_start)
            )
            event_rows = list(events.scalars().all())
            trade_rows = list(trades.scalars().all())

        executed = [trade for trade in trade_rows if trade.execute]
        rejected = [event for event in event_rows if event.status in {"blocked", "rejected", "error"}]
        return {
            "total_signals": len(event_rows),
            "executed": len(executed),
            "rejected": len(rejected),
            "tickers": sorted({trade.ticker for trade in executed if trade.ticker}),
        }


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title=settings.app_name,
        description="AI-powered crypto trading signal processor with subscriptions",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
    )

    # Middleware
    setup_middleware(app)

    # Static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Include routers
    app.include_router(webhook_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(user_router)
    app.include_router(subscription_router)
    app.include_router(ai_config_router)
    app.include_router(backtest_router)
    app.include_router(websocket_router)
    app.include_router(strategies_router)
    app.include_router(chart_router)
    app.include_router(strategy_editor_router)
    app.include_router(social_router)
    app.include_router(i18n_router)

    # API v1 aliases
    for router in [
        webhook_router, auth_router, admin_router, user_router, subscription_router,
        ai_config_router, backtest_router, strategies_router, chart_router,
        strategy_editor_router, social_router, i18n_router,
    ]:
        _mount_v1_aliases(app, router)

    # Page routes
    _setup_page_routes(app)

    # Utility routes
    _setup_utility_routes(app)

    # Global error handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        from loguru import logger
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(f"Unhandled exception (rid={request_id}): {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )

    return app
