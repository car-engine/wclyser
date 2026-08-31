"""OAuth client-credentials flow with an on-disk token cache.

The cached token is bound to a hash of the client id, so rotating credentials
invalidates the cache rather than sending a stale token.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from ..config import TOKEN_URL
from ..errors import AuthError

log = logging.getLogger(__name__)

# Refresh this many seconds before the token actually expires.
EXPIRY_MARGIN_S = 120.0


@dataclass
class Token:
    access_token: str
    expires_at: float

    def valid(self, margin_s: float = EXPIRY_MARGIN_S) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - margin_s


class TokenStore:
    """Fetches access tokens, caching the current one at ``path``."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        path: Path,
        timeout_s: float = 30.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._path = path
        self._timeout_s = timeout_s
        self._token: Token | None = None

    @property
    def _fingerprint(self) -> str:
        return hashlib.sha256(self._client_id.encode("utf-8")).hexdigest()[:16]

    def get(self) -> str:
        if self._token is None:
            self._token = self._read_cache()
        if self._token is None or not self._token.valid():
            self._token = self._request()
            self._write_cache(self._token)
        return self._token.access_token

    def invalidate(self) -> None:
        """Drop the cached token so the next :meth:`get` re-authenticates."""
        self._token = None
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass

    def _read_cache(self) -> Token | None:
        try:
            blob = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        if blob.get("fingerprint") != self._fingerprint:
            log.debug("cached token belongs to different credentials, discarding")
            return None
        return Token(
            access_token=blob.get("access_token", ""),
            expires_at=float(blob.get("expires_at", 0)),
        )

    def _write_cache(self, token: Token) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "fingerprint": self._fingerprint,
                    "access_token": token.access_token,
                    "expires_at": token.expires_at,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            # Windows ACLs do not map onto POSIX modes; the file still lands
            # under the user's own data directory.
            pass

    def _request(self) -> Token:
        log.debug("requesting a new access token")
        try:
            resp = requests.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
                timeout=self._timeout_s,
            )
        except requests.RequestException as exc:
            raise AuthError(f"token request failed: {exc}") from exc
        if resp.status_code != 200:
            raise AuthError(
                f"token endpoint returned {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise AuthError(f"token response contained no access_token: {payload}")
        expires_in = float(payload.get("expires_in", 3600))
        return Token(access_token=access_token, expires_at=time.time() + expires_in)
