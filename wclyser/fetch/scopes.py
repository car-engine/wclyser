"""The fetch plan: which dataTypes are pulled, and at what scope.

Invariant: every stream is fetched at exactly one scope. A stream scoped to
subjects is never also fetched raid-wide, so the delete-and-replace import can
never produce two rows for the same event.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

RAID_SUBJECT = "__raid__"
ENEMY_SUBJECT = "__enemy__"


class Scope(str, Enum):
    SUBJECT = "subject"
    RAID = "raid"
    ENEMY = "enemy"


class FilterSide(str, Enum):
    SOURCE = "source"
    TARGET = "target"
    NONE = "none"


@dataclass(frozen=True)
class Stream:
    """One fetchable stream of events.

    ``key`` is the cache and import identity, and is not always the dataType:
    debuffs the subject applies and debuffs sitting on the subject are the same
    dataType filtered from opposite sides, so they get separate keys.
    """

    key: str
    data_type: str
    scope: Scope
    hostility: str
    filter_side: FilterSide = FilterSide.NONE

    @property
    def is_subject_scoped(self) -> bool:
        return self.scope is Scope.SUBJECT

    @property
    def fingerprint(self) -> str:
        """Identity of this stream's definition.

        A cached blob records the fingerprint it was fetched under. Changing a
        stream's dataType, hostility or filter side changes the fingerprint, so
        blobs fetched under the old definition are refetched rather than read
        back with the wrong meaning.
        """
        parts = f"{self.data_type}|{self.hostility}|{self.filter_side.value}|{self.scope.value}"
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:12]


DEFAULT_PLAN: tuple[Stream, ...] = (
    # Per-subject streams. Verified against a live report: with hostilityType
    # Friendlies, sourceID selects the subject player and their pets whichever
    # way the event points, and the dataType carries the direction. So damage
    # taken by the subject and buffs sitting on the subject are both filtered
    # with sourceID, and the returned events still name the real attacker or
    # buff applier.
    Stream("casts", "Casts", Scope.SUBJECT, "Friendlies", FilterSide.SOURCE),
    Stream("buffs", "Buffs", Scope.SUBJECT, "Friendlies", FilterSide.SOURCE),
    Stream("debuffs_on_self", "Debuffs", Scope.SUBJECT, "Friendlies", FilterSide.SOURCE),
    Stream("damage_done", "DamageDone", Scope.SUBJECT, "Friendlies", FilterSide.SOURCE),
    Stream("damage_taken", "DamageTaken", Scope.SUBJECT, "Friendlies", FilterSide.SOURCE),
    # Healing is the one stream filtered from the target side, which makes this
    # healing received. Flip to SOURCE for a healer subject.
    Stream("healing_received", "Healing", Scope.SUBJECT, "Friendlies", FilterSide.TARGET),
    Stream("resources", "Resources", Scope.SUBJECT, "Friendlies", FilterSide.SOURCE),
    # Debuffs on enemies cannot be narrowed to one applier: under hostilityType
    # Enemies the id filters resolve against enemy indices the report does not
    # expose, and every filtered form returns nothing. Fetched raid-wide and
    # narrowed at import instead. Roughly 7-8k events per mythic pull.
    Stream("debuffs_on_enemies", "Debuffs", Scope.ENEMY, "Enemies"),
    # Raid-wide streams. Low volume, and they carry the context a single
    # player's own events cannot: who else covered an interrupt, who died
    # alongside me, what everyone was talented into.
    Stream("deaths", "Deaths", Scope.RAID, "Friendlies"),
    Stream("interrupts", "Interrupts", Scope.RAID, "Friendlies"),
    Stream("dispels", "Dispels", Scope.RAID, "Friendlies"),
    Stream("combatant_info", "CombatantInfo", Scope.RAID, "Friendlies"),
    # The boss timeline, used to align defensives against incoming abilities.
    Stream("enemy_casts", "Casts", Scope.ENEMY, "Enemies"),
)

BY_KEY: dict[str, Stream] = {s.key: s for s in DEFAULT_PLAN}


def subject_slug(stream: Stream, player_key: str | None) -> str:
    """The cache and import identity of a stream's subject slot."""
    if stream.scope is Scope.SUBJECT:
        if not player_key:
            raise ValueError(f"stream {stream.key} needs a subject")
        return player_key
    if stream.scope is Scope.ENEMY:
        return ENEMY_SUBJECT
    return RAID_SUBJECT


def resolve_streams(keys: list[str] | None) -> list[Stream]:
    """Select streams by key, defaulting to the whole plan."""
    if not keys:
        return list(DEFAULT_PLAN)
    unknown = [k for k in keys if k not in BY_KEY]
    if unknown:
        known = ", ".join(sorted(BY_KEY))
        raise KeyError(f"unknown stream(s) {unknown}; known keys are: {known}")
    return [BY_KEY[k] for k in keys]
