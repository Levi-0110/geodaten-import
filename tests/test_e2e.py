"""End-to-End-Test: WFS (GML, GeoJSON) und CSV (auch INKAR-ZIP) gegen PostGIS.

Die Zieltabellen bestehen vorab, wie im MOSIDI-System (``demo/mosidi_zieltabellen.sql``);
die Pipeline schreibt nur Zeilen hinein. Vor Lauf 1 stehen schon Zeilen anderer
Quellen in den Tabellen (OpenStreetMap-Kennzahlen in ``dashboard_data_de`` und eine
fremde Geometrie); sie dürfen nie verändert werden.

1. Lauf 1 mit den aufgezeichneten Antworten: alles ist neu. Eine absichtlich defekte
   Quelle (HTTP 500) schlägt fehl, ohne die anderen zu blockieren.
2. Lauf 2 mit geänderten Antworten: neue, geänderte und entfernte Datensätze stehen im
   Änderungsprotokoll und im Bericht; die Zeilen jeder Quelle werden ersetzt;
   Schemaabweichungen erscheinen als Warnung.
3. Lauf 3: eine Quelle liefert einen Wert, den die Zieltabelle ablehnt. Ihre
   Transaktion wird vollständig zurückgerollt (Zeilen, Protokoll, Stand), die übrigen
   Quellen laufen normal.

Für jeden Lauf wird geprüft, dass JSON- und Markdown-Bericht mit dem Datenbankinhalt
übereinstimmen.
"""

from __future__ import annotations

import csv
import decimal
import io
import json
import re
import secrets
import zipfile
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg import sql
from psycopg.conninfo import make_conninfo

from geoimport.cli import main

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).parents[1]
SOURCES = {
    "lgb_ortsteile_brandenburg_havel.yml": {"url": "/wfs/lgb_vg"},
    "lfu_naturschutzgebiete.yml": {"url": "/wfs/lfu_schutzg"},
    "bnetza_ladesaeulen_potsdam.yml": {"url": "/csv/ladesaeulen_potsdam.csv"},
    # sucht inkar_2026.zip (fehlt) und nimmt dann inkar_2025.zip
    "inkar_gemeinden.yml": {
        "url": "/files/inkar_{year}.zip",
        "latest_year": {"first": 2026, "back": 2},
    },
}
LGB, LFU, CSV_ID, INKAR, BROKEN = (
    "lgb_ortsteile_brb",
    "lfu_nsg_brb",
    "bnetza_ladesaeulen_potsdam",
    "inkar_gemeinden",
    "defekter_wfs",
)
#: Quelle -> (Zieltabelle, Wert der Quellenspalte; None = exklusive Tabelle)
TARGETS = {
    LGB: ('"Geometrien (Demo)"', "LGB Verwaltungsgrenzen: Ortsteile"),
    LFU: ('"Naturschutzgebiete"', None),
    CSV_ID: ('"Geometrien (Demo)"', "BNetzA Ladesäulenregister"),
    INKAR: ("dashboard_data_de", "INKAR (BBSR)"),
    BROKEN: ('"Geometrien (Demo)"', "Defekter Dienst"),
}
FOREIGN_ROWS_SQL = """
INSERT INTO dashboard_data_de (indikator, kennziffer, wert, zeitbezug, granularity, source,
                               data_type)
VALUES ('Anzahl Supermärkte', '12051000', 42, 2025, 'Gemeindeebene', 'OpenStreetMap (OSM)',
        'integer'),
       ('Anzahl Supermärkte', '12052000', 57, 2025, 'Gemeindeebene', 'OpenStreetMap (OSM)',
        'integer'),
       ('Anzahl Supermärkte', '12054000', 98, 2025, 'Gemeindeebene', 'OpenStreetMap (OSM)',
        'integer');
INSERT INTO "Geometrien (Demo)" (geo_ref, name, geom, source)
VALUES ('12051000', 'Brandenburg an der Havel', ST_GeomFromText('POINT(12.55 52.41)', 4326),
        'Fremdquelle');
"""


# ----------------------------------------------------------------- Hilfsfunktionen


