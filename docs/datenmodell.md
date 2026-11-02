# Datenmodell

Die Pipeline arbeitet mit **zwei Arten von Tabellen**:

1. **Zieltabellen des Fachsystems** (z. B. im MOSIDI-System die Attributtabelle
   `dashboard_data_de`, Geometrietabellen und die DCAT-AP-Metadatentabelle
   `table_metadata`). Sie bestehen bereits, werden vom Dashboard gelesen und von
   der Pipeline **nie angelegt oder verändert**. Die Pipeline führt dort nur
   `DELETE` und `INSERT` aus.
2. Das **eigene Schema der Pipeline** (Standard `pipeline_meta`, Migration
   `src/geoimport/migrations/001_pipeline_meta.sql`) für Stand und
   Änderungsprotokoll. Es berührt keine Fachtabelle.

```text
Quelle (WFS/CSV) ──► Staging (temporär) ──► Vergleich mit pipeline_meta.record_state
                                              │
                         ┌────────────────────┴─────────────────────┐
                         ▼                                          ▼
     Zieltabelle: Zeilen der Quelle ersetzen        pipeline_meta.change_log:
     (DELETE … WHERE source = 'X'; INSERT …)        neu / geändert / entfernt je Lauf
```

Beides geschieht in **einer Transaktion je Quelle**: Lesende sehen bis zum
`COMMIT` den alten Stand, danach den neuen. Schlägt etwas fehl, bleibt alles
beim alten Stand, auch das Protokoll.

## Abbildung einer Quelle auf ihre Zieltabelle

Jede Quelle beschreibt in ihrer YAML-Datei unter `target`, wohin sie schreibt:

```yaml
target:
  table: dashboard_data_de          # vorhandene Tabelle (optional mit Schema: schema.tabelle)
  source_column: source             # Spalte mit der Quellenangabe
  source_value: INKAR (BBSR)        # Wert dieser Quelle (Standard: ID der Quelle)
  columns:                          # Zielspalte: Attribut nach dem Mapping (attributes)
    kennziffer: kennziffer          #   oder @key für den fachlichen Schlüssel
    indikator: indikator
    zeitbezug: zeitbezug
    wert: wert
  constants:                        # Zielspalte: fester Wert
    granularity: Gemeindeebene
  # geometry_column: geom           # Geometriespalte, falls die Tabelle eine hat
```

- **Geteilte Tabellen** (`source_column`): Ersetzt werden nur die Zeilen mit
  `source = 'INKAR (BBSR)'`. Zeilen anderer Quellen, z. B. die
  OpenStreetMap-Kennzahlen in `dashboard_data_de`, bleiben unberührt (mit IDs).
- **Eigene Tabellen** (`exclusive: true`): Die Tabelle gehört allein dieser
  Quelle; alle Zeilen werden ersetzt. `check-config` weist ab, wenn eine
  zweite Quelle in dieselbe Tabelle schreibt.
- **Prüfung vor dem Schreiben**: Tabelle vorhanden, alle Spalten vorhanden,
  Pflichtspalten (`NOT NULL` ohne Standardwert) belegt, Geometriespalte im
  Ziel-KBS. Sonst schlägt die Quelle mit einer klaren Meldung fehl, ohne zu
  schreiben.
- **Datentypen**: Jeder Wert wird in den Typ der Zielspalte umgewandelt (Text,
  Zahl, Datum, …). Ein Wert, der nicht passt oder zu lang ist, führt zum Fehler
  und zum Zurückrollen der Quelle, nie zu stiller Kürzung.
- **Geometrien** werden mit `ST_Transform` ins Ziel-KBS gebracht (Standard
  EPSG:4326, WGS 84) und auf 2D reduziert; bei `MULTI…`-Spalten wird `ST_Multi`
  angewendet.
- **Fachliche Schlüssel** (`key_fields`) bestimmen, was „derselbe Datensatz“ ist,
  z. B. für INKAR `Kennziffer, Bereich, Kürzel, Indikator, Zeitbezug`, für WFS die
  Objektkennung. `@key` schreibt den Schlüssel in eine Spalte (z. B. als
  Geo-Referenz).
- **Verknüpfung über den AGS**: Mit `zero_pad: {Kennziffer: 8}` wird der
  Gemeindeschlüssel immer achtstellig als Text geschrieben (`01001000` statt
  `1001000`), passend zu `"Kommunale Gebiete Deutschland".nationalco`.

### Metadaten (DCAT-AP)

Optional schreibt eine Quelle je Lauf eine Zeile in eine Metadatentabelle
(`metadata_record`). Die Werte sind Vorlagen mit Platzhaltern
(`{title}`, `{description}`, `{provider}`, `{licence}`, `{licence_url}`,
`{attribution}`, `{update_cycle}`, `{url}`, `{layer}`, `{target_table}`,
`{source_value}`, `{records}`, `{run_id}`, `{run_time}`, `{id}`):

```yaml
metadata_record:
  table: table_metadata
  key_columns:                      # bestimmen die Zeile (wird je Lauf ersetzt)
    table_name: "dashboard_data_de: {source_value}"
    dcatde_politicalgeocodingleveluri: http://dcat-ap.de/def/politicalGeocoding/Level/municipality
  columns:
    dct_title: "{title}"
    dct_license: "{licence}"
    dct_accessurl: "{url}"          # tatsächlich verwendete URL bzw. Ersatzdatei
    dct_modified: "{run_time}"      # Zeitpunkt des Laufs (UTC)
```

