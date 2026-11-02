"""Konfiguration der Pipeline (YAML, geprüft mit pydantic).

Aufbau:

* ``pipeline.yml`` enthält globale Einstellungen (Datenbank, Zeitplan, Bericht, HTTP).
* Jede Datenquelle steht in einer eigenen YAML-Datei im Verzeichnis ``sources_dir``
  (Standard: ``sources/`` neben ``pipeline.yml``). Eine neue Quelle erfordert nur
  eine neue Datei, keine Codeänderung. Die Datei legt auch fest, in welche
  vorhandene Tabelle und Spalten die Quelle schreibt (``target``) und woran ihre
  Zeilen dort erkennbar sind (``source_column``/``source_value``).

In allen Zeichenketten werden ``${VAR}`` und ``${VAR:-standardwert}`` durch
Umgebungsvariablen ersetzt. Zugangsdaten gehören nie in die YAML-Dateien.
"""

from __future__ import annotations

import codecs
import os
import re
import string
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from geoimport.cron import crontab_trigger
from geoimport.geometry import UnknownCrsError, srid_from_crs

DEFAULT_CONFIG_PATH = "config/pipeline.yml"
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

#: Spaltenbelegung mit dem fachlichen Schlüssel eines Datensatzes
KEY_REF = "@key"

#: Platzhalter, die in ``metadata_record`` erlaubt sind
METADATA_PLACEHOLDERS = (
    "id",
    "title",
    "description",
    "provider",
    "licence",
    "licence_url",
    "attribution",
    "update_cycle",
    "url",
    "layer",
    "target_table",
    "source_value",
    "records",
    "run_id",
    "run_time",
)


class ConfigError(Exception):
    """Fehler in der Konfiguration."""