def write_config(tmp_path: Path, base_url: str) -> Path:
    """Nutzt die mitgelieferten Quelldateien; nur die URLs zeigen auf den Testserver."""
    cfg_dir = tmp_path / "config"
    (cfg_dir / "sources").mkdir(parents=True)
    pipeline = yaml.safe_load((PROJECT_ROOT / "config" / "pipeline.yml").read_text("utf-8"))
    pipeline["report"]["directory"] = str(tmp_path / "reports")
    pipeline["http"].update(retries=1, backoff_seconds=0, timeout_seconds=10)
    (cfg_dir / "pipeline.yml").write_text(yaml.safe_dump(pipeline, allow_unicode=True), "utf-8")
    for name, changes in SOURCES.items():
        data = yaml.safe_load((PROJECT_ROOT / "config" / "sources" / name).read_text("utf-8"))
        data.update(changes)
        data["url"] = base_url + changes["url"]
        save_source(cfg_dir / "sources" / name, data)
    broken = {
        "id": BROKEN,
        "type": "wfs",
        "title": "Absichtlich nicht erreichbarer Dienst",
        "url": base_url + "/status/500",
        "type_name": "app:irgendwas",
        "key_field": "id",
        "target": {
            "table": "Geometrien (Demo)",
            "source_column": "source",
            "source_value": "Defekter Dienst",
            "columns": {"geo_ref": "@key"},
            "geometry_column": "geom",
        },
        "metadata": {"provider": "Test", "licence": "keine"},
    }
    save_source(cfg_dir / "sources" / "zz_defekt.yml", broken)
    return cfg_dir / "pipeline.yml"


