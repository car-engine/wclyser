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

from wclyser.config import Config, FetchSettings, SubjectRules  # noqa: E402
from wclyser.errors import ConfigError  # noqa: E402
from wclyser.fetch import Fetcher, resolve_streams  # noqa: E402
from wclyser.fetch.meta import parse_report  # noqa: E402
from wclyser.fetch.scopes import BY_KEY  # noqa: E402
from wclyser.db import Importer, open_database  # noqa: E402

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
    tmp = Path(tempfile.mkdtemp(prefix="wcly-check-"))
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

        print("coverage and pruning")
        _check_coverage(tmp, config, meta, alpha, fetcher)

        print("havoc analysis")
        _check_havoc()
        _check_shard_model()

        print("report page")
        _check_report(tmp)

        print("command line flags")
        _check_cli_flags()

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

def _check_coverage(tmp: Path, config, meta, alpha, fetcher) -> None:
    """The feedback an analysis gets when the database lacks what it needs."""
    from wclyser.db import MissingData, check as coverage_check, prune_streams
    from wclyser.db import require as coverage_require

    casts = BY_KEY["casts"]
    buffs = BY_KEY["buffs"]
    deaths = BY_KEY["deaths"]
    fight = meta.fights[0]
    conn = open_database(tmp / "check.sqlite")

    cov = coverage_check(conn, config, meta, [fight], [alpha], ["casts"])
    check("an imported stream reads as complete", cov.complete)

    # buffs was fetched into the cache by the fetch checks above, so the
    # remedy is a free re-import.
    fetcher.fetch_stream(meta, fight, buffs, alpha)
    cov = coverage_check(conn, config, meta, [fight], [alpha], ["casts", "buffs"])
    check("a cached but unimported stream is a gap", len(cov.gaps) == 1)
    check("its remedy is an import", cov.needs_import and not cov.needs_fetch)
    check("the message names the command",
          "wcly import" in cov.message() and "--stream buffs" in cov.message(),
          cov.message())

    # deaths was never fetched, so the remedy has to start with a fetch.
    cov = coverage_check(conn, config, meta, [fight], [alpha], ["deaths"])
    check("an uncached stream needs fetching", len(cov.needs_fetch) == 1)
    check("the message says a fetch comes first", "wcly fetch" in cov.message(),
          cov.message())

    try:
        coverage_require(conn, config, meta, [fight], [alpha], ["deaths"])
        check("require raises on a gap", False)
    except MissingData:
        check("require raises on a gap", True)

    cov = coverage_require(conn, config, meta, [fight], [alpha], ["casts"])
    check("require passes when the data is there", cov.complete)

    # Pruning drops a stream the import list no longer wants.
    from wclyser.fetch.scopes import subject_slug
    from wclyser.db import Importer

    Importer(conn, config, meta).run([fight], [alpha], [casts, buffs])
    before = conn.execute(
        "SELECT COUNT(*) n FROM event WHERE stream = 'buffs'"
    ).fetchone()["n"]
    check("buffs imported for the prune check", before > 0)

    summary = prune_streams(conn, meta.code, ["casts"], dry_run=True)
    still = conn.execute(
        "SELECT COUNT(*) n FROM event WHERE stream = 'buffs'"
    ).fetchone()["n"]
    check("a dry run deletes nothing", still == before)
    check("a dry run still counts what it would remove",
          summary.removed_rows == before, str(summary.removed_rows))

    prune_streams(conn, meta.code, ["casts"])
    gone = conn.execute(
        "SELECT COUNT(*) n FROM event WHERE stream = 'buffs'"
    ).fetchone()["n"]
    kept = conn.execute(
        "SELECT COUNT(*) n FROM event WHERE stream = 'casts'"
    ).fetchone()["n"]
    check("prune removed the unwanted stream", gone == 0)
    check("prune kept the wanted stream", kept > 0)
    logged = conn.execute(
        "SELECT COUNT(*) n FROM import_log WHERE stream = 'buffs'"
    ).fetchone()["n"]
    check("prune cleared the import log too", logged == 0)

    cov = coverage_check(conn, config, meta, [fight], [alpha], ["buffs"])
    check("a pruned stream reads as a gap again", not cov.complete)

    conn.close()

