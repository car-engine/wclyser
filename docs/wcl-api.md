# Warcraft Logs v2 API notes

Behaviour established against report `dYGnBvDgfjcTNqKX`, a 23-pull mythic
night. Several of these are not what the field names suggest, and each one was
wrong in the first version of the fetch plan.

## Endpoints

- Token: `POST https://www.warcraftlogs.com/oauth/token`, grant type
  `client_credentials`, HTTP basic auth with the client id and secret.
- GraphQL: `https://www.warcraftlogs.com/api/v2/client`.

## Event filters

**`sourceID` selects the subject player and their pets, whichever way the
event points. The dataType carries the direction.**

Measured on fight 1, subject actor id 5:

| Query | Events | Sources returned | Targets returned |
|---|---|---|---|
| `DamageTaken` / Friendlies / `sourceID: 5` | 862 | boss, adds, self | the subject |
| `DamageTaken` / Friendlies / `targetID: 5` | 509 | self only | self only |
| `Debuffs` / Friendlies / `sourceID: 5` | 132 | boss, adds | the subject |
| `Debuffs` / Friendlies / `targetID: 5` | 6 | self only | self only |
| `Buffs` / Friendlies / `sourceID: 5` | 2144 | healers, self | the subject |
| `Buffs` / Friendlies / `targetID: 5` | 2271 | self only | the subject and pets |

So `sourceID` gives the auras sitting on the subject and the damage the
subject took, with the real applier or attacker still in each event's own
`sourceID` field. `targetID` gives what the subject applied to others.

`DamageDone` with `sourceID` includes pet damage, which for a Demonology
Warlock is most of it: Wild Imps and Dreadstalkers appear as sources.

`Healing` is the one stream in the fetch plan filtered from the target side,
which makes it healing received. A healer subject would want the source side.

## Enemies are not filterable by actor id

Under `hostilityType: Enemies` the id filters resolve against enemy indices
the report does not expose. Every filtered form returned zero rows, including
`targetID: 31` where 31 is the boss's own masterData actor id. Unfiltered, the
same query returned 7,650 debuff events with correct source and target names.

Debuffs on enemies and enemy casts are therefore fetched raid-wide and
narrowed at import. See the open item in `LOG.md`.

## filterExpression

Returned zero rows in every form tried — `source.id = 5`, `target.id = 5` —
on streams where the equivalent id filter returned thousands. Not usable as an
escape hatch at present.

## useAbilityIDs and useActorIDs

Leave both at their default of true. Events then carry `sourceID`, `targetID`
and `abilityGameID` as integers, resolvable from masterData, and stay compact.

Setting them false embeds whole actor and ability objects in every event and
**removes** the id fields. The prototype set them false and then looked up
`ev["sourceID"]`, so every lookup missed and the exported CSV held Python dict
reprs in its source column and blanks in its ability id column.

## Specialisation is per fight

`masterData.actors[].icon` is `Class-Spec` for a player who held one
specialisation for the whole report, and bare `Class` for a player who
changed. Four of 21 players in the sample report showed the bare form.

Two reliable sources, both giving specialisation names rather than ids, so a
specialisation added in a later patch does not fall out of them:

- `playerDetails(startTime, endTime, killType)` for the whole report returns
  `specs: [{spec, count}]` per player, which is enough to resolve subjects
  before any events are fetched.
- `playerDetails(fightIDs: [id])` for one pull gives that pull's
  specialisation. One cheap request per fight, cached.

`CombatantInfo` events carry a numeric `specID` per fight and agree with the
above: Nevaritius shows 266 (Demonology) on pulls 1 to 12 and 267
(Destruction) on pulls 13 to 23.

The `talents` array on CombatantInfo is empty in this expansion's data.
`talentTree` may hold it instead; unverified.

## Fields worth knowing

- `ReportFight.friendlyPlayers` lists the actor ids present for that pull, so
  a subject absent from a pull can be skipped rather than producing an empty
  result.
- `Report.region { slug }` is needed to build a stable player key across
  reports, since two characters can share a name on different servers.
- `Report.startTime` is unix milliseconds. Every event and fight timestamp is
  milliseconds relative to that.
- Difficulty 5 is Mythic, confirmed. The other ids in
  `wcl_analyser/fetch/meta.py` are assumed.

## Rate limit

`rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn }`. The budget
observed is 3600 points an hour. A pull costs roughly 14 points across all 13
streams for one subject, so a full night costs around 320.
