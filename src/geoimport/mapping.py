"""Abbildung von Quelldatensätzen auf das Zielmodell.

Hier liegt die quellenunabhängige Logik: Schlüsselbildung, Attribut-Mapping,
Geometrieprüfung und Schemaprüfung. Die Datensätze werden einzeln
weitergereicht (Streaming); Dubletten, Vergleich und Speicherung erledigt
:mod:`geoimport.store` mengenbasiert in PostgreSQL. So bleibt der
Speicherbedarf auch bei Quellen mit Millionen Zeilen klein.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import shapely

from geoimport.config import SourceBase
from geoimport.hashing import attribute_text
from geoimport.models import Feature, RawRecord, Warnings

ID_FIELD = "@id"


def extract_key(record: RawRecord, key_fields: list[str]) -> str | None:
    """Fachlicher Schlüssel: Werte der Schlüsselfelder, mit ``|`` verbunden."""
    parts: list[str] = []
    for name in key_fields:
        value = record.feature_id if name == ID_FIELD else record.properties.get(name)
        text = attribute_text(value)
        if text is None or not text.strip():
            return None
        parts.append(text.strip())
    return "|".join(parts)


def map_attributes(properties: dict[str, Any], source: SourceBase) -> dict[str, Any]:
    """Ordnet Quellfelder den Zielattributen zu (``attributes: {quelle: ziel}``)."""
    if not source.attributes:
        return dict(properties)
    mapped = {target: properties.get(src) for src, target in source.attributes.items()}
    if source.keep_unmapped:
        for name, value in properties.items():
            if name not in source.attributes and name not in mapped:
                mapped[name] = value
    return mapped


def build_feature(record: RawRecord, source: SourceBase, warnings: Warnings) -> Feature | None:
    """Erzeugt ein Feature oder ``None`` (mit Warnung), wenn der Datensatz unbrauchbar ist."""
    where = record.ref or record.feature_id or "?"
    if record.problem is not None:
        code, example = record.problem
        warnings.add(code, example=example)
        return None
    key = extract_key(record, source.key_fields)
    if key is None:
        warnings.add("missing_key", example=where)
        return None
    attributes = map_attributes(record.properties, source)
    if not source.needs_geometry:
        # Ziel ohne Geometriespalte: die Geometrie fließt weder in den Vergleich noch in die DB
        return Feature(key=key, attributes=attributes, geometry=None, srid=None, ref=record.ref)
    geom = record.geometry
    if geom is None or geom.is_empty:
        if record.geometry_error:
            warnings.add("geometry_unreadable", example=f"{key} ({where}): {record.geometry_error}")
        else:
            warnings.add("missing_geometry", example=f"{key} ({where})")
        return None
    if record.srid is None:
        warnings.add("geometry_unreadable", example=f"{key}: Bezugssystem unbekannt")
        return None
    if not geom.is_valid:
        reason = shapely.is_valid_reason(geom)
        if source.repair_invalid_geometries:
            geom = shapely.make_valid(geom)
            warnings.add("invalid_geometry_repaired", example=f"{key}: {reason}")
        else:
            warnings.add("invalid_geometry", example=f"{key}: {reason}")
    return Feature(key=key, attributes=attributes, geometry=geom, srid=record.srid, ref=record.ref)


class FeatureStream:
    """Bildet Rohdatensätze nacheinander auf Features ab und zählt dabei mit.

    Die Zähler (``received``, ``skipped``) und die beobachteten Felder sind nach
    vollständigem Durchlaufen gültig.
    """

    def __init__(self, records: Iterable[RawRecord], source: SourceBase, warnings: Warnings):
        self._records = records
        self._source = source
        self._warnings = warnings
        self.received = 0
        self.skipped = 0
        self.observed_fields: set[str] = set()

    def __iter__(self) -> Iterator[Feature]:
        for record in self._records:
            self.received += 1
            self.observed_fields.update(record.properties.keys())
            feature = build_feature(record, self._source, self._warnings)
            if feature is None:
                self.skipped += 1
                continue
            yield feature


def check_schema(
    source: SourceBase,
    declared_fields: list[str] | None,
    observed_fields: set[str],
    previous_fields: list[str] | None,
    warnings: Warnings,
) -> list[str]:
    """Prüft Schemaabweichungen und liefert die zu speichernde Feldliste."""
    current = list(declared_fields) if declared_fields is not None else sorted(observed_fields)
    available = set(current) | observed_fields
    if declared_fields is not None:
        for name in sorted(observed_fields - set(declared_fields)):
            warnings.add("schema_unexpected_field", example=name)
    needed = [f for f in source.key_fields if f != ID_FIELD] + list(source.attributes)
    if available:
        for name in needed:
            if name not in available:
                warnings.add("schema_mapped_field_missing", example=name)
    if previous_fields is not None and declared_fields is not None:
        added = sorted(set(current) - set(previous_fields))
        removed = sorted(set(previous_fields) - set(current))
        if added or removed:
            detail = []
            if added:
                detail.append("neu: " + ", ".join(added))
            if removed:
                detail.append("entfallen: " + ", ".join(removed))
            warnings.add("schema_changed", example="; ".join(detail))
    return current
