# wcl-analyser

Personal Warcraft Logs raid performance analysis. Fetches raw event data for
chosen players, accumulates it across raid nights, and reports on how the play
changed.

Built so far: the client and fetch layers. The normalise, analysis and report
layers are described below but not written.

## Setup

Create an API client at https://www.warcraftlogs.com/api/clients/. Copy
`.env.example` to `.env` and fill in the client id and secret. `.env` is
gitignored. An exported `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET` take
precedence over the file when both are present.

```bash
pip install -e .
```

Then `cp config.example.yaml config.yaml` and edit. `config.yaml` is
gitignored, and holds the players and the pulls under study.

## Commands

```bash
wcla budget                                    # hourly API point budget
wcla fights <report_code>                      # pulls, with kill/wipe and pull numbers
wcla players <report_code>                     # roster, specs played, resolved subjects
wcla streams                                   # the fetch plan
wcla fetch <report_code> --player <name>       # fetch and cache raw events
```

`import`, `analyse`, `report` and `watch` arrive with the layers above.

Offline checks, needing no credentials and no network:

```bash
python tests/offline_check.py
```

## Other documents

- [CLAUDE.md](CLAUDE.md) — conventions and invariants for anyone working here.
- [LOG.md](LOG.md) — what has been built, and what is outstanding.
- [docs/wcl-api.md](docs/wcl-api.md) — measured API behaviour.

## Architecture

### The fetch unit

`(report_code, fight_id, stream, subject)` is the unit of fetching, of caching
and of import replacement. One paginated call sequence produces one gzipped
blob at
`data/raw/<report>/<fight_id:04d>/events__<stream>__<subject>.json.gz`, and
import replaces exactly the rows that blob produced. Re-running any of the
three is therefore idempotent without needing a natural key over event fields.

`subject` is a player key such as `Nevaritius-Frostmourne-US`, or the sentinel
`__raid__` or `__enemy__` for the streams that are not filtered to one player.
Blobs are per subject, so adding a third warlock to the subject list fetches
only that warlock instead of invalidating the other two.

### Cache rules

A completed fight is immutable, so a cache hit never reaches the network. Two
rules keep that safe:

- The fights list is never served from cache. It is the only part of a report
  that changes, and the watcher depends on seeing new pulls.
- A blob is written only for a fight that has certainly finished: any fight but
  the newest, or the newest once the report has run on past its end by
  `finality_margin_s`. An in-progress pull is still fetched and returned, just
  not written.

Each blob records a fingerprint of the stream definition it was fetched under.
Changing a stream's dataType, hostility or filter side changes the fingerprint,
and blobs carrying the old one are refetched instead of being read back with
the wrong meaning.

Those rules make the planned `watch` command a loop over `report_meta` and
`fetch_fight` with no fetching logic of its own.

### Verified API filter semantics

The event filter fields do not mean what their names suggest. Under
`hostilityType: Friendlies`, `sourceID` selects the subject player and their
pets whichever way the event points, and the dataType carries the direction.
Under `hostilityType: Enemies` the id filters are unusable and streams have to
be fetched raid-wide.

Full measurements are in [docs/wcl-api.md](docs/wcl-api.md). Read it before
changing a stream definition.

### Specialisation is per fight

`masterData` reports only the class for a player who changed specialisation
during the report. In the sample night, Nevaritius played Demonology for pulls
1 to 12 and Destruction for pulls 13 to 23.

`playerDetails` gives every specialisation a player was seen in, which is
enough to resolve subjects before any events are fetched. The specialisation
that applies to a given pull comes from the `specID` on that fight's
`CombatantInfo` event, so the analysis layer must key the specialisation
profile on `(report, fight, player)`.

### Planned SQLite schema

Dimension tables `report`, `fight`, `actor`, `ability`, plus `fight_player`
holding the per-fight specialisation, role, item level and talents.

One wide `event` table rather than one table per event type. Analysis crosses
types constantly: what was cast in the seconds before a death, whether a
defensive was up when a boss ability landed. A single timeline ordered by
`t_rel_ms` makes each of those one query. `t_rel_ms` and `source_key` are
denormalised onto every row because they are the axes almost every query uses.

`import_log(report_code, fight_id, stream, subject, blob_sha256, rows,
imported_at)` lets import skip a blob whose hash has not changed.

Pre-aggregated `table()` and `graph()` responses are stored separately in
`table_snapshot`, with typed projections extracted as needed.

### Analysis modules

Each module declares the streams it needs and returns a title, summary lines,
an optional table, and a flat `metrics` dictionary. Every metric is written to
one long-format table:

```sql
metric(player_key, report_code, fight_id, analysis_key, metric_key, value)
```

Trend comparison, night-over-night reporting and history all read `metric` and
know nothing about individual modules, so a new module needs no schema change
and no edit to the report layer.

Specialisation knowledge — cooldown list and durations, defensives, expected
buffs — lives in a data file rather than in module code, so one generic module
covers cooldown usage against theoretical maximum for any specialisation.

## Measured cost

One mythic raid night, 23 pulls, one subject, all 13 streams:

| | |
|---|---|
| Events | 441,239 |
| Blobs | 299 |
| Requests | 308 |
| API points | 322 of 3600 per hour |
| Wall clock | 2m 6s |
| Disk, gzipped | 7.0 MB |
| Re-run, fully cached | 3.3s, 2 requests |

At roughly 7 MB per night, a full tier of raid nights is a few hundred
megabytes of raw JSON.
