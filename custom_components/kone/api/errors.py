"""Errors raised by the KONE API client."""

from __future__ import annotations


class KoneError(Exception):
    """Base class for everything this client raises."""


class KoneAuthError(KoneError):
    """The client id/secret were rejected, or a scope was refused."""


class KoneConnectionError(KoneError):
    """KONE could not be reached, or the WebSocket dropped."""


class KoneRequestError(KoneError):
    """KONE answered a request with an error.

    `status` is the HTTP status, or the HTTP-analogous `statusCode` KONE puts
    in WebSocket error responses.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class KoneTimeout(KoneError):
    """A WebSocket request got no answer in time."""


class KoneControlDisabled(KoneError):
    """A lift call or cancel was attempted on a read-only client."""