def expand_env(value: Any) -> Any:
    """Ersetzt ``${VAR}`` und ``${VAR:-default}`` rekursiv in Zeichenketten."""
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ConfigError(f"Umgebungsvariable {name} ist nicht gesetzt")

        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _check_crs(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        srid_from_crs(value)
    except UnknownCrsError as exc:
        raise ValueError(str(exc)) from exc
    return value


def _check_cron(value: str | None, timezone: str = "Europe/Berlin") -> str | None:
    if value is None:
        return None
    try:
        crontab_trigger(value, timezone)
    except ValueError as exc:
        raise ValueError(f"ungültiger Cron-Ausdruck {value!r}: {exc}") from exc
    return value


def split_table_name(name: str) -> tuple[str | None, str]:
    """``"schema.tabelle"`` oder ``"tabelle"``; Tabellennamen dürfen Leerzeichen enthalten."""
    schema, dot, table = name.partition(".")
    if dot and _SCHEMA_NAME.match(schema) and table:
        return schema, table
    return None, name


# ------------------------------------------------------------------ Ziel


Constant = str | int | float | bool | None


class TargetSettings(_Model):
    """Vorhandene Zieltabelle einer Quelle und die Belegung ihrer Spalten.

    Die Pipeline ändert die Struktur der Tabelle nie. Bei jedem erfolgreichen Lauf
    ersetzt sie die Zeilen dieser Quelle: Zeilen mit ``source_column = source_value``
    werden gelöscht und aus dem neuen Abruf eingefügt, Zeilen anderer Quellen
    bleiben unberührt. Gehört eine Tabelle allein dieser Quelle (``exclusive``),
    werden alle Zeilen ersetzt.
    """

    table: str = Field(min_length=1)
    source_column: str | None = None
    source_value: str | None = None
    exclusive: bool = False
    #: Zielspalte -> Attribut nach dem Mapping (``attributes``) oder ``@key``
    columns: dict[str, str] = Field(default_factory=dict)
    #: Zielspalte -> fester Wert
    constants: dict[str, Constant] = Field(default_factory=dict)
    geometry_column: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> TargetSettings:
        if self.exclusive and self.source_column:
            raise ValueError("exclusive und source_column schließen sich aus")
        if not self.exclusive and not self.source_column:
            raise ValueError(
                "source_column angeben (Spalte mit der Quellenangabe) oder exclusive: true "
                "setzen, wenn die Tabelle nur dieser Quelle gehört"
            )
        if self.source_value is not None and not self.source_column:
            raise ValueError("source_value setzt source_column voraus")
        if not self.columns and not self.geometry_column:
            raise ValueError("mindestens eine Spalte (columns) oder geometry_column angeben")
        used: dict[str, str] = {}
        for kind, names in (
            ("columns", list(self.columns)),
            ("constants", list(self.constants)),
            ("source_column", [self.source_column] if self.source_column else []),
            ("geometry_column", [self.geometry_column] if self.geometry_column else []),
        ):
            for name in names:
                if name in used:
                    raise ValueError(f"Spalte {name!r} ist in {used[name]} und {kind} belegt")
                used[name] = kind
        return self

    def column_names(self) -> list[str]:
        names = [*self.columns, *self.constants]
        if self.source_column:
            names.append(self.source_column)
        if self.geometry_column:
            names.append(self.geometry_column)
        return names


class MetadataRecord(_Model):
    """Eine Zeile je Quelle in einer Metadatentabelle (z. B. DCAT-AP ``table_metadata``).

    ``key_columns`` bestimmen die Zeile (sie wird je Lauf ersetzt), ``columns`` die
    übrigen Werte. Alle Werte sind Vorlagen mit Platzhaltern wie ``{title}`` oder
    ``{run_time}`` (siehe ``METADATA_PLACEHOLDERS``).
    """

    table: str = Field(default="table_metadata", min_length=1)
    key_columns: dict[str, str] = Field(min_length=1)
    columns: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _templates(self) -> MetadataRecord:
        overlap = set(self.key_columns) & set(self.columns)
        if overlap:
            raise ValueError(f"Spalten doppelt belegt: {', '.join(sorted(overlap))}")
        formatter = string.Formatter()
        for column, template in {**self.key_columns, **self.columns}.items():
            try:
                fields = [f for _, f, _, _ in formatter.parse(template) if f is not None]
            except ValueError as exc:
                raise ValueError(f"{column}: ungültige Vorlage ({exc})") from exc
            unknown = [f for f in fields if f not in METADATA_PLACEHOLDERS]
            if unknown:
                raise ValueError(
                    f"{column}: unbekannte Platzhalter {', '.join(unknown)} "
                    f"(erlaubt: {', '.join(METADATA_PLACEHOLDERS)})"
                )
        return self


# ------------------------------------------------------------------ Quellen


class SourceMetadata(_Model):
    """Fachliche Metadaten einer Quelle (Bericht, ``metadata_record``, Status)."""

    provider: str
    licence: str
    licence_url: str | None = None
    attribution: str | None = None
    update_cycle: str | None = None
    description: str | None = None


class LonLatGeometry(_Model):
    """Punktgeometrie aus zwei Spalten (Rechts-/Hochwert bzw. Länge/Breite)."""

    type: Literal["lonlat"]
    lon: str = Field(validation_alias=AliasChoices("lon", "x"))
    lat: str = Field(validation_alias=AliasChoices("lat", "y"))


class WktGeometry(_Model):
    """Geometrie aus einer WKT-Spalte."""

    type: Literal["wkt"]
    column: str


CsvGeometry = Annotated[LonLatGeometry | WktGeometry, Field(discriminator="type")]


class SourceBase(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,62}$")
    type: str
    title: str | None = None
    enabled: bool = True
    #: Fachlicher Schlüssel: eine oder mehrere Spalten; ``@id`` = Objekt-ID des Dienstes
    key_fields: list[str] = Field(validation_alias=AliasChoices("key_fields", "key_field"))
    attributes: dict[str, str] = Field(default_factory=dict)
    keep_unmapped: bool = False
    source_crs: str | None = None
    duplicate_keys: Literal["warn", "merge", "error"] = "warn"
    #: Anteil gelöschter Objekte, ab dem der Lauf der Quelle abgewiesen wird
    max_delete_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Anteil übersprungener Datensätze, bis zu dem der Abruf noch als vollständig gilt
    max_skipped_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    repair_invalid_geometries: bool = False
    #: Eigener Rhythmus als Cron-Ausdruck; ohne Angabe gilt ``schedule.cron``
    schedule: str | None = None
    target: TargetSettings
    metadata_record: MetadataRecord | None = None
    metadata: SourceMetadata

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)
    _origin: str | None = PrivateAttr(default=None)

    @field_validator("key_fields", mode="before")
    @classmethod
    def _key_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("key_fields")
    @classmethod
    def _key_not_empty(cls, value: list[str]) -> list[str]:
        if not value or any(not v.strip() for v in value):
            raise ValueError("key_fields darf nicht leer sein")
        if len(set(value)) != len(value):
            raise ValueError("key_fields enthält doppelte Felder")
        return value

    @field_validator("source_crs")
    @classmethod
    def _crs_valid(cls, value: str | None) -> str | None:
        return _check_crs(value)

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, value: str | None) -> str | None:
        return _check_cron(value)

    @model_validator(mode="after")
    def _target_columns_known(self) -> SourceBase:
        known = set(self.attributes.values()) if self.attributes else None
        for column, attr in self.target.columns.items():
            if attr == KEY_REF:
                continue
            if known is not None and attr not in known and not self.keep_unmapped:
                raise ValueError(
                    f"target.columns.{column}: Attribut {attr!r} kommt im Mapping "
                    f"(attributes) nicht vor"
                )
        return self

    @property
    def location(self) -> str | None:
        """URL oder Pfad der Quelle (für Bericht und Metadaten)."""
        return getattr(self, "url", None) or getattr(self, "path", None)

    @property
    def layer(self) -> str | None:
        return getattr(self, "type_name", None)

    @property
    def origin(self) -> str | None:
        return self._origin

    @property
    def source_value(self) -> str | None:
        """Wert der Quellenspalte, an dem die Zeilen dieser Quelle erkennbar sind."""
        if not self.target.source_column:
            return None
        return self.target.source_value or self.id

    @property
    def needs_geometry(self) -> bool:
        return self.target.geometry_column is not None


