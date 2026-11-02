"""Schnittstelle für Quelladapter.

Ein Adapter liest Datensätze aus genau einer konfigurierten Quelle und liefert
sie als :class:`~geoimport.models.RawRecord`. Mapping, Änderungserkennung,
Änderungsprotokoll und Bericht sind quellenunabhängig und liegen nicht im Adapter.

Neue Quelltypen (z. B. OGC API Features) entstehen durch eine Unterklasse von
:class:`SourceAdapter`, die mit :func:`register` registriert wird, und ein
passendes Konfigurationsmodell in :mod:`geoimport.config`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, ClassVar

from geoimport.config import SourceBase
from geoimport.http import HttpClient
from geoimport.models import RawRecord, Warnings


class SourceError(Exception):
    """Fehler beim Lesen einer Quelle (führt zum Abbruch genau dieser Quelle)."""


class SourceAdapter(ABC):
    #: Wert von ``type`` in der Quellkonfiguration
    source_type: ClassVar[str]

    def __init__(self, config: SourceBase, http: HttpClient, warnings: Warnings) -> None:
        self.config = config
        self.http = http
        self.warnings = warnings
        self.details: dict[str, Any] = {}
        #: False, wenn der Abruf bewusst begrenzt war (z. B. ``max_features``); dann
        #: wird der Bestand nicht ersetzt, weil fehlende Objekte nicht als gelöscht gelten dürfen
        self.complete = True

    @abstractmethod
    def read(self) -> Iterator[RawRecord]:
        """Liefert alle Datensätze der Quelle (bei WFS über alle Seiten)."""

    def declared_fields(self) -> list[str] | None:
        """Vom Dienst bzw. Dateikopf erklärte Felder (ohne Geometrie), falls bekannt.

        Wird nach :meth:`read` abgefragt und dient der Schemaprüfung.
        """
        return None


_REGISTRY: dict[str, type[SourceAdapter]] = {}


def register(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    _REGISTRY[cls.source_type] = cls
    return cls


def create_adapter(config: SourceBase, http: HttpClient, warnings: Warnings) -> SourceAdapter:
    try:
        cls = _REGISTRY[config.type]
    except KeyError as exc:
        raise SourceError(f"Kein Adapter für Quelltyp {config.type!r} registriert") from exc
    return cls(config, http, warnings)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)
