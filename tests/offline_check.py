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
from wcl_analyser.db import Importer, open_database  # noqa: E402

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
                "id": 40,
                "gameID": 4,
                "name": "Wild Imp",
                "type": "Pet",
                "subType": "Warlock",
                "icon": "Warlock",
                "server": None,
                "petOwner": 5,
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

        print("database round trip")
        _check_database(tmp, config, meta, alpha, fetcher)

        print("\nall offline checks passed")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_database(tmp: Path, config, meta, alpha, fetcher) -> None:
    """Import a hand-built blob and check what lands in the tables."""
    casts = BY_KEY["casts"]
    fight = meta.fights[0]
    path = fetcher.cache.path(meta.code, fight.id, casts.key, alpha.player_key)
    fetcher.cache.write(
        path,
        {
            "report_code": meta.code,
            "fight_id": fight.id,
            "stream": casts.key,
            "data_type": casts.data_type,
            "subject": alpha.player_key,
            "stream_fingerprint": casts.fingerprint,
            "events": [
                # The player casts.
                {"timestamp": 2000, "type": "cast", "sourceID": 5, "targetID": 31,
                 "abilityGameID": 116858, "buffs": "1.2.3."},
                # A pet casts, and must resolve to its owner.
                {"timestamp": 3000, "type": "cast", "sourceID": 40, "targetID": 31,
                 "abilityGameID": 104318},
                # A soul shard spend while carrying mana: two different
                # resources that must not share columns.
                {"timestamp": 4000, "type": "resourcechange", "sourceID": 5,
                 "targetID": 5, "abilityGameID": 116858,
                 "resourceChange": 2, "resourceChangeType": 7,
                 "maxResourceAmount": 50,
                 "classResources": [{"amount": 242263, "max": 250000, "type": 0}]},
            ],
        },
    )

    conn = open_database(tmp / "check.sqlite")
    summary = Importer(conn, config, meta).run([fight], [alpha], [casts])
    check("three event rows imported", summary.rows == 3, str(summary.rows))

    row = conn.execute(
        "SELECT source_key, source_is_pet FROM event WHERE source_id = 40"
    ).fetchone()
    check("pet resolves to its owner", row["source_key"] == alpha.player_key,
          str(row["source_key"]))
    check("pet is flagged as one", row["source_is_pet"] == 1)

    row = conn.execute(
        "SELECT * FROM event WHERE event_type = 'resourcechange'"
    ).fetchone()
    check("resource change keeps its own type", row["resource_type"] == 7)
    check("resource change keeps its own maximum", row["resource_max"] == 50)
    check("the actor's pool is a separate resource", row["pool_type"] == 0)
    check("the actor's pool keeps its own amount", row["pool_amount"] == 242263)

    row = conn.execute(
        "SELECT t_rel_ms, buffs_active FROM event "
        "WHERE ability_id = 116858 AND event_type = 'cast'"
    ).fetchone()
    check("t_rel_ms is relative to the pull",
          row["t_rel_ms"] == 2000 - fight.start_ms, str(row["t_rel_ms"]))
    check("active buffs are carried", row["buffs_active"] == "1.2.3.")

    again = Importer(conn, config, meta).run([fight], [alpha], [casts])
    check("re-import changes nothing", again.rows == 0 and again.skipped == 1,
          f"rows={again.rows} skipped={again.skipped}")
    total = conn.execute("SELECT COUNT(*) n FROM event").fetchone()["n"]
    check("re-import does not duplicate rows", total == 3, str(total))

    forced = Importer(conn, config, meta).run([fight], [alpha], [casts], force=True)
    total = conn.execute("SELECT COUNT(*) n FROM event").fetchone()["n"]
    check("forced re-import replaces rather than appends",
          forced.rows == 3 and total == 3, str(total))

    row = conn.execute(
        "SELECT boss_name, difficulty_name, pull_in_report, pull_seq FROM v_pull LIMIT 1"
    ).fetchone()
    check("the pull view resolves", row["difficulty_name"] == "Mythic")

    conn.close()

if __name__ == "__main__":
    raise SystemExit(main())
