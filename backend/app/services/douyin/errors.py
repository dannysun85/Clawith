"""Typed errors for Douyin official OpenAPI integration."""


class DouyinError(Exception):
    """Base Douyin integration error."""

    def __init__(
        self,
        message: str,
        *,
        code: str | int | None = None,
        log_id: str | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = str(code) if code is not None else None
        self.log_id = log_id
        self.status_code = status_code

    def redacted_summary(self) -> dict:
        return {
            "message": self.message,
            "code": self.code,
            "log_id": self.log_id,
            "status_code": self.status_code,
        }


class DouyinNotConfiguredError(DouyinError):
    """Douyin app credentials are missing."""


class DouyinAuthError(DouyinError):
    """OAuth/token error; usually requires reauth."""


class DouyinPermissionError(DouyinError):
    """Missing Douyin scope or official permission."""


class DouyinRateLimitError(DouyinError):
    """Official API rate limit."""


class DouyinOfficialError(DouyinError):
    """Other official API error."""


_INDETERMINATE_TRANSPORT_CODES = {
    "timeout",
    "network_error",
    "invalid_json",
}


def is_douyin_submission_indeterminate(exc: Exception) -> bool:
    """Return whether an external write may have reached Douyin.

    Authentication, permission, rate-limit, and normal official business
    errors are deterministic. A timeout, transport error, malformed response,
    or 5xx after request submission cannot prove that Douyin did not apply the
    write, so callers must require verification instead of retrying it.
    """

    if not isinstance(exc, DouyinOfficialError):
        return False
    if exc.code in _INDETERMINATE_TRANSPORT_CODES:
        return True
    return exc.status_code is not None and exc.status_code >= 500