def save_source(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")


def edit_source(cfg: Path, name: str, **changes) -> None:
    path = cfg.parent / "sources" / name
    data = yaml.safe_load(path.read_text("utf-8"))
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    save_source(path, data)


def run_cli(cfg_path: Path, *extra: str) -> tuple[int, dict, str]:
    code = main(["--config", str(cfg_path), "--log-level", "WARNING", "run", *extra])
    report_dir = cfg_path.parents[1] / "reports"
    report = json.loads((report_dir / "latest.json").read_text("utf-8"))
    markdown = (report_dir / "latest.md").read_text("utf-8")
    named = [p for p in report_dir.glob("import_*.json") if report["run_id"][:8] in p.name]
    assert len(named) == 1, "Bericht mit Lauf-ID im Dateinamen fehlt"
    assert named[0].with_suffix(".md").read_text("utf-8") == markdown
    return code, report, markdown


def by_id(report: dict) -> dict[str, dict]:
    return {s["id"]: s for s in report["sources"]}


def warning_codes(source: dict) -> set[str]:
    return {w["code"] for w in source["warnings"]}


def db_rows(dsn: str, query: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def target_rows(dsn: str, sid: str) -> int:
    table, value = TARGETS[sid]
    if value is None:
        return db_rows(dsn, f"SELECT count(*) FROM {table}")[0][0]
    return db_rows(dsn, f"SELECT count(*) FROM {table} WHERE source = %s", (value,))[0][0]


def foreign_rows(dsn: str) -> list[tuple]:
    """Zeilen anderer Quellen, die die Pipeline nie anfassen darf (mit ihren IDs)."""
    return db_rows(
        dsn,
        "SELECT id, indikator, kennziffer, wert, zeitbezug, source FROM dashboard_data_de "
        "WHERE source = 'OpenStreetMap (OSM)' ORDER BY id",
    ) + db_rows(
        dsn,
        'SELECT id, geo_ref, name, ST_AsText(geom), source FROM "Geometrien (Demo)" '
        "WHERE source = 'Fremdquelle'",
    )


def change_log(dsn: str, run_id: str, sid: str) -> dict[str, int]:
    return dict(
        db_rows(
            dsn,
            "SELECT action, count(*) FROM pipeline_meta.change_log "
            "WHERE run_id = %s AND source_id = %s GROUP BY action",
            (run_id, sid),
        )
    )


def assert_report_matches_db(dsn: str, report: dict) -> None:
    """Kennzahlen im Bericht == Änderungsprotokoll, Stand und Zieltabellen."""
    run_id = report["run_id"]
    for src in report["sources"]:
        sid = src["id"]
        status, last_run, records = db_rows(
            dsn,
            "SELECT last_status, last_run_id::text, records FROM pipeline_meta.source_state "
            "WHERE source_id = %s",
            (sid,),
        )[0]
        assert (status, last_run) == (src["status"], run_id), sid
        logged = change_log(dsn, run_id, sid)
        if src["status"] != "success":
            assert logged == {}, f"{sid}: fehlgeschlagene Quelle hat protokolliert"
            continue
        c = src["counts"]
        expected = {k: c[k] for k in ("new", "changed", "deleted") if c[k]}
        assert logged == expected, sid
        state = db_rows(
            dsn, "SELECT count(*) FROM pipeline_meta.record_state WHERE source_id = %s", (sid,)
        )[0][0]
        assert state == records == c["records"] == c["written"] == target_rows(dsn, sid), sid
        assert c["records"] == c["new"] + c["changed"] + c["unchanged"], sid
        assert c["received"] == c["records"] + c["skipped"] + c["merged"], sid
    status, summary = db_rows(
        dsn, "SELECT status, summary FROM pipeline_meta.import_run WHERE run_id = %s", (run_id,)
    )[0]
    assert status == report["status"]
    assert summary == {k: v for k, v in report["summary"].items() if k != "warnings"}


def assert_markdown(markdown: str, report: dict) -> None:
    assert markdown.startswith("# Import-Bericht vom ")
    assert f"`{report['run_id']}`" in markdown
    for src in report["sources"]:
        c = src["counts"]
        if src["status"] == "success":
            row = (
                f"| {src['id']} | {src['type'].upper()} | OK | {c['new']} | {c['changed']} | "
                f"{c['deleted']} | {c['unchanged']} | {c['skipped']} | {c['written']} |"
            )
            assert row in markdown, row
        else:
            assert f"| {src['id']} | {src['type'].upper()} | FEHLER |" in markdown
            assert f"- **{src['id']}**" in markdown
            assert src["error"]["message"][:40] in markdown


# ----------------------------------------------------------------- Änderungen der Quellen


def drop_last_gml_member(xml: bytes) -> bytes:
    text = xml.decode("utf-8")
    members = list(re.finditer(r"\s*<wfs:member>.*?</wfs:member>", text, flags=re.S))
    last = members[-1]
    return (text[: last.start()] + text[last.end() :]).encode("utf-8")


def shift_coords(coords, dx: float):
    if coords and isinstance(coords[0], (int, float)):
        return [coords[0] + dx, *coords[1:]]
    return [shift_coords(c, dx) for c in coords]


def edit_geojson(responder, rel: str, edit) -> None:
    data = json.loads(responder.read(rel))
    for feature in data.get("features", []):
        edit(feature)
    responder.overrides[rel] = json.dumps(data).encode("utf-8")


INKAR_ZIP = "files/inkar_2025.zip"
INKAR_MEMBER = "inkar_2025/inkar_2025.csv"


def edit_inkar(responder, edit) -> None:
    """Baut das INKAR-ZIP mit geänderter CSV neu auf (Zeilen als Listen)."""
    with zipfile.ZipFile(io.BytesIO(responder.read(INKAR_ZIP))) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    rows = list(csv.reader(io.StringIO(members[INKAR_MEMBER].decode("utf-8")), delimiter=";"))
    rows = [rows[0], *edit(rows[1:])]
    out = io.StringIO(newline="")
    csv.writer(out, delimiter=";", lineterminator="\n").writerows(rows)
    members[INKAR_MEMBER] = out.getvalue().encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    responder.overrides[INKAR_ZIP] = buffer.getvalue()


def apply_run2_changes(responder) -> int:
    # LGB (GML): Ortsteil umbenannt, letzter Ortsteil entfällt
    rel = "wfs/lgb_vg/getfeature_otl_start0_count3.xml"
    responder.overrides[rel] = responder.read(rel).replace(
        b"<app:otl_name>G\xc3\xb6ttin</app:otl_name>",
        "<app:otl_name>Göttin (Havel)</app:otl_name>".encode(),
    )
    rel = "wfs/lgb_vg/getfeature_otl_start6_count3.xml"
    responder.overrides[rel] = drop_last_gml_member(responder.read(rel))

    # LfU (GeoJSON): Geometrie eines Gebiets geändert, neues Feld in allen Objekten
    def lfu_edit(feature):
        feature["properties"]["Neues_Feld"] = "unerwartet"
        if feature["properties"]["Gebietsnummer"] == "3641-505":  # Stadthavel
            geom = feature["geometry"]
            geom["coordinates"] = shift_coords(geom["coordinates"], 0.0005)

    for start in (0, 4, 8):
        edit_geojson(
            responder, f"wfs/lfu_schutzg/getfeature_nsg_start{start}_count4.json", lfu_edit
        )

    # INKAR: Wert geändert, Zeile entfällt, neues Jahr; "40,50" statt "40,5" ist keine Änderung
    def inkar_edit(rows):
        out = []
        for row in rows:
            kennziffer, kuerzel, jahr = row[5], row[2], row[7]
            if (kennziffer, kuerzel, jahr) == ("12054000", "t_anteil", "2023"):
                row[8] = "41,0"
            if (kennziffer, kuerzel, jahr) == ("12054000", "t_anteil", "2022"):
                row[8] = "40,00"
            if (kennziffer, kuerzel, jahr) == ("12052000", "t_anzahl", "2022"):
                continue
            out.append(row)
            if (kennziffer, kuerzel, jahr) == ("12054000", "t_anzahl", "2023"):
                out.append([*row[:7], "2024", "4020"])
        return out

    edit_inkar(responder, inkar_edit)

    # CSV: geändert, gelöscht, neu, Zeile ohne Koordinaten, neue Spalte
    rel = "csv/ladesaeulen_potsdam.csv"
    text = responder.read(rel).decode("utf-8")
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=";"))
    header, body = rows[0], rows[1:]
    idx = {name: i for i, name in enumerate(header)}
    body[0][idx["nennleistung_ladeeinrichtung_kw"]] = "50.0"  # 1004465.0 geändert
    deleted = body.pop(1)  # 1004466.0 entfällt
    assert deleted[idx["ladeeinrichtungs_id"]] == "1004466.0"
    new_row = list(body[2])
    new_row[idx["ladeeinrichtungs_id"]] = "9999001.0"
    new_row[idx["breitengrad"]], new_row[idx["langengrad"]] = "52.40912", "12.96784"
    no_coords = list(body[3])
    no_coords[idx["ladeeinrichtungs_id"]] = "9999002.0"
    no_coords[idx["breitengrad"]] = no_coords[idx["langengrad"]] = ""
    body += [new_row, no_coords]
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow([*header, "neue_spalte"])
    for row in body:
        writer.writerow([*row, "x"])
    text = out.getvalue()
    responder.overrides[rel] = text.encode("utf-8")
    last = io.StringIO(newline="")
    csv.writer(last, delimiter=";", lineterminator="\r\n").writerow([*no_coords, "x"])
    return text[: text.rindex(last.getvalue())].count("\n") + 1  # Startzeile der letzten Zeile


