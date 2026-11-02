from __future__ import annotations

import decimal
import zipfile
from pathlib import Path

import pytest
import yaml

from geoimport.config import HttpSettings, parse_source
from geoimport.http import HttpClient
from geoimport.mapping import FeatureStream
from geoimport.models import Warnings
from geoimport.sources import create_adapter
from geoimport.sources.csv_source import CsvError, parse_number

PROJECT_ROOT = Path(__file__).parents[1]


def csv_source(tmp_path: Path, content: bytes | None = None, **overrides):
    data = {
        "id": "csv_test",
        "type": "csv",
        "path": "daten.csv",
        "delimiter": ";",
        "geometry": {"type": "lonlat", "lon": "lon", "lat": "lat"},
        "key_field": "id",
        "target": {"table": "ziel", "exclusive": True, "geometry_column": "geom"},
        "metadata": {"provider": "A", "licence": "B"},
        **overrides,
    }
    if content is not None:
        (tmp_path / data["path"]).write_bytes(content)
    return parse_source(data, "test", tmp_path)


def read_all(source, http=None):
    warnings = Warnings()
    client = http or HttpClient(HttpSettings(retries=0))
    adapter = create_adapter(source, client, warnings)
    records = list(adapter.read())
    return adapter, records, warnings


def test_parse_number_variants():
    assert parse_number("13.05369") == pytest.approx(13.05369)
    assert parse_number("13,05369", ",") == pytest.approx(13.05369)
    assert parse_number("1.234,5", ",") == pytest.approx(1234.5)
    assert parse_number("  ") is None
    with pytest.raises(ValueError):
        parse_number("abc")


def test_real_fixture_ladesaeulen_potsdam(fixtures_dir):
    data = {
        "id": "ladesaeulen",
        "type": "csv",
        "path": str(fixtures_dir / "csv" / "ladesaeulen_potsdam.csv"),
        "delimiter": ";",
        "geometry": {"type": "lonlat", "lon": "langengrad", "lat": "breitengrad"},
        "source_crs": "EPSG:4326",
        "key_field": "ladeeinrichtungs_id",
        "attributes": {"betreiber": "betreiber", "status": "status"},
        "target": {"table": "ziel", "exclusive": True, "geometry_column": "geom"},
        "metadata": {"provider": "BNetzA", "licence": "CC BY 4.0"},
    }
    source = parse_source(data, "test", Path.cwd())
    adapter, records, warnings = read_all(source)
    assert len(records) == 133
    assert adapter.declared_fields()[:3] == ["betreiber", "strasse", "hausnummer"]
    assert len(adapter.declared_fields()) == 48
    first = records[0]
    assert first.srid == 4326 and first.geometry.geom_type == "Point"
    assert 13.0 < first.geometry.x < 13.2 and 52.3 < first.geometry.y < 52.5
    stream = FeatureStream(records, source, warnings)
    features = {f.key: f for f in stream}
    assert len(features) == 133 and stream.skipped == 0
    feature = features["1004465.0"]
    assert feature.attributes == {
        "betreiber": "Energie und Wasser Potsdam GmbH",
        "status": "In Betrieb",
    }
    assert len(warnings) == 0


def test_latin1_with_decimal_comma_and_bom_free_header(tmp_path):
    content = "id;name;lon;lat\n1;Gärtnerei Müller;13,0512;52,3906\n2;Straße;12,55;52,41\n"
    src = csv_source(tmp_path, content.encode("latin-1"), encoding="latin-1", decimal=",")
    _, records, warnings = read_all(src)
    assert [r.properties["name"] for r in records] == ["Gärtnerei Müller", "Straße"]
    assert records[0].geometry.x == pytest.approx(13.0512)
    assert len(warnings) == 0


def test_utf8_bom_is_removed(tmp_path):
    src = csv_source(tmp_path, "﻿id;lon;lat\n1;13;52\n".encode())
    adapter, records, _ = read_all(src)
    assert adapter.declared_fields() == ["id", "lon", "lat"]
    assert records[0].properties["id"] == "1"


def test_wrong_encoding_fails_with_clear_message(tmp_path):
    src = csv_source(tmp_path, "id;lon;lat\n1;13;52\nä".encode("latin-1"))
    with pytest.raises(CsvError, match="Kodierung"):
        read_all(src)


