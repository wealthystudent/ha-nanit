"""Exceptions for the aionanit library."""

from __future__ import annotations


class NanitError(Exception):
    """Base exception for all aionanit errors."""


class NanitAuthError(NanitError):
    """Authentication failed (invalid credentials, expired token, MFA failure)."""


class NanitMfaRequiredError(NanitAuthError):
    """MFA code required to complete login."""

    def __init__(self, mfa_token: str) -> None:

        super().__init__("MFA verification required")
        self.mfa_token = mfa_token


class NanitConnectionError(NanitError):
    """Network-level connection failure (DNS, TCP, TLS)."""


class NanitTransportError(NanitError):
    """WebSocket transport error (unexpected close, protocol violation)."""


class NanitRequestTimeout(NanitError):
    """Protobuf request did not receive a response within the timeout."""

    def __init__(self, request_type: str, request_id: int, timeout: float) -> None:

        super().__init__(f"Request {request_type} (id={request_id}) timed out after {timeout}s")
        self.request_type = request_type
        self.request_id = request_id
        self.timeout = timeout


class NanitProtocolError(NanitError):
    """Protobuf decode failure or unexpected message structure."""


class NanitCameraUnavailable(NanitError):
    """Camera is not reachable via any transport."""


class NanitWebRtcRefused(NanitError):
    """The camera answered a WebRTC offer with a non-200 status."""

    def __init__(self, status_code: int | None, status_message: str = "") -> None:

        super().__init__(
            f"Camera refused the WebRTC offer: {status_code} {status_message}".rstrip()
        )
        self.status_code = status_code
        self.status_message = status_message


class NanitWebRtcClosed(NanitError):
    """A WebRTC session ended before the camera answered its offer."""

    def __init__(self, error: int, error_message: str = "") -> None:

        super().__init__(f"WebRTC session closed: {error} {error_message}".rstrip())
        self.error = error
        self.error_message = error_message