def _check_havoc() -> None:
    """Window construction, lead-in and shard reading, on hand-built casts."""
    from wclyser.analysis import REGISTRY, resolve
    from wclyser.analysis.havoc import (
        Cast, Generation, Priorities, _build_windows, shards_at,
    )
    from wclyser.analysis.profiles import available, load_profile

    check("the havoc module is registered", "havoc" in REGISTRY)
    check("it declares the casts stream",
          REGISTRY["havoc"].requires == frozenset({"casts"}))
    check("resolve finds it by key", resolve(["havoc"])[0].key == "havoc")

    profile = load_profile("warlock_destruction")
    check("the Destruction profile loads", profile is not None)
    check("it is listed", "warlock_destruction" in available())
    check("its window is 20 seconds", profile["havoc"]["window_s"] == 20)
    check("its lead-in is 5 seconds", profile["havoc"]["lead_in_s"] == 5)

    priorities = Priorities(profile["havoc"])
    check("Soul Fire is the best good spell", priorities.classify("Soul Fire") == ("good", 1))
    check("Incinerate is the filler", priorities.classify("Incinerate") == ("good", 5))
    check("Summon Infernal is the worst bad spell",
          priorities.classify("Summon Infernal") == ("bad", 1))
    check("an unlisted spell is other",
          priorities.classify("Burning Rush") == ("other", None))
    check("matching ignores case", priorities.classify("chaos bolt") == ("good", 3))

    def bolt(t, shards=None, cost=None):
        """A Chaos Bolt, optionally reporting a shard pool in tenths."""
        return Cast(t, "Chaos Bolt", 116858, pool_type=7 if shards is not None else 0,
                    pool_amount=shards, pool_cost=cost, pool_max=50)

    # A pull of 100s with two Havoc casts, the second only 5s after the first.
    casts = [
        Cast(500, "Wither", 445468),          # cleared before the window opens
        Cast(1_000, "Havoc", 80240),
        bolt(2_000, shards=50, cost=20),
        Cast(4_000, "Wither", 445468),
        Cast(6_000, "Havoc", 80240),          # cuts the first window short
        Cast(7_000, "Shadowburn", 17877),
        bolt(95_000, shards=30, cost=20),     # beyond the second window
    ]
    windows = _build_windows(casts, "Havoc", 20_000, 5_000, 100_000,
                             fight_id=1, pull_number=1)
    check("one window per Havoc cast", len(windows) == 2, str(len(windows)))

    first, second = windows
    check("the next Havoc cuts the first window short", first.end_ms == 6_000,
          str(first.end_ms))
    check("the first window holds only what fell inside it",
          [c.ability for c in first.casts] == ["Chaos Bolt", "Wither"],
          str([c.ability for c in first.casts]))
    check("the opening Havoc is not counted as content",
          all(c.ability != "Havoc" for c in first.casts))
    check("the second window runs its full length", second.end_ms == 26_000,
          str(second.end_ms))
    check("a cast after the window is excluded",
          [c.ability for c in second.casts] == ["Shadowburn"],
          str([c.ability for c in second.casts]))

    # Lead-in.
    check("the lead-in collects the cast before the window",
          [c.ability for c in first.lead_casts] == ["Wither"],
          str([c.ability for c in first.lead_casts]))
    check("the lead-in never reaches into the previous window",
          second.lead_start_ms == 6_000, str(second.lead_start_ms))
    check("so the second window has an empty lead-in", second.lead_casts == [])
    check("the lead-in excludes the opening Havoc itself",
          all(c.ability != "Havoc" for c in first.lead_casts))

    # Shard reading: the first shard-spending cast at or after the opening.
    check("the reading comes from the first shard spend inside",
          first.shards.reading == 5.0, str(first.shards.reading))
    check("its delay is measured from the opening",
          first.shards.delay_ms == 1_000, str(first.shards.delay_ms))
    check("the cap is read from the pool maximum", first.shards.cap == 5.0)
    check("the first window has no earlier spend to floor it",
          first.shards.floor is None)
    check("the second window floors on the previous spend",
          second.shards.floor == 3.0, str(second.shards.floor))
    check("a window with no later spend has no reading",
          shards_at([Cast(0, "Havoc", 80240)], 0, 0, 20_000, Generation()).reading is None)

    # A Havoc late in the pull is truncated by the end of the pull.
    late = _build_windows(
        [Cast(90_000, "Havoc", 80240), Cast(95_000, "Chaos Bolt", 116858)],
        "Havoc", 20_000, 5_000, 100_000, fight_id=1, pull_number=1,
    )
    check("the end of the pull truncates a window", late[0].end_ms == 100_000,
          str(late[0].end_ms))
    check("casts before the pull ends still count", len(late[0].casts) == 1)

    # A cast landing exactly on the boundary belongs to the window.
    edge = _build_windows(
        [Cast(0, "Havoc", 80240), Cast(20_000, "Chaos Bolt", 116858),
         Cast(20_001, "Incinerate", 29722)],
        "Havoc", 20_000, 5_000, 100_000, fight_id=1, pull_number=1,
    )
    check("a cast on the boundary is inside",
          [c.ability for c in edge[0].casts] == ["Chaos Bolt"],
          str([c.ability for c in edge[0].casts]))

    check("a pull with no Havoc has no windows",
          _build_windows([Cast(1_000, "Chaos Bolt", 116858)], "Havoc", 20_000,
                         5_000, 100_000, fight_id=1, pull_number=1) == [])