def test_wkt_column_and_projected_crs(tmp_path):
    content = (
        "nr,wkt,bemerkung\n"
        '1,"POINT (368000 5808000)",Punkt\n'
        '2,"LINESTRING (368000 5808000, 368100 5808100)",Linie\n'
        '3,"POLYGON ((0 0, 2 2, 2 0, 0 2, 0 0))",Schleife\n'
        "4,kein wkt,Fehler\n"
    )
    src = csv_source(
        tmp_path,
        content.encode(),
        delimiter=",",
        source_crs="EPSG:25833",
        key_field="nr",
        geometry={"type": "wkt", "column": "wkt"},
    )
    _, records, warnings = read_all(src)
    assert [r.geometry.geom_type if r.geometry else None for r in records] == [
        "Point",
        "LineString",
        "Polygon",
        None,
    ]
    assert records[3].geometry_error.startswith("WKT nicht lesbar")
    assert sorted(f.key for f in FeatureStream(records, src, warnings)) == ["1", "2", "3"]
    assert warnings.codes() == {"invalid_geometry", "geometry_unreadable"}


def test_missing_and_out_of_range_coordinates(tmp_path):
    content = "id;lon;lat\n1;13.1;52.4\n2;;\n3;52.4;181.0\n4;13.2;52.5;zu viel\n"
    src = csv_source(tmp_path, content.encode())
    _, records, warnings = read_all(src)
    assert sorted(f.key for f in FeatureStream(records, src, warnings)) == ["1", "4"]
    entries = {e["code"]: e for e in warnings.as_list()}
    assert entries["missing_geometry"]["count"] == 1
    example = entries["geometry_unreadable"]["examples"][0]
    assert example.startswith("3 (Zeile 4): ") and "Spalten vertauscht" in example
    assert entries["csv_extra_values"]["examples"] == ["Zeile 5"]


def test_missing_geometry_column_fails_the_source(tmp_path):
    src = csv_source(tmp_path, b"id,lon,lat\n1,13,52\n")  # falsches Trennzeichen
    with pytest.raises(CsvError, match="Geometriespalte"):
        read_all(src)


def test_csv_from_url(http_client):
    data = {
        "id": "csv_url",
        "type": "csv",
        "url": "http://fixtures.test/csv/ladesaeulen_potsdam.csv",
        "delimiter": ";",
        "geometry": {"type": "lonlat", "lon": "langengrad", "lat": "breitengrad"},
        "key_field": "ladeeinrichtungs_id",
        "target": {"table": "ziel", "exclusive": True, "geometry_column": "geom"},
        "metadata": {"provider": "BNetzA", "licence": "CC BY 4.0"},
    }
    adapter, records, _ = read_all(parse_source(data, "test", Path.cwd()), http_client)
    assert len(records) == 133
    assert adapter.details["url_used"].endswith("/csv/ladesaeulen_potsdam.csv")
    assert adapter.details["bytes"] > 10_000
    # Zugangsdaten in der URL erscheinen weder im Bericht noch in den Metadaten
    data["url"] = "http://nutzer:geheim@fixtures.test/csv/ladesaeulen_potsdam.csv?apikey=geheim"
    adapter, _, _ = read_all(parse_source(data, "test", Path.cwd()), http_client)
    assert "geheim" not in adapter.details["url_used"]
    assert adapter.details["url_used"].startswith("http://fixtures.test/csv/")


# ----------------------------------------------------------------- INKAR-Format (ZIP)

INKAR_KEYS = ["Kennziffer", "Bereich", "Kürzel", "Indikator", "Zeitbezug"]


def inkar_source(location: dict, **overrides):
    """Wie config/sources/inkar_gemeinden.yml, Ort und Optionen anpassbar."""
    data = yaml.safe_load(
        (PROJECT_ROOT / "config" / "sources" / "inkar_gemeinden.yml").read_text("utf-8")
    )
    for key in ("url", "path", "latest_year", "fallback_path"):
        data.pop(key, None)
    data.update(location)
    data.update(overrides)
    return parse_source(data, "inkar_test", PROJECT_ROOT / "config" / "sources")


def test_inkar_zip_member_filter_padding_and_decimals(fixtures_dir):
    src = inkar_source({"path": str(fixtures_dir / "files" / "inkar_2025.zip")})
    adapter, records, warnings = read_all(src)
    assert adapter.details["archive_member"] == "inkar_2025/inkar_2025.csv"
    # 16 Gemeinde-Zeilen aus Brandenburg; 2 Kreis-Zeilen und 2 Zeilen aus
    # Schleswig-Holstein fallen durch row_filter heraus
    assert (adapter.details["rows"], adapter.details["rows_filtered"]) == (16, 4)
    assert {r.properties["Kennziffer"] for r in records} == {
        "12051000",
        "12052000",
        "12053000",
        "12054000",
    }
    first = records[0]
    assert first.geometry is None and first.srid is None
    assert first.properties["Wert"] == decimal.Decimal("1000")
    assert any(r.properties["Wert"] == decimal.Decimal("10.5") for r in records)
    features = list(FeatureStream(records, src, warnings))
    assert len(features) == 16 and len({f.key for f in features}) == 16
    assert features[0].attributes == {
        "kennziffer": "12051000",
        "indikator": "Anzahl (Testwert)",
        "zeitbezug": "2022",
        "wert": decimal.Decimal("1000"),
    }
    assert features[0].key.split("|")[:2] == ["12051000", "Testbereich A"]
    assert len(warnings) == 0


