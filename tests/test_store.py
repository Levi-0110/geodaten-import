"""Staging und doppelte Schlüssel direkt in PostgreSQL (benötigt ``GEOIMPORT_TEST_DSN``)."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from shapely.geometry import Polygon

from geoimport import store
from geoimport.config import parse_source
from geoimport.models import Feature, Warnings

pytestmark = pytest.mark.e2e

A = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
B = Polygon([(2, 0), (3, 0), (3, 1), (2, 1)])


def make_source(duplicate_keys: str):
    data = {
        "id": "test",
        "type": "csv",
        "path": "x.csv",
        "geometry": {"type": "wkt", "column": "wkt"},
        "key_field": "id",
        "duplicate_keys": duplicate_keys,
        "target": {"table": "ziel", "exclusive": True, "geometry_column": "geom"},
        "metadata": {"provider": "A", "licence": "B"},
    }
    return parse_source(data, "test", Path.cwd())


def feature(key: str, geom: Polygon, n: str = "1") -> Feature:
    return Feature(key=key, attributes={"n": n}, geometry=geom, srid=25833)


def staged(conn: psycopg.Connection, features: list[Feature], mode: str):
    """Lädt die Features in die Staging-Tabelle und löst Dubletten auf."""
    warnings = Warnings()
    store.create_stage(conn)
    store.load_stage(conn, features)
    counts = store.resolve_duplicates(conn, make_source(mode), warnings)
    rows = conn.execute(
        "SELECT record_key, content_hash, ST_GeometryType(ST_GeomFromWKB(geom_wkb)), "
        "       ST_Area(ST_GeomFromWKB(geom_wkb)), attrs->>'n' "
        f"FROM {store.STAGE_TABLE} ORDER BY record_key"
    ).fetchall()
    return counts, rows, warnings


def test_duplicate_keys_warn_merge_error(pg_dsn):
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        records = [feature("x", A), feature("x", B), feature("y", B)]

        with conn.transaction():
            counts, rows, w = staged(conn, records, "warn")
        assert counts == {"merged": 0, "duplicates_dropped": 1}
        assert [(r[0], r[2], r[3]) for r in rows] == [
            ("x", "ST_Polygon", 1.0),  # der zuerst gelieferte Datensatz bleibt
            ("y", "ST_Polygon", 1.0),
        ]
        assert w.codes() == {"duplicate_key"}

        with conn.transaction():
            counts, rows, w = staged(conn, records, "merge")
        assert counts == {"merged": 1, "duplicates_dropped": 0}
        assert [(r[0], r[2], r[3]) for r in rows] == [
            ("x", "ST_MultiPolygon", 2.0),
            ("y", "ST_Polygon", 1.0),
        ]
        assert w.codes() == {"duplicate_key_merged"}
        merged_hash = rows[0][1]

        # gleicher Inhalt in anderer Lieferreihenfolge: gleicher Hash, also "unverändert"
        with conn.transaction():
            _, rows, _ = staged(conn, [feature("y", B), feature("x", B), feature("x", A)], "merge")
        assert rows[0][1] == merged_hash

        # widersprüchliche Attribute: Warnung, Attribute des ersten Datensatzes
        with conn.transaction():
            _, rows, w = staged(conn, [feature("x", A, "1"), feature("x", B, "2")], "merge")
        assert rows[0][4] == "1"
        assert w.codes() == {"duplicate_key_merged", "duplicate_key_conflict"}

        error = pytest.raises(store.DuplicateKeyError, match="1 Schlüssel kommen mehrfach vor")
        with error, conn.transaction():
            staged(conn, records, "error")