def _check_shard_model() -> None:
    """Back-extrapolation from the first spend to the window opening."""
    from wclyser.analysis.havoc import Cast, Generation, shards_at
    from wclyser.analysis.profiles import load_profile

    gen = Generation.from_profile(load_profile("warlock_destruction")["havoc"])
    check("Conflagrate generates half a shard", gen.per_cast["conflagrate"] == 0.5)
    check("Soul Fire generates one", gen.per_cast["soul fire"] == 1.0)
    check("Incinerate generates a fifth", gen.per_cast["incinerate"] == 0.2)
    check("the passive tick is 0.1 every 1.5s",
          gen.passive_per_tick == 0.1 and gen.tick_s == 1.5)
    check("Havoc doubles generation", gen.multiplier == 2.0)

    # Four seconds inside a window holding one Conflagrate.
    casts = [
        Cast(0, "Havoc", 80240),
        Cast(1_000, "Conflagrate", 17962),
        Cast(4_000, "Chaos Bolt", 116858, pool_type=7, pool_amount=35,
             pool_cost=20, pool_max=50),
    ]
    # Conflagrate 0.5 x2 = 1.0, passive 4s / 1.5s x 0.1 x2 = 0.5333.
    generated = gen.between(casts, 0, 4_000, window_end_ms=20_000)
    check("generation inside the window is doubled",
          abs(generated - (1.0 + 4.0 / 1.5 * 0.1 * 2)) < 1e-9, f"{generated:.4f}")

    shards = shards_at(casts, 0, 0, 20_000, gen)
    check("the reading is the pool at the first spend", shards.reading == 3.5)
    check("the entry level subtracts what was generated",
          abs(shards.entry - (3.5 - generated)) < 1e-9, str(shards.entry))
    check("the delay is recorded", shards.delay_ms == 4_000)

    # The same casts outside any window generate half as much.
    outside = gen.between(casts, 0, 4_000, window_end_ms=0)
    check("generation outside the window is not doubled",
          abs(outside - (0.5 + 4.0 / 1.5 * 0.1)) < 1e-9, f"{outside:.4f}")

    # A window that ends before the first spend splits the passive at the edge.
    split = gen.between(casts, 0, 4_000, window_end_ms=2_000)
    expected = 1.0 + (2.0 / 1.5 * 0.1 * 2) + (2.0 / 1.5 * 0.1)
    check("passive generation splits at the window edge",
          abs(split - expected) < 1e-9, f"{split:.4f}")

    check("a cast on the boundary does not generate",
          gen.between(casts, 1_000, 4_000, 20_000)
          < gen.between(casts, 0, 4_000, 20_000))

    # An entry level never goes below zero.
    poor = shards_at(
        [Cast(0, "Havoc", 80240)]
        + [Cast(t, "Conflagrate", 17962) for t in range(500, 9_000, 500)]
        + [Cast(9_500, "Chaos Bolt", 116858, pool_type=7, pool_amount=20,
                pool_cost=20, pool_max=50)],
        0, 0, 20_000, gen,
    )
    check("an over-subtracted entry is clamped at zero", poor.entry == 0.0,
          str(poor.entry))

    # With no rates configured nothing is subtracted.
    bare = shards_at(casts, 0, 0, 20_000, Generation())
    check("without rates the entry equals the reading", bare.entry == 3.5)
    check("an empty model is inactive", not Generation().active)


