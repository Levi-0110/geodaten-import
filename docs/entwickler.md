# Entwicklung

## Entwicklungsumgebung

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check src tests && ruff format --check src tests
pytest                                          # Unit-Tests
scripts/test-datenbank.sh start                 # lokale PostGIS-Testdatenbank
export GEOIMPORT_TEST_DSN="$(scripts/test-datenbank.sh dsn)"
pytest                                          # zusätzlich End-to-End-Tests
```

Konventionen: Bezeichner im Code englisch, Meldungen, Berichte und
Dokumentation deutsch. Abhängigkeiten nur mit freizügiger oder LGPL-Lizenz
(`docs/lizenzen.md`).

## Aufbau

| Modul | Aufgabe |
|---|---|
| `cli.py` | Kommandos `run`, `schedule`, `init-db`, `check-config`; Rückgabewerte |
| `config.py` | pydantic-Modelle für `pipeline.yml` und Quellen, Laden, `${VAR}`-Ersetzung |
| `sources/base.py` | Adapter-Schnittstelle `SourceAdapter`, Registrierung |
| `sources/wfs.py` | WFS 2.0.0/1.1.0: Capabilities, DescribeFeatureType, Paginierung, GML/GeoJSON |
| `sources/csv_source.py` | CSV aus Datei, URL oder ZIP-Archiv (z. B. INKAR), Filter, Zahlen, AGS |
| `geometry.py` | GML-Geometrien, KBS-Kennungen, Achsenreihenfolge, WKB |
| `mapping.py` | Schlüssel, Attribut-Mapping, Geometrieprüfung, Schemaprüfung (Streaming) |
| `hashing.py` | Normierung der Werte und Inhalts-Hash |
| `store.py` | Staging per `COPY`, Dubletten, Änderungserkennung, Änderungsprotokoll |
| `targets.py` | Prüfung der Zieltabelle, Ersetzen der Zeilen einer Quelle, DCAT-AP-Zeile |
| `pipeline.py` | Ablauf je Lauf und je Quelle, Transaktionen, Fehlerbehandlung |
| `report.py` | Bericht als JSON und Markdown |
| `scheduler.py`, `cron.py` | Zeitsteuerung (APScheduler) mit crontab-Schreibweise |
| `db.py`, `migrations/` | Verbindung, SQL-Migrationen, Schemaprüfung |

Datenfluss je Quelle, in einer Transaktion: Adapter liefert `RawRecord`s
(Attribute, Shapely-Geometrie, SRID) → `mapping.FeatureStream` bildet daraus
nacheinander `Feature`s (Schlüssel, Zielattribute) → `store.load_stage` schreibt
sie per `COPY` in eine temporäre Staging-Tabelle → Vollständigkeits- und
Dublettenprüfung → `store.compare_with_state` und `store.write_change_log`
(neu/geändert/entfernt) → `targets.TargetPlan.replace_rows` ersetzt die Zeilen
der Quelle in der Zieltabelle → Ergebnis und Warnungen gehen in den Bericht.
Es wird nie die ganze Quelle im Speicher gehalten; auch die INKAR-Gesamtdatei
für Deutschland passt so in wenig Arbeitsspeicher. Alles außer dem Adapter ist
quellenunabhängig.

## Neue Quelle hinzufügen (nur Konfiguration)

Eine neue Datei in `config/sources/` anlegen, z. B. `config/sources/meine_quelle.yml`.
Der Dateiname ist frei; maßgeblich ist `id` (Kleinbuchstaben, Ziffern, `_`, `-`).
Danach:

```bash
geoimport check-config                  # Schema der YAML-Datei prüfen
geoimport run --source meine_quelle     # nur diese Quelle importieren
cat reports/latest.md                   # Kennzahlen und Warnungen ansehen
```

Fehlerhafte Konfigurationen werden mit Datei und Feld gemeldet, z. B.:

```text
Fehler in der Konfiguration:
config/sources/meine_quelle.yml: ungültige Konfiguration
  - wfs.key_field: Field required
```

### WFS-Quelle

Minimal:

```yaml
id: lgb_gemeinden
type: wfs
title: Gemeinden Brandenburg
url: https://isk.geobasis-bb.de/ows/vg_wfs
type_name: app:gem
key_fields: ags
attributes:            # Quellfeld: Zielattribut
  ags: gemeinde_schluessel
  gem_name: name
