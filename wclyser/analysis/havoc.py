"""What was cast inside each Havoc window, and what led into it.

Havoc duplicates the caster's spells onto a second target for the duration of
the window, so the question the module answers is which spells filled that
time. There is no strict priority to check against, because resource state and
competing cooldowns move the right answer around. The profile lists a rough
ordering, and the output shows the sequence so the pull can be judged by eye.

A window opens on a Havoc cast and runs for the profile's duration, cut short
by the next Havoc cast or the end of the pull, whichever comes first.

Two things are reported alongside the window itself.

The lead-in is the few seconds before the window opens. A spell from the bad
list cast there is a good sign: it was cleared out of the way rather than
spending a duplicated global cooldown.

The shard level is read at the first shard-spending cast inside the window,
with the delay reported alongside it. Havoc costs no shards, so its own cast
event reports the mana pool, and across three reports every shard
``resourcechange`` event carries a change of zero. A cast that spends shards is
the only place the pool is ever reported, so there is no continuous track.

Only generation happens between the Havoc cast and that first spend, so the
reading is an upper bound on the level at entry. The level after the previous
spend is a lower bound, kept as ``shards_floor_mean`` in the metrics. That
bracket runs about three shards wide, which is why the observed reading leads.

Reads ``casts`` only. The window is taken from the Havoc cast rather than from
the debuff on the target, which would need ``debuffs_on_enemies``, a stream
that is fetched raid-wide and normally left out of the import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import Ctx, Result, analysis
from .profiles import load_profile, profile_name

GOOD, BAD, OTHER = "good", "bad", "other"

# Soul Shards are reported in tenths: a pool maximum of 50 is five shards and a
# Chaos Bolt costing 20 spends two.
SOUL_SHARD = 7
SHARD_UNIT = 10.0


@dataclass
class Cast:
    t_ms: int
    ability: str
    ability_id: int | None
    pool_type: int | None = None
    pool_amount: int | None = None
    pool_cost: int | None = None
    pool_max: int | None = None

    @property
    def spends_shards(self) -> bool:
        return self.pool_type == SOUL_SHARD and self.pool_amount is not None


@dataclass
class Shards:
    """What was observed about the shard pool around a window opening."""

    # The pool at the first shard-spending cast inside the window, and how far
    # into the window that cast was. An upper bound on the level at entry.
    reading: float | None = None
    delay_ms: int | None = None
    # The pool after the previous shard-spending cast, a lower bound.
    floor: float | None = None
    cap: float | None = None

    def describe(self) -> str:
        if self.reading is None:
            return "shards    ?    "
        delay = (self.delay_ms or 0) / 1000
        return f"shards {self.reading:4.1f}@{delay:4.1f}s"


@dataclass
class Window:
    fight_id: int
    pull_number: int
    index: int
    start_ms: int
    end_ms: int
    lead_start_ms: int
    casts: list[Cast] = field(default_factory=list)
    lead_casts: list[Cast] = field(default_factory=list)
    shards: Shards = field(default_factory=Shards)

    @property
    def length_s(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0


def _metric_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class Priorities:
    """The profile's spell lists, as a lookup."""

    def __init__(self, havoc: dict) -> None:
        self.good = list(havoc.get("good") or [])
        self.bad = list(havoc.get("bad") or [])
        self._rank: dict[str, tuple[str, int]] = {}
        for i, name in enumerate(self.good, start=1):
            self._rank[name.lower()] = (GOOD, i)
        for i, name in enumerate(self.bad, start=1):
            self._rank[name.lower()] = (BAD, i)

    def classify(self, ability: str) -> tuple[str, int | None]:
        return self._rank.get(ability.lower(), (OTHER, None))

    def is_bad(self, ability: str) -> bool:
        return self.classify(ability)[0] == BAD

    @property
    def named(self) -> set[str]:
        return {n.lower() for n in self.good + self.bad}