def _check_report(tmp: Path) -> None:
    """Rendering a page, without a browser."""
    import json
    import re

    from wclyser import report as rp
    from wclyser.errors import WCLError

    payload = {
        "title": "Havoc windows · Alpha · 2026-08-30",
        "report": "TEST123", "night": "2026-08-30", "zone": "Test Raid",
        "player": "Alpha-Frostmourne-US", "source_id": 5,
        "player_class": "Warlock", "spec": "Destruction",
        "encounter": "Test Boss", "difficulty": "Mythic",
        "window_s": 20, "lead_in_s": 5, "ability": "Havoc",
        "good": ["Chaos Bolt"], "bad": ["Wither"],
        "pulls": [{"pull": 1, "fight_id": 1, "outcome": "wipe", "duration_s": 100.0,
                   "end_percent": 42.0, "fight_start_ms": 1000, "fight_end_ms": 101000}],
        "windows": [{
            "pull": 1, "idx": 1, "fight_id": 1, "start_s": 10.0, "len_s": 20.0,
            "abs_start_ms": 11000, "abs_end_ms": 31000, "abs_lead_ms": 6000,
            "entry": 4.2, "reading": 50, "delay_s": 2.0, "cap": 5.0,
            "casts": [{"t": 1.0, "n": "Chaos Bolt", "c": "good", "r": 1, "lead": False},
                      {"t": -2.0, "n": "Wither", "c": "bad", "r": 1, "lead": True}],
        }],
    }

    check("havoc has a page template", rp.available("havoc"))
    check("an unknown module has none", not rp.available("nothing"))

    html = rp.render("havoc", payload, footer_note="wclyser · TEST123")
    check("no placeholder survives rendering",
          "{{TITLE}}" not in html and "{{FOOTER_NOTE}}" not in html
          and "/*__DATA__*/" not in html)
    check("the title is applied", "<title>" + payload["title"] + "</title>" in html)
    check("the footer note is applied", "wclyser · TEST123" in html)

    body = re.search(r'type="application/json">(.*?)</script>', html, re.S).group(1)
    back = json.loads(body)
    check("the payload round-trips", back == payload)

    # Only Google Fonts may be reached; everything else has to be inline.
    hosts = set(re.findall(r'https?://([^/"\s]+)', html))
    allowed = {"fonts.googleapis.com", "fonts.gstatic.com", "www.warcraftlogs.com"}
    check("the page reaches no unexpected host", hosts <= allowed, str(hosts - allowed))
    check("no external script or stylesheet beyond fonts",
          html.count("<script src") == 0)

    # A spell name carrying a closing tag must not break out of the script.
    nasty = json.loads(json.dumps(payload))
    nasty["windows"][0]["casts"][0]["n"] = "Chaos </script><script>alert(1)</script>"
    dirty = rp.render("havoc", nasty)
    check("an embedded closing tag is escaped",
          "</script><script>alert(1)" not in dirty)
    reparsed = json.loads(
        re.search(r'type="application/json">(.*?)</script>', dirty, re.S)
        .group(1).replace("<\\/", "</")
    )
    check("and the escaped payload still parses",
          reparsed["windows"][0]["casts"][0]["n"].startswith("Chaos "))

    try:
        rp.render("nothing", payload)
        check("rendering an unknown module raises", False)
    except WCLError:
        check("rendering an unknown module raises", True)

    name = rp.filename("TEST123", "Alpha-Frostmourne-US", "havoc")
    check("the filename is safe and identifies the page",
          name == "TEST123-Alpha-Frostmourne-US-havoc.html", name)

    out = rp.write(tmp / "reports" / name, html)
    check("the page is written to disk", out.is_file() and out.stat().st_size > 1000)


