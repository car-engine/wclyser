# Adding an analysis module

The walkthrough for the usual task: get the data a new question needs into the
database, compute the measure, and render a page.

Read `CLAUDE.md` first. One rule there governs this whole document: **do not
build a module unprompted.** A module answers one pattern the user wants
examined, and the priorities and thresholds it applies are their judgement, not
a default. Ask for the design before writing the analysis.

`wclyser/analysis/havoc.py` is the worked example for every step below.

## 1. Settle what the module reads

`wcly streams` lists the thirteen streams and the scope each is fetched at:

```
casts  buffs  debuffs_on_self  damage_done  damage_taken  healing_received
resources  debuffs_on_enemies  deaths  interrupts  dispels  combatant_info
enemy_casts
```

A module names the ones it reads in its `requires` set, using those keys. The
runner passes them to the coverage check, so a module never sees a pull whose
data was not imported.

Two things decide whether a stream can answer the question at all, and both are
measured in `docs/wcl-api.md`:

- Under `hostilityType: Friendlies`, `sourceID` selects the subject and their
  pets whichever way the event points. The dataType carries the direction, so
  damage taken and buffs held are both filtered from the source side.
- Under `hostilityType: Enemies` the id filters return nothing, so
  `debuffs_on_enemies` and `enemy_casts` arrive raid-wide and have to be
  narrowed after import.

A question needing something the plan does not fetch means a new `Stream` in
`wclyser/fetch/scopes.py`. Each stream is fetched at exactly one scope, so two
streams can never produce a row for the same event. Adding one changes no
existing blob, because a blob records a fingerprint of the definition it was
fetched under.

## 2. Get the data in

Nothing needs importing by hand. `wcly analyse` closes a coverage gap itself
where the blobs are already cached:

```bash
wcly analyse <code> --module <key>
  imported 2214 rows from 11 cached blobs for <key>
```

That is `db.coverage.ensure`: it checks, imports what is free, and re-checks.
Only a gap with no blob stops it, and the message then names the fetch. So the
sequence for a stream that was never fetched is:

```bash
wcly sync    <code> --config configs/<saved-run>.yaml   # spends points
wcly analyse <code> --module <key>                      # cannot
```

`wcly coverage <code> --stream <name>` answers the same question without
running anything.

A module needing a column the schema lacks gets a numbered migration in
`wclyser/db/migrations/`, forward-only. `import_log` records the schema version
alongside the blob hash, so existing blobs re-import rather than leaving the
new column null. See `docs/schema.md`.

## 3. Write the module

One file, `wclyser/analysis/<key>.py`, registered by the decorator:

```python
@analysis("havoc", title="Havoc windows", requires={"casts"})
def run(ctx: Ctx) -> dict[str, Result]: ...
```

`Ctx` carries the connection, the config, the report metadata, the pulls in
focus, the subjects, and `ctx.option(...)` for flags. `ctx.spec_of(fight_id,
player_key)` gives the class and specialisation that player held for that pull.
The return maps a player key to a `Result`.

Add the import to `wclyser/analysis/__init__.py`, which is what registers it:

```python
from . import havoc  # noqa: F401  (registers the module)
```

A `Result` has seven fields and three of them matter most:

| Field | Carries |
|---|---|
| `lines` | the terminal output, printed as given |
| `metrics` | report-level numbers, one flat dictionary |
| `fight_metrics` | the same per pull, keyed by fight id |
| `payload` | the structured result the page renders |
| `warnings` | anything to flag, printed after the lines |
| `key`, `title` | identity, matching the decorator |

Every entry in `metrics` and `fight_metrics` goes to one long-format table:

```sql
metric(player_key, report_code, fight_id, analysis_key, metric_key, value)
```

A trend query reads `metric` and knows nothing about individual modules, so
emitting a number there costs no schema change. `fight_id` is `-1` for a
report-level metric.

Presentation stays in the module. A general table renderer would suit none of
them, which is why `lines` is preformatted text.

### Thresholds and priorities go in a profile

`wclyser/profiles/<class>_<spec>.yaml` holds them, and the profile is chosen per
pull from `fight_player.spec`, because a player who respecs between bosses has
one specialisation for some pulls and another for the rest. The Destruction
file carries the Havoc window length, the lead-in, both spell lists and the
shard generation rates. Changing what counts as good is an edit to that file.

## 4. Render a page

The page needs three things.

**A payload.** Build it in the module and assign `result.payload`. Carry each
time twice: relative to the pull for drawing, and relative to the report for
linking, because Warcraft Logs URLs take the report base. A link is
`?fight=<id>&type=casts&source=<actor_id>&start=<ms>&end=<ms>`, where the
milliseconds are `fight.start_ms + t_rel_ms`.

**A template**, at `wclyser/report/templates/<key>.html`. It is a whole page
with three placeholders substituted at render time:

| Placeholder | Becomes |
|---|---|
| `{{TITLE}}` | `payload["title"]`, the browser tab and gallery name |
| `{{FOOTER_NOTE}}` | a provenance line |
| `/*__DATA__*/` | the payload as JSON, inside a `<script type="application/json">` |

The page carries its own CSS, script and data, because it is sent to someone
who has none of this installed. Google Fonts is the only permitted external
request. Closing tags in the embedded JSON are escaped, so a spell name
carrying markup cannot break out of the script element.

**A registration**, in `TEMPLATES` in `wclyser/report/html.py`:

```python
TEMPLATES = {"havoc": "havoc.html"}
```

Templates are per module rather than generic. Pages land at
`reports/<night>_<report>/<player>_<module>.html`.

```bash
wcly analyse <code> --module <key> --html
```

Package data already covers `report/templates/*.html` and `profiles/*.yaml`, so
a new file in either directory installs without a `pyproject.toml` edit.

## 5. Extend the offline checks

`tests/offline_check.py` needs no credentials and no network. Add a section for
the new module and call it from `main`. What is worth covering, from what the
Havoc checks caught:

- The pure functions that carve the data up, on hand-built events. Window
  construction, boundary cases, and truncation were all wrong once.
- The profile loading, so a renamed key fails here rather than mid-analysis.
- Every name the command line reaches for. A missing export from
  `wclyser.report` once raised `AttributeError` after a whole analysis had run.

```bash
python tests/offline_check.py
```

## 6. Check the result against the log

The measure has to survive contact with real data before it is trusted. Two
findings from the Havoc module, both of which looked like bugs and were not:

- 22 casts inside a 20-second window seemed impossible. The `begincast` events
  showed why: a hard cast holds the global cooldown from its start, so a queued
  instant fires in the same millisecond the cast completes.
- Pet damage appeared under the player. That is `source_key` resolving a pet to
  its owner, which is right for damage and would have been wrong for a cast
  count.

Compare a suspect number against the raw blob under `data/raw/` before changing
any code. A blob and the database agreeing means the API said it.

Where a measure is modelled rather than observed, the output says so. The shard
entry level is extrapolated, so the page and the terminal both note that
critical strikes are ignored and the passive tick is assumed to be running.
