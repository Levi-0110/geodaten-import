"""Quelladapter für CSV-Dateien (lokal, per URL oder in einem ZIP-Archiv).

Unterstützt frei wählbares Trennzeichen, Zeichenkodierung und Dezimalkomma,
Geometrien aus zwei Koordinatenspalten (``type: lonlat``) oder einer WKT-Spalte
(``type: wkt``) sowie reine Sachdaten ohne Geometrie (z. B. Kennzahlen je
Gemeinde, die über den AGS mit Gemeindegeometrien verknüpft werden).

Für große Dateien wie die INKAR-Gesamtdatei wird nichts vollständig in den
Speicher gelesen: Downloads werden in eine temporäre Datei gestreamt, ZIP-Inhalte
direkt aus dem Archiv gelesen und die Zeilen einzeln weitergereicht.

Weitere Optionen: ``row_filter`` (nur passende Zeilen, z. B. ``Raumbezug:
Gemeinden``), ``numeric_fields`` (Zahlen mit Dezimalkomma), ``zero_pad`` (z. B.
AGS mit führenden Nullen), ``latest_year`` (neueste Jahresdatei suchen, URL mit
``{year}``) und ``fallback_path`` (abgelegte Datei, falls die URL nicht abrufbar
ist).
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import decimal
import fnmatch
import io
import math
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import shapely.wkt
from shapely.errors import GEOSException
from shapely.geometry import Point

from geoimport.config import CsvSource, LonLatGeometry
from geoimport.geometry import is_geographic, srid_from_crs
from geoimport.http import HttpError, redact_url
from geoimport.mapping import ID_FIELD
from geoimport.models import RawRecord
from geoimport.sources.base import SourceAdapter, SourceError, register

csv.field_size_limit(16 * 1024 * 1024)


class CsvError(SourceError):
    """Fehler beim Lesen einer CSV-Quelle."""


def _clean_number(text: str, decimal_sep: str) -> str | None:
    value = text.strip().replace(" ", "").replace(" ", "").replace(" ", "")
    if not value:
        return None
    if decimal_sep == "," and "," in value:
        value = value.replace(".", "").replace(",", ".")
    return value


def parse_number(text: str | None, decimal: str = ".") -> float | None:
    """Wandelt einen Zahlentext um; unterstützt Dezimalkomma (``decimal: ","``)."""
    if text is None:
        return None
    value = _clean_number(text, decimal)
    if value is None:
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError("keine endliche Zahl")
    return number


def parse_decimal(text: str | None, decimal_sep: str = ".") -> decimal.Decimal | None:
    """Wie :func:`parse_number`, aber exakt (``Decimal``) für Kennzahlen."""
    if text is None:
        return None
    value = _clean_number(text, decimal_sep)
    if value is None:
        return None
    try:
        number = decimal.Decimal(value)
    except decimal.InvalidOperation as exc:
        raise ValueError(f"keine Zahl: {text!r}") from exc
    if not number.is_finite():
        raise ValueError("keine endliche Zahl")
    return number


def _unique_header(raw: list[str]) -> list[str]:
    header: list[str] = []
    seen: dict[str, int] = {}
    for i, name in enumerate(raw, start=1):
        name = name.strip() or f"spalte_{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        header.append(name)
    return header


def _encoding(name: str) -> str:
    # utf-8-sig liest Dateien mit und ohne Byte-Order-Mark
    return "utf-8-sig" if name.lower().replace("-", "").replace("_", "") == "utf8" else name


@register
class CsvAdapter(SourceAdapter):
    source_type = "csv"
    config: CsvSource

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._header: list[str] | None = None

    def declared_fields(self) -> list[str] | None:
        return self._header

    # ------------------------------------------------------------ Datei beschaffen

    def _latest_url(self) -> str:
        cfg = self.config
        assert cfg.url and cfg.latest_year
        first = cfg.latest_year.first or dt.date.today().year + 1
        tried = []
        for year in range(first, first - cfg.latest_year.back - 1, -1):
            url = cfg.url.replace("{year}", str(year))
            tried.append(str(year))
            if self.http.exists(url):
                self.details["year"] = year
                return url
        raise CsvError(f"Keine Datei gefunden für die Jahre {', '.join(tried)} ({cfg.url})")

    @contextlib.contextmanager
    def _local_file(self) -> Iterator[Path]:
        cfg = self.config
        if cfg.path:
            path = cfg.resolved_path()
            if not path.is_file():
                raise CsvError(f"CSV-Datei nicht lesbar: {path} (nicht vorhanden)")
            self.details["file"] = str(path)
            yield path
            return
        with tempfile.TemporaryDirectory(prefix="geoimport-") as tmp:
            target = Path(tmp) / "download"
            try:
                url = self._latest_url() if cfg.latest_year else cfg.url
                assert url is not None
                self.details["bytes"] = self.http.download(url, target)
                self.details["url_used"] = redact_url(url)
            except (HttpError, CsvError) as exc:
                if not cfg.fallback_path:
                    raise
                fallback = cfg.resolve(cfg.fallback_path)
                if not fallback.is_file():
                    raise CsvError(
                        f"{exc} – abgelegte Ersatzdatei {fallback} ebenfalls nicht vorhanden"
                    ) from exc
                self.warnings.add("fallback_file_used", example=f"{fallback.name}: {exc}"[:300])
                self.details["fallback_file"] = str(fallback)
                yield fallback
                return
            yield target

    @contextlib.contextmanager
    def _text_stream(self, path: Path) -> Iterator[IO[str]]:
        cfg = self.config
        encoding = _encoding(cfg.encoding)
        is_zip = zipfile.is_zipfile(path)
        if cfg.archive_member and not is_zip:
            raise CsvError(f"{path.name} ist kein ZIP-Archiv, archive_member ist aber gesetzt")
        if is_zip and not cfg.archive_member:
            raise CsvError(f"{path.name} ist ein ZIP-Archiv; archive_member angeben (z. B. *.csv)")
        if not is_zip:
            with path.open("r", encoding=encoding, newline="") as fh:
                yield fh
            return
        with zipfile.ZipFile(path) as archive:
            pattern = cfg.archive_member or "*.csv"
            members = [
                m
                for m in archive.namelist()
                if not m.endswith("/") and fnmatch.fnmatch(m.rsplit("/", 1)[-1], pattern)
            ]
            if len(members) != 1:
                raise CsvError(
                    f"Im Archiv passt {'keine' if not members else 'mehr als eine'} Datei zu "
                    f"{pattern!r}" + (f": {', '.join(members[:5])}" if members else "")
                )
            self.details["archive_member"] = members[0]
            with archive.open(members[0]) as raw:
                yield io.TextIOWrapper(raw, encoding=encoding, newline="")

    # ------------------------------------------------------------ Lesen

    def read(self) -> Iterator[RawRecord]:
        with self._local_file() as path, self._text_stream(path) as fh:
            yield from self._rows(fh)

    def _rows(self, fh: IO[str]) -> Iterator[RawRecord]:
        cfg = self.config
        reader = csv.reader(fh, delimiter=cfg.delimiter, quotechar=cfg.quotechar)
        try:
            header_row = next(reader)
        except StopIteration as exc:
            raise CsvError("CSV-Datei ist leer") from exc
        except UnicodeDecodeError as exc:
            raise CsvError(f"CSV-Datei passt nicht zur Kodierung {cfg.encoding!r}") from exc
        header = _unique_header(header_row)
        self._header = header
        self._check_header(header)
        geo = cfg.geometry
        srid = srid_from_crs(cfg.source_crs) if geo is not None else None
        geographic = is_geographic(srid) if srid is not None else False
        filters = {
            name: [pattern] if isinstance(pattern, str) else list(pattern)
            for name, pattern in cfg.row_filter.items()
        }
        width = len(header)
        rows = filtered = 0
        next_line = reader.line_num + 1  # erste physische Zeile des nächsten Datensatzes
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except UnicodeDecodeError as exc:
                raise CsvError(
                    f"CSV-Datei passt nicht zur Kodierung {cfg.encoding!r} "
                    f"(nach Zeile {next_line - 1}: {exc.reason})"
                ) from exc
            line, next_line = next_line, reader.line_num + 1
            if not any(cell.strip() for cell in row):
                continue
            if len(row) > width:
                self.warnings.add("csv_extra_values", example=f"Zeile {line}")
                row = row[:width]
            values: dict[str, Any] = {}
            for name, value in zip(header, row, strict=False):
                values[name] = None if (cfg.empty_as_null and value.strip() == "") else value
            for name in header[len(row) :]:
                values[name] = None
            self._pad(values)
            if filters and not self._matches(values, filters):
                filtered += 1
                continue
            rows += 1
            problem = self._numbers(values, line)
            geometry, error = (None, None)
            if geo is not None and problem is None:
                geometry, error = self._geometry(values, srid, geographic)
            yield RawRecord(
                values,
                geometry,
                srid,
                None,
                ref=f"Zeile {line}",
                geometry_error=error,
                problem=problem,
            )
        self.details.update(
            {
                "rows": rows,
                "rows_filtered": filtered,
                "columns": width,
                "encoding": cfg.encoding,
                "delimiter": cfg.delimiter,
                "source_srid": srid,
            }
        )

    def _check_header(self, header: list[str]) -> None:
        cfg = self.config
        needed: dict[str, str] = {}
        for name in cfg.key_fields:
            if name != ID_FIELD:
                needed[name] = "Schlüsselspalte"
        for name in cfg.row_filter:
            needed.setdefault(name, "Filterspalte")
        for name in [*cfg.numeric_fields, *cfg.zero_pad]:
            needed.setdefault(name, "Spalte")
        geo = cfg.geometry
        if isinstance(geo, LonLatGeometry):
            needed.setdefault(geo.lon, "Geometriespalte")
            needed.setdefault(geo.lat, "Geometriespalte")
        elif geo is not None:
            needed.setdefault(geo.column, "Geometriespalte")
        missing = [f"{kind} {name}" for name, kind in needed.items() if name not in header]
        if missing:
            raise CsvError(
                f"{', '.join(missing)} fehlen im CSV-Kopf (Trennzeichen {cfg.delimiter!r} und "
                f"Kodierung korrekt?). Vorhanden: {', '.join(header[:20])}"
            )

    @staticmethod
    def _matches(values: dict[str, Any], filters: dict[str, list[str]]) -> bool:
        for name, patterns in filters.items():
            value = values.get(name)
            text = "" if value is None else str(value).strip()
            if not any(fnmatch.fnmatchcase(text, p) for p in patterns):
                return False
        return True

    def _pad(self, values: dict[str, Any]) -> None:
        """Führende Nullen ergänzen (vor dem Filter, damit er normierte Werte sieht)."""
        for name, width in self.config.zero_pad.items():
            value = values.get(name)
            if isinstance(value, str) and value.strip().isdigit():
                values[name] = value.strip().zfill(width)

    def _numbers(self, values: dict[str, Any], line: int) -> tuple[str, str] | None:
        cfg = self.config
        for name in cfg.numeric_fields:
            try:
                values[name] = parse_decimal(values.get(name), cfg.decimal)
            except ValueError:
                return ("number_unreadable", f"Zeile {line}: {name}={values.get(name)!r}")
        return None

    def _geometry(self, values: dict[str, Any], srid: int | None, geographic: bool):
        geo = self.config.geometry
        if isinstance(geo, LonLatGeometry):
            raw_x, raw_y = values.get(geo.lon), values.get(geo.lat)
            if raw_x is None or raw_y is None:
                return None, None
            try:
                x = parse_number(raw_x, self.config.decimal)
                y = parse_number(raw_y, self.config.decimal)
            except ValueError:
                return None, f"Koordinaten nicht lesbar ({raw_x!r}, {raw_y!r})"
            if x is None or y is None:
                return None, None
            if geographic and not (-180 <= x <= 180 and -90 <= y <= 90):
                return None, (
                    f"Koordinaten außerhalb des gültigen Bereichs ({x}, {y}); Spalten vertauscht?"
                )
            return Point(x, y), None
        assert geo is not None
        wkt = values.get(geo.column)
        if wkt is None:
            return None, None
        try:
            return shapely.wkt.loads(wkt), None
        except (GEOSException, ValueError) as exc:
            return None, f"WKT nicht lesbar: {str(exc)[:120]}"