## Schema `pipeline_meta`

### `source_state` – eine Zeile je Quelle

| Spalte | Inhalt |
|---|---|
| `source_id` | ID aus der Konfiguration (Primärschlüssel) |
| `source_type`, `title`, `location`, `layer` | Quelltyp (`wfs`/`csv`), Titel, URL bzw. Pfad, Feature-Typ |
| `target_table`, `source_value` | Zieltabelle und Wert der Quellenspalte (`NULL` bei exklusiver Tabelle) |
| `provider`, `licence` | aus `metadata:` der Quelldatei |
| `schema_fields` | Felder der Quelle beim letzten erfolgreichen Lauf (Erkennung von Schemaänderungen) |
| `first_success_at`, `last_run_at`, `last_success_at` | Zeitpunkte |
| `last_run_id`, `last_status`, `last_error`, `last_counts` | Lauf-ID, `success`/`failed`, Fehlermeldung, Kennzahlen (JSON) |
| `records` | Anzahl der Datensätze nach dem letzten erfolgreichen Lauf |

### `record_state` – Fingerabdruck je Datensatz

| Spalte | Inhalt |
|---|---|
| `source_id`, `record_key` | Quelle und fachlicher Schlüssel (mehrere Felder mit `\|` verbunden) |
| `content_hash` | SHA-256 über die abgebildeten Attribute, die Quellgeometrie und ihr KBS |
| `first_run_id`, `last_change_run_id` | Lauf der ersten Übernahme und der letzten Änderung |

Grundlage der Änderungserkennung. Frühere Fassungen der Daten werden nicht
aufbewahrt. Zahlen werden vor dem Vergleich normiert (`10,0` und `10` sind
gleich), damit eine geänderte Schreibweise der Quelle keine Änderung meldet.

### `change_log` – Änderungsprotokoll je Lauf

| Spalte | Inhalt |
|---|---|
| `run_id` | Importlauf (auch im Bericht und in `import_run`) |
| `source_id`, `record_key` | Quelle und Datensatz |
| `action` | `new`, `changed` oder `deleted` |
| `old_hash`, `new_hash` | Fingerabdruck vorher/nachher (`NULL` bei neu bzw. entfernt) |
| `changed_at` | Zeitpunkt des Laufs |

**Entfernt** gilt ein Datensatz nur, wenn er bei einem **vollständigen und
fehlerfreien** Abruf fehlt. Ist ein Abruf unvollständig (gekürzte WFS-Antwort,
`max_features`, mehr unbrauchbare Datensätze als `max_skipped_ratio`) oder fehlen
mehr als `max_delete_ratio` der bisherigen Datensätze, wird die Quelle nicht
geschrieben und nichts protokolliert.

### `import_run` und Sicht `change_summary`

`import_run` hat eine Zeile je Lauf (Auslöser, Beginn, Ende, Status,
Zusammenfassung). `change_summary` zählt die Änderungen je Lauf und Quelle.

## Beispielabfragen

Was hat sich im letzten Lauf geändert?

```sql
SELECT * FROM pipeline_meta.change_summary
 WHERE run_id = (SELECT run_id FROM pipeline_meta.import_run ORDER BY started_at DESC LIMIT 1);
```

Welche INKAR-Datensätze sind in den letzten 90 Tagen entfallen?

```sql
SELECT record_key, changed_at FROM pipeline_meta.change_log
 WHERE source_id = 'inkar_gemeinden' AND action = 'deleted'
   AND changed_at > now() - interval '90 days'
 ORDER BY changed_at DESC;
```

Wann wurde ein Datensatz zuletzt geändert?

```sql
SELECT action, changed_at, run_id FROM pipeline_meta.change_log
 WHERE source_id = 'lgb_ortsteile_brb' AND record_key = '120510000075'
 ORDER BY changed_at;
```

INKAR-Kennzahlen je Gemeinde mit Geometrie (Verknüpfung über den AGS):

```sql
SELECT g.name, d.indikator, d.zeitbezug, d.wert, g.geom
  FROM dashboard_data_de d
  JOIN "Kommunale Gebiete Deutschland" g ON g.nationalco = d.kennziffer
 WHERE d.source = 'INKAR (BBSR)' AND d.zeitbezug = 2023;
```

Stand aller Quellen:

```sql
SELECT source_id, target_table, last_status, last_success_at, records, last_error
  FROM pipeline_meta.source_state ORDER BY source_id;
```

## Demo-Zieltabellen

`demo/mosidi_zieltabellen.sql` legt für Demo und Tests an:

| Tabelle | Vorlage |
|---|---|
| `table_metadata` | DCAT-AP-Metadaten wie im MOSIDI-Skript `db_table_metadata/script.py` |
| `dashboard_data_de` | Attributtabelle wie in MOSIDI `db_table_schema/schema.sqlalchemy` |
| `"Geometrien (Demo)"` | Beispiel einer Geometrietabelle, die sich mehrere Quellen teilen (`source`) |
| `"Naturschutzgebiete"` | Beispiel einer themenbezogenen Tabelle, die einer Quelle gehört (`exclusive`) |

Die endgültigen Geometrietabellen ergeben sich aus den Schemas des Zielsystems;
angepasst wird dann nur die YAML-Konfiguration, nicht der Code.
