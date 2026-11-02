# geodaten-import (`geoimport`)

Geodaten-Import-Pipeline: liest Geodaten und Kennzahlen aus **WFS-Diensten** (OGC
WFS 2.0.0 und 1.1.0) und **CSV-Dateien** (lokal, per URL oder im ZIP-Archiv, z. B.
INKAR) und schreibt sie in **vorhandene Tabellen** einer
**PostgreSQL/PostGIS**-Datenbank. Bei jedem Lauf werden die Zeilen jeder Quelle
ersetzt; was neu, geändert oder entfallen ist, hält ein **Änderungsprotokoll** je
Lauf fest. Jeder Lauf erzeugt einen Import-Bericht als JSON und Markdown.

Die mitgelieferte Konfiguration schreibt in Tabellen nach dem Vorbild des
öffentlichen MOSIDI-Datenmodells (`dashboard_data_de`, `table_metadata`,
Datenbankdienst `brandenburg_db`); die Zieltabellen bleiben dabei unverändert,
die Abbildung steht allein in der YAML-Konfiguration.

Lizenz: Apache-2.0 (siehe `LICENSE`). Alle Abhängigkeiten sind Open Source
(siehe `docs/lizenzen.md`).

Arbeitsweise: Bei Entwicklung, Tests und Dokumentation setze ich KI-Werkzeuge
ein. Planung, Prüfung, Abnahme und Verantwortung liegen bei mir. Jede Änderung
wird mit den automatisierten Unit- und End-to-End-Tests geprüft (siehe „Tests“).

## Funktionsumfang

- **Quelladapter hinter einer Schnittstelle**
  - WFS: GetCapabilities, DescribeFeatureType, GetFeature mit Paginierung über
    `STARTINDEX`/`COUNT`, Beachtung von `CountDefault`, Erkennung gekürzter
    Antworten, GeoJSON wenn angeboten, sonst GML 3.2 (GML 3.1.1/2 werden ebenfalls
    gelesen), `SRSNAME` und Achsenreihenfolge, Filter (FES) oder BBOX, Sortierung,
    Rückfall auf WFS 1.1.0.
  - CSV: Pfad, URL oder ZIP-Archiv, neueste Jahresdatei (`inkar_{year}.zip`),
    abgelegte Ersatzdatei bei Ausfall der URL, Trennzeichen, Zeichenkodierung,
    Dezimalkomma, Zeilenfilter, Gemeindeschlüssel mit führenden Nullen, Geometrie
    aus Länge/Breite bzw. Rechts-/Hochwert oder WKT – oder reine Sachdaten.
- **Neue Quelle = neue YAML-Datei** in `config/sources/`, geprüft mit pydantic:
  Adresse, Layer, fachlicher Schlüssel, Attribut-Mapping, Zieltabelle und
  Spalten, Takt, Metadaten (Anbieter, Lizenz, Aktualisierungszyklus).
- **Vorhandene Zieltabellen**: Die Pipeline führt dort nur `DELETE` und `INSERT`
  aus, nie `CREATE`, `ALTER` oder `DROP`. Vor dem Schreiben prüft sie Tabelle,
  Spalten, Pflichtspalten und Koordinatenbezugssystem; Werte werden in den Typ der
  Spalte umgewandelt, Geometrien nach EPSG:4326 transformiert.
- **Ersetzen je Quelle**: In geteilten Tabellen werden nur die Zeilen der Quelle
  ersetzt (z. B. `source = 'INKAR (BBSR)'`); Zeilen anderer Quellen, etwa
  OpenStreetMap-Kennzahlen, bleiben unberührt. Tabellen, die einer Quelle allein
  gehören, werden vollständig ersetzt.
- **Änderungsprotokoll** im eigenen Schema `pipeline_meta`: SHA-256-Fingerabdruck
  je Datensatz; je Lauf werden neue, geänderte und entfernte Datensätze mit
  Lauf-ID protokolliert. Frühere Fassungen der Daten werden nicht aufbewahrt.
