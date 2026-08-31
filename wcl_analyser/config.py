"""Configuration loading.

Credentials come from the environment only. Everything else comes from a YAML
file, searched for at ``$WCL_ANALYSER_CONFIG``, then ``./config.yaml``, then
``~/.config/wcl-analyser/config.yaml``. A missing file is not an error; the
defaults below are usable on their own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
API_URL = "https://www.warcraftlogs.com/api/v2/client"

_CONFIG_NAMES = ("config.yaml", "config.yml")


@dataclass
class SubjectRules:
    """How to decide which players a report is analysed for.

    Resolution order is command line, then ``by_report``, then ``auto_select``,
    then ``default``. The first source that yields any player wins.
    """

    default: list[str] = field(default_factory=list)
    by_report: dict[str, list[str]] = field(default_factory=dict)
    auto_select_class: str | None = None
    auto_select_spec: str | None = None


@dataclass
class FocusRules:
    """Narrows a fetch to the encounter and specialisation under study.

    Any of these may be left unset, in which case it does not narrow anything.
    """

    encounter: str | int | None = None
    difficulty: str | int | None = None
    spec: str | None = None

    def describe(self) -> str:
        parts = [
            f"{label} {value}"
            for label, value in (
                ("encounter", self.encounter),
                ("difficulty", self.difficulty),
                ("spec", self.spec),
            )
            if value is not None
        ]
        return ", ".join(parts) or "everything"


@dataclass
class FetchSettings:
    encounters_only: bool = True
    event_limit: int = 10000
    include_resources: bool = True
    # Seconds a fight must have been over before its events are cached.
    finality_margin_s: float = 30.0
    # Refuse to start a fetch when fewer than this many points remain.
    point_floor: int = 200


@dataclass
class ImportSettings:
    """What goes into the database.

    Fetching stays wide because a blob costs API points once and then sits on
    disk at a few hundred kilobytes. Importing is narrowed because every row in
    the event table costs space in a file meant to grow across a whole tier,
    and re-importing from cache is free.
    """

    streams: list[str] | None = None


@dataclass
class HTTPSettings:
    timeout_s: float = 60.0
    max_attempts: int = 6
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 60.0


@dataclass
class Config:
    client_id: str
    client_secret: str
    data_dir: Path
    subjects: SubjectRules = field(default_factory=SubjectRules)
    focus: FocusRules = field(default_factory=FocusRules)
    fetch: FetchSettings = field(default_factory=FetchSettings)
    import_: ImportSettings = field(default_factory=ImportSettings)
    http: HTTPSettings = field(default_factory=HTTPSettings)
    source_path: Path | None = None
    # Overrides the database location, for a side-by-side rebuild or a test.
    database: Path | None = None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def db_path(self) -> Path:
        return self.database or self.data_dir / "wcl.sqlite"


def find_config_file(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        return path
    env = os.environ.get("WCL_ANALYSER_CONFIG")
    if env:
        path = Path(env)
        if not path.is_file():
            raise ConfigError(f"WCL_ANALYSER_CONFIG points at a missing file: {path}")
        return path
    candidates = [Path.cwd() / name for name in _CONFIG_NAMES]
    candidates += [
        Path.home() / ".config" / "wcl-analyser" / name for name in _CONFIG_NAMES
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` file. Blank lines and ``#`` comments are skipped."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_credentials(config_path: Path | None) -> tuple[str, str]:
    """Read the client credentials.

    A real environment variable wins. Otherwise a ``.env`` file is read, from
    beside the config file first and then the working directory. The values are
    never written back into ``os.environ``, so they cannot leak into a child
    process.
    """
    client_id = os.environ.get("WCL_CLIENT_ID", "")
    client_secret = os.environ.get("WCL_CLIENT_SECRET", "")
    if client_id and client_secret:
        return client_id, client_secret

    searched: list[Path] = []
    if config_path is not None:
        searched.append(config_path.parent / ".env")
    searched.append(Path.cwd() / ".env")

    for candidate in searched:
        if not candidate.is_file():
            continue
        values = read_dotenv(candidate)
        client_id = client_id or values.get("WCL_CLIENT_ID", "")
        client_secret = client_secret or values.get("WCL_CLIENT_SECRET", "")
        if client_id and client_secret:
            return client_id, client_secret

    where = ", ".join(str(p) for p in searched)
    raise ConfigError(
        "no client credentials. Export WCL_CLIENT_ID and WCL_CLIENT_SECRET, or "
        f"put them in a .env file ({where}). Create a client at "
        "https://www.warcraftlogs.com/api/clients/"
    )


def load_config(explicit: str | Path | None = None) -> Config:
    path = find_config_file(explicit)
    raw: dict[str, Any] = {}
    if path is not None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    client_id, client_secret = load_credentials(path)

    data_dir = Path(raw.get("data_dir", "data")).expanduser()
    if path is not None and not data_dir.is_absolute():
        data_dir = (path.parent / data_dir).resolve()
    else:
        data_dir = data_dir.resolve()

    subj = raw.get("subjects") or {}
    auto = subj.get("auto_select") or {}
    subjects = SubjectRules(
        default=list(subj.get("default") or []),
        by_report={k: list(v) for k, v in (subj.get("by_report") or {}).items()},
        auto_select_class=auto.get("class"),
        auto_select_spec=auto.get("spec"),
    )

    focus = FocusRules(**(raw.get("focus") or {}))
    fetch = FetchSettings(**(raw.get("fetch") or {}))
    import_ = ImportSettings(**(raw.get("import") or {}))
    http = HTTPSettings(**(raw.get("http") or {}))

    return Config(
        client_id=client_id,
        client_secret=client_secret,
        data_dir=data_dir,
        subjects=subjects,
        focus=focus,
        fetch=fetch,
        import_=import_,
        http=http,
        source_path=path,
    )
