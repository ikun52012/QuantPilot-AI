"""
P2-FIX: API Version Management
Support for multiple API versions with smooth migration path.

Features:
    - Version routing (v1, v2, etc.)
    - Version deprecation warnings
    - Sunset dates for deprecated versions
    - Version-specific request/response schemas
    - Automatic version detection from headers
"""
from datetime import datetime

from fastapi import APIRouter, Request, Response
from loguru import logger


class APIVersionManager:
    """API version manager for multi-version support.

    P2-FIX: Enables smooth API versioning and migration.

    Example:
        manager = APIVersionManager(default_version="v1")

        # Register v1 router
        manager.register_version("v1", v1_router, prefix="/api/v1")

        # Register v2 router
        manager.register_version("v2", v2_router, prefix="/api/v2")

        # Deprecate v1
        manager.deprecate_version("v1", sunset_date="2025-08-01")
    """

    def __init__(
        self,
        default_version: str = "v1",
        latest_version: str = "v2",
        enable_version_header: bool = True,
    ):
        """Initialize API version manager.

        Args:
            default_version: Default version for unversioned requests
            latest_version: Latest stable version
            enable_version_header: Add API-Version header to responses
        """
        self.default_version = default_version
        self.latest_version = latest_version
        self.enable_version_header = enable_version_header

        self.versions: dict[str, dict[str, APIRouter]] = {}
        self.deprecated_versions: dict[str, dict[str, str]] = {}
        self.version_metadata: dict[str, dict[str, str]] = {}

        logger.info(
            f"[P2-FIX] APIVersionManager initialized: "
            f"default={default_version}, latest={latest_version}"
        )

    def register_version(
        self,
        version: str,
        router: APIRouter,
        prefix: str = "",
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Register API version router.

        Args:
            version: Version identifier (e.g., "v1", "v2")
            router: FastAPI router for this version
            prefix: URL prefix (e.g., "/api/v1")
            metadata: Version metadata (release_date, stability, etc.)
        """
        if version not in self.versions:
            self.versions[version] = {}

        self.versions[version][prefix] = router

        # Store metadata
        self.version_metadata[version] = metadata or {
            "release_date": datetime.utcnow().isoformat(),
            "stability": "stable",
        }

        logger.info(
            f"[P2-FIX] API version registered: {version} "
            f"(prefix={prefix}, router={router.prefix})"
        )

    def deprecate_version(
        self,
        version: str,
        sunset_date: str,
        migration_guide_url: str | None = None,
    ) -> None:
        """Mark API version as deprecated.

        Args:
            version: Version to deprecate
            sunset_date: Date when version will be removed
            migration_guide_url: URL to migration guide
        """
        self.deprecated_versions[version] = {
            "sunset_date": sunset_date,
            "migration_guide_url": migration_guide_url or "",
            "deprecated_at": datetime.utcnow().isoformat(),
        }

        # Update metadata
        if version in self.version_metadata:
            self.version_metadata[version]["stability"] = "deprecated"

        logger.warning(
            f"[P2-FIX] API version deprecated: {version} "
            f"(sunset={sunset_date})"
        )

    def get_router(self, version: str, prefix: str = "") -> APIRouter | None:
        """Get router for specific version.

        Args:
            version: Version identifier
            prefix: URL prefix

        Returns:
            APIRouter or None if not found
        """
        return self.versions.get(version, {}).get(prefix)

    def get_version_from_request(self, request: Request) -> str:
        """Detect API version from request.

        Detection order:
            1. X-API-Version header
            2. URL path prefix (/api/v1/...)
            3. Default version

        Args:
            request: FastAPI request

        Returns:
            Version identifier
        """
        # Check header
        version_header = request.headers.get("X-API-Version")
        if version_header and version_header in self.versions:
            return version_header

        # Check URL path
        path = request.url.path
        for version_key in self.versions.keys():
            if f"/api/{version_key}" in path:
                return version_key

        # Return default
        return self.default_version

    def add_version_headers(self, response: Response, version: str) -> Response:
        """Add version-related headers to response.

        Headers:
            - API-Version: Current version
            - API-Latest-Version: Latest stable version
            - Deprecation: If deprecated (true; sunset=...)
            - Link: Migration guide link (if deprecated)

        Args:
            response: FastAPI response
            version: Current version

        Returns:
            Response with added headers
        """
        if not self.enable_version_header:
            return response

        # Add version headers
        response.headers["API-Version"] = version
        response.headers["API-Latest-Version"] = self.latest_version

        # Add deprecation headers if deprecated
        if version in self.deprecated_versions:
            deprecation_info = self.deprecated_versions[version]
            sunset_date = deprecation_info.get("sunset_date", "")

            response.headers["Deprecation"] = f"true; sunset={sunset_date}"

            # Add warning header
            response.headers["Warning"] = (
                f"299 - \"Deprecated API version {version}. "
                f"Will be removed on {sunset_date}. "
                f"Use {self.latest_version} instead.\""
            )

            # Add migration guide link
            migration_url = deprecation_info.get("migration_guide_url")
            if migration_url:
                response.headers["Link"] = (
                    f'<{migration_url}>; rel="deprecation"; '
                    f'type="text/html"; title="Migration Guide"'
                )

        return response

    def get_all_versions(self) -> dict[str, dict[str, str]]:
        """Get all registered versions with metadata.

        Returns:
            Dict of version -> metadata
        """
        versions_info = {}

        for version in self.versions.keys():
            versions_info[version] = {
                "metadata": self.version_metadata.get(version, {}),
                "deprecated": version in self.deprecated_versions,
                "deprecation_info": self.deprecated_versions.get(version, {}),
            }

        return versions_info

    def get_version_info_response(self) -> dict[str, any]:
        """Get API version info for /api/versions endpoint.

        Returns:
            Version info dict
        """
        return {
            "default_version": self.default_version,
            "latest_version": self.latest_version,
            "available_versions": self.get_all_versions(),
            "how_to_specify_version": {
                "header": "X-API-Version: v2",
                "url_prefix": "/api/v2/...",
                "default": f"Uses {self.default_version} if not specified",
            },
        }

    def is_version_deprecated(self, version: str) -> bool:
        """Check if version is deprecated.

        Args:
            version: Version identifier

        Returns:
            True if deprecated
        """
        return version in self.deprecated_versions

    def is_version_sunset(self, version: str) -> bool:
        """Check if version sunset date has passed.

        Args:
            version: Version identifier

        Returns:
            True if sunset date passed
        """
        if version not in self.deprecated_versions:
            return False

        sunset_date_str = self.deprecated_versions[version].get("sunset_date")
        if not sunset_date_str:
            return False

        try:
            sunset_date = datetime.fromisoformat(sunset_date_str)
            return datetime.utcnow() > sunset_date
        except Exception:
            return False


def create_versioned_router(
    version: str,
    tags: list[str] | None = None,
) -> APIRouter:
    """Create router for specific API version.

    P2-FIX: Helper to create versioned routers with consistent settings.

    Args:
        version: Version identifier
        tags: OpenAPI tags

    Returns:
        Configured APIRouter
    """
    router = APIRouter(
        prefix=f"/api/{version}",
        tags=tags or [f"API {version}"],
    )

    logger.debug(f"[P2-FIX] Created versioned router: {version}")

    return router


def add_version_middleware(app):
    """Add middleware for version detection and headers.

    P2-FIX: Middleware to automatically handle API versioning.

    Args:
        app: FastAPI app instance
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    manager = APIVersionManager()

    class VersionMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Detect version
            version = manager.get_version_from_request(request)

            # Add to request state
            request.state.api_version = version

            # Call next
            response = await call_next(request)

            # Add version headers
            response = manager.add_version_headers(response, version)

            return response

    app.add_middleware(VersionMiddleware)

    logger.info("[P2-FIX] API version middleware added")


# Example v1 and v2 routers
v1_router = create_versioned_router("v1", tags=["Trading v1"])
v2_router = create_versioned_router("v2", tags=["Trading v2"])


# Example endpoints for different versions
@v1_router.post("/trade/execute")
async def execute_trade_v1(request: Request):
    """Execute trade (v1 API - deprecated)."""
    return {
        "version": "v1",
        "status": "success",
        "message": "Trade executed (legacy API)",
    }


@v2_router.post("/trade/execute")
async def execute_trade_v2(request: Request):
    """Execute trade (v2 API - current)."""
    return {
        "version": "v2",
        "status": "success",
        "message": "Trade executed (modern API)",
        "enhanced_features": ["multi_tp", "trailing_stop", "dynamic_leverage"],
    }


@v2_router.get("/versions")
async def get_api_versions():
    """Get API version information."""
    manager = APIVersionManager(default_version="v1", latest_version="v2")
    manager.register_version("v1", v1_router)
    manager.register_version("v2", v2_router)
    manager.deprecate_version("v1", sunset_date="2025-08-01", migration_guide_url="/docs/api-migration")

    return manager.get_version_info_response()
