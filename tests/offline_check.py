"""Offline checks for the fetch layer, run without credentials or network.

    python tests/offline_check.py

A fake client stands in for the API, so these checks cover the parts that are
easy to get wrong and expensive to debug against a live report: pagination,
the cache-first rule, the finality rule, and subject resolution.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wcl_analyser.config import Config, FetchSettings, SubjectRules  # noqa: E402
from wcl_analyser.errors import ConfigError  # noqa: E402
from wcl_analyser.fetch import Fetcher, resolve_streams  # noqa: E402
from wcl_analyser.fetch.meta import parse_report  # noqa: E402
from wcl_analyser.fetch.scopes import BY_KEY  # noqa: E402

REPORT_PAYLOAD = {
    "code": "TEST123",
    "title": "Test night",
    "startTime": 1_700_000_000_000,
    "endTime": 1_700_000_600_000,  # report spans 600_000 ms
    "zone": {"id": 42, "name": "Test Raid"},
    "owner": {"name": "Owner"},
    "guild": {"name": "Test Guild"},
    "region": {"slug": "US"},
    "fights": [
        {
            "id": 1,
            "encounterID": 3001,
            "name": "Test Boss",
            "difficulty": 5,
            "size": 20,
            "kill": False,
            "startTime": 1000,
            "endTime": 100_000,
            "fightPercentage": 42.0,
            "bossPercentage": 42.0,
            "lastPhase": 1,
            "friendlyPlayers": [5, 6],
        },
        {
            "id": 2,
            "encounterID": 3001,
            "name": "Test Boss",
            "difficulty": 5,
            "size": 20,
            "kill": True,
            "startTime": 200_000,
            "endTime": 300_000,
            "fightPercentage": 0.0,
            "bossPercentage": 0.0,
            "lastPhase": 2,
            "friendlyPlayers": [5],
        },
        {
            # Newest fight, ending 20s before the report does: inside the
            # 30s finality margin, so it may still be running.
            "id": 3,
            "encounterID": 3002,
            "name": "Other Boss",
            "difficulty": 5,
            "size": 20,
            "kill": False,
            "startTime": 400_000,
            "endTime": 580_000,
            "fightPercentage": 88.0,
            "bossPercentage": 88.0,
            "lastPhase": 1,
            "friendlyPlayers": [5, 6],
        },
    ],
    "masterData": {
        "abilities": [{"gameID": 264178, "name": "Demonbolt", "icon": "x.jpg", "type": 32}],
        "actors": [
            {
                "id": 5,
                "gameID": 1,
                "name": "Alpha",
                "type": "Player",
                "subType": "Warlock",
                "icon": "Warlock-Demonology",
                "server": "Frostmourne",
                "petOwner": None,
            },
            {
                "id": 6,
                "gameID": 2,
                "name": "Beta",
                "type": "Player",
                "subType": "Warlock",
                "icon": "Warlock-Destruction",
                "server": "Frostmourne",
                "petOwner": None,
            },
            {
                "id": 31,
                "gameID": 3,
                "name": "Test Boss",
                "type": "NPC",
                "subType": "Boss",
                "icon": "Boss",
                "server": None,
                "petOwner": None,
            },
        ],
    },
}


class FakeClient:
    """Returns two pages of events, and counts how often it is called."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests_made = 0
        self._page_two_cursors: set[float] = set()

    def ensure_budget(self, floor: int | None = None) -> None:
        return None

    def execute(self, query: str, variables: dict) -> dict:
        self.calls += 1
        self.requests_made += 1
        if "rateLimitData" in query:
            return {
                "rateLimitData": {
                    "limitPerHour": 3600,
                    "pointsSpentThisHour": 10,
                    "pointsResetIn": 1800,
                }
            }
        if "masterData" in query:
            return {"reportData": {"report": REPORT_PAYLOAD}}
        start = float(variables["start"])
        if start not in self._page_two_cursors:
            # First page of this fight: hand back a cursor for the second.
            cursor = start + 50_000
            self._page_two_cursors.add(cursor)
            return {
                "reportData": {
                    "report": {
                        "events": {
                            "data": [
                                {"timestamp": start + 500, "type": "cast", "sourceID": 5},
                                {"timestamp": start + 1500, "type": "cast", "sourceID": 5},
                            ],
                            "nextPageTimestamp": cursor,
                        }
                    }
                }
            }
        return {
            "reportData": {
                "report": {
                    "events": {
                        "data": [
                            {"timestamp": start + 500, "type": "cast", "sourceID": 5}
                        ],
                        "nextPageTimestamp": None,
                    }
                }
            }
        }


