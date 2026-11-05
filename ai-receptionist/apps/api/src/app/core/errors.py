"""Application error hierarchy and FastAPI exception handlers.

Errors carry a machine-readable `code` so the dashboard and integrations can
branch on failures without string-matching messages. Internal detail is never
leaked to callers in production.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "", **context: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.context = context


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class TenantNotFoundError(NotFoundError):
    code = "tenant_not_found"


class ValidationFailedError(AppError):
    status_code = 422
    code = "validation_failed"


class WebhookSignatureError(AppError):
    status_code = 401
    code = "invalid_webhook_signature"


class AuthenticationError(AppError):
    """No valid credentials at all — missing/expired/malformed token, or a
    login attempt with the wrong email/password. Deliberately the same
    message and code for "no such user" and "wrong password" (see
    tenants.routes.login) so a failed login can't be used to enumerate
    which emails exist on a tenant."""

    status_code = 401
    code = "authentication_required"


class RateLimitedError(AppError):
    """Too many attempts. Carries `Retry-After` so well-behaved clients (and
    the dashboard) can tell the user how long to wait."""

    status_code = 429
    code = "too_many_attempts"

    def __init__(self, message: str = "", *, retry_after_seconds: int = 60) -> None:
        super().__init__(message)
        self.headers = {"Retry-After": str(max(1, retry_after_seconds))}


class AuthorizationError(AppError):
    """Valid credentials, insufficient role — e.g. a viewer calling an
    admin-only route. Distinct from AuthenticationError (401): the caller
    is who they say they are, they just can't do this."""

    status_code = 403
    code = "forbidden"


class IntegrationError(AppError):
    """A downstream integration (calendar, CRM, email, SMS) failed.

    Treated as transient by the job queue: worth retrying with backoff.
    """

    status_code = 502
    code = "integration_error"


class PermanentIntegrationError(IntegrationError):
    """The integration rejected the request and will keep rejecting it —
    a malformed payload, a revoked grant, a deleted object.

    The queue dead-letters these immediately rather than retrying. Retrying a
    400 five times only delays the alert, and for a revoked grant it hammers
    a vendor that has already said no.
    """

    code = "permanent_integration_error"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "app_error", code=exc.code, path=request.url.path, message=exc.message, **exc.context
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=getattr(exc, "headers", None),
        )