def test_zero_pad_runs_before_the_row_filter(fixtures_dir):
    """Schleswig-Holstein (Land 01): 1001000 wird zu 01001000 und passt dann zu 01*."""
    src = inkar_source(
        {"path": str(fixtures_dir / "files" / "inkar_2025.zip")},
        row_filter={"Raumbezug": "Gemeinden", "Kennziffer": "01*"},
    )
    _, records, _ = read_all(src)
    assert [r.properties["Kennziffer"] for r in records] == ["01001000", "01001000"]


def test_zip_needs_exactly_one_matching_member(fixtures_dir, tmp_path):
    path = str(fixtures_dir / "files" / "inkar_2025.zip")
    with pytest.raises(CsvError, match="archive_member angeben"):
        read_all(inkar_source({"path": path}, archive_member=None))
    with pytest.raises(CsvError, match=r"keine Datei zu 'gibts\*\.csv'"):
        read_all(inkar_source({"path": path}, archive_member="gibts*.csv"))
    two = tmp_path / "zwei.zip"
    with zipfile.ZipFile(two, "w") as archive:
        archive.writestr("inkar_a.csv", "x")
        archive.writestr("inkar_b.csv", "x")
    with pytest.raises(CsvError, match="mehr als eine"):
        read_all(inkar_source({"path": str(two)}))


def test_unreadable_number_skips_the_row_with_warning(tmp_path):
    content = "id;wert\n1;1,5\n2;n.v.\n3;\n"
    src = csv_source(
        tmp_path,
        content.encode(),
        geometry=None,
        decimal=",",
        numeric_fields=["wert"],
        target={"table": "werte", "source_column": "source", "columns": {"wert": "wert"}},
    )
    _, records, warnings = read_all(src)
    stream = FeatureStream(records, src, warnings)
    assert [(f.key, f.attributes["wert"]) for f in stream] == [
        ("1", decimal.Decimal("1.5")),
        ("3", None),
    ]
    assert stream.skipped == 1
    (entry,) = warnings.as_list()
    assert entry["code"] == "number_unreadable" and entry["examples"] == ["Zeile 3: wert='n.v.'"]


def test_latest_year_takes_the_newest_existing_file(http_client, responder):
    src = inkar_source(
        {
            "url": "http://fixtures.test/files/inkar_{year}.zip",
            "latest_year": {"first": 2027, "back": 3},
        }
    )
    adapter, records, _ = read_all(src, http_client)
    assert len(records) == 16 and adapter.details["year"] == 2025
    assert adapter.details["url_used"] == "http://fixtures.test/files/inkar_2025.zip"
    tried = [p for p, _ in responder.requests if p.startswith("/files/")]
    assert tried == [
        "/files/inkar_2027.zip",
        "/files/inkar_2026.zip",
        "/files/inkar_2025.zip",
        "/files/inkar_2025.zip",  # HEAD zur Suche, danach GET zum Herunterladen
    ]


def test_latest_year_without_any_file_fails(http_client):
    src = inkar_source(
        {
            "url": "http://fixtures.test/files/inkar_{year}.zip",
            "latest_year": {"first": 2024, "back": 1},
        }
    )
    with pytest.raises(CsvError, match="Jahre 2024, 2023"):
        read_all(src, http_client)


def test_fallback_file_when_url_is_unavailable(http_client, fixtures_dir):
    src = inkar_source(
        {
            "url": "http://fixtures.test/status/503",
            "fallback_path": str(fixtures_dir / "files" / "inkar_2025.zip"),
        }
    )
    adapter, records, warnings = read_all(src, http_client)
    assert len(records) == 16
    assert adapter.details["fallback_file"].endswith("inkar_2025.zip")
    assert "url_used" not in adapter.details
    (entry,) = warnings.as_list()
    assert entry["code"] == "fallback_file_used" and "HTTP 503" in entry["examples"][0]

    missing = inkar_source(
        {"url": "http://fixtures.test/status/503", "fallback_path": "gibt_es_nicht.zip"}
    )
    with pytest.raises(CsvError, match="Ersatzdatei .* ebenfalls nicht vorhanden"):
        read_all(missing, http_client)
