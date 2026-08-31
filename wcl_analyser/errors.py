"""Exception types shared across the package."""


class WCLError(Exception):
    """Base class for every error this package raises."""


class ConfigError(WCLError):
    """Configuration is missing or malformed."""


class AuthError(WCLError):
    """The OAuth token endpoint rejected the credentials."""


class TransportError(WCLError):
    """An HTTP-level failure that survived the retry policy."""


class GraphQLError(WCLError):
    """The API returned a 200 response carrying an ``errors`` array."""

    def __init__(self, errors: list[dict]) -> None:
        self.errors = errors
        messages = "; ".join(e.get("message", repr(e)) for e in errors)
        super().__init__(messages)


class RateLimitExhausted(WCLError):
    """The hourly point budget is spent, or too low to continue safely."""


class FightNotFinal(WCLError):
    """Events were requested for a fight that may still be in progress."""
