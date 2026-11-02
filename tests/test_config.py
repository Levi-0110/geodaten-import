from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from geoimport.config import (
    KEY_REF,
    ConfigError,
    CsvSource,
    WfsSource,
    expand_env,
    load_config,
    parse_source,
)

PROJECT_ROOT = Path(__file__).parents[1]


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_shipped_configuration_is_valid():
    cfg = load_config(PROJECT_ROOT / "config" / "pipeline.yml")
    ids = {s.id for s in cfg.sources}
    assert ids == {
        "lgb_ortsteile_brb",
        "lfu_nsg_brb",
        "bnetza_ladesaeulen_potsdam",
        "inkar_gemeinden",
    }
    assert cfg.database.target_srid == 4326 and cfg.database.meta_schema == "pipeline_meta"
    by_id = {s.id: s for s in cfg.sources}
    lgb = by_id["lgb_ortsteile_brb"]
    assert isinstance(lgb, WfsSource)
    assert lgb.type_name == "app:otl" and lgb.key_fields == ["otls"]
    assert lgb.target.table == "Geometrien (Demo)" and lgb.target.columns["geo_ref"] == KEY_REF
    assert lgb.source_value == "LGB Verwaltungsgrenzen: Ortsteile"
    nsg = by_id["lfu_nsg_brb"]
    assert nsg.target.exclusive and nsg.source_value is None
    assert nsg.metadata_record is not None
    csv_src = by_id["bnetza_ladesaeulen_potsdam"]
    assert isinstance(csv_src, CsvSource)
    assert csv_src.geometry.type == "lonlat" and csv_src.delimiter == ";"
    inkar = by_id["inkar_gemeinden"]
    assert isinstance(inkar, CsvSource) and inkar.geometry is None and not inkar.needs_geometry
    assert inkar.key_fields == ["Kennziffer", "Bereich", "Kürzel", "Indikator", "Zeitbezug"]
    assert inkar.zero_pad == {"Kennziffer": 8} and inkar.numeric_fields == ["Wert"]
    assert inkar.target.table == "dashboard_data_de" and inkar.source_value == "INKAR (BBSR)"
    assert inkar.target.constants == {"granularity": "Gemeindeebene"}
    # eigener Takt für INKAR, alle anderen Quellen im Standardtakt
    groups = cfg.schedule_groups()
    assert groups["0 4 2 * *"] == ["inkar_gemeinden"]
    assert sorted(groups[cfg.schedule.cron]) == [
        "bnetza_ladesaeulen_potsdam",
        "lfu_nsg_brb",
        "lgb_ortsteile_brb",
    ]


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("GI_TEST_HOST", "example.org")
    assert expand_env({"u": "https://${GI_TEST_HOST}/wfs"}) == {"u": "https://example.org/wfs"}
    assert expand_env("${GI_TEST_UNSET:-standard}") == "standard"
    with pytest.raises(ConfigError, match="GI_TEST_UNSET"):
        expand_env("${GI_TEST_UNSET}")


def test_new_source_needs_only_a_yaml_file(tmp_path):
    cfg_file = _write(tmp_path / "pipeline.yml", "sources_dir: quellen\n")
    _write(
        tmp_path / "quellen" / "punkte.yml",
        """
        id: meine_punkte
        type: csv
        path: punkte.csv
        delimiter: ";"
        geometry: {type: wkt, column: wkt}
        source_crs: EPSG:25833
        key_field: [gemeinde, nr]
        target: {table: punkte, source_column: quelle, geometry_column: geom}
        metadata: {provider: Testamt, licence: CC0-1.0}
        """,
    )
    cfg = load_config(cfg_file)
    (src,) = cfg.sources
    assert src.key_fields == ["gemeinde", "nr"]
    assert src.source_value == "meine_punkte"  # Standard: die ID der Quelle
    assert src.resolved_path() == (tmp_path / "quellen" / "punkte.csv").resolve()
    assert cfg.report_dir() == tmp_path.resolve() / "reports"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"id": "Mit Leerzeichen"}, "id"),
        ({"bbox": [1, 2, 3]}, "bbox"),
        ({"filter": "<fes:Filter/>", "bbox": [1, 2, 3, 4]}, "schließen sich"),
        ({"source_crs": "EPSG:999999"}, "source_crs"),
        ({"unbekannt": 1}, "unbekannt"),
        ({"url": "ftp://example.org"}, "url"),
    ],
)
def test_invalid_wfs_source_is_rejected(patch, message):
    data = {
        "id": "test",
        "type": "wfs",
        "url": "https://example.org/wfs",
        "type_name": "app:x",
        "key_field": "id",
        "target": {"table": "t", "exclusive": True, "geometry_column": "geom"},
        "metadata": {"provider": "A", "licence": "B"},
        **patch,
    }
    with pytest.raises(ConfigError, match=message):
        parse_source(data, "test.yml", Path.cwd())


CSV = {
    "id": "x",
    "type": "csv",
    "geometry": {"type": "lonlat", "lon": "x", "lat": "y"},
    "key_field": "id",
    "target": {"table": "t", "exclusive": True, "geometry_column": "geom"},
    "metadata": {"provider": "A", "licence": "B"},
}