def apply_run3_changes(responder) -> None:
    def lfu_edit(feature):
        props = feature["properties"]
        if props["Gebietsnummer"] == "3541-502":  # Mittlere Havel: gültige Änderung
            geom = feature["geometry"]
            geom["coordinates"] = shift_coords(geom["coordinates"], 0.0005)
        if props["Gebietsnummer"] == "3640-501":  # Gränert: Name länger als die Spalte
            props["Gebietsname"] = "Gränert " * 40

    edit_geojson(responder, "wfs/lfu_schutzg/getfeature_nsg_start0_count4.json", lfu_edit)


# ----------------------------------------------------------------- Tests


def test_end_to_end_three_runs(clean_db, fixture_server, tmp_path, capsys):
    dsn = clean_db
    responder = fixture_server.responder
    base = fixture_server.base_url
    cfg = write_config(tmp_path, base)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(FOREIGN_ROWS_SQL)
    foreign = foreign_rows(dsn)
    assert len(foreign) == 4

    assert main(["--config", str(cfg), "init-db"]) == 0
    assert "Migrationen angewendet (Schema pipeline_meta): 001_pipeline_meta" in (
        capsys.readouterr().out
    )
    assert main(["--config", str(cfg), "init-db"]) == 0
    assert "keine Migration ausstehend" in capsys.readouterr().out

    # ---------------------------------------------------------------- Lauf 1
    code, report, markdown = run_cli(cfg)
    assert code == 1  # eine Quelle (absichtlich) fehlgeschlagen
    assert report["status"] == "partial" and report["trigger"] == "manual"
    assert report["target_srid"] == 4326
    assert report["summary"] == {
        "sources_total": 5,
        "sources_succeeded": 4,
        "sources_failed": 1,
        "new": 8 + 8 + 133 + 16,
        "changed": 0,
        "deleted": 0,
        "unchanged": 0,
        "warnings": 2,
    }
    s = by_id(report)
    assert s[LGB]["counts"] == {
        "received": 8,
        "skipped": 0,
        "merged": 0,
        "new": 8,
        "changed": 0,
        "unchanged": 0,
        "deleted": 0,
        "records": 8,
        "target_removed": 0,
        "written": 8,
    }
    assert s[LGB]["target"] == "Geometrien (Demo) (source = 'LGB Verwaltungsgrenzen: Ortsteile')"
    assert s[LGB]["details"]["pages"] == 3 and s[LGB]["details"]["format_kind"] == "gml"
    assert s[LFU]["counts"]["received"] == 10 and s[LFU]["counts"]["merged"] == 2
    assert s[LFU]["counts"]["new"] == 8 and s[LFU]["details"]["format_kind"] == "geojson"
    assert s[LFU]["target"] == "Naturschutzgebiete (exklusiv)"
    assert warning_codes(s[LFU]) == {"duplicate_key_merged"}
    assert s[CSV_ID]["counts"]["new"] == 133 and s[CSV_ID]["warnings"] == []
    assert s[INKAR]["counts"]["new"] == 16 and s[INKAR]["warnings"] == []
    inkar_details = s[INKAR]["details"]
    assert inkar_details["year"] == 2025
    assert inkar_details["url_used"] == base + "/files/inkar_2025.zip"
    assert (inkar_details["rows"], inkar_details["rows_filtered"]) == (16, 4)
    assert s[BROKEN]["status"] == "failed"
    assert s[BROKEN]["error"]["type"] == "HttpError"
    assert "HTTP 500" in s[BROKEN]["error"]["message"]
    assert "ACCEPTVERSIONS" in s[BROKEN]["error"]["message"]  # Ursache der 2.0.0-Anfrage
    assert "Rückfall auf WFS 1.1.0 ebenfalls fehlgeschlagen" in s[BROKEN]["error"]["message"]
    assert_report_matches_db(dsn, report)
    assert_markdown(markdown, report)
    run1 = report["run_id"]

    # Geometrien in EPSG:4326 (Länge/Breite), Naturschutzgebiete als MULTIPOLYGON
    assert db_rows(dsn, 'SELECT DISTINCT ST_SRID(geom) FROM "Geometrien (Demo)"') == [(4326,)]
    x, y = db_rows(
        dsn,
        'SELECT ST_X(ST_Centroid(geom)), ST_Y(ST_Centroid(geom)) FROM "Geometrien (Demo)" '
        "WHERE source = %s AND geo_ref = '120510000075'",
        (TARGETS[LGB][1],),
    )[0]
    assert 12.2 < x < 12.9 and 52.2 < y < 52.6  # Göttin, Brandenburg an der Havel
    x, y = db_rows(
        dsn,
        'SELECT ST_X(geom), ST_Y(geom) FROM "Geometrien (Demo)" '
        "WHERE source = %s AND geo_ref = '1004465.0'",
        (TARGETS[CSV_ID][1],),
    )[0]
    assert 12.9 < x < 13.2 and 52.3 < y < 52.5  # Potsdam
    assert db_rows(
        dsn,
        'SELECT DISTINCT GeometryType(geom), ST_SRID(geom) FROM "Naturschutzgebiete"',
    ) == [("MULTIPOLYGON", 4326)]
    assert db_rows(
        dsn,
        'SELECT name, ST_NumGeometries(geom) FROM "Naturschutzgebiete" '
        "WHERE gebietsnummer = '3540-501'",
    ) == [("Große Freiheit bei Plaue", 2)]  # zwei Teilflächen zusammengefasst

    # INKAR in dashboard_data_de: AGS achtstellig als Text, Werte als Zahl
    inkar = db_rows(
        dsn,
        "SELECT kennziffer, indikator, zeitbezug, wert, granularity FROM dashboard_data_de "
        "WHERE source = 'INKAR (BBSR)' ORDER BY kennziffer, indikator, zeitbezug",
    )
    assert len(inkar) == 16 and {len(r[0]) for r in inkar} == {8}
    assert inkar[0] == (
        "12051000",
        "Anteil in Prozent (Testwert)",
        2022,
        decimal.Decimal("10"),
        "Gemeindeebene",
    )
    assert inkar[-1][:4] == ("12054000", "Anzahl (Testwert)", 2023, decimal.Decimal("4010"))
    assert foreign_rows(dsn) == foreign  # OSM-Kennzahlen und fremde Geometrie unberührt

    # DCAT-AP-Metadaten je Quelle in table_metadata
    meta = db_rows(
        dsn,
        "SELECT table_name, dcatde_politicalgeocodingleveluri, dct_title, dct_license, "
        "       dct_accessurl, dct_identifier, dct_language, imported IS NOT NULL "
        'FROM table_metadata ORDER BY table_name COLLATE "C"',
    )
    licence = "Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0)"
    level = "http://dcat-ap.de/def/politicalGeocoding/Level/"
    assert meta == [
        (
            "Naturschutzgebiete",
            level + "state",
            "Naturschutzgebiete im Raum Brandenburg an der Havel",
            licence,
            base + "/wfs/lfu_schutzg",
            LFU,
            "de",
            True,
        ),
        (
            "dashboard_data_de: INKAR (BBSR)",
            level + "municipality",
            "INKAR – Indikatoren je Gemeinde (Brandenburg)",
            licence,
            base + "/files/inkar_2025.zip",
            INKAR,
            "de",
            True,
        ),
    ]
    summary = db_rows(
        dsn,
        "SELECT source_id, new, changed, deleted FROM pipeline_meta.change_summary "
        'WHERE run_id = %s ORDER BY source_id COLLATE "C"',
        (run1,),
    )
    assert summary == [(CSV_ID, 133, 0, 0), (INKAR, 16, 0, 0), (LFU, 8, 0, 0), (LGB, 8, 0, 0)]

    # ---------------------------------------------------------------- Lauf 2
    no_coords_line = apply_run2_changes(responder)
    code, report, markdown = run_cli(cfg)
    assert code == 1
    s = by_id(report)
    assert s[LGB]["counts"] == {
        "received": 7,
        "skipped": 0,
        "merged": 0,
        "new": 0,
        "changed": 1,
        "unchanged": 6,
        "deleted": 1,
        "records": 7,
        "target_removed": 8,
        "written": 7,
    }
    assert s[LFU]["counts"] == {
        "received": 10,
        "skipped": 0,
        "merged": 2,
        "new": 0,
        "changed": 1,
        "unchanged": 7,
        "deleted": 0,
        "records": 8,
        "target_removed": 8,
        "written": 8,
    }
    assert warning_codes(s[LFU]) == {"duplicate_key_merged", "schema_unexpected_field"}
    assert s[CSV_ID]["counts"] == {
        "received": 134,
        "skipped": 1,
        "merged": 0,
        "new": 1,
        "changed": 1,
        "unchanged": 131,
        "deleted": 1,
        "records": 133,
        "target_removed": 133,
        "written": 133,
    }
    csv_warnings = {w["code"]: w for w in s[CSV_ID]["warnings"]}
    assert csv_warnings["missing_geometry"]["examples"] == [f"9999002.0 (Zeile {no_coords_line})"]
    assert csv_warnings["schema_changed"]["examples"] == ["neu: neue_spalte"]
    assert s[INKAR]["counts"] == {
        "received": 16,
        "skipped": 0,
        "merged": 0,
        "new": 1,
        "changed": 1,
        "unchanged": 14,
        "deleted": 1,
        "records": 16,
        "target_removed": 16,
        "written": 16,
    }
    assert report["summary"]["changed"] == 4 and report["summary"]["deleted"] == 3
    assert report["summary"]["new"] == 2
    assert_report_matches_db(dsn, report)
    assert_markdown(markdown, report)
    assert "Unerwartete Felder" in markdown and "Neues_Feld" in markdown
    run2 = report["run_id"]

    # Zeilen der Quelle ersetzt: neuer Name, entfernter Ortsteil fehlt, Fremdes unberührt
    assert db_rows(
        dsn,
        'SELECT geo_ref, name FROM "Geometrien (Demo)" WHERE source = %s AND geo_ref IN '
        "('120510000075', '120510000245') ORDER BY geo_ref",
        (TARGETS[LGB][1],),
    ) == [("120510000075", "Göttin (Havel)")]
    assert foreign_rows(dsn) == foreign
    # Änderungsprotokoll: was, wann (Lauf), alter und neuer Fingerabdruck
    history = db_rows(
        dsn,
        "SELECT run_id::text, action, old_hash IS NOT NULL, new_hash IS NOT NULL "
        "FROM pipeline_meta.change_log WHERE source_id = %s AND record_key = %s ORDER BY id",
        (LGB, "120510000075"),
    )
    assert history == [(run1, "new", False, True), (run2, "changed", True, True)]
    removed = db_rows(
        dsn,
        "SELECT action, new_hash FROM pipeline_meta.change_log "
        "WHERE run_id = %s AND source_id = %s AND record_key = '120510000245'",
        (run2, LGB),
    )
    assert removed == [("deleted", None)]
    inkar_log = db_rows(
        dsn,
        "SELECT record_key, action FROM pipeline_meta.change_log "
        'WHERE run_id = %s AND source_id = %s ORDER BY record_key COLLATE "C"',
        (run2, INKAR),
    )
    assert inkar_log == [
        ("12052000|Testbereich A|t_anzahl|Anzahl (Testwert)|2022", "deleted"),
        ("12054000|Testbereich A|t_anzahl|Anzahl (Testwert)|2024", "new"),
        ("12054000|Testbereich B|t_anteil|Anteil in Prozent (Testwert)|2023", "changed"),
    ]
    assert db_rows(
        dsn,
        "SELECT zeitbezug, wert FROM dashboard_data_de WHERE source = 'INKAR (BBSR)' "
        "AND kennziffer = '12054000' AND indikator = 'Anteil in Prozent (Testwert)' "
        "ORDER BY zeitbezug",
    ) == [(2022, decimal.Decimal("40")), (2023, decimal.Decimal("41"))]

    # ---------------------------------------------------------------- Lauf 3
    nsg_before = db_rows(
        dsn,
        'SELECT id, gebietsnummer, name, ST_AsBinary(geom) FROM "Naturschutzgebiete" ORDER BY id',
    )
    state_before = db_rows(
        dsn,
        "SELECT record_key, content_hash FROM pipeline_meta.record_state "
        "WHERE source_id = %s ORDER BY record_key",
        (LFU,),
    )
    apply_run3_changes(responder)
    code, report, markdown = run_cli(cfg)
    assert code == 1
    s = by_id(report)
    assert s[LFU]["status"] == "failed"
    assert s[LFU]["error"]["type"] == "DatabaseError"
    assert s[LFU]["error"]["sqlstate"] == "22001"  # Wert zu lang für die Zielspalte
    assert s[LGB]["status"] == "success" and s[LGB]["counts"]["unchanged"] == 7
    assert s[CSV_ID]["status"] == "success" and s[CSV_ID]["counts"]["unchanged"] == 133
    assert s[INKAR]["status"] == "success" and s[INKAR]["counts"]["unchanged"] == 16
    assert report["summary"]["sources_failed"] == 2
    assert_report_matches_db(dsn, report)
    assert_markdown(markdown, report)
    nsg_after = db_rows(
        dsn,
        'SELECT id, gebietsnummer, name, ST_AsBinary(geom) FROM "Naturschutzgebiete" ORDER BY id',
    )
    assert nsg_after == nsg_before  # vollständig zurückgerollt, auch die gültige Änderung
    state_after = db_rows(
        dsn,
        "SELECT record_key, content_hash FROM pipeline_meta.record_state "
        "WHERE source_id = %s ORDER BY record_key",
        (LFU,),
    )
    assert state_after == state_before
    status = db_rows(
        dsn,
        "SELECT last_status, last_success_at = (SELECT started_at FROM pipeline_meta.import_run "
        "WHERE run_id = %s), last_error IS NOT NULL FROM pipeline_meta.source_state "
        "WHERE source_id = %s",
        (run2, LFU),
    )[0]
    assert status == ("failed", True, True)
    assert foreign_rows(dsn) == foreign