def _load_casts(ctx: Ctx, player_key: str, fight_ids: list[int]) -> dict[int, list[Cast]]:
    placeholders = ",".join("?" * len(fight_ids))
    rows = ctx.conn.execute(
        f"""
        SELECT e.fight_id, e.t_rel_ms, e.ability_id,
               e.pool_type, e.pool_amount, e.pool_cost, e.pool_max,
               COALESCE(a.name, 'ability ' || e.ability_id) AS ability
        FROM event e
        LEFT JOIN ability a ON a.ability_id = e.ability_id
        WHERE e.report_code = ? AND e.stream = 'casts' AND e.event_type = 'cast'
          AND e.source_key = ? AND e.fight_id IN ({placeholders})
        ORDER BY e.fight_id, e.t_rel_ms
        """,
        [ctx.meta.code, player_key, *fight_ids],
    ).fetchall()

    by_fight: dict[int, list[Cast]] = {}
    for row in rows:
        by_fight.setdefault(row["fight_id"], []).append(
            Cast(
                t_ms=row["t_rel_ms"],
                ability=row["ability"],
                ability_id=row["ability_id"],
                pool_type=row["pool_type"],
                pool_amount=row["pool_amount"],
                pool_cost=row["pool_cost"],
                pool_max=row["pool_max"],
            )
        )
    return by_fight


def shards_at(casts: list[Cast], position: int, opened_ms: int) -> Shards:
    """Read the shard pool around the window opening at ``casts[position]``.

    Shards are only ever reported on a cast that spends them. The first such
    cast at or after the opening gives the reading; the one before it gives the
    floor, because the level only rises between two spends.
    """
    result = Shards()
    for cast in reversed(casts[:position]):
        if cast.spends_shards:
            spent = cast.pool_cost or 0
            result.floor = max(0.0, (cast.pool_amount - spent) / SHARD_UNIT)
            if cast.pool_max:
                result.cap = cast.pool_max / SHARD_UNIT
            break
    for cast in casts[position:]:
        if cast.spends_shards:
            result.reading = cast.pool_amount / SHARD_UNIT
            result.delay_ms = cast.t_ms - opened_ms
            if cast.pool_max:
                result.cap = cast.pool_max / SHARD_UNIT
            break
    return result


def _build_windows(
    casts: list[Cast],
    havoc_name: str,
    window_ms: int,
    lead_ms: int,
    fight_end_ms: int,
    fight_id: int,
    pull_number: int,
) -> list[Window]:
    havoc = havoc_name.lower()
    opens = [(i, c) for i, c in enumerate(casts) if c.ability.lower() == havoc]
    windows: list[Window] = []

    for n, (position, opener) in enumerate(opens):
        end = opener.t_ms + window_ms
        if n + 1 < len(opens):
            end = min(end, opens[n + 1][1].t_ms)
        end = min(end, fight_end_ms)

        # The lead-in never reaches back into the previous window.
        lead_start = max(0, opener.t_ms - lead_ms)
        if n > 0:
            previous_end = min(opens[n - 1][1].t_ms + window_ms, opener.t_ms)
            lead_start = max(lead_start, previous_end)

        window = Window(
            fight_id=fight_id,
            pull_number=pull_number,
            index=n + 1,
            start_ms=opener.t_ms,
            end_ms=end,
            lead_start_ms=lead_start,
            shards=shards_at(casts, position, opener.t_ms),
        )
        window.casts = [
            c for c in casts
            if opener.t_ms < c.t_ms <= end and c.ability.lower() != havoc
        ]
        window.lead_casts = [
            c for c in casts
            if lead_start <= c.t_ms < opener.t_ms and c.ability.lower() != havoc
        ]
        windows.append(window)
    return windows


