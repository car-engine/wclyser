"""Report metadata: the fights list, master data, and subject resolution.

The fights list is the one thing in a report that changes, so it is always
fetched from the network. A copy is written to disk afterwards so that import
and analysis can run offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import ConfigError

log = logging.getLogger(__name__)

_ROLE_NAMES = {"tanks": "tank", "healers": "healer", "dps": "dps"}

DIFFICULTY_NAMES = {
    1: "LFR",
    2: "Normal",
    3: "Normal",
    4: "Heroic",
    5: "Mythic",
    10: "Mythic+",
}


@dataclass
class Actor:
    id: int
    game_id: int | None
    name: str
    type: str
    sub_type: str | None
    icon: str | None
    server: str | None
    pet_owner: int | None
    region: str | None = None
    role: str | None = None
    # Specialisations this player was seen in, most-played first. A player who
    # swaps spec between pulls has more than one, so the spec that applies to a
    # given fight is settled per fight from CombatantInfo, not from here.
    specs_played: list[str] = field(default_factory=list)
    item_level: int | None = None

    @property
    def is_player(self) -> bool:
        return self.type == "Player"

    @property
    def player_class(self) -> str | None:
        return self.sub_type if self.is_player else None

    @property
    def spec(self) -> str | None:
        """The player's most-played specialisation across the report."""
        if self.specs_played:
            return self.specs_played[0]
        if self.icon and "-" in self.icon:
            return self.icon.split("-", 1)[1]
        return None

    @property
    def swapped_spec(self) -> bool:
        return len(self.specs_played) > 1

    def played_spec(self, spec: str) -> bool:
        wanted = spec.lower()
        if any(s.lower() == wanted for s in self.specs_played):
            return True
        return (self.spec or "").lower() == wanted

    @property
    def player_key(self) -> str:
        parts = [self.name]
        if self.server:
            parts.append(self.server)
        if self.region:
            parts.append(self.region)
        return "-".join(parts)


@dataclass
class Fight:
    id: int
    encounter_id: int
    name: str
    difficulty: int | None
    size: int | None
    kill: bool | None
    start_ms: int
    end_ms: int
    fight_percentage: float | None
    boss_percentage: float | None
    last_phase: int | None
    friendly_players: list[int] = field(default_factory=list)
    pull_number: int | None = None

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def difficulty_name(self) -> str:
        if self.difficulty is None:
            return "Unknown"
        return DIFFICULTY_NAMES.get(self.difficulty, str(self.difficulty))

    @property
    def outcome(self) -> str:
        return "kill" if self.kill else "wipe"

    def describe(self) -> str:
        pull = f" pull {self.pull_number}" if self.pull_number else ""
        return (
            f"[{self.id:>3}] {self.difficulty_name} {self.name}{pull} "
            f"{self.outcome} {self.duration_ms / 1000:.0f}s"
        )


@dataclass
class ReportMeta:
    code: str
    title: str | None
    start_time_utc: int
    end_time_utc: int
    zone_id: int | None
    zone_name: str | None
    owner: str | None
    guild: str | None
    region: str | None
    fights: list[Fight]
    actors: dict[int, Actor]
    abilities: dict[int, dict]
    fetched_at: str

    # -- lookups ---------------------------------------------------------

    def fight(self, fight_id: int) -> Fight | None:
        for f in self.fights:
            if f.id == fight_id:
                return f
        return None

    @property
    def last_fight_id(self) -> int | None:
        return max((f.id for f in self.fights), default=None)

    def players(self) -> list[Actor]:
        return [a for a in self.actors.values() if a.is_player]

    @property
    def raid_night(self) -> str:
        """Local date the report started, used as the night grouping key."""
        started = datetime.fromtimestamp(self.start_time_utc / 1000, tz=timezone.utc)
        return started.astimezone().date().isoformat()

    # -- subject resolution ----------------------------------------------

    def resolve_subjects(
        self, config: Config, explicit: list[str] | None = None
    ) -> list[Actor]:
        """Decide which players this report is analysed for.

        Order: command line, per-report configuration, auto-select by class and
        specialisation, configured default list. The first source that matches
        anyone in this report wins.
        """
        rules = config.subjects
        for names in (explicit, rules.by_report.get(self.code)):
            if names:
                return self._match_names(names, strict=True)

        if rules.auto_select_class or rules.auto_select_spec:
            matched = [a for a in self.players() if self._matches_rules(a, rules)]
            if matched:
                return sorted(matched, key=lambda a: a.name)

        if rules.default:
            return self._match_names(rules.default, strict=False)

        return []

    @staticmethod
    def _matches_rules(actor: Actor, rules: Any) -> bool:
        if rules.auto_select_class:
            if (actor.player_class or "").lower() != rules.auto_select_class.lower():
                return False
        if rules.auto_select_spec:
            if not actor.played_spec(rules.auto_select_spec):
                return False
        return True

    def _match_names(self, names: list[str], strict: bool) -> list[Actor]:
        found: list[Actor] = []
        missing: list[str] = []
        for name in names:
            actor = self._match_one(name)
            if actor is None:
                missing.append(name)
            else:
                found.append(actor)
        if missing and strict:
            present = ", ".join(sorted(a.player_key for a in self.players()))
            raise ConfigError(
                f"player(s) {missing} are not in report {self.code}. Present: {present}"
            )
        if missing:
            log.debug("configured default players absent from report: %s", missing)
        return found

    def _match_one(self, name: str) -> Actor | None:
        wanted = name.lower()
        for actor in self.players():
            if actor.player_key.lower() == wanted:
                return actor
        # Fall back to a bare character name when it is unambiguous.
        hits = [a for a in self.players() if a.name.lower() == wanted]
        if len(hits) == 1:
            return hits[0]
        return None

    def subjects_in_fight(self, fight: Fight, subjects: list[Actor]) -> list[Actor]:
        """Subjects that were actually present for a pull."""
        if not fight.friendly_players:
            return subjects
        present = set(fight.friendly_players)
        return [a for a in subjects if a.id in present]