def _check_cli_flags() -> None:
    """Repeatable flags accept comma-separated values as well."""
    import contextlib
    import io

    from wclyser.cli import build_parser

    p = build_parser()

    def parsed(argv):
        return p.parse_args(argv)

    # import carries --player, --fight and --stream and needs no module.
    a = parsed(["import", "C", "--player", "A,B"])
    check("a comma-separated flag splits", a.player == ["A", "B"], str(a.player))

    a = parsed(["import", "C", "--player", "A", "--player", "B"])
    check("repeating the flag still works", a.player == ["A", "B"], str(a.player))

    a = parsed(["import", "C", "--player", "A,B", "--player", "C"])
    check("the two forms mix", a.player == ["A", "B", "C"], str(a.player))

    a = parsed(["import", "C", "--player", " A , B "])
    check("surrounding spaces are trimmed", a.player == ["A", "B"], str(a.player))

    a = parsed(["import", "C", "--player", "A,,B,"])
    check("empty entries are dropped", a.player == ["A", "B"], str(a.player))

    a = parsed(["import", "C", "--fight", "22,23"])
    check("fight ids come back as integers", a.fight == [22, 23], str(a.fight))

    a = parsed(["import", "C", "--fight", "22", "--fight", "23,24"])
    check("mixed forms convert too", a.fight == [22, 23, 24], str(a.fight))

    a = parsed(["fetch", "C", "--stream", "casts,resources"])
    check("streams split", a.stream == ["casts", "resources"], str(a.stream))

    a = parsed(["prune", "C", "--keep", "casts,resources"])
    check("kept streams split", a.keep == ["casts", "resources"], str(a.keep))

    a = parsed(["analyse", "C", "--module", "havoc,other"])
    check("modules split", a.module == ["havoc", "other"], str(a.module))

    a = parsed(["report", "C", "--module", "havoc"])
    check("an absent flag stays None", a.player is None and a.fight is None)

    # A fight id that is not a number names the flag and stops the run.
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            parsed(["import", "C", "--fight", "22,abc"])
        check("a non-numeric fight id is refused", False)
    except SystemExit:
        message = err.getvalue()
        check("a non-numeric fight id is refused",
              "--fight" in message and "abc" in message, message.strip()[-60:])

    # analyse and report each name the module to run rather than defaulting.
    for command in ("analyse", "report"):
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                parsed([command, "C"])
            check(f"{command} demands --module", False)
        except SystemExit:
            check(f"{command} demands --module", "--module" in err.getvalue())


if __name__ == "__main__":
    raise SystemExit(main())