@analysis("havoc", title="Havoc windows", requires={"casts"})
def run(ctx: Ctx) -> dict[str, Result]:
    results: dict[str, Result] = {}
    detail = bool(ctx.option("detail"))

    for subject in ctx.subjects:
        result = Result(key="havoc", title="Havoc windows")
        windows: list[Window] = []
        priorities: Priorities | None = None
        havoc_name = "Havoc"
        window_s = 20.0
        lead_s = 5.0
        skipped: list[str] = []

        usable = [
            f for f in ctx.fights if subject.id in (f.friendly_players or [subject.id])
        ]
        by_fight = (
            _load_casts(ctx, subject.player_key, [f.id for f in usable]) if usable else {}
        )
        analysed_ms = 0

        for fight in usable:
            player_class, spec = ctx.spec_of(fight.id, subject.player_key)
            name = profile_name(player_class, spec)
            profile = load_profile(name) if name else None
            if not profile or "havoc" not in profile:
                skipped.append(f"pull {fight.pull_number} ({spec or 'unknown spec'})")
                continue

            havoc = profile["havoc"]
            havoc_name = havoc.get("ability", "Havoc")
            window_s = float(havoc.get("window_s", 20))
            lead_s = float(havoc.get("lead_in_s", 5))
            priorities = Priorities(havoc)

            analysed_ms += fight.duration_ms
            windows.extend(
                _build_windows(
                    by_fight.get(fight.id, []),
                    havoc_name,
                    int(window_s * 1000),
                    int(lead_s * 1000),
                    fight.duration_ms,
                    fight.id,
                    fight.pull_number or 0,
                )
            )

        if skipped:
            result.warnings.append(f"no {havoc_name} profile for: {', '.join(skipped)}")
        if priorities is None:
            result.lines.append("no pull had a profile with a havoc section")
            results[subject.player_key] = result
            continue

        _render(
            result, subject, windows, priorities, havoc_name, window_s, lead_s,
            detail, analysed_ms,
        )
        results[subject.player_key] = result

    return results


