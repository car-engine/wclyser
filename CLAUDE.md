# Working in this repository

Personal Warcraft Logs analysis toolkit. Read `README.md` for the architecture
and `LOG.md` for what has been done and what is outstanding.

## Commands

```bash
pip install -e .                     # into .venv
python tests/offline_check.py        # no credentials, no network
wcly fights <report_code>            # pulls, with the focus marked
wcly players <report_code>           # roster and resolved subjects
wcly fetch <report_code>             # fetch and cache, narrowed by the focus
wcly import <report_code>            # cached blobs into SQLite, no credentials
wcly coverage <report_code>          # what is needed against what is imported
wcly prune <report_code>             # drop streams the import list no longer wants
wcly budget                          # hourly API point budget
```

Run the offline checks after any change to the fetch layer. They cover
pagination, the cache-first rule, the finality rule and subject resolution.

## Rules

**Credentials.** Read from the environment, then a gitignored `.env`. Never
write a credential into a source file, a config file, a test or a commit
message. `.env` and `wcl_casts_export.py` are both gitignored; keep them that
way.

**Do not build analysis modules unprompted.** This is a tool for specific
patterns the user wants examined, and each module needs their design input
first. Generic output — casts per minute, activity percentage, damage rankings
— is what Warcraft Logs already shows on its own site, and is not wanted here.
Ask before writing anything in the analysis layer.

**Verify against a real report before moving up a layer.** The API's filter
behaviour is not what the field names suggest, and several definitions were
wrong until they were checked against live data. See `docs/wcl-api.md`.

**Fetch wide, import narrow.** Fetching pulls every stream, because a blob
costs points once and then sits on disk. `import.streams` decides what reaches
the database, because rows cost space in a file meant to grow across a tier.
Never widen `import.streams` to make an analysis pass; the analysis declares
what it reads and `db.coverage.require` reports the gap.

## Invariants

Breaking any of these breaks the live-watch design, which depends on the fetch
layer being genuinely cache-first.

- `(report_code, fight_id, stream, subject)` is the unit of fetching, caching
  and import replacement. Import replaces exactly the rows one blob produced.
- A cache hit never reaches the network.
- The fights list is never served from cache. It is the only part of a report
  that changes.
- A blob is written only for a fight that has certainly finished. An
  in-progress pull is fetched and returned, and not written.
- Each stream is fetched at exactly one scope, so two streams can never
  produce a row for the same event.
- An analysis never answers over missing rows. It declares the streams it
  reads and calls `db.coverage.require`, which raises with the command to run.
- `import_log` records the blob hash and the schema version. Both must match
  for import to skip the work, because a migration that adds a column leaves
  existing rows null in it.
- A blob records a fingerprint of the stream definition it was fetched under.
  Change a stream's dataType, hostility or filter side and the fingerprint
  changes, so old blobs are refetched instead of being read with the wrong
  meaning. Never loosen this check to avoid a refetch.

## Cost

Roughly 14 API points and 300 KB per pull per subject, against a budget of
3600 points an hour. A full night of 23 mythic pulls for one subject took 322
points, 2m 6s and 7.0 MB. Fetching is cheap enough not to optimise, and the
`focus` block in `config.yaml` narrows it further.

## Style

The user's global instructions in `~/.claude/CLAUDE.md` govern all prose,
including code comments, commit messages and printed output. British spelling
in prose; never in identifiers, API tokens or string literals the machine
reads.