- **Entfernt nur nach vollständigem Abruf**: Ist ein Abruf unvollständig
  (gekürzte WFS-Antwort, Obergrenze, zu viele unbrauchbare Zeilen) oder fehlen
  plötzlich zu viele Datensätze (`max_delete_ratio`), wird die Quelle nicht
  geschrieben.
- **Fehlertoleranz**: jede Quelle läuft in einer eigenen Transaktion mit Sperre.
  Schlägt eine Quelle fehl, wird nur sie zurückgerollt und im Bericht
  ausgewiesen; die übrigen Quellen laufen weiter.
- **Metadaten**: optional eine DCAT-AP-Zeile je Quelle in `table_metadata`
  (Titel, Lizenz, Zugriffs-URL, Zeitpunkt des Imports).
- **Import-Bericht** je Lauf (`reports/import_<zeit>_<id>.json` und `.md`, dazu
  `latest.json`/`latest.md`) mit Zeitpunkt, Laufzeit, Ziel und Kennzahlen je
  Quelle, Fehlern und Warnungen (z. B. Schemaänderungen, ungültige Geometrien).
- **Betrieb** per CLI, zeitgesteuert mit eigenem Takt je Quelle (Cron-Ausdruck),
  per Docker Compose neben der Datenbank; im Betrieb genügt eine Rolle ohne
  DDL-Rechte.

## Ablauf eines Importlaufs

```text
config/pipeline.yml + config/sources/*.yml      (pydantic-Prüfung)
                 │
                 ▼
   ┌──────────────────────────┐   je Quelle, nacheinander
   │ Quelladapter (WFS | CSV) │── HTTP/Datei ──► Datensätze (Attribute + Geometrie), gestreamt
   └──────────────────────────┘
                 │  Schlüssel, Mapping, Geometrieprüfung
                 ▼
   ┌───────────────────────────────┐   eine Transaktion je Quelle
   │ PostGIS                       │   COPY in Staging → Vollständigkeit, Dubletten →
   │ pipeline_meta (Stand, Proto-  │   Vergleich der Fingerabdrücke → Änderungsprotokoll →
   │ koll) + vorhandene Zieltabelle│   Zeilen der Quelle ersetzen (ST_Transform nach 4326)
   └───────────────────────────────┘
                 │
                 ▼
   Import-Bericht (JSON + Markdown), Rückgabewert 0 / 1 / 2
```

## Schnellstart mit Docker Compose

```bash
cp .env.example .env              # Passwörter setzen
docker compose up -d --build      # PostGIS (brandenburg_db) + zeitgesteuerte Pipeline
docker compose run --rm geoimport run                             # sofort alle Quellen
docker compose run --rm geoimport run --source inkar_gemeinden
docker compose run --rm geoimport check-config
docker compose logs -f geoimport
```

Die Demo-Datenbank legt beim ersten Start Zieltabellen nach MOSIDI-Vorbild an
(`demo/mosidi_zieltabellen.sql`). Die Konfiguration wird aus `./config`
schreibgeschützt eingebunden, Berichte landen im Volume `reports`. Einbindung in
ein bestehendes MOSIDI-Compose: `docs/mosidi-integration.md`; Betrieb:
`docs/betrieb.md`.

## Schnellstart lokal

Voraussetzungen: Python 3.12, PostgreSQL mit PostGIS und vorhandenen
Zieltabellen (für eine leere Datenbank: `psql -f demo/mosidi_zieltabellen.sql`).

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