target:                # vorhandene Tabelle, siehe docs/datenmodell.md
  table: Geometrien (Demo)
  source_column: source
  source_value: LGB Verwaltungsgrenzen Gemeinden
  columns: {geo_ref: gemeinde_schluessel, name: name}
  geometry_column: geom
metadata:
  provider: Landesvermessung und Geobasisinformation Brandenburg (LGB)
  licence: Datenlizenz Deutschland – Namensnennung – Version 2.0
  licence_url: https://www.govdata.de/dl-de/by-2-0
  attribution: "© GeoBasis-DE/LGB, dl-de/by-2-0"
  update_cycle: vierteljährlich
```

Weitere Optionen:

| Option | Standard | Bedeutung |
|---|---|---|
| `type_name` (auch `typeName`, `layer`) | – | Feature-Typ, z. B. `app:otl` |
| `version` | `auto` | `auto` (aus den Capabilities, bevorzugt 2.0.0, sonst 1.1.0), `2.0.0`, `1.1.0` |
| `output_format` | `auto` | `auto`: GeoJSON, wenn angeboten, sonst GML 3.2; `geojson`, `gml` oder ein exakter MIME-Typ |
| `srs_name` | DefaultCRS | angefordertes KBS (`SRSNAME`); GeoJSON wird nach RFC 7946 als WGS 84 gelesen, sofern die Antwort kein `crs`-Element enthält |
| `source_crs` | – | KBS der gelieferten Koordinaten erzwingen, falls der Dienst es falsch oder gar nicht angibt |
| `geometry_field` | erstes Geometriefeld | Name der Geometrie-Eigenschaft bei mehreren Geometrien |
| `page_size` | `1000` | Objekte je Seite; wird auf `CountDefault` des Servers begrenzt (Warnung) |
| `paging` | `auto` | `auto`: Paginierung, wenn der Dienst `ImplementsResultPaging` nicht verneint; `always`, `never` |
| `max_features` | – | Obergrenze der Objekte (Tests, Stichproben); ein so begrenzter Abruf gilt als unvollständig, die Quelle wird dann nicht geschrieben |
| `sort_by` | – | `SORTBY`, für stabile Paginierung empfohlen (z. B. Schlüsselfeld) |
| `bbox`, `bbox_crs` | – | räumlicher Ausschnitt `[minx, miny, maxx, maxy]` |
| `filter` | – | FES-2.0- bzw. OGC-Filter als XML (schließt `bbox` aus) |
| `namespaces` | – | Präfix → Namensraum, für `NAMESPACES`-Parameter und Filter |
| `extra_params` | – | zusätzliche Anfrageparameter (z. B. herstellerspezifische) |
| `use_advertised_urls` | `false` | GetFeature an die URL aus den Capabilities statt an `url` senden |

Hinweise aus der Praxis (an den Brandenburger deegree-Diensten geprüft):
Manche Dienste melden `ImplementsResultPaging=FALSE`, paginieren aber korrekt –
dann `paging: always`. Ignoriert ein Dienst `STARTINDEX`, bricht der Adapter mit
einer klaren Meldung ab, statt Dubletten zu erzeugen. Ohne Paginierung wird eine
gekürzte Antwort (`CountDefault` erreicht oder `numberMatched` größer als
geliefert) als Fehler gemeldet. Objekte, die aus mehreren Teilflächen mit
gleichem Schlüssel bestehen, mit `duplicate_keys: merge` zusammenführen.

### CSV-Quelle

Per URL mit Länge/Breite:

```yaml
id: bnetza_ladesaeulen_potsdam
type: csv
url: https://opendata.potsdam.de/api/explore/v2.1/catalog/datasets/ladesaeulenregister/exports/csv
delimiter: ";"
geometry: {type: lonlat, lon: langengrad, lat: breitengrad}
source_crs: EPSG:4326
key_fields: ladeeinrichtungs_id
attributes: {betreiber: betreiber, status: status}
target:
  table: Geometrien (Demo)
  source_column: source
  source_value: BNetzA Ladesäulenregister
  columns: {geo_ref: "@key", name: betreiber}
  geometry_column: geom