DIFFICULTY_IDS = {
    "lfr": 1,
    "normal": 3,
    "heroic": 4,
    "mythic": 5,
    "mythic+": 10,
}


def parse_difficulty(value: str | int | None) -> int | None:
    """Accept either a difficulty name or the raw id WCL uses."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = value.strip().lower()
    if text.isdigit():
        return int(text)
    if text not in DIFFICULTY_IDS:
        known = ", ".join(sorted(DIFFICULTY_IDS))
        raise ConfigError(f"unknown difficulty {value!r}; known names are: {known}")
    return DIFFICULTY_IDS[text]


def select_fights(
    fights: list[Fight],
    encounter: str | int | None = None,
    difficulty: str | int | None = None,
    fight_ids: list[int] | None = None,
) -> list[Fight]:
    """Narrow a fights list by encounter, difficulty and explicit ids.

    ``encounter`` matches the encounter id when it is a number, and otherwise
    is a case-insensitive substring of the boss name.
    """
    selected = list(fights)

    if fight_ids:
        selected = [f for f in selected if f.id in set(fight_ids)]

    if encounter is not None:
        text = str(encounter).strip()
        if text.isdigit():
            wanted_id = int(text)
            selected = [f for f in selected if f.encounter_id == wanted_id]
        else:
            needle = text.lower()
            selected = [f for f in selected if needle in f.name.lower()]

    wanted_difficulty = parse_difficulty(difficulty)
    if wanted_difficulty is not None:
        selected = [f for f in selected if f.difficulty == wanted_difficulty]

    return selected


def number_pulls(fights: list[Fight]) -> None:
    """Number pulls within the report, per encounter and difficulty."""
    counters: dict[tuple[int, int | None], int] = {}
    for fight in sorted(fights, key=lambda f: f.start_ms):
        key = (fight.encounter_id, fight.difficulty)
        counters[key] = counters.get(key, 0) + 1
        fight.pull_number = counters[key]


def parse_report(payload: dict[str, Any], fetched_at: str) -> ReportMeta:
    region = (payload.get("region") or {}).get("slug")
    master = payload.get("masterData") or {}

    actors: dict[int, Actor] = {}
    for raw in master.get("actors") or []:
        actors[raw["id"]] = Actor(
            id=raw["id"],
            game_id=raw.get("gameID"),
            name=raw.get("name") or f"actor-{raw['id']}",
            type=raw.get("type") or "Unknown",
            sub_type=raw.get("subType"),
            icon=raw.get("icon"),
            server=raw.get("server"),
            pet_owner=raw.get("petOwner"),
            region=region,
        )

    for detail in iter_player_details(payload.get("playerDetails")):
        actor = actors.get(detail.get("id"))
        if actor is None:
            continue
        specs = sorted(
            detail.get("specs") or [],
            key=lambda s: s.get("count", 0),
            reverse=True,
        )
        actor.specs_played = [s["spec"] for s in specs if s.get("spec")]
        actor.role = detail.get("role")
        actor.item_level = detail.get("maxItemLevel")

    abilities = {
        raw["gameID"]: raw
        for raw in (master.get("abilities") or [])
        if raw.get("gameID")
    }

    fights = [
        Fight(
            id=raw["id"],
            encounter_id=raw.get("encounterID") or 0,
            name=raw.get("name") or "Unknown",
            difficulty=raw.get("difficulty"),
            size=raw.get("size"),
            kill=raw.get("kill"),
            start_ms=raw["startTime"],
            end_ms=raw["endTime"],
            fight_percentage=raw.get("fightPercentage"),
            boss_percentage=raw.get("bossPercentage"),
            last_phase=raw.get("lastPhase"),
            friendly_players=list(raw.get("friendlyPlayers") or []),
        )
        for raw in payload.get("fights") or []
    ]
    number_pulls(fights)

    return ReportMeta(
        code=payload["code"],
        title=payload.get("title"),
        start_time_utc=payload.get("startTime") or 0,
        end_time_utc=payload.get("endTime") or 0,
        zone_id=(payload.get("zone") or {}).get("id"),
        zone_name=(payload.get("zone") or {}).get("name"),
        owner=(payload.get("owner") or {}).get("name"),
        guild=(payload.get("guild") or {}).get("name"),
        region=region,
        fights=fights,
        actors=actors,
        abilities=abilities,
        fetched_at=fetched_at,
    )


def iter_player_details(payload: Any) -> Any:
    """Yield each player entry from the ``playerDetails`` JSON scalar.

    The payload nests as ``{"data": {"playerDetails": {"tanks": [...], ...}}}``,
    with the role as the key of each list.
    """
    if not payload:
        return
    details = (payload.get("data") or {}).get("playerDetails") or {}
    for role, entries in details.items():
        for entry in entries or []:
            yield {**entry, "role": _ROLE_NAMES.get(role, role)}


def meta_path(raw_dir: Path, code: str) -> Path:
    """Where the offline copy of a report's metadata lives."""
    return Path(raw_dir) / code / "report.json.gz"