def make_config(data_dir: Path, **subject_kwargs) -> Config:
    return Config(
        client_id="id",
        client_secret="secret",
        data_dir=data_dir,
        subjects=SubjectRules(**subject_kwargs),
        fetch=FetchSettings(),
    )


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        raise SystemExit(1)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wcla-check-"))
    try:
        meta = parse_report(REPORT_PAYLOAD, "2026-08-31T00:00:00+00:00")

        print("report parsing")
        check("three fights parsed", len(meta.fights) == 3)
        check("two players found", len(meta.players()) == 2)
        alpha = meta.actors[5]
        check("player key built", alpha.player_key == "Alpha-Frostmourne-US",
              alpha.player_key)
        check("spec read from icon", alpha.spec == "Demonology", str(alpha.spec))
        check("pull numbering per encounter",
              [f.pull_number for f in meta.fights] == [1, 2, 1],
              str([f.pull_number for f in meta.fights]))
        check("difficulty named", meta.fights[0].difficulty_name == "Mythic")

        print("subject resolution")
        config = make_config(tmp, auto_select_class="Warlock", auto_select_spec="Demonology")
        check("auto-select picks the matching spec only",
              [a.name for a in meta.resolve_subjects(config)] == ["Alpha"])
        check("explicit name overrides auto-select",
              [a.name for a in meta.resolve_subjects(config, ["Beta"])] == ["Beta"])
        check("bare name resolves",
              meta.resolve_subjects(config, ["alpha"])[0].id == 5)
        by_report = make_config(tmp, by_report={"TEST123": ["Beta-Frostmourne-US"]})
        check("per-report override applies",
              [a.name for a in meta.resolve_subjects(by_report)] == ["Beta"])
        try:
            meta.resolve_subjects(config, ["Nobody"])
            check("absent player raises", False)
        except ConfigError:
            check("absent player raises", True)
        check("fight presence filters subjects",
              [a.name for a in meta.subjects_in_fight(meta.fights[1], meta.players())]
              == ["Alpha"])

        print("finality")
        client = FakeClient()
        fetcher = Fetcher(client, config)
        check("older fight is final", fetcher.is_final(meta, meta.fights[0]))
        check("newest fight inside the margin is not final",
              not fetcher.is_final(meta, meta.fights[2]))

        print("fetching")
        casts = BY_KEY["casts"]
        result = fetcher.fetch_stream(meta, meta.fights[0], casts, alpha)
        check("pagination concatenated both pages", result.event_count == 3,
              str(result.event_count))
        check("two pages recorded", result.pages == 2, str(result.pages))
        check("blob written to disk", result.blob.path.is_file())
        check("first fetch is not from cache", not result.cached)

        calls_after_first = client.calls
        again = fetcher.fetch_stream(meta, meta.fights[0], casts, alpha)
        check("second fetch served from cache", again.cached)
        check("cache hit made no request", client.calls == calls_after_first)
        check("cached content matches", again.event_count == 3)
        check("content hash is stable",
              again.blob.content_hash == result.blob.content_hash)

        forced = fetcher.fetch_stream(meta, meta.fights[0], casts, alpha, force=True)
        check("force bypasses the cache", not forced.cached and client.calls > calls_after_first)

        live = fetcher.fetch_stream(meta, meta.fights[2], casts, alpha)
        check("in-progress fight is not cached to disk", not live.blob.path.is_file())
        check("in-progress fight still returns events", live.event_count == 3)

        print("fetch plan")
        streams = resolve_streams(None)
        check("plan has no duplicate keys",
              len({s.key for s in streams}) == len(streams))
        shapes = [
            (s.data_type, s.scope.value, s.hostility, s.filter_side.value)
            for s in streams
        ]
        check("no two streams select the same rows",
              len(set(shapes)) == len(shapes))
        summary = fetcher.fetch_report(meta, [alpha], [casts], finished_only=True)
        check("finished_only skips the live fight",
              summary.skipped_fights == [(3, "still in progress")],
              str(summary.skipped_fights))

        print("\nall offline checks passed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
