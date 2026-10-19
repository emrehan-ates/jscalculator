"""
Custom exceptions for the APIC client. We raise these instead of generic
exceptions so the tool layer can produce informative error responses for
the LLM rather than opaque "Unexpected error" messages.
"""


class APICError(Exception):
    """Base class for all APIC-related errors."""

    def __init__(self, message: str, *, url: str = "", status: int = 0,
                 apic_errors: list | None = None):
        super().__init__(message)
        self.message = message
        self.url = url
        self.status = status
        self.apic_errors = apic_errors or []

    def to_dict(self) -> dict:
        """Structured representation for tool responses."""
        d = {"error": self.message}
        if self.url:
            d["url"] = self.url
        if self.status:
            d["http_status"] = self.status
        if self.apic_errors:
            d["apic_errors"] = self.apic_errors
        return d


class APICAuthError(APICError):
    """Login or session-related failure."""


class APICConnectionError(APICError):
    """Network-level failure: timeout, connection refused, DNS, SSL."""


class APICRequestError(APICError):
    """APIC returned a non-2xx response (4xx, 5xx)."""