def _render(
    result: Result,
    subject,
    windows: list[Window],
    priorities: Priorities,
    havoc_name: str,
    window_s: float,
    lead_s: float,
    detail: bool,
    analysed_ms: int,
) -> None:
    totals = {GOOD: 0, BAD: 0, OTHER: 0}
    per_ability: dict[str, tuple[str, int]] = {}
    windows_with_bad = 0
    empty_windows = 0
    bad_windows_by_ability: dict[str, int] = {}
    lead_bad_by_ability: dict[str, int] = {}
    windows_with_lead_bad = 0
    windows_cleared_and_clean = 0
    per_fight: dict[int, dict[str, float]] = {}
    seen_names: set[str] = set()
    entry_readings: list[float] = []
    entry_delays: list[float] = []
    entry_floors: list[float] = []
    entry_capped = 0
    cap = next((w.shards.cap for w in windows if w.shards.cap), None)

    result.lines.append(
        f"{subject.player_key}   {len(windows)} {havoc_name} windows "
        f"of {window_s:.0f}s across {len({w.fight_id for w in windows})} pulls"
    )
    result.lines.append("")

    for window in windows:
        parts: list[str] = []
        counts = {GOOD: 0, BAD: 0, OTHER: 0}
        for cast in window.casts:
            category, _ = priorities.classify(cast.ability)
            counts[category] += 1
            totals[category] += 1
            seen_names.add(cast.ability.lower())
            existing = per_ability.get(cast.ability, (category, 0))
            per_ability[cast.ability] = (category, existing[1] + 1)
            mark = {GOOD: "", BAD: " [BAD]", OTHER: " [-]"}[category]
            parts.append(f"{cast.ability}{mark}")

        lead_bad = [c for c in window.lead_casts if priorities.is_bad(c.ability)]
        if lead_bad:
            windows_with_lead_bad += 1
            for ability in {c.ability for c in lead_bad}:
                lead_bad_by_ability[ability] = lead_bad_by_ability.get(ability, 0) + 1
            if not counts[BAD]:
                windows_cleared_and_clean += 1

        if counts[BAD]:
            windows_with_bad += 1
            for ability in {
                c.ability for c in window.casts if priorities.is_bad(c.ability)
            }:
                bad_windows_by_ability[ability] = (
                    bad_windows_by_ability.get(ability, 0) + 1
                )
        if not window.casts:
            empty_windows += 1

        if window.shards.reading is not None:
            entry_readings.append(window.shards.reading)
            if window.shards.delay_ms is not None:
                entry_delays.append(window.shards.delay_ms / 1000)
            if cap and window.shards.reading >= cap - 0.05:
                entry_capped += 1
        if window.shards.floor is not None:
            entry_floors.append(window.shards.floor)

        fm = per_fight.setdefault(
            window.fight_id,
            {"windows": 0, "good_casts": 0, "bad_casts": 0, "other_casts": 0,
             "lead_bad_casts": 0},
        )
        fm["windows"] += 1
        fm["good_casts"] += counts[GOOD]
        fm["bad_casts"] += counts[BAD]
        fm["other_casts"] += counts[OTHER]
        fm["lead_bad_casts"] += len(lead_bad)

        flag = "  <-- bad cast" if counts[BAD] else ""
        result.lines.append(
            f"  pull {window.pull_number:>2}  w{window.index}  "
            f"{window.start_ms / 1000:>6.1f}s  {window.length_s:4.1f}s  "
            f"{window.shards.describe()}  "
            f"{counts[GOOD]}g {counts[BAD]}b {counts[OTHER]}o{flag}"
        )
        if lead_bad:
            cleared = ", ".join(c.ability for c in lead_bad)
            result.lines.append(f"        cleared first: {cleared}")
        if detail:
            for cast in window.lead_casts:
                category, _ = priorities.classify(cast.ability)
                offset = (cast.t_ms - window.start_ms) / 1000
                label = "cleared first" if category == BAD else "-"
                result.lines.append(
                    f"        {offset:6.1f}s  {cast.ability:<28} {label}"
                )
            for cast in window.casts:
                category, rank = priorities.classify(cast.ability)
                offset = (cast.t_ms - window.start_ms) / 1000
                label = {GOOD: f"good {rank}", BAD: f"BAD {rank}", OTHER: "-"}[category]
                result.lines.append(
                    f"        +{offset:5.1f}s  {cast.ability:<28} {label}"
                )
        else:
            result.lines.append(f"        {' > '.join(parts) or '(no casts)'}")

    result.lines.append("")
    result.lines.append("  casts inside windows, by spell:")
    order = {GOOD: 0, BAD: 1, OTHER: 2}
    for ability, (category, count) in sorted(
        per_ability.items(), key=lambda kv: (order[kv[1][0]], -kv[1][1])
    ):
        _, rank = priorities.classify(ability)
        label = {GOOD: f"good {rank}", BAD: f"BAD {rank}", OTHER: "other"}[category]
        share = 100 * count / max(1, sum(totals.values()))
        result.lines.append(f"    {ability:<28} {label:<8} {count:>4}  {share:5.1f}%")

    total_casts = sum(totals.values())
    n = max(1, len(windows))
    result.lines.append("")
    result.lines.append(
        f"  {total_casts} casts in {len(windows)} windows, "
        f"{total_casts / n:.1f} per window"
    )
    result.lines.append(
        f"  {totals[GOOD]} good, {totals[BAD]} bad, {totals[OTHER]} other"
    )
    result.lines.append(
        f"  {windows_with_bad} of {len(windows)} windows contain a bad cast "
        f"({100 * windows_with_bad / n:.0f}%)"
    )

    window_ms = sum(w.end_ms - w.start_ms for w in windows)
    if analysed_ms:
        result.lines.append(
            f"  windows cover {window_ms / 1000:.0f}s of {analysed_ms / 1000:.0f}s "
            f"analysed, {100 * window_ms / analysed_ms:.0f}% of the time in these pulls"
        )

    if bad_windows_by_ability or lead_bad_by_ability:
        result.lines.append("")
        result.lines.append(
            f"  bad spells, cleared in the {lead_s:.0f}s lead-in against cast inside:"
        )
        result.lines.append(
            f"    {'spell':<28}{'cleared first':>14}{'spoiled a window':>19}"
        )
        for ability in sorted(
            set(bad_windows_by_ability) | set(lead_bad_by_ability),
            key=lambda a: -bad_windows_by_ability.get(a, 0),
        ):
            result.lines.append(
                f"    {ability:<28}{lead_bad_by_ability.get(ability, 0):>14}"
                f"{bad_windows_by_ability.get(ability, 0):>19}"
            )
        result.lines.append(
            f"  {windows_with_lead_bad} of {len(windows)} windows had a bad spell "
            f"cleared in the lead-in"
        )
        result.lines.append(
            f"  {windows_cleared_and_clean} of those stayed clean inside the window"
        )

    if entry_readings:
        cap_text = f" of {cap:.0f}" if cap else ""
        mean_delay = sum(entry_delays) / len(entry_delays) if entry_delays else 0.0
        ordered = sorted(entry_readings)
        result.lines.append("")
        result.lines.append(
            f"  shards at the first spend inside the window, read {mean_delay:.1f}s "
            f"in on average. An upper bound on the level at entry."
        )
        result.lines.append(
            f"    mean {sum(entry_readings) / len(entry_readings):.1f}{cap_text}, "
            f"median {ordered[len(ordered) // 2]:.1f}, "
            f"range {ordered[0]:.1f} to {ordered[-1]:.1f}"
        )
        if cap:
            capped_share = 100 * entry_capped / max(1, len(windows))
            low = sum(1 for r in entry_readings if r < cap / 2)
            result.lines.append(
                f"    {entry_capped} of {len(windows)} windows read at the "
                f"{cap:.0f} shard cap ({capped_share:.0f}%), "
                f"{low} read below {cap / 2:.1f}"
            )
        unknown = len(windows) - len(entry_readings)
        if unknown:
            result.lines.append(
                f"    {unknown} windows had no shard-spending cast to read from"
            )

    if empty_windows:
        result.lines.append(f"  {empty_windows} windows contain no casts at all")

    unmatched = priorities.named - seen_names
    if unmatched:
        result.warnings.append(
            "named in the profile but never cast inside a window: "
            + ", ".join(sorted(unmatched))
        )

    result.metrics = {
        "windows": float(len(windows)),
        "casts_in_window": float(total_casts),
        "casts_per_window": total_casts / n,
        "good_casts": float(totals[GOOD]),
        "bad_casts": float(totals[BAD]),
        "other_casts": float(totals[OTHER]),
        "good_per_window": totals[GOOD] / n,
        "windows_with_bad": float(windows_with_bad),
        "windows_with_bad_share": windows_with_bad / n,
        "window_seconds": window_ms / 1000,
        "window_time_share": (window_ms / analysed_ms) if analysed_ms else 0.0,
        "windows_with_lead_bad": float(windows_with_lead_bad),
        "windows_cleared_and_clean": float(windows_cleared_and_clean),
        "lead_bad_share": windows_with_lead_bad / n,
    }
    if entry_readings:
        result.metrics["shards_at_first_spend_mean"] = sum(entry_readings) / len(
            entry_readings
        )
        result.metrics["shards_at_first_spend_min"] = min(entry_readings)
        result.metrics["shards_at_first_spend_max"] = max(entry_readings)
        result.metrics["windows_entering_capped"] = float(entry_capped)
    if entry_delays:
        result.metrics["shard_reading_delay_mean_s"] = sum(entry_delays) / len(
            entry_delays
        )
    if entry_floors:
        result.metrics["shards_floor_mean"] = sum(entry_floors) / len(entry_floors)
    for ability, (_, count) in per_ability.items():
        result.metrics[f"in_window.{_metric_key(ability)}"] = float(count)
    for ability, count in bad_windows_by_ability.items():
        result.metrics[f"windows_spoiled.{_metric_key(ability)}"] = float(count)
    for ability, count in lead_bad_by_ability.items():
        result.metrics[f"cleared_first.{_metric_key(ability)}"] = float(count)

    for fight_id, fm in per_fight.items():
        w = max(1.0, fm["windows"])
        fm["good_per_window"] = fm["good_casts"] / w
        result.fight_metrics[fight_id] = fm