def test_single_source_and_unknown_source(clean_db, fixture_server, tmp_path):
    cfg = write_config(tmp_path, fixture_server.base_url)
    code, report, _ = run_cli(cfg, "--source", INKAR)
    assert code == 0 and report["status"] == "success"
    assert [s["id"] for s in report["sources"]] == [INKAR]
    assert target_rows(clean_db, INKAR) == 16
    assert main(["--config", str(cfg), "run", "--source", "unbekannt"]) == 2


def test_target_checks_before_writing(clean_db, fixture_server, tmp_path):
    """Passt eine Zieltabelle nicht zur Konfiguration, wird nichts geschrieben."""
    cfg = write_config(tmp_path, fixture_server.base_url)
    edit_source(
        cfg,
        "bnetza_ladesaeulen_potsdam.yml",
        target={"columns": {"geo_ref": "@key", "betreiber_name": "betreiber"}},
    )
    edit_source(cfg, "inkar_gemeinden.yml", target={"table": "dashboard_data_xx"})
    edit_source(cfg, "lgb_ortsteile_brandenburg_havel.yml", target={"columns": {"name": "name"}})
    text = cfg.read_text("utf-8").replace("target_srid: 4326", "target_srid: 25833")
    cfg.write_text(text, "utf-8")
    code, report, markdown = run_cli(cfg)
    assert code == 1 and report["summary"]["sources_succeeded"] == 0
    errors = {sid: src["error"] for sid, src in by_id(report).items()}
    assert errors[CSV_ID]["type"] == "TargetError"
    assert "betreiber_name fehlen" in errors[CSV_ID]["message"]
    assert "dashboard_data_xx" in errors[INKAR]["message"]
    assert "existiert nicht" in errors[INKAR]["message"]
    assert "Pflichtspalte(n) geo_ref" in errors[LGB]["message"]
    assert "EPSG:4326" in errors[LFU]["message"] and "EPSG:25833" in errors[LFU]["message"]
    assert "## Fehlgeschlagene Quellen" in markdown
    for table in ('"Geometrien (Demo)"', '"Naturschutzgebiete"', "dashboard_data_de"):
        assert db_rows(clean_db, f"SELECT count(*) FROM {table}")[0][0] == 0
    assert db_rows(clean_db, "SELECT count(*) FROM pipeline_meta.change_log")[0][0] == 0


