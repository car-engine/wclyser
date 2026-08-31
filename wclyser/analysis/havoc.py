"""What was cast inside each Havoc window.

Havoc duplicates the caster's spells onto a second target for the duration of
the window, so the question the module answers is which spells filled that
time. There is no strict priority to check against, because resource state and
competing cooldowns move the right answer around. The profile lists a rough
ordering, and the output shows the sequence so the pull can be judged by eye.

A window opens on a Havoc cast and runs for the profile's duration, cut short
by the next Havoc cast or the end of the pull, whichever comes first.

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


@dataclass
class Cast:
    t_ms: int
    ability: str
    ability_id: int | None

    @property
    def t_s(self) -> float:
        return self.t_ms / 1000.0


@dataclass
class Window:
    fight_id: int
    pull_number: int
    index: int
    start_ms: int
    end_ms: int
    casts: list[Cast] = field(default_factory=list)

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

    @property
    def named(self) -> set[str]:
        return {n.lower() for n in self.good + self.bad}


def _load_casts(ctx: Ctx, player_key: str, fight_ids: list[int]) -> dict[int, list[Cast]]:
    placeholders = ",".join("?" * len(fight_ids))
    rows = ctx.conn.execute(
        f"""
        SELECT e.fight_id, e.t_rel_ms, e.ability_id,
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
            Cast(row["t_rel_ms"], row["ability"], row["ability_id"])
        )
    return by_fight


def _build_windows(
    casts: list[Cast], havoc_name: str, window_ms: int, fight_end_ms: int,
    fight_id: int, pull_number: int,
) -> list[Window]:
    opens = [c for c in casts if c.ability.lower() == havoc_name.lower()]
    windows: list[Window] = []
    for i, opener in enumerate(opens):
        end = opener.t_ms + window_ms
        if i + 1 < len(opens):
            end = min(end, opens[i + 1].t_ms)
        end = min(end, fight_end_ms)
        window = Window(
            fight_id=fight_id,
            pull_number=pull_number,
            index=i + 1,
            start_ms=opener.t_ms,
            end_ms=end,
        )
        window.casts = [
            c
            for c in casts
            if opener.t_ms < c.t_ms <= end and c.ability.lower() != havoc_name.lower()
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
        skipped: list[str] = []

        usable = [
            f for f in ctx.fights if subject.id in (f.friendly_players or [subject.id])
        ]
        by_fight = _load_casts(ctx, subject.player_key, [f.id for f in usable]) if usable else {}
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
            priorities = Priorities(havoc)

            analysed_ms += fight.duration_ms
            windows.extend(
                _build_windows(
                    by_fight.get(fight.id, []),
                    havoc_name,
                    int(window_s * 1000),
                    fight.duration_ms,
                    fight.id,
                    fight.pull_number or 0,
                )
            )

        if skipped:
            result.warnings.append(
                f"no {havoc_name} profile for: {', '.join(skipped)}"
            )
        if priorities is None:
            result.lines.append("no pull had a profile with a havoc section")
            results[subject.player_key] = result
            continue

        _render(result, subject, windows, priorities, havoc_name, window_s,
                detail, analysed_ms)
        results[subject.player_key] = result

    return results


def _render(
    result: Result,
    subject,
    windows: list[Window],
    priorities: Priorities,
    havoc_name: str,
    window_s: float,
    detail: bool,
    analysed_ms: int,
) -> None:
    totals = {GOOD: 0, BAD: 0, OTHER: 0}
    per_ability: dict[str, tuple[str, int]] = {}
    windows_with_bad = 0
    empty_windows = 0
    bad_windows_by_ability: dict[str, int] = {}
    per_fight: dict[int, dict[str, float]] = {}
    seen_names: set[str] = set()

    result.lines.append(
        f"{subject.player_key}   {len(windows)} {havoc_name} windows "
        f"of {window_s:.0f}s across {len({w.fight_id for w in windows})} pulls"
    )
    result.lines.append("")

    for window in windows:
        parts: list[str] = []
        counts = {GOOD: 0, BAD: 0, OTHER: 0}
        for cast in window.casts:
            category, rank = priorities.classify(cast.ability)
            counts[category] += 1
            totals[category] += 1
            seen_names.add(cast.ability.lower())
            existing = per_ability.get(cast.ability, (category, 0))
            per_ability[cast.ability] = (category, existing[1] + 1)
            mark = {GOOD: "", BAD: " [BAD]", OTHER: " [-]"}[category]
            parts.append(f"{cast.ability}{mark}")

        if counts[BAD]:
            windows_with_bad += 1
            for ability in {
                c.ability for c in window.casts
                if priorities.classify(c.ability)[0] == BAD
            }:
                bad_windows_by_ability[ability] = (
                    bad_windows_by_ability.get(ability, 0) + 1
                )
        if not window.casts:
            empty_windows += 1

        fm = per_fight.setdefault(
            window.fight_id,
            {"windows": 0, "good_casts": 0, "bad_casts": 0, "other_casts": 0},
        )
        fm["windows"] += 1
        fm["good_casts"] += counts[GOOD]
        fm["bad_casts"] += counts[BAD]
        fm["other_casts"] += counts[OTHER]

        flag = "  <-- bad cast" if counts[BAD] else ""
        header = (
            f"  pull {window.pull_number:>2}  w{window.index}  "
            f"{window.start_ms / 1000:>6.1f}s  {window.length_s:4.1f}s  "
            f"{counts[GOOD]}g {counts[BAD]}b {counts[OTHER]}o{flag}"
        )
        result.lines.append(header)
        if detail:
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
        result.lines.append(
            f"    {ability:<28} {label:<8} {count:>4}  {share:5.1f}%"
        )

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
        share = 100 * window_ms / analysed_ms
        result.lines.append(
            f"  windows cover {window_ms / 1000:.0f}s of {analysed_ms / 1000:.0f}s "
            f"analysed, {share:.0f}% of the time in these pulls"
        )

    if bad_windows_by_ability:
        result.lines.append("")
        result.lines.append("  windows spoiled, by spell:")
        for ability, count in sorted(
            bad_windows_by_ability.items(), key=lambda kv: -kv[1]
        ):
            result.lines.append(
                f"    {ability:<28} {count:>3} of {len(windows)} windows"
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
    }
    for ability, count in bad_windows_by_ability.items():
        result.metrics[f"windows_spoiled.{_metric_key(ability)}"] = float(count)
    for ability, (_, count) in per_ability.items():
        result.metrics[f"in_window.{_metric_key(ability)}"] = float(count)

    for fight_id, fm in per_fight.items():
        w = max(1.0, fm["windows"])
        fm["good_per_window"] = fm["good_casts"] / w
        result.fight_metrics[fight_id] = fm