class WfsSource(SourceBase):
    type: Literal["wfs"]
    url: str
    type_name: str = Field(validation_alias=AliasChoices("type_name", "typeName", "layer"))
    version: Literal["auto", "2.0.0", "1.1.0"] = "auto"
    output_format: str = "auto"
    srs_name: str | None = None
    geometry_field: str | None = None
    page_size: int = Field(default=1000, ge=1)
    paging: Literal["auto", "always", "never"] = "auto"
    #: Nur zum Testen: ein begrenzter Abruf gilt als unvollständig und wird nicht übernommen
    max_features: int | None = Field(default=None, ge=1)
    sort_by: str | None = None
    bbox: list[float] | None = None
    bbox_crs: str | None = None
    filter: str | None = None
    namespaces: dict[str, str] = Field(default_factory=dict)
    extra_params: dict[str, str] = Field(default_factory=dict)
    use_advertised_urls: bool = False

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url muss mit http:// oder https:// beginnen")
        return value

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, value: list[float] | None) -> list[float] | None:
        if value is not None and len(value) != 4:
            raise ValueError("bbox benötigt genau vier Werte: minx, miny, maxx, maxy")
        return value

    @field_validator("srs_name", "bbox_crs")
    @classmethod
    def _srs(cls, value: str | None) -> str | None:
        return _check_crs(value)

    @model_validator(mode="after")
    def _filter_or_bbox(self) -> WfsSource:
        if self.bbox is not None and self.filter:
            raise ValueError("bbox und filter schließen sich in WFS-Anfragen aus")
        return self


class LatestYear(_Model):
    """Suche nach der neuesten Jahresdatei, wenn die URL ``{year}`` enthält."""

    first: int | None = Field(default=None, ge=1900, le=2200)  # Standard: aktuelles Jahr + 1
    back: int = Field(default=5, ge=0, le=30)