def test_plausibility_and_incomplete_fetch(clean_db, fixture_server, tmp_path):
    """Massenlöschung wird abgewiesen; ohne vollständigen Abruf gilt nichts als gelöscht."""
    responder = fixture_server.responder
    cfg = write_config(tmp_path, fixture_server.base_url)
    code, report, _ = run_cli(cfg, "--source", CSV_ID, "--source", LGB)
    assert code == 0 and report["summary"]["new"] == 133 + 8

    rel = "csv/ladesaeulen_potsdam.csv"
    rows = list(
        csv.reader(io.StringIO(responder.read(rel).decode("utf-8"), newline=""), delimiter=";")
    )

    def serve(body: list[list[str]]) -> None:
        out = io.StringIO(newline="")
        csv.writer(out, delimiter=";", lineterminator="\r\n").writerows([rows[0], *body])
        responder.overrides[rel] = out.getvalue().encode("utf-8")

    # (a) nur noch 10 von 133 Ladesäulen: unplausibel, nichts wird ersetzt
    serve(rows[1:11])
    code, report, markdown = run_cli(cfg, "--source", CSV_ID)
    src = by_id(report)[CSV_ID]
    assert code == 1 and src["error"]["type"] == "PlausibilityError"
    assert "123 von 133 Datensätzen würden entfernt" in src["error"]["message"]
    assert "## Fehlgeschlagene Quellen" in markdown
    assert target_rows(clean_db, CSV_ID) == 133
    assert_report_matches_db(clean_db, report)

    # (b) 5 von 133 Zeilen ohne Koordinaten (> 2 %): Abruf nicht fehlerfrei
    idx = {name: i for i, name in enumerate(rows[0])}
    body = [list(r) for r in rows[1:]]
    for row in body[:5]:
        row[idx["breitengrad"]] = row[idx["langengrad"]] = ""
    serve(body)
    code, report, _ = run_cli(cfg, "--source", CSV_ID)
    src = by_id(report)[CSV_ID]
    assert code == 1 and src["error"]["type"] == "IncompleteFetchError"
    assert "5 von 133 Datensätzen" in src["error"]["message"]
    assert warning_codes(src) == {"missing_geometry"}
    assert target_rows(clean_db, CSV_ID) == 133

    # (c) bewusst begrenzter WFS-Abruf (max_features): Fehlendes gilt nicht als gelöscht
    edit_source(cfg, "lgb_ortsteile_brandenburg_havel.yml", max_features=6)
    code, report, _ = run_cli(cfg, "--source", LGB)
    src = by_id(report)[LGB]
    assert code == 1 and src["error"]["type"] == "IncompleteFetchError"
    assert "max_features" in src["error"]["message"]
    assert target_rows(clean_db, LGB) == 8
    assert_report_matches_db(clean_db, report)


