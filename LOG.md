# Log

What has been built, and what is still open. Newest entries first.

## Open items

- **Debuffs on enemies cannot be narrowed to one applier.** Under
  `hostilityType: Enemies` the actor id filters resolve against enemy indices
  the report does not expose, and every filtered form returned zero rows.
  `filterExpression` returned zero as well. The stream is fetched raid-wide as
  a result, at roughly 7,650 events per mythic pull, and has to be narrowed at
  import. Worth revisiting: try `filterExpression` with a different syntax,
  check whether the enemy indices can be recovered from `enemyNPCs` on the
  fight, or use `table(dataType: Debuffs)` where only uptime is needed.
- **`filterExpression` returned nothing in every form tried**, including
  `source.id = 5` and `target.id = 5`, on streams where the equivalent id
  filter worked. Either the syntax is different from what was tried or the
  feature needs something this client does not have. Unresolved.
- **The difficulty id map is mostly unverified.** Only 5 = Mythic is confirmed
  against a real report. `LFR`, `Normal`, `Heroic` and `Mythic+` in
  `wcl_analyser/fetch/meta.py` are assumed and should be checked against a
  report at each difficulty.
- **CombatantInfo carries an empty `talents` array** in this expansion's data.
  Talent information may sit under `talentTree` instead. Check before building
  specialisation profiles, which need to know the talents taken.
- **The `Healing` stream is filtered from the target side**, giving healing
  received. A healer subject would want it filtered from the source side. The
  fetch plan has no per-subject stream overrides yet.
- **Analysis modules await design input.** The intent is specific patterns the
  user wants examined, not generic throughput statistics.

## 2026-08-31

Client and fetch layers built and verified against report `dYGnBvDgfjcTNqKX`,
a mythic night from 2026-08-30.

Ported from the throwaway prototype `wcl_casts_export.py`, which stays
untracked because it carries a client secret as a hardcoded default. Those
credentials were used for the verification runs and **should be rotated**.

Built:

- OAuth client-credentials flow with an on-disk token cache bound to a hash of
  the client id.
- GraphQL transport with exponential backoff on 429 and 5xx, `Retry-After`
  honoured, one transparent re-authentication on 401, and hourly point budget
  tracking.
- Cache-first fetch layer over 13 streams, keyed by
  `(report, fight, stream, subject)`.
- Encounter, difficulty and specialisation filters, driven by a `focus` block
  in `config.yaml`.
- Credentials read from environment then a gitignored `.env`.
- Offline checks with a fake client, needing no credentials.

Found, in the course of verifying:

- **The event filter fields do not mean what their names suggest.** With
  `hostilityType: Friendlies`, `sourceID` selects the subject player and their
  pets whichever way the event points; the dataType carries the direction.
  `DamageTaken` filtered by `sourceID` gave 862 events with real enemy
  attackers, where filtering by `targetID` gave 509 events of nothing but
  self-damage. Four stream definitions were wrong and were corrected. Recorded
  in `docs/wcl-api.md`.
- **Specialisation is a property of the pull, not the player.** Nevaritius
  played Demonology on pulls 1 to 12 and Destruction on pulls 13 to 23,
  confirmed by `specID` 266 then 267 on each fight's CombatantInfo. The
  schema therefore needs a `fight_player` table, and the analysis layer must
  key the specialisation profile on `(report, fight, player)`.
- **The prototype's CSV was malformed** because it set `useAbilityIDs: false`
  and `useActorIDs: false`. That embeds whole actor and ability objects in
  each event and removes the `sourceID` and `abilityGameID` fields the script
  then looked up, so the class and ability id columns came out empty and the
  source column held a Python dict. The package uses the id form.

Measured, for one mythic night of 23 pulls with one subject and all 13
streams: 441,239 events in 299 blobs, 308 requests, 322 of 3600 hourly points,
2m 6s, 7.0 MB gzipped. Re-running took 3.3 seconds and 2 requests with every
blob served from disk.

Decided:

- Analysis focus for the first trial is Nevaritius playing Destruction Warlock
  on Entombed Sentinels, Mythic. Eleven pulls, 125,035 events, already cached.
- Raw blobs are kept indefinitely. They are the only way to rebuild the
  database after a schema change without re-spending API points.

## Next

The normalise layer: SQLite schema, migrations, and `wcla import`.