metadata: {provider: Bundesnetzagentur, licence: CC BY 4.0}
```

Kennzahlen ohne Geometrie aus einer ZIP-Datei, neueste Jahresdatei
(Ausschnitt aus `config/sources/inkar_gemeinden.yml`):

```yaml
id: inkar_gemeinden
type: csv
url: https://www.bbr-server.de/imagemap/inkar/download/inkar_{year}.zip
latest_year: {back: 5}              # aktuelles Jahr + 1, dann bis zu 5 Jahre zurück
archive_member: "inkar*.csv"        # Datei im Archiv (Muster, genau ein Treffer)
fallback_path: ../daten/inkar.zip   # optional: abgelegte Datei, falls die URL ausfällt
delimiter: ";"
decimal: ","
row_filter: {Raumbezug: Gemeinden, Kennziffer: "12*"}
numeric_fields: [Wert]              # exakt als Dezimalzahl, Dezimalkomma erlaubt
zero_pad: {Kennziffer: 8}           # AGS achtstellig (01001000)
key_fields: [Kennziffer, Bereich, Kürzel, Indikator, Zeitbezug]
duplicate_keys: error
schedule: "0 4 2 * *"               # monatlich
target:
  table: dashboard_data_de
  source_column: source
  source_value: INKAR (BBSR)
  columns: {kennziffer: kennziffer, indikator: indikator, zeitbezug: zeitbezug, wert: wert}
  constants: {granularity: Gemeindeebene}
```

Lokale Datei mit WKT-Geometrie, Latin-1 und Dezimalkomma:

```yaml
id: messstellen
type: csv
path: ../daten/messstellen.csv      # relativ zur YAML-Datei
encoding: latin-1
delimiter: ";"
decimal: ","
geometry: {type: wkt, column: geometrie}
source_crs: EPSG:25833
key_fields: [gebiet, messstelle]    # zusammengesetzter Schlüssel ("gebiet|messstelle")
attributes: {bezeichnung: name, messwert: wert}
target:                             # Tabelle gehört nur dieser Quelle
  table: messstellen
  exclusive: true
  columns: {schluessel: "@key", name: name, wert: wert}
  geometry_column: geom
metadata: {provider: Beispielamt, licence: dl-de/by-2-0}
```

| Option | Standard | Bedeutung |
|---|---|---|
| `path` oder `url` | – | genau eines von beiden; `{year}` in `url` zusammen mit `latest_year` |
| `latest_year` | – | `{first: <jahr>, back: <anzahl>}`: neueste vorhandene Jahresdatei suchen (Standard `first` = aktuelles Jahr + 1) |
| `fallback_path` | – | abgelegte Datei, falls die URL nicht abrufbar ist (Warnung im Bericht) |
| `archive_member` | – | Muster der CSV-Datei in einem ZIP-Archiv (Pflicht bei ZIP) |
| `delimiter`, `quotechar` | `,` / `"` | Trennzeichen (`\t` für Tabulator), Textbegrenzer |
| `encoding` | `utf-8` | Zeichenkodierung; ein UTF-8-BOM wird erkannt |
| `decimal` | `.` | Dezimaltrennzeichen der Koordinaten und Zahlenfelder |
| `geometry` | – | `{type: lonlat, lon: <spalte>, lat: <spalte>}` (auch `x`/`y` für Rechts-/Hochwert) oder `{type: wkt, column: <spalte>}`; weglassen bei reinen Sachdaten |
| `row_filter` | – | nur Zeilen übernehmen, deren Spalten zu den Mustern passen (`*`, `?`; Liste = eines davon) |
| `numeric_fields` | – | Spalten als exakte Dezimalzahl lesen; unlesbare Werte → Zeile übersprungen mit Warnung |
| `zero_pad` | – | `{spalte: länge}`: Ziffernfolgen mit führenden Nullen auffüllen (vor dem Filter) |
| `source_crs` | `EPSG:4326` | KBS der Koordinaten bzw. des WKT |
| `empty_as_null` | `true` | leere Zellen als NULL speichern |

Zeilen ohne oder mit ungültigen Koordinaten (z. B. Breite > 90°, typisch für
vertauschte Spalten) werden mit Zeilennummer im Bericht gemeldet und übersprungen.