class CsvSource(SourceBase):
    type: Literal["csv"]
    path: str | None = None
    url: str | None = None
    #: Abgelegte Datei, die gelesen wird, wenn der Abruf per URL fehlschlägt
    fallback_path: str | None = None
    latest_year: LatestYear | None = None
    #: Muster (fnmatch) der CSV-Datei, wenn Pfad bzw. URL auf ein ZIP-Archiv zeigt
    archive_member: str | None = None
    delimiter: str = ","
    quotechar: str = '"'
    encoding: str = "utf-8"
    decimal: Literal[".", ","] = "."
    #: Ohne Geometrie: reine Sachdaten (z. B. Kennzahlen je Gemeinde über den AGS)
    geometry: CsvGeometry | None = None
    #: Nur Zeilen übernehmen, deren Spalten passen (Wert, Liste oder Muster wie ``12*``)
    row_filter: dict[str, str | list[str]] = Field(default_factory=dict)
    #: Spalten, die als Zahl gelesen werden (Dezimalkomma laut ``decimal``)
    numeric_fields: list[str] = Field(default_factory=list)
    #: Spalten, die mit führenden Nullen auf feste Länge gebracht werden (z. B. AGS: 8)
    zero_pad: dict[str, int] = Field(default_factory=dict)
    empty_as_null: bool = True
    source_crs: str = "EPSG:4326"

    @field_validator("delimiter", "quotechar")
    @classmethod
    def _single_char(cls, value: str) -> str:
        if value == "\\t":
            value = "\t"
        if len(value) != 1:
            raise ValueError("muss genau ein Zeichen sein")
        return value

    @field_validator("encoding")
    @classmethod
    def _encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            raise ValueError(f"unbekannte Zeichenkodierung {value!r}") from exc
        return value

    @field_validator("zero_pad")
    @classmethod
    def _pad(cls, value: dict[str, int]) -> dict[str, int]:
        for name, width in value.items():
            if not 1 <= width <= 64:
                raise ValueError(f"zero_pad.{name}: Länge zwischen 1 und 64")
        return value

    @model_validator(mode="after")
    def _path_or_url(self) -> CsvSource:
        if bool(self.path) == bool(self.url):
            raise ValueError("genau eines von path oder url angeben")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError("url muss mit http:// oder https:// beginnen")
        if self.fallback_path and not self.url:
            raise ValueError("fallback_path ist nur zusammen mit url sinnvoll")
        has_year = bool(self.url and "{year}" in self.url)
        if has_year != (self.latest_year is not None):
            raise ValueError("latest_year und der Platzhalter {year} in url gehören zusammen")
        if self.needs_geometry and self.geometry is None:
            raise ValueError("target.geometry_column gesetzt, aber keine geometry angegeben")
        return self

    def resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self._base_dir / p)

    def resolved_path(self) -> Path:
        assert self.path is not None
        return self.resolve(self.path)


SourceConfig = Annotated[WfsSource | CsvSource, Field(discriminator="type")]
_SOURCE_ADAPTER: TypeAdapter[WfsSource | CsvSource] = TypeAdapter(SourceConfig)


# ------------------------------------------------------------------ global


class DatabaseSettings(_Model):
    #: Einheitliches Ziel-KBS aller Geometrien (Vorgabe der Hochschule: EPSG:4326)
    target_srid: int = 4326
    #: Eigenes Schema der Pipeline für Steuerung und Änderungsprotokoll
    meta_schema: str = "pipeline_meta"
    dsn_env: str = "GEOIMPORT_DB_DSN"
    password_file_env: str = "GEOIMPORT_DB_PASSWORD_FILE"
    connect_timeout_seconds: int = 10

    @field_validator("target_srid")
    @classmethod
    def _srid(cls, value: int) -> int:
        _check_crs(f"EPSG:{value}")
        return value

    @field_validator("meta_schema")
    @classmethod
    def _schema(cls, value: str) -> str:
        if not _SCHEMA_NAME.match(value):
            raise ValueError("meta_schema: nur Kleinbuchstaben, Ziffern und _ (max. 63 Zeichen)")
        return value


class ScheduleSettings(_Model):
    cron: str = "0 3 * * *"
    timezone: str = "Europe/Berlin"
    run_on_start: bool = False

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unbekannte Zeitzone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _cron(self) -> ScheduleSettings:
        _check_cron(self.cron, self.timezone)
        return self


class ReportSettings(_Model):
    directory: str = "reports"
    write_latest: bool = True
    #: Zusätzlich eine lesbare Zusammenfassung als Markdown schreiben
    markdown: bool = True


class HttpSettings(_Model):
    timeout_seconds: float = Field(default=60.0, gt=0)
    retries: int = Field(default=3, ge=0, le=10)
    backoff_seconds: float = Field(default=2.0, ge=0)
    user_agent: str = "geoimport/0.2"
    verify_tls: bool = True


