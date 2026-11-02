from __future__ import annotations

from pathlib import Path

from shapely.geometry import Point, Polygon

from geoimport.config import parse_source
from geoimport.mapping import FeatureStream, build_feature, check_schema, map_attributes
from geoimport.models import RawRecord, Warnings

BOWTIE = Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)])


def make_source(**overrides):
    data = {
        "id": "test",
        "type": "csv",
        "path": "x.csv",
        "geometry": {"type": "lonlat", "lon": "x", "lat": "y"},
        "key_field": "id",
        "target": {"table": "ziel", "exclusive": True, "geometry_column": "geom"},
        "metadata": {"provider": "A", "licence": "B"},
        **overrides,
    }
    return parse_source(data, "test", Path.cwd())


def attribute_source(**overrides):
    """Quelle ohne Geometrie im Ziel (z. B. Kennzahlen je Gemeinde)."""
    return make_source(
        geometry=None,
        attributes={"id": "id", "wert": "wert"},
        target={"table": "werte", "source_column": "source", "columns": {"wert": "wert"}},
        **overrides,
    )


DEFAULT_POINT = Point(13.0, 52.4)


def rec(props, geom=DEFAULT_POINT, srid=4326, fid=None, error=None):
    return RawRecord(dict(props), geom, srid, fid, ref="Zeile 2", geometry_error=error)


def test_mapping_renames_and_selects_fields():
    src = make_source(attributes={"otl_name": "name", "ags": "gemeinde"})
    props = {"otl_name": "Plaue", "ags": "12051000", "intern": "x"}
    assert map_attributes(props, src) == {"name": "Plaue", "gemeinde": "12051000"}


def test_mapping_keep_unmapped_and_missing_fields():
    src = make_source(attributes={"a": "ziel_a", "fehlt": "ziel_f"}, keep_unmapped=True)
    assert map_attributes({"a": 1, "b": 2}, src) == {"ziel_a": 1, "ziel_f": None, "b": 2}


def test_mapping_without_configuration_keeps_all_fields():
    assert map_attributes({"a": 1, "b": None}, make_source()) == {"a": 1, "b": None}


def test_composite_and_feature_id_keys():
    w = Warnings()
    f = build_feature(rec({"gem": "12051000", "nr": 7}), make_source(key_field=["gem", "nr"]), w)
    assert f.key == "12051000|7"
    g = build_feature(rec({}, fid="otl_452"), make_source(key_field="@id"), w)
    assert g.key == "otl_452"
    assert len(w) == 0


def test_records_without_key_or_geometry_are_skipped_with_warning():
    w = Warnings()
    src = make_source()
    assert build_feature(rec({"id": " "}), src, w) is None
    assert build_feature(rec({"id": "1"}, geom=None), src, w) is None
    assert build_feature(rec({"id": "2"}, geom=None, error="WKT nicht lesbar"), src, w) is None
    assert w.codes() == {"missing_key", "missing_geometry", "geometry_unreadable"}


def test_invalid_geometry_is_reported_or_repaired():
    w = Warnings()
    f = build_feature(rec({"id": "1"}, geom=BOWTIE, srid=25833), make_source(), w)
    assert f is not None and not f.geometry.is_valid
    (entry,) = w.as_list()
    assert entry["code"] == "invalid_geometry" and "Self-intersection" in entry["examples"][0]

    w2 = Warnings()
    g = build_feature(
        rec({"id": "1"}, geom=BOWTIE, srid=25833), make_source(repair_invalid_geometries=True), w2
    )
    assert g.geometry.is_valid
    assert w2.codes() == {"invalid_geometry_repaired"}


def test_feature_stream_counts_and_skips():
    w = Warnings()
    records = [
        rec({"id": "1", "a": "x"}),
        rec({"id": ""}),
        rec({"id": "3", "b": "y"}, geom=None),
        RawRecord(
            {"id": "4"},
            None,
            None,
            None,
            ref="Zeile 5",
            problem=("number_unreadable", "Zeile 5: wert='x'"),
        ),
        rec({"id": "5"}),
    ]
    stream = FeatureStream(records, make_source(), w)
    assert [f.key for f in stream] == ["1", "5"]
    assert (stream.received, stream.skipped) == (5, 3)
    assert stream.observed_fields == {"id", "a", "b"}
    assert w.codes() == {"missing_key", "missing_geometry", "number_unreadable"}


def test_target_without_geometry_ignores_coordinates():
    w = Warnings()
    src = attribute_source()
    f = build_feature(rec({"id": "12051000", "wert": "3"}, geom=None, srid=None), src, w)
    assert f is not None and f.geometry is None and f.srid is None
    assert f.attributes == {"id": "12051000", "wert": "3"}
    assert src.needs_geometry is False and src.source_value == "test"
    assert len(w) == 0


def test_schema_check_reports_missing_unexpected_and_changed_fields():
    src = make_source(attributes={"name": "name", "alt": "alt"})
    w = Warnings()
    fields = check_schema(
        src,
        declared_fields=["id", "name", "neu"],
        observed_fields={"id", "name", "zusatz"},
        previous_fields=["id", "name", "alt"],
        warnings=w,
    )
    assert fields == ["id", "name", "neu"]
    entries = {e["code"]: e for e in w.as_list()}
    assert entries["schema_mapped_field_missing"]["examples"] == ["alt"]
    assert entries["schema_unexpected_field"]["examples"] == ["zusatz"]
    assert entries["schema_changed"]["examples"] == ["neu: neu; entfallen: alt"]