### Gemeinsame Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| `key_fields` (auch `key_field`) | – | fachlicher Schlüssel; Liste für zusammengesetzte Schlüssel; `@id` = Objekt-ID des WFS (`gml:id`) |
| `attributes` | alle Felder | Mapping `quellfeld: zielattribut`; leer = alle Felder unverändert übernehmen |
| `keep_unmapped` | `false` | zusätzlich alle nicht gemappten Felder übernehmen |
| `target` | – | Zieltabelle und Spaltenbelegung (Pflicht), siehe `docs/datenmodell.md` |
| `metadata_record` | – | optional eine Metadatenzeile je Lauf, z. B. DCAT-AP in `table_metadata` |
| `schedule` | Standardtakt | eigener Takt der Quelle (Cron-Ausdruck) |
| `max_delete_ratio` | `0.5` | Abbruch der Quelle, wenn mehr als dieser Anteil entfernt würde |
| `max_skipped_ratio` | `0` | höchstens dieser Anteil unbrauchbarer Datensätze; sonst gilt der Abruf als unvollständig |
| `duplicate_keys` | `warn` | `warn` (erstes Objekt), `merge` (Geometrien vereinigen), `error` (Quelle abbrechen) |
| `repair_invalid_geometries` | `false` | ungültige Geometrien mit `make_valid` reparieren statt nur zu melden |
| `enabled` | `true` | Quelle bei `geoimport run` ohne `--source` auslassen |
| `title`, `metadata.*` | – | Anbieter, Lizenz, Lizenz-URL, Namensnennung, Aktualisierungszyklus, Beschreibung; in `pipeline_meta.source_state` und als Platzhalter für `metadata_record` |

## Adapter-Schnittstelle (neuer Quelltyp)

Ein neuer Quelltyp – etwa OGC API Features oder eine Datenbanktabelle – braucht
eine Adapterklasse und ein Konfigurationsmodell. Mapping, Hashing,
Änderungsprotokoll, Zieltabellen, Transaktionen und Bericht werden unverändert
genutzt.

```python
# src/geoimport/sources/ogcapi.py
from collections.abc import Iterator

import shapely.geometry

from geoimport.models import RawRecord
from geoimport.sources.base import SourceAdapter, SourceError, register


@register
class OgcApiFeaturesAdapter(SourceAdapter):
    source_type = "ogcapi"  # Wert von "type" in der YAML-Datei

    def read(self) -> Iterator[RawRecord]:
        url = f"{self.config.url}/collections/{self.config.collection}/items"
        params = {"limit": self.config.page_size, "f": "json"}
        while url:
            data = self.http.get(url, params=params).json()
            for f in data.get("features", []):
                yield RawRecord(
                    properties=f.get("properties") or {},
                    geometry=shapely.geometry.shape(f["geometry"]) if f.get("geometry") else None,
                    srid=4326,
                    feature_id=str(f.get("id")),
                )
            links = data.get("links", [])
            url = next((x["href"] for x in links if x.get("rel") == "next"), None)
            params = None
        self.details["pages_read"] = "..."  # erscheint im Bericht unter details

    def declared_fields(self) -> list[str] | None:
        return None  # optional: Felder laut Schema der Quelle
```

Dazu:

1. In `config.py` ein Modell `OgcApiSource(SourceBase)` mit `type: Literal["ogcapi"]`
   und den eigenen Feldern anlegen und in die Union `SourceConfig` aufnehmen.
2. Das Modul in `sources/__init__.py` importieren (Registrierung).
3. Tests mit aufgezeichneten Antworten unter `tests/fixtures/` und einem Eintrag
   im Testserver (`tests/fixture_server.py`).

Vertrag der Schnittstelle:

- `read()` liefert alle Datensätze (über alle Seiten) als Generator. Fehler, die
  die ganze Quelle betreffen, als `SourceError` (oder Unterklasse) auslösen;
  einzelne unbrauchbare Datensätze als `RawRecord` mit `geometry=None`,
  `geometry_error` oder `problem` liefern – sie werden gezählt und gemeldet.
- Ist der Abruf bewusst unvollständig (z. B. Obergrenze erreicht), setzt der
  Adapter `self.complete = False`; die Pipeline schreibt die Quelle dann nicht.
- `RawRecord.srid` ist das KBS der gelieferten Koordinaten in
  x/y-Reihenfolge (Ost/Nord bzw. Länge/Breite). Die Transformation ins Ziel-KBS
  übernimmt PostGIS.
- `self.warnings.add(code, example=...)` für Auffälligkeiten; `self.details` für
  technische Angaben im Bericht (Seiten, Format, Version ...).
- Kein Datenbankzugriff im Adapter.

## Änderungserkennung