class PipelineConfig(_Model):
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    sources_dir: str = "sources"
    sources: list[SourceConfig] = Field(default_factory=list)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def report_dir(self) -> Path:
        p = Path(self.report.directory)
        return p if p.is_absolute() else self._base_dir / p

    def select_sources(self, ids: list[str] | None = None) -> list[WfsSource | CsvSource]:
        if not ids:
            return [s for s in self.sources if s.enabled]
        known = {s.id: s for s in self.sources}
        missing = [i for i in ids if i not in known]
        if missing:
            raise ConfigError(
                f"Unbekannte Quelle(n): {', '.join(missing)}. Bekannt: {', '.join(known)}"
            )
        return [known[i] for i in ids]

    def schedule_groups(self) -> dict[str, list[str]]:
        """Cron-Ausdruck -> IDs der aktiven Quellen mit diesem Rhythmus."""
        groups: dict[str, list[str]] = {}
        for s in self.sources:
            if s.enabled:
                groups.setdefault(s.schedule or self.schedule.cron, []).append(s.id)
        return groups


def _format_validation_error(origin: str, exc: ValidationError) -> str:
    lines = [f"{origin}: ungültige Konfiguration"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"  - {loc or '(Wurzel)'}: {err.get('msg')}")
    return "\n".join(lines)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{path}: Datei nicht lesbar ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: kein gültiges YAML ({exc})") from exc


def parse_source(data: Any, origin: str, base_dir: Path) -> WfsSource | CsvSource:
    if not isinstance(data, dict):
        raise ConfigError(f"{origin}: Quelle muss ein YAML-Objekt sein")
    try:
        source = _SOURCE_ADAPTER.validate_python(expand_env(data))
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(origin, exc)) from exc
    source._base_dir = base_dir
    source._origin = origin
    return source


def load_config(path: str | os.PathLike[str] | None = None) -> PipelineConfig:
    """Lädt ``pipeline.yml`` und alle Quelldateien und prüft sie."""
    cfg_path = Path(path or os.environ.get("GEOIMPORT_CONFIG") or DEFAULT_CONFIG_PATH)
    if not cfg_path.is_file():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {cfg_path}")
    raw = _read_yaml(cfg_path) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: Wurzel muss ein YAML-Objekt sein")
    raw = expand_env(raw)
    inline_sources = raw.pop("sources", None) or []
    try:
        cfg = PipelineConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(str(cfg_path), exc)) from exc
    cfg._base_dir = cfg_path.parent.resolve()

    sources: list[WfsSource | CsvSource] = []
    for i, item in enumerate(inline_sources):
        sources.append(parse_source(item, f"{cfg_path} (sources[{i}])", cfg._base_dir))

    src_dir = Path(cfg.sources_dir)
    if not src_dir.is_absolute():
        src_dir = cfg._base_dir / src_dir
    if src_dir.is_dir():
        files = sorted([*src_dir.glob("*.yml"), *src_dir.glob("*.yaml")])
        for file in files:
            doc = _read_yaml(file)
            items = doc.get("sources") if isinstance(doc, dict) and "sources" in doc else [doc]
            for item in items or []:
                sources.append(parse_source(item, str(file), file.parent.resolve()))

    seen: dict[str, str] = {}
    for s in sources:
        if s.id in seen:
            raise ConfigError(
                f"Quellen-ID {s.id!r} ist doppelt vergeben ({seen[s.id]}, {s.origin})"
            )
        seen[s.id] = s.origin or "?"
    for s in sources:
        if s.schedule:
            try:
                _check_cron(s.schedule, cfg.schedule.timezone)
            except ValueError as exc:
                raise ConfigError(f"{s.origin}: schedule: {exc}") from exc
    _check_target_ownership(sources)
    cfg.sources = sources
    return cfg


def _check_target_ownership(sources: list[WfsSource | CsvSource]) -> None:
    """Zwei aktive Quellen dürfen nie dieselben Zeilen einer Tabelle ersetzen."""
    by_table: dict[str, list[WfsSource | CsvSource]] = {}
    for s in sources:
        if s.enabled:
            by_table.setdefault(s.target.table, []).append(s)
    for table, users in by_table.items():
        exclusive = [s for s in users if s.target.exclusive]
        if exclusive and len(users) > 1:
            others = ", ".join(s.id for s in users if s is not exclusive[0])
            raise ConfigError(
                f"Tabelle {table!r} gehört exklusiv der Quelle {exclusive[0].id!r}, "
                f"wird aber auch von {others} beschrieben"
            )
        seen: dict[str | None, str] = {}
        for s in users:
            value = s.source_value
            if value in seen:
                raise ConfigError(
                    f"Quellen {seen[value]!r} und {s.id!r} verwenden in {table!r} denselben "
                    f"source_value {value!r}"
                )
            seen[value] = s.id
