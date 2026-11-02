# Herkunft und Lizenzen der Testdaten

Die Dateien unter `wfs/` und `csv/` sind aufgezeichnete Antworten öffentlicher
Dienste (abgerufen am 31.10.2026). Die Datei unter `files/` ist künstlich
(Abschnitt „INKAR-Format“ unten). Sie werden von den Tests über einen lokalen
HTTP-Server bzw. einen httpx-Mock-Transport ausgeliefert, sodass die Tests ohne
Internetzugang laufen. Gesamtgröße unter 1 MB.

Abweichungen von den Originalantworten:

- In den Capabilities-Dokumenten wurde der Inhalt von
  `ows:ElectronicMailAddress` entfernt (Kontaktangaben werden nicht benötigt).
- In der CSV-Datei wurden die Namen von zwei Betreibern, die natürliche Personen
  sind, durch `Privatperson (Name entfernt)` ersetzt.
- Die Änderungen für den zweiten und dritten Lauf der End-to-End-Tests
  (geänderte, entfernte und neue Datensätze, ein Wert, der zu lang für die Zielspalte ist) werden in
  `tests/test_e2e.py` zur Laufzeit erzeugt; die Dateien hier bleiben unverändert.

## 1. WFS Verwaltungsgrenzen Berlin/Brandenburg (LGB) – GML-Pfad

| | |
|---|---|
| Dienst | WFS BB-BE VG, `https://isk.geobasis-bb.de/ows/vg_wfs` (deegree) |
| Feature-Typ | `app:otl` (Ortsteile), gefiltert auf `app:ags = 12051000` (Brandenburg an der Havel, 8 Objekte) |
| Anbieter | Landesvermessung und Geobasisinformation Brandenburg (LGB) |
| Lizenz | Datenlizenz Deutschland – Namensnennung – Version 2.0, <https://www.govdata.de/dl-de/by-2-0> |
| Namensnennung | © GeoBasis-DE/LGB, dl-de/by-2-0 (Daten geändert) |

| Datei | Anfrage |
|---|---|
| `wfs/lgb_vg/capabilities_2_0_0.xml` | `SERVICE=WFS&REQUEST=GetCapabilities&ACCEPTVERSIONS=2.0.0,1.1.0` |
| `wfs/lgb_vg/capabilities_1_1_0.xml` | `SERVICE=WFS&REQUEST=GetCapabilities&VERSION=1.1.0` |
| `wfs/lgb_vg/describefeaturetype_otl.xml` | `SERVICE=WFS&VERSION=2.0.0&REQUEST=DescribeFeatureType&TYPENAMES=app:otl` |
| `wfs/lgb_vg/getfeature_otl_start0_count3.xml` | GetFeature 2.0.0, `TYPENAMES=app:otl`, `SRSNAME=urn:ogc:def:crs:EPSG::25833`, `SORTBY=app:otls`, FES-Filter `app:ags = 12051000`, `COUNT=3&STARTINDEX=0` |
| `wfs/lgb_vg/getfeature_otl_start3_count3.xml` | wie oben, `STARTINDEX=3` |
| `wfs/lgb_vg/getfeature_otl_start6_count3.xml` | wie oben, `STARTINDEX=6` (letzte Seite, 2 Objekte) |
| `wfs/lgb_vg/getfeature_otl_v110_maxfeatures2.xml` | GetFeature 1.1.0 (GML 3.1.1), `TYPENAME=app:otl&MAXFEATURES=2`, OGC-Filter `app:ags = 12051000` |

Beobachtungen am echten Dienst, die in Code und Konfiguration berücksichtigt sind:
Der Dienst meldet `ImplementsResultPaging=FALSE`, verarbeitet `STARTINDEX`/`COUNT`
aber korrekt (daher `paging: always`); `numberMatched` ist `unknown`; GeoJSON wird
nicht angeboten.

## 2. WFS Schutzgebiete Brandenburg (LfU) – GeoJSON-Pfad

| | |
|---|---|
| Dienst | WFS Schutzgebiete BB (WFS-LFU-SCHUTZG), `https://inspire.brandenburg.de/services/schutzg_wfs` (deegree) |
| Feature-Typ | `app:nsg` (Naturschutzgebiete), BBOX `321000,5796800,345200,5815700` in EPSG:25833 (Raum Brandenburg an der Havel, 10 Teilflächen, 8 Gebiete) |
| Anbieter | Landesamt für Umwelt Brandenburg (LfU); Dienst betrieben von der LGB |
| Lizenz | Datenlizenz Deutschland – Namensnennung – Version 2.0, <https://www.govdata.de/dl-de/by-2-0> |
| Namensnennung | © Landesamt für Umwelt Brandenburg |

