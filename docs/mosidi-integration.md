# Einbindung in das MOSIDI-System

Diese Seite beschreibt, wie `geoimport` als zusätzlicher Dienst im
Docker-Compose des MOSIDI-Systems läuft und in dessen vorhandene Tabellen
schreibt. Grundlage ist das öffentliche Repository `MOSIDI-ORG/MOSIDI`
(Stand 02.11.2026); die endgültigen Tabellenschemas werden bei der Inbetriebnahme
abgestimmt und nur in der YAML-Konfiguration nachgezogen.

## Dienst im Compose ergänzen

Der Dienst nutzt die vorhandene Datenbank `brandenburg_db` und dieselben
Variablen aus `.env` wie das Backend (`POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`). Beispiel, wenn der Quellcode unter `services/geoimport`
liegt:

```yaml
services:
  geoimport:
    build: ./services/geoimport
    restart: unless-stopped
    depends_on:
      - brandenburg_db
    env_file:
      - .env
    environment:
      PGHOST: brandenburg_db
      PGPORT: "5432"
      PGDATABASE: ${POSTGRES_DB}
      # im Betrieb besser eine eigene Rolle ohne DDL-Rechte (docs/betrieb.md)
      PGUSER: ${POSTGRES_USER}
      PGPASSWORD: ${POSTGRES_PASSWORD}
      GEOIMPORT_CONFIG: /config/pipeline.yml
      GEOIMPORT_REPORT_DIR: /data/reports
      TZ: Europe/Berlin
    volumes:
      - ./services/geoimport/config:/config:ro
      - geoimport_reports:/data/reports
    command: ["schedule"]

volumes:
  geoimport_reports:
```

Einmalig nach dem ersten Start:

```bash
docker compose run --rm geoimport init-db        # legt nur das Schema pipeline_meta an
docker compose run --rm geoimport check-config   # Quellen, Ziele und Takte prüfen
docker compose run --rm geoimport run            # erster Import
```

Andere Dienste (Backend, Tileserver, GeoServer) bleiben unverändert. Der Dienst
öffnet keinen Port.

Hinweis aus dem Test: `postgis/postgis` ohne Versionsangabe lädt inzwischen
PostgreSQL 18. Dessen Image erwartet das Datenverzeichnis unter
`/var/lib/postgresql`; ein neu erzeugter Container mit einem Volume unter
`/var/lib/postgresql/data` startet nicht. Für die bestehende Datenbank empfiehlt
sich deshalb eine feste Version passend zur laufenden PostgreSQL-Version.

## Abbildung auf die MOSIDI-Tabellen

| MOSIDI-Tabelle | Inhalt | Konfiguration |
|---|---|---|
| `dashboard_data_de` | Attributwerte: Geo-Referenz `kennziffer` (AGS), Attribut-Typ `indikator`, `wert`, Zeit-Referenz `zeitbezug`, `granularity`, Quellenangabe `source` | `target.source_column: source`, je Quelle ein eigener `source_value` (z. B. `INKAR (BBSR)`); die Zeilen anderer Quellen wie `OpenStreetMap (OSM)` bleiben unberührt |
| `table_metadata` | DCAT-AP-Beschreibung je Datensatz, Schlüssel `(table_name, dcatde_politicalGeocodingLevelURI)` | `metadata_record` je Quelle (Titel, Lizenz, Zugriffs-URL, `imported`) |
| Geometrietabellen (z. B. `"Kommunale Gebiete Deutschland"`) | Geometrien in EPSG:4326, Verknüpfung über den AGS (`nationalco`) | `target.geometry_column`, `columns`; bei Tabellen, die einer Quelle allein gehören, `exclusive: true` |

Unterschiede zum bisherigen INKAR-Skript (`services/backend/src/inkar_import`):

- Die Tabellen werden nicht gelöscht und neu angelegt; ersetzt werden nur die
  INKAR-Zeilen. Indizes, Rechte, Sichten und Zeilen anderer Quellen bleiben
  erhalten, das Dashboard sieht bis zum Abschluss den alten Stand.
- Der AGS bleibt Text mit führenden Nullen (achtstellig), statt als Zahl
  gespeichert zu werden; die Verknüpfung mit `nationalco` ist damit auch für die
  Länder 01 bis 09 eindeutig.
- Die Datei wird gestreamt (`COPY`), auch die Gesamtdatei für Deutschland.
- Neu, geändert und entfallen stehen je Lauf im Änderungsprotokoll
  (`pipeline_meta.change_log`) und im Bericht.

Spalten und Filter der mitgelieferten INKAR-Quelle folgen `config.xml` der
bestehenden Skripte (`Raumbezug = Gemeinden`); welche Indikatoren übernommen
werden, wird bei der Inbetriebnahme festgelegt (`row_filter`).

## Weitere Quellen

- **Geobasis Brandenburg (WFS)**: je Feature-Typ eine YAML-Datei nach dem
  Muster `config/sources/lgb_ortsteile_brandenburg_havel.yml`; für das ganze Land
  ohne Filter, mit `page_size: 1000` und `sort_by` auf dem Schlüsselfeld.
- **Amt für Statistik Berlin-Brandenburg (CSV)**: per URL; fällt die Adresse aus
  oder gibt es keine stabile URL, wird eine abgelegte Datei verwendet
  (`fallback_path`) oder direkt `path` gesetzt.
- Der Takt je Quelle steht in ihrer YAML-Datei (`schedule`), z. B. monatlich für
  INKAR und Statistik.