export GEOIMPORT_DB_DSN="postgresql://postgres:<passwort>@localhost:5432/brandenburg"
geoimport check-config            # Konfiguration prüfen, Quellen, Ziele und Takte auflisten
geoimport init-db                 # Schema pipeline_meta anlegen (Migrationen)
geoimport run                     # alle aktiven Quellen importieren
geoimport run --source lfu_nsg_brb
geoimport schedule                # Dauerbetrieb im Takt der Quellen
```

Rückgabewerte von `geoimport run`: `0` alle Quellen erfolgreich, `1` mindestens
eine Quelle fehlgeschlagen (Details im Bericht), `2` Lauf nicht möglich
(Konfiguration oder Datenbank).

## Tests

```bash
pytest                                        # Unit-Tests; Datenbanktests ohne DB übersprungen
scripts/test-datenbank.sh start               # Wegwerf-PostGIS-Cluster auf 127.0.0.1:55433
export GEOIMPORT_TEST_DSN="$(scripts/test-datenbank.sh dsn)"
pytest                                        # alle Tests inkl. End-to-End
scripts/test-datenbank.sh remove
ruff check . && ruff format --check .
```

Die End-to-End-Tests starten einen lokalen HTTP-Server mit aufgezeichneten
Antworten echter WFS-Dienste, einer echten CSV-Datei und einer künstlichen
INKAR-Datei (`tests/fixtures/QUELLEN.md`) und prüfen unter anderem drei Läufe:
Erstimport, Lauf mit neuen, geänderten und entfernten Datensätzen (Protokoll,
Zieltabellen, Bericht; Zeilen anderer Quellen bleiben unberührt) und einen Lauf
mit einer fehlerhaften Quelle, deren Transaktion zurückgerollt wird. Geprüft mit
PostgreSQL 16 / PostGIS 3.4 und PostgreSQL 18 / PostGIS 3.6.

## Mitgelieferte Beispielquellen

| ID | Typ | Inhalt | Ziel | Lizenz |
|---|---|---|---|---|
| `lgb_ortsteile_brb` | WFS (GML 3.2) | Ortsteile der Stadt Brandenburg an der Havel, WFS Verwaltungsgrenzen der LGB | `"Geometrien (Demo)"` | dl-de/by-2-0 |
| `lfu_nsg_brb` | WFS (GeoJSON) | Naturschutzgebiete im Raum Brandenburg an der Havel, WFS Schutzgebiete des LfU | `"Naturschutzgebiete"` (eigene Tabelle) | dl-de/by-2-0 |
| `bnetza_ladesaeulen_potsdam` | CSV per URL | Ladesäulen in Potsdam (Ladesäulenregister der Bundesnetzagentur) | `"Geometrien (Demo)"` | CC BY 4.0 |
| `inkar_gemeinden` | CSV im ZIP | INKAR-Indikatoren je Gemeinde in Brandenburg (BBSR), monatlich | `dashboard_data_de` | dl-de/by-2-0 |

Die Seitengrößen der beiden WFS-Quellen sind für die Demonstration klein
gewählt (3 bzw. 4), damit die Paginierung sichtbar wird.

## Verzeichnisstruktur

```text
config/                    pipeline.yml und je Quelle eine YAML-Datei
src/geoimport/             Paket: cli, config, pipeline, store, targets, report, scheduler, ...
src/geoimport/sources/     Quelladapter (base, wfs, csv_source)
src/geoimport/migrations/  SQL-Migrationen des Schemas pipeline_meta
demo/                      Zieltabellen nach MOSIDI-Vorbild (nur Demo und Tests)
tests/                     Unit- und End-to-End-Tests, Testdaten
scripts/                   Hilfsskripte (Testdatenbank)
docs/                      Betrieb, Entwicklung, Datenmodell, MOSIDI-Einbindung, Lizenzen
Dockerfile, docker-compose.yml, .env.example
```

## Dokumentation

- `docs/betrieb.md` – Installation, Konfiguration, Zeitplan, Rechte, Überwachung, Fehlerfälle
- `docs/entwickler.md` – neue WFS-/CSV-Quellen per Konfiguration, Adapter-Schnittstelle, Tests
- `docs/datenmodell.md` – Zieltabellen, Ersetzen je Quelle, Änderungsprotokoll, Beispielabfragen
- `docs/mosidi-integration.md` – Einbindung als Dienst im MOSIDI-Compose, Abbildung auf die MOSIDI-Tabellen
- `docs/lizenzen.md` – Lizenzen der Abhängigkeiten und Testdaten
