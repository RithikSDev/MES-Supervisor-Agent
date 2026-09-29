from typing import Any


class DomainError(Exception):
    """Business rule violation. Mapped to a JSON error response by the web layer."""

    status_code = 409
    default_code = "conflict"

    def __init__(self, message: str, *, code: str | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.details = details


class NotFound(DomainError):
    status_code = 404
    default_code = "not_found"


class ValidationFailed(DomainError):
    status_code = 422
    default_code = "validation_failed"


class Forbidden(DomainError):
    status_code = 403
    default_code = "forbidden"


class InvalidTransition(DomainError):
    default_code = "invalid_transition"


class StaleVersion(DomainError):
    default_code = "stale_version"


class Unauthorized(DomainError):
    status_code = 401
    default_code = "unauthorized"


class TooManyAttempts(DomainError):
    status_code = 429
    default_code = "too_many_attempts"