def test_csv_source_requires_exactly_one_location():
    with pytest.raises(ConfigError, match="path oder url"):
        parse_source(CSV, "x.yml", Path.cwd())
    with pytest.raises(ConfigError, match="path oder url"):
        parse_source({**CSV, "path": "a.csv", "url": "https://e.org/a.csv"}, "x.yml", Path.cwd())


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"path": "a.csv", "fallback_path": "b.csv"}, "fallback_path"),
        ({"url": "https://e.org/inkar_{year}.zip"}, "latest_year"),
        ({"url": "https://e.org/a.zip", "latest_year": {"back": 2}}, "latest_year"),
        ({"path": "a.csv", "geometry": None}, "geometry"),
        ({"path": "a.csv", "zero_pad": {"ags": 0}}, "zero_pad"),
        ({"path": "a.csv", "schedule": "jeden Montag"}, "schedule"),
    ],
)
def test_invalid_csv_options_are_rejected(patch, message):
    with pytest.raises(ConfigError, match=message):
        parse_source({**CSV, **patch}, "x.yml", Path.cwd())


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ({"table": "t", "geometry_column": "geom"}, "source_column angeben"),
        (
            {"table": "t", "exclusive": True, "source_column": "s", "geometry_column": "g"},
            "schließen sich aus",
        ),
        (
            {"table": "t", "exclusive": True, "source_value": "v", "geometry_column": "g"},
            "setzt source_column voraus",
        ),
        ({"table": "t", "exclusive": True}, "mindestens eine Spalte"),
        (
            {"table": "t", "source_column": "s", "columns": {"s": "id"}, "geometry_column": "g"},
            "Spalte 's'",
        ),
        (
            {"table": "t", "exclusive": True, "columns": {"a": "fehlt"}, "geometry_column": "g"},
            "Mapping",
        ),
    ],
)
def test_invalid_target_is_rejected(target, message):
    data = {**CSV, "path": "a.csv", "attributes": {"id": "id"}, "target": target}
    with pytest.raises(ConfigError, match=message):
        parse_source(data, "x.yml", Path.cwd())


def test_metadata_record_templates_are_checked():
    record = {"key_columns": {"table_name": "{gibts_nicht}"}}
    with pytest.raises(ConfigError, match="unbekannte Platzhalter gibts_nicht"):
        parse_source({**CSV, "path": "a.csv", "metadata_record": record}, "x.yml", Path.cwd())
    ok = {"key_columns": {"table_name": "t: {source_value}"}, "columns": {"dct_title": "{title}"}}
    src = parse_source({**CSV, "path": "a.csv", "metadata_record": ok}, "x.yml", Path.cwd())
    assert src.metadata_record.table == "table_metadata"


def _source_file(directory: Path, name: str, target: str) -> None:
    _write(
        directory / f"{name}.yml",
        f"""
        id: {name}
        type: csv
        path: a.csv
        geometry: {{type: lonlat, x: lon, y: lat}}
        key_field: id
        target: {target}
        metadata: {{provider: A, licence: B}}
        """,
    )


def test_two_sources_never_replace_the_same_rows(tmp_path):
    cfg_file = _write(tmp_path / "pipeline.yml", "sources_dir: sources\n")
    src = tmp_path / "sources"
    _source_file(src, "a", "{table: geo, source_column: source, geometry_column: geom}")
    _source_file(src, "b", "{table: geo, source_column: source, geometry_column: geom}")
    assert len(load_config(cfg_file).sources) == 2  # verschiedene source_value (IDs)
    _source_file(
        src, "b", "{table: geo, source_column: source, source_value: a, geometry_column: geom}"
    )
    with pytest.raises(ConfigError, match="denselben source_value 'a'"):
        load_config(cfg_file)
    _source_file(src, "b", "{table: geo, exclusive: true, geometry_column: geom}")
    with pytest.raises(ConfigError, match="exklusiv der Quelle 'b'"):
        load_config(cfg_file)


def test_duplicate_source_ids_are_rejected(tmp_path):
    cfg_file = _write(tmp_path / "pipeline.yml", "sources_dir: sources\n")
    body = """
        id: doppelt
        type: csv
        path: a.csv
        geometry: {type: lonlat, x: lon, y: lat}
        key_field: id
        target: {table: t, exclusive: true, geometry_column: geom}
        metadata: {provider: A, licence: B}
        """
    _write(tmp_path / "sources" / "a.yml", body)
    _write(tmp_path / "sources" / "b.yml", body)
    with pytest.raises(ConfigError, match="doppelt"):
        load_config(cfg_file)


def test_invalid_cron_is_rejected(tmp_path):
    cfg_file = _write(tmp_path / "pipeline.yml", "schedule: {cron: '61 * * * *'}\n")
    with pytest.raises(ConfigError, match="Cron"):
        load_config(cfg_file)


def test_select_unknown_source(tmp_path):
    cfg = load_config(_write(tmp_path / "pipeline.yml", "{}\n"))
    with pytest.raises(ConfigError, match="Unbekannte Quelle"):
        cfg.select_sources(["gibt_es_nicht"])
