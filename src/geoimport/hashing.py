"""Normalisierung von Attributwerten und Inhalts-Hash für die Änderungserkennung.

Der Hash umfasst die abgebildeten Attribute (in Textform, wie sie gespeichert
werden), die Geometrie als 2D-WKB im Quell-KBS und den EPSG-Code. Nicht
abgebildete Felder fließen nicht ein, damit neue, nicht genutzte Felder einer
Quelle keine Scheinänderungen auslösen.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import math
from typing import Any


def attribute_text(value: Any) -> str | None:
    """Einheitliche Textdarstellung eines Attributwerts (Hash, Staging, Zielspalten)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return repr(value)
    if isinstance(value, decimal.Decimal):
        # 10,0 und 10 sind derselbe Wert: keine Änderung melden, nur weil sich die
        # Schreibweise der Quelle ändert
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def normalize_attributes(attributes: dict[str, Any]) -> dict[str, str | None]:
    return {str(k): attribute_text(v) for k, v in attributes.items()}


def content_hash(attributes: dict[str, Any], geometry_wkb: bytes | None, srid: int | None) -> str:
    """SHA-256 über Attribute, Geometrie und Bezugssystem (hexadezimal, 64 Zeichen)."""
    payload = {
        "a": dict(sorted(normalize_attributes(attributes).items())),
        "g": geometry_wkb.hex() if geometry_wkb is not None else None,
        "s": srid,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
