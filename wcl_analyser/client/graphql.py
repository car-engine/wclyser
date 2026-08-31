"""GraphQL transport for the Warcraft Logs v2 client endpoint.

Handles token attachment, retry with exponential backoff on 429 and 5xx, one
transparent re-authentication on 401, and tracking of the hourly point budget.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from ..config import API_URL, Config
from ..errors import GraphQLError, RateLimitExhausted, TransportError
from .auth import TokenStore

log = logging.getLogger(__name__)

RATE_LIMIT_QUERY = """
query {
  rateLimitData {
    limitPerHour
    pointsSpentThisHour
    pointsResetIn
  }
}
"""

# How many requests may pass before the point budget is read again.
_REFRESH_EVERY = 25


@dataclass
class RateLimit:
    limit_per_hour: float
    points_spent: float
    resets_in_s: int
    observed_at: float
    # Requests issued since the figures above were read; each costs at least
    # one point, so this is a lower bound on what has been spent since.
    estimated_since: float = 0.0

    @property
    def remaining(self) -> float:
        return self.limit_per_hour - self.points_spent - self.estimated_since

    def __str__(self) -> str:
        return (
            f"{self.remaining:.0f}/{self.limit_per_hour:.0f} points remaining, "
            f"resets in {self.resets_in_s // 60}m{self.resets_in_s % 60:02d}s"
        )


class GraphQLClient:
    """A thin, retrying GraphQL client. One instance per process is enough."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._http = config.http
        self._tokens = TokenStore(
            config.client_id,
            config.client_secret,
            Path(config.token_path),
            timeout_s=config.http.timeout_s,
        )
        self._session = requests.Session()
        self._rate: RateLimit | None = None
        self._since_refresh = 0
        self.requests_made = 0

    # -- rate limit ------------------------------------------------------

    def rate_limit(self, refresh: bool = False) -> RateLimit:
        """Return the current point budget, reading it from the API if needed."""
        if refresh or self._rate is None or self._since_refresh >= _REFRESH_EVERY:
            data = self._execute(RATE_LIMIT_QUERY, {})["rateLimitData"]
            self._rate = RateLimit(
                limit_per_hour=float(data["limitPerHour"]),
                points_spent=float(data["pointsSpentThisHour"]),
                resets_in_s=int(data["pointsResetIn"]),
                observed_at=time.time(),
            )
            self._since_refresh = 0
        else:
            self._rate.estimated_since = float(self._since_refresh)
        return self._rate

    def ensure_budget(self, floor: int | None = None) -> RateLimit:
        """Raise if fewer than ``floor`` points remain in the current hour."""
        floor = self._config.fetch.point_floor if floor is None else floor
        rate = self.rate_limit()
        if rate.remaining < floor:
            raise RateLimitExhausted(
                f"only {rate.remaining:.0f} points left (floor {floor}); "
                f"budget resets in {rate.resets_in_s}s"
            )
        return rate

    # -- execution -------------------------------------------------------

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        data = self._execute(query, variables or {})
        self._since_refresh += 1
        return data

    def _execute(self, query: str, variables: dict[str, Any]) -> dict:
        payload = {"query": query, "variables": variables}
        last_error: Exception | None = None
        reauthed = False

        for attempt in range(self._http.max_attempts):
            token = self._tokens.get()
            try:
                resp = self._session.post(
                    API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self._http.timeout_s,
                )
            except requests.RequestException as exc:
                last_error = exc
                self._sleep(attempt, reason=f"transport error: {exc}")
                continue

            self.requests_made += 1

            if resp.status_code == 401 and not reauthed:
                log.debug("401 from API, re-authenticating once")
                self._tokens.invalidate()
                reauthed = True
                continue

            if resp.status_code == 429:
                self._sleep(attempt, reason="rate limited", retry_after=resp.headers.get("Retry-After"))
                last_error = TransportError("429 Too Many Requests")
                continue

            if resp.status_code >= 500:
                last_error = TransportError(f"{resp.status_code} from API")
                self._sleep(attempt, reason=f"server error {resp.status_code}")
                continue

            if resp.status_code != 200:
                raise TransportError(
                    f"{resp.status_code} from API: {resp.text[:300]}"
                )

            try:
                body = resp.json()
            except ValueError as exc:
                raise TransportError(
                    f"API returned a non-JSON body: {resp.text[:300]}"
                ) from exc

            if body.get("errors"):
                raise GraphQLError(body["errors"])
            if "data" not in body:
                raise TransportError(f"API response carried no data: {body}")
            return body["data"]

        raise TransportError(
            f"gave up after {self._http.max_attempts} attempts: {last_error}"
        )

    def _sleep(self, attempt: int, reason: str, retry_after: str | None = None) -> None:
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = min(
                self._http.backoff_cap_s,
                self._http.backoff_base_s * (2**attempt),
            )
            delay *= 0.5 + random.random() / 2.0
        log.warning("%s; retrying in %.1fs (attempt %d)", reason, delay, attempt + 1)
        time.sleep(delay)
