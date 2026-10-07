"""Domain errors. Each carries a stable machine-readable `code` that clients branch on;
the HTTP status only groups codes into broad classes."""
from typing import Any, Optional


class AppError(Exception):
    status_code = 500

    def __init__(self, code: str, message: str, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class InvalidRequest(AppError):
    """The request itself is malformed or out of range; retrying it unchanged will not help."""
    status_code = 400


class PaymentDeclined(AppError):
    status_code = 402


class NotFound(AppError):
    """The resource named in the URL path does not exist."""
    status_code = 404


class Conflict(AppError):
    """The request is valid but conflicts with current state (stock, cart or coupon status).
    The client may change something and try again."""
    status_code = 409


class Unprocessable(AppError):
    """The request is well-formed but references something unusable (unknown product or
    coupon, empty cart, reused idempotency key)."""
    status_code = 422


class ServiceBusy(AppError):
    """The database lock could not be acquired in time. Safe to retry."""
    status_code = 503
