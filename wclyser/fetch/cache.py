"""On-disk cache of raw API responses.

Layout::

    <data_dir>/raw/<report_code>/<fight_id:04d>/<kind>__<stream>__<subject>.json.gz

One file per ``(report, fight, stream, subject)``. That tuple is the unit of
fetching, of caching and of import replacement, which is what makes re-running
any of the three idempotent.

Blobs are written atomically and never rewritten once present, because a
completed fight is immutable. Only fights that pass the finality check in
:mod:`wclyser.fetch.events` are written at all.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BLOB_SCHEMA = 1

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(value: str) -> str:
    """Make a subject or stream name safe to use as a filename component."""
    cleaned = _UNSAFE.sub("-", value).strip("-")
    return cleaned or "unnamed"


@dataclass
class Blob:
    """A cached response together with the metadata needed to import it."""

    path: Path
    payload: dict[str, Any]
    from_cache: bool

    @property
    def events(self) -> list[dict]:
        return self.payload.get("events", [])

    @property
    def data(self) -> Any:
        """The payload for table and graph blobs, which are not event lists."""
        return self.payload.get("data")

    @property
    def content_hash(self) -> str:
        return self.payload["content_hash"]

    def __len__(self) -> int:
        return len(self.events)


class BlobCache:
    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = Path(raw_dir)

    def path(
        self,
        report_code: str,
        fight_id: int,
        stream: str,
        subject: str,
        kind: str = "events",
    ) -> Path:
        name = f"{slug(kind)}__{slug(stream)}__{slug(subject)}.json.gz"
        return self.raw_dir / slug(report_code) / f"{fight_id:04d}" / name

    def exists(self, *args: Any, **kwargs: Any) -> bool:
        return self.path(*args, **kwargs).is_file()

    def read(self, path: Path) -> Blob | None:
        if not path.is_file():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            # A truncated blob is treated as absent; the caller refetches and
            # overwrites it.
            return None
        if payload.get("blob_schema") != BLOB_SCHEMA:
            return None
        return Blob(path=path, payload=payload, from_cache=True)

    def write(self, path: Path, payload: dict[str, Any]) -> Blob:
        payload = dict(payload)
        payload["blob_schema"] = BLOB_SCHEMA
        payload["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = payload.get("events", payload.get("data"))
        payload["content_hash"] = content_hash(body)

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        return Blob(path=path, payload=payload, from_cache=False)


def content_hash(body: Any) -> str:
    """Hash of the response body, used to skip re-importing unchanged blobs."""
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