| Datei | Anfrage |
|---|---|
| `wfs/lfu_schutzg/capabilities_2_0_0.xml` | `SERVICE=WFS&REQUEST=GetCapabilities&ACCEPTVERSIONS=2.0.0,1.1.0` |
| `wfs/lfu_schutzg/describefeaturetype_nsg.xml` | `SERVICE=WFS&VERSION=2.0.0&REQUEST=DescribeFeatureType&TYPENAMES=app:nsg` |
| `wfs/lfu_schutzg/getfeature_nsg_start0_count4.json` | GetFeature 2.0.0, `TYPENAMES=app:nsg`, `SORTBY=app:Gebietsnummer`, BBOX wie oben, `OUTPUTFORMAT=application/geo+json`, `COUNT=4&STARTINDEX=0` |
| `wfs/lfu_schutzg/getfeature_nsg_start4_count4.json` | wie oben, `STARTINDEX=4` |
| `wfs/lfu_schutzg/getfeature_nsg_start8_count4.json` | wie oben, `STARTINDEX=8` (2 Objekte) |
| `wfs/lfu_schutzg/getfeature_nsg_start12_count4.json` | wie oben, `STARTINDEX=12` (leere Seite) |

Beobachtungen: GeoJSON-Ausgabe immer in CRS84 (Länge/Breite), unabhängig von
`SRSNAME`; eine leere Seite enthält kein `features`-Element; die `Gebietsnummer` ist
bei Gebieten aus mehreren Teilflächen nicht eindeutig (daher `duplicate_keys: merge`);
`Flaeche_ha_gesamt` bezieht sich auf die einzelne Teilfläche.

## 3. Ladesäulen in Potsdam (CSV)

| | |
|---|---|
| Datei | `csv/ladesaeulen_potsdam.csv` (133 Datensätze, Trennzeichen `;`, UTF-8, Koordinaten in Spalten `breitengrad`/`langengrad`, WGS 84) |
| Abruf | `https://opendata.potsdam.de/api/explore/v2.1/catalog/datasets/ladesaeulenregister/exports/csv` |
| Herausgeber | Bundesnetzagentur (Ladesäulenregister), bereitgestellt über das Open-Data-Portal der Landeshauptstadt Potsdam |
| Lizenz | Creative Commons Namensnennung 4.0 International (CC BY 4.0), <https://creativecommons.org/licenses/by/4.0/> |
| Namensnennung | Bundesnetzagentur, Ladesäulenregister |

## Neu aufzeichnen

Die Anfragen oben lassen sich z. B. mit `curl -o <datei> '<url>'` wiederholen
(Parameter URL-kodieren). Ändern sich die Daten beim Anbieter, müssen die
erwarteten Werte in `tests/test_wfs.py`, `tests/test_gml.py`, `tests/test_csv.py`
und `tests/test_e2e.py` angepasst werden.

## INKAR-Format (künstliche Testdatei)

| | |
|---|---|
| Datei | `files/inkar_2025.zip` mit `inkar_2025/inkar_2025.csv` und `inkar_2025/LIESMICH.txt` |
| Inhalt | **ausgedachte Werte**, keine INKAR-Daten; 20 Zeilen: 16 Gemeinde-Zeilen aus Brandenburg (4 Gemeinden × 2 Indikatoren × 2 Jahre), 2 Kreis-Zeilen und 2 Gemeinde-Zeilen aus Schleswig-Holstein (Kennziffer ohne führende Null), die die Filter bzw. `zero_pad` prüfen |
| Spalten | `Bereich;ID;Kürzel;Indikator;Raumbezug;Kennziffer;Name;Zeitbezug;Wert` – Aufbau nach `config.xml` der bestehenden MOSIDI-Importskripte (`services/backend/src/inkar_import`) und dem abgestimmten Schlüssel (Kennziffer, Bereich, Kürzel, Indikator, Zeitbezug); Trennzeichen `;`, Dezimalkomma |
| Zweck | Test von ZIP-Lesen, Jahressuche (`inkar_{year}.zip`), Zeilenfilter, AGS mit führenden Nullen, Dezimalzahlen und dem Schreiben nach `dashboard_data_de` |

Die Abstimmung mit der echten INKAR-Gesamtdatei (Spaltennamen, Kodierung,
Umfang) ist Teil der Inbetriebnahme; Anpassungen erfolgen in
`config/sources/inkar_gemeinden.yml`.
