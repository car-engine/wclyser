"""The fetch layer.

Every call goes through :meth:`Fetcher.fetch_stream`, which is cache-first: a
blob already on disk is returned without touching the network. Blobs are only
written for fights that have finished, because an in-progress pull would cache
a partial event list that later reads would trust.

The live watcher is a loop over :meth:`Fetcher.report_meta` and
:meth:`Fetcher.fetch_fight` with no extra fetching logic of its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from ..client import GraphQLClient
from ..config import Config
from ..errors import WCLError
from .cache import Blob, BlobCache
from .meta import (
    Actor,
    Fight,
    ReportMeta,
    iter_player_details,
    meta_path,
    parse_report,
)
from .queries import (
    EVENTS,
    GRAPH,
    PLAYER_DETAILS,
    REPORT_META,
    TABLE,
    document_hash,
)
from .scopes import ENEMY_SUBJECT, RAID_SUBJECT, FilterSide, Scope, Stream

log = logging.getLogger(__name__)

# A pull longer than this many pages is a bug, not a raid.
MAX_PAGES = 500


@dataclass
class FetchResult:
    stream: str
    subject: str
    fight_id: int
    blob: Blob
    cached: bool
    pages: int = 0

    @property
    def event_count(self) -> int:
        return len(self.blob.events)


@dataclass
class FetchSummary:
    code: str
    results: list[FetchResult] = field(default_factory=list)
    skipped_fights: list[tuple[int, str]] = field(default_factory=list)

    @property
    def cached(self) -> int:
        return sum(1 for r in self.results if r.cached)

    @property
    def fetched(self) -> int:
        return sum(1 for r in self.results if not r.cached)

    @property
    def events(self) -> int:
        return sum(r.event_count for r in self.results)


class Fetcher:
    def __init__(self, client: GraphQLClient, config: Config) -> None:
        self.client = client
        self.config = config
        self.cache = BlobCache(config.raw_dir)

    # -- report metadata -------------------------------------------------

    def report_meta(self, code: str, offline: bool = False) -> ReportMeta:
        """Fetch the fights list and master data.

        Never served from cache while online: the fights list is the only part
        of a report that changes, and the watcher depends on seeing new pulls.
        """
        path = meta_path(self.config.raw_dir, code)
        if offline:
            blob = self.cache.read(path)
            if blob is None:
                raise WCLError(f"no offline metadata for report {code} at {path}")
            return parse_report(blob.payload["data"], blob.payload["fetched_at"])

        kill_type = "Encounters" if self.config.fetch.encounters_only else "All"
        data = self.client.execute(
            REPORT_META, {"code": code, "killType": kill_type}
        )
        payload = data["reportData"]["report"]
        if payload is None:
            raise WCLError(f"report {code} not found, or not visible to this client")

        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.cache.write(
            path,
            {
                "report_code": code,
                "kind": "meta",
                "kill_type": kill_type,
                "query_hash": document_hash(REPORT_META),
                "data": payload,
            },
        )
        return parse_report(payload, fetched_at)

    # -- finality --------------------------------------------------------

    def is_final(self, meta: ReportMeta, fight: Fight) -> bool:
        """Whether a fight has certainly finished, so its events are immutable.

        Any fight but the newest is finished. The newest one counts as finished
        once the report has run on past its end by the configured margin.
        """
        if fight.id != meta.last_fight_id:
            return True
        report_span_ms = meta.end_time_utc - meta.start_time_utc
        margin_ms = self.config.fetch.finality_margin_s * 1000
        return (report_span_ms - fight.end_ms) > margin_ms

    # -- per-fight specialisation ----------------------------------------

    def fight_player_details(
        self, meta: ReportMeta, fight: Fight, force: bool = False
    ) -> dict[int, dict]:
        """Player details for one pull, keyed by actor id.

        Specialisation is a property of the pull: a player who respecs between
        bosses has one specialisation for some fights and another for the rest.
        This is the cheap source of that, needing one request per fight and no
        table of specialisation ids that new specialisations would fall out of.
        """
        path = self.cache.path(
            meta.code, fight.id, "player_details", RAID_SUBJECT, kind="meta"
        )
        blob = None if force else self.cache.read(path)
        if blob is None:
            self.client.ensure_budget()
            data = self.client.execute(
                PLAYER_DETAILS, {"code": meta.code, "fightID": fight.id}
            )
            payload = {
                "report_code": meta.code,
                "fight_id": fight.id,
                "kind": "meta",
                "stream": "player_details",
                "query_hash": document_hash(PLAYER_DETAILS),
                "data": data["reportData"]["report"]["playerDetails"],
            }
            if self.is_final(meta, fight):
                blob = self.cache.write(path, payload)
            else:
                blob = Blob(path=path, payload={**payload, "content_hash": ""},
                            from_cache=False)
        return {
            entry["id"]: entry
            for entry in iter_player_details(blob.payload.get("data"))
            if entry.get("id") is not None
        }

    def spec_in_fight(
        self, meta: ReportMeta, fight: Fight, actor: Actor
    ) -> str | None:
        """The specialisation a player was in for one pull."""
        entry = self.fight_player_details(meta, fight).get(actor.id)
        if not entry:
            return None
        specs = entry.get("specs") or []
        if not specs:
            return None
        return max(specs, key=lambda s: s.get("count", 0)).get("spec")

    def filter_fights_by_spec(
        self,
        meta: ReportMeta,
        fights: list[Fight],
        subjects: list[Actor],
        spec: str,
    ) -> list[Fight]:
        """Keep the pulls where at least one subject played ``spec``."""
        wanted = spec.strip().lower()
        kept: list[Fight] = []
        for fight in fights:
            present = meta.subjects_in_fight(fight, subjects)
            if any(
                (self.spec_in_fight(meta, fight, actor) or "").lower() == wanted
                for actor in present
            ):
                kept.append(fight)
            else:
                log.debug("fight %d has no subject playing %s", fight.id, spec)
        return kept

    # -- one stream ------------------------------------------------------

    def fetch_stream(
        self,
        meta: ReportMeta,
        fight: Fight,
        stream: Stream,
        subject: Actor | None = None,
        force: bool = False,
    ) -> FetchResult:
        subject_slug = _subject_slug(stream, subject)
        path = self.cache.path(meta.code, fight.id, stream.key, subject_slug)

        if not force:
            blob = self.cache.read(path)
            if blob is not None:
                if blob.payload.get("stream_fingerprint") == stream.fingerprint:
                    return FetchResult(
                        stream=stream.key,
                        subject=subject_slug,
                        fight_id=fight.id,
                        blob=blob,
                        cached=True,
                    )
                log.info(
                    "%s on fight %d was cached under an older stream definition; "
                    "refetching",
                    stream.key,
                    fight.id,
                )

        self.client.ensure_budget()
        events, pages = self._paginate(meta, fight, stream, subject)

        payload = {
            "report_code": meta.code,
            "fight_id": fight.id,
            "stream": stream.key,
            "data_type": stream.data_type,
            "hostility": stream.hostility,
            "filter_side": stream.filter_side.value,
            "scope": stream.scope.value,
            "stream_fingerprint": stream.fingerprint,
            "subject": subject_slug,
            "subject_actor_id": subject.id if subject else None,
            "fight_start_ms": fight.start_ms,
            "fight_end_ms": fight.end_ms,
            "query_hash": document_hash(EVENTS),
            "page_count": pages,
            "events": events,
        }

        final = self.is_final(meta, fight)
        if final:
            blob = self.cache.write(path, payload)
        else:
            log.info(
                "fight %d is still in progress; returning %d events uncached",
                fight.id,
                len(events),
            )
            blob = Blob(path=path, payload={**payload, "content_hash": ""}, from_cache=False)

        return FetchResult(
            stream=stream.key,
            subject=subject_slug,
            fight_id=fight.id,
            blob=blob,
            cached=False,
            pages=pages,
        )

    def _paginate(
        self,
        meta: ReportMeta,
        fight: Fight,
        stream: Stream,
        subject: Actor | None,
    ) -> tuple[list[dict], int]:
        source_id = None
        target_id = None
        if subject is not None:
            if stream.filter_side is FilterSide.SOURCE:
                source_id = subject.id
            elif stream.filter_side is FilterSide.TARGET:
                target_id = subject.id

        events: list[dict] = []
        cursor: float | None = float(fight.start_ms)
        pages = 0

        while cursor is not None:
            if pages >= MAX_PAGES:
                raise WCLError(
                    f"{stream.key} on fight {fight.id} exceeded {MAX_PAGES} pages"
                )
            data = self.client.execute(
                EVENTS,
                {
                    "code": meta.code,
                    "start": cursor,
                    "end": float(fight.end_ms),
                    "fightID": fight.id,
                    "dataType": stream.data_type,
                    "hostility": stream.hostility,
                    "sourceID": source_id,
                    "targetID": target_id,
                    "limit": self.config.fetch.event_limit,
                    "includeResources": self.config.fetch.include_resources,
                },
            )
            page = data["reportData"]["report"]["events"]
            events.extend(page.get("data") or [])
            cursor = page.get("nextPageTimestamp")
            pages += 1

        return events, pages

    # -- one fight, one report -------------------------------------------

    def fetch_fight(
        self,
        meta: ReportMeta,
        fight: Fight,
        subjects: list[Actor],
        streams: Iterable[Stream],
        force: bool = False,
    ) -> list[FetchResult]:
        present = meta.subjects_in_fight(fight, subjects)
        results: list[FetchResult] = []
        for stream in streams:
            if stream.scope is Scope.SUBJECT:
                for actor in present:
                    results.append(
                        self.fetch_stream(meta, fight, stream, actor, force=force)
                    )
            else:
                results.append(self.fetch_stream(meta, fight, stream, None, force=force))
        return results

    def fetch_report(
        self,
        meta: ReportMeta,
        subjects: list[Actor],
        streams: Iterable[Stream],
        fights: list[Fight] | None = None,
        force: bool = False,
        finished_only: bool = False,
    ) -> FetchSummary:
        streams = list(streams)
        summary = FetchSummary(code=meta.code)
        for fight in meta.fights if fights is None else fights:
            if finished_only and not self.is_final(meta, fight):
                summary.skipped_fights.append((fight.id, "still in progress"))
                continue
            if not meta.subjects_in_fight(fight, subjects) and any(
                s.scope is Scope.SUBJECT for s in streams
            ):
                log.debug("no subject present in fight %d", fight.id)
            summary.results.extend(
                self.fetch_fight(meta, fight, subjects, streams, force=force)
            )
        return summary

    # -- pre-aggregated queries ------------------------------------------

    def fetch_table(
        self,
        meta: ReportMeta,
        fight: Fight,
        data_type: str,
        hostility: str = "Friendlies",
        subject: Actor | None = None,
        force: bool = False,
    ) -> FetchResult:
        return self._fetch_aggregate(
            meta, fight, data_type, hostility, subject, TABLE, "table", force
        )

    def fetch_graph(
        self,
        meta: ReportMeta,
        fight: Fight,
        data_type: str,
        hostility: str = "Friendlies",
        subject: Actor | None = None,
        force: bool = False,
    ) -> FetchResult:
        return self._fetch_aggregate(
            meta, fight, data_type, hostility, subject, GRAPH, "graph", force
        )

    def _fetch_aggregate(
        self,
        meta: ReportMeta,
        fight: Fight,
        data_type: str,
        hostility: str,
        subject: Actor | None,
        document: str,
        kind: str,
        force: bool,
    ) -> FetchResult:
        subject_slug = subject.player_key if subject else RAID_SUBJECT
        stream_name = f"{data_type}-{hostility}"
        path = self.cache.path(meta.code, fight.id, stream_name, subject_slug, kind=kind)

        if not force:
            blob = self.cache.read(path)
            if blob is not None:
                return FetchResult(stream_name, subject_slug, fight.id, blob, cached=True)

        self.client.ensure_budget()
        data = self.client.execute(
            document,
            {
                "code": meta.code,
                "start": float(fight.start_ms),
                "end": float(fight.end_ms),
                "fightID": fight.id,
                "dataType": data_type,
                "hostility": hostility,
                "sourceID": subject.id if subject else None,
                "targetID": None,
            },
        )
        body = data["reportData"]["report"][kind]

        payload = {
            "report_code": meta.code,
            "fight_id": fight.id,
            "kind": kind,
            "data_type": data_type,
            "hostility": hostility,
            "subject": subject_slug,
            "subject_actor_id": subject.id if subject else None,
            "query_hash": document_hash(document),
            "data": body,
        }

        if self.is_final(meta, fight):
            blob = self.cache.write(path, payload)
        else:
            blob = Blob(path=path, payload={**payload, "content_hash": ""}, from_cache=False)
        return FetchResult(stream_name, subject_slug, fight.id, blob, cached=False)


def _subject_slug(stream: Stream, subject: Actor | None) -> str:
    if stream.scope is Scope.SUBJECT:
        if subject is None:
            raise ValueError(f"stream {stream.key} needs a subject")
        return subject.player_key
    if stream.scope is Scope.ENEMY:
        return ENEMY_SUBJECT
    return RAID_SUBJECT
