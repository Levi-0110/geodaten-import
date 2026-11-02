"""Gemeinsame Datenstrukturen der Pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry.base import BaseGeometry


@dataclass(slots=True)
class RawRecord:
    """Ein Datensatz, so wie ihn ein Quelladapter liefert (vor dem Mapping)."""

    properties: dict[str, Any]
    geometry: BaseGeometry | None
    srid: int | None
    feature_id: str | None = None
    ref: str | None = None
    geometry_error: str | None = None
    #: (Warnungscode, Beispieltext), wenn der Datensatz nicht verwendbar ist
    problem: tuple[str, str] | None = None


@dataclass(slots=True)
class Feature:
    """Ein abgebildeter Datensatz, bereit für Änderungserkennung und Speicherung.

    Reine Sachdaten (z. B. Kennzahlen je Gemeinde) haben keine Geometrie.
    """

    key: str
    attributes: dict[str, Any]
    geometry: BaseGeometry | None
    srid: int | None
    ref: str | None = None


WARNING_TEXTS: dict[str, str] = {
    "missing_key": "Datensätze ohne Schlüsselwert übersprungen",
    "missing_geometry": "Datensätze ohne Geometrie übersprungen",
    "geometry_unreadable": "Datensätze mit nicht lesbarer Geometrie übersprungen",
    "invalid_geometry": "Objekte mit ungültiger Geometrie (unverändert übernommen)",
    "invalid_geometry_repaired": "Objekte mit ungültiger Geometrie (automatisch repariert)",
    "duplicate_key": "Doppelte Schlüsselwerte (nur der erste Datensatz übernommen)",
    "duplicate_key_merged": (
        "Doppelte Schlüsselwerte (Teilgeometrien zu einem Objekt zusammengefasst)"
    ),
    "duplicate_key_conflict": (
        "Doppelte Schlüsselwerte mit abweichenden Attributen (Attribute des ersten Objekts)"
    ),
    "schema_mapped_field_missing": "Im Mapping konfigurierte Felder fehlen in der Quelle",
    "schema_unexpected_field": "Unerwartete Felder, die nicht im Schema der Quelle stehen",
    "schema_changed": "Schema der Quelle hat sich seit dem letzten erfolgreichen Lauf geändert",
    "schema_unavailable": "Schema der Quelle konnte nicht abgefragt werden",
    "csv_extra_values": "Zeilen mit mehr Werten als Spalten im Kopf (überzählige Werte ignoriert)",
    "number_unreadable": "Zahlenwerte nicht lesbar (Datensatz übersprungen)",
    "fallback_file_used": "Abruf per URL fehlgeschlagen, abgelegte Datei gelesen",
    "page_size_limited": "Seitengröße durch Serverbegrenzung (CountDefault) reduziert",
}


@dataclass
class WarningEntry:
    code: str
    message: str
    count: int = 0
    examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "count": self.count,
            "examples": list(self.examples),
        }


class Warnings:
    """Sammelt Warnungen einer Quelle und fasst gleichartige zusammen."""

    def __init__(self, max_examples: int = 5) -> None:
        self._entries: dict[tuple[str, str], WarningEntry] = {}
        self._max_examples = max_examples

    def add(self, code: str, example: str | None = None, message: str | None = None) -> None:
        text = message or WARNING_TEXTS.get(code, code)
        entry = self._entries.get((code, text))
        if entry is None:
            entry = self._entries[(code, text)] = WarningEntry(code, text)
        entry.count += 1
        if example is not None and len(entry.examples) < self._max_examples:
            entry.examples.append(example)

    def as_list(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self._entries.values()]

    def codes(self) -> set[str]:
        return {code for code, _ in self._entries}

    def __len__(self) -> int:
        return len(self._entries)