Der Inhalts-Hash ist SHA-256 über eine kanonische JSON-Darstellung aus den
gemappten Attributen (als normierter Text, `NULL` bleibt `NULL`, Zahlen ohne
überflüssige Nullen), dem 2D-WKB der Geometrie im Quell-KBS und dessen SRID.
Bei Zielen ohne Geometriespalte zählt nur der Sachinhalt. Änderungen am Mapping
erscheinen beim nächsten Lauf als „geändert“, reine Reihenfolgeänderungen in der
Quelle nicht. Einzelheiten: `docs/datenmodell.md`.

## Warnungen im Bericht

| Code | Bedeutung |
|---|---|
| `missing_key` | Datensatz ohne Schlüsselwert übersprungen |
| `missing_geometry`, `geometry_unreadable` | Datensatz ohne bzw. mit unbrauchbarer Geometrie (z. B. Koordinaten außerhalb des Wertebereichs) übersprungen |
| `invalid_geometry`, `invalid_geometry_repaired` | ungültige Geometrie übernommen bzw. repariert |
| `duplicate_key`, `duplicate_key_merged`, `duplicate_key_conflict` | doppelte Schlüssel (erstes Objekt, zusammengeführt, abweichende Attribute) |
| `schema_mapped_field_missing` | im Mapping konfiguriertes Feld fehlt in der Quelle |
| `schema_unexpected_field` | Feld geliefert, das nicht im Schema (DescribeFeatureType) steht |
| `schema_changed` | Felder seit dem letzten erfolgreichen Lauf neu oder entfallen |
| `schema_unavailable` | DescribeFeatureType nicht abrufbar |
| `csv_extra_values` | CSV-Zeile mit mehr Werten als Spalten |
| `number_unreadable` | Zahlenfeld nicht lesbar, Zeile übersprungen |
| `fallback_file_used` | URL nicht abrufbar, abgelegte Ersatzdatei verwendet |
| `page_size_limited` | Server begrenzt die Seitengröße (`CountDefault`) |

## Tests

| Datei | Inhalt |
|---|---|
| `test_config.py` | Validierung, Zielangaben, Eigentum an Tabellen, Takte, Fehlermeldungen |
| `test_mapping.py`, `test_hashing.py` | Mapping, Schlüssel, Streaming, Geometrieprüfung, Hash-Stabilität |
| `test_csv.py` | CSV mit der echten Datei und Varianten (Latin-1, BOM, WKT, Fehlerfälle), INKAR-ZIP, Filter, AGS, Jahressuche, Ersatzdatei |
| `test_store.py` | Staging und doppelte Schlüssel in PostgreSQL (warn, merge, error) |
| `test_gml.py` | GML-Varianten, Achsenreihenfolge, aufgezeichnete GML-/GeoJSON-Seiten, XML-Angriffe |
| `test_wfs.py` | Capabilities, Paginierung gegen aufgezeichnete Antworten, CountDefault, 1.1.0-Rückfall, Fehlerfälle |
| `test_report.py` | Berichtsformat, Dateien, Status, Cron-Ausdrücke, Takte je Quelle |
| `test_e2e.py` | Läufe gegen PostGIS und lokalen HTTP-Server: Erstimport, Änderungen mit Protokoll, Zeilen anderer Quellen unberührt, Rollback, Zielprüfung, Plausibilität, unvollständiger Abruf, Betriebsrolle ohne DDL-Rechte |

`tests/fixture_server.py` beantwortet WFS-Anfragen anhand der Parameter
(`REQUEST`, `TYPENAMES`, `STARTINDEX`, `COUNT` ...) mit Dateien aus
`tests/fixtures/`; unbekannte Anfragen ergeben einen OWS-ExceptionReport.
Für Änderungsszenarien setzen Tests `responder.overrides[<datei>]`. Herkunft der
Aufzeichnungen: `tests/fixtures/QUELLEN.md`.

## Migrationen

Neue Migrationen als `src/geoimport/migrations/NNN_beschreibung.sql` anlegen
(fortlaufende Nummer). Bereits ausgelieferte Migrationen nicht mehr ändern; die
Pipeline speichert Prüfsummen und warnt bei nachträglichen Änderungen. Der
Platzhalter `{{meta_schema}}` wird durch das konfigurierte Schema der Pipeline
ersetzt. Migrationen legen nur Objekte in diesem Schema an, nie in den
Zieltabellen.