def test_runtime_role_without_ddl_rights(clean_db, fixture_server, tmp_path, monkeypatch):
    """Nach ``init-db`` genügt für ``geoimport run`` eine Rolle ohne DDL-Rechte.

    Die Rechte entsprechen docs/betrieb.md: Zeilen lesen, einfügen und löschen in
    den Zieltabellen, das eigene Schema der Pipeline beschreiben, temporäre Tabellen.
    """
    cfg = write_config(tmp_path, fixture_server.base_url)
    assert main(["--config", str(cfg), "--log-level", "WARNING", "init-db"]) == 0
    role = "geoimport_test_betrieb"
    password = secrets.token_urlsafe(16)
    ident = sql.Identifier(role)
    with psycopg.connect(clean_db, autocommit=True) as conn:
        allowed = conn.execute(
            "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]
        if not allowed:
            pytest.skip("Testbenutzer darf keine Rollen anlegen")
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            conn.execute(sql.SQL("DROP OWNED BY {}").format(ident))
            conn.execute(sql.SQL("DROP ROLE {}").format(ident))
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(ident, sql.Literal(password))
        )
        for stmt in (
            "GRANT CONNECT, TEMPORARY ON DATABASE {db} TO {role}",
            "GRANT USAGE ON SCHEMA public, pipeline_meta TO {role}",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA pipeline_meta TO {role}",
            "GRANT SELECT, INSERT, DELETE ON dashboard_data_de, table_metadata, "
            '"Geometrien (Demo)", "Naturschutzgebiete" TO {role}',
            "GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {role}",
        ):
            conn.execute(sql.SQL(stmt).format(db=sql.Identifier(dbname), role=ident))
    try:
        role_dsn = make_conninfo(clean_db, user=role, password=password)
        with psycopg.connect(role_dsn, autocommit=True) as conn:
            for ddl in (
                "CREATE TABLE public.ddl_probe (x int)",
                "CREATE TABLE pipeline_meta.ddl_probe (x int)",
                "ALTER TABLE dashboard_data_de ADD COLUMN x int",
                "DROP TABLE dashboard_data_de",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(ddl)
        monkeypatch.setenv("GEOIMPORT_DB_DSN", role_dsn)
        code, report, _ = run_cli(cfg)
        assert code == 1  # nur die absichtlich defekte Quelle schlägt fehl
        assert report["summary"]["sources_succeeded"] == 4
        assert report["summary"]["new"] == 165
        code, report, _ = run_cli(cfg)
        assert report["summary"]["unchanged"] == 165 and report["summary"]["new"] == 0
        assert_report_matches_db(clean_db, report)
    finally:
        with psycopg.connect(clean_db, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(ident))
            conn.execute(sql.SQL("DROP ROLE {}").format(ident))
