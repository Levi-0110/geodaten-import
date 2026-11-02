# Betrieb

## Komponenten

| Komponente | Aufgabe |
|---|---|
| PostgreSQL mit PostGIS | Zieldatenbank mit den vorhandenen Tabellen (z. B. MOSIDI: `dashboard_data_de`, `table_metadata`, Geometrietabellen) und dem eigenen Schema `pipeline_meta` der Pipeline. Geprüft mit PostgreSQL 16 / PostGIS 3.4 und PostgreSQL 18 / PostGIS 3.6. |
| `geoimport` (Python 3.12) | Import-Pipeline: CLI, Zeitsteuerung, Bericht |
| `config/` | `pipeline.yml` (global) und je Quelle eine YAML-Datei in `config/sources/` |
| `reports/` bzw. Volume `reports` | Import-Berichte (JSON, optional Markdown) |

Die Zieltabellen gehören dem Fachsystem. Die Pipeline legt sie nicht an und
ändert ihre Struktur nie; sie löscht und schreibt nur die Zeilen der jeweiligen
Quelle (siehe `docs/datenmodell.md`). Für die Demo legt
`demo/mosidi_zieltabellen.sql` passende Tabellen an.

## Installation mit Docker Compose

```bash
cp .env.example .env                 # Passwörter setzen, ggf. GEOIMPORT_CRON
docker compose up -d --build
docker compose ps                    # brandenburg_db: healthy, geoimport: running
```

- `brandenburg_db` nutzt wie das MOSIDI-System das Image `postgis/postgis`; die
  Daten liegen im Volume `brandenburg_db`, eingebunden unter `/var/lib/postgresql`.
  Ab PostgreSQL 18 erwartet das Image das Datenverzeichnis dort
  (`/var/lib/postgresql/18/docker`); ein Volume unter `/var/lib/postgresql/data`
  lässt einen neuen Container nicht mehr starten. Für reproduzierbare Umgebungen
  empfiehlt sich eine feste Version (z. B. `postgis/postgis:17-3.5`).
  Beim ersten Start legt `demo/mosidi_zieltabellen.sql` die Demo-Zieltabellen an.
  Ein Port wird standardmäßig **nicht** nach außen freigegeben.
- `geoimport` startet `geoimport schedule` und führt die Importe im Takt der
  Quellen aus. Der Container läuft als unprivilegierter Benutzer (UID 10001),
  `./config` ist schreibgeschützt eingebunden.
- Das Schema `pipeline_meta` wird beim ersten Lauf automatisch angelegt;
  explizit mit `docker compose run --rm geoimport init-db`.

Im MOSIDI-System entfällt der Datenbankdienst dieser Datei; dort wird nur der
Dienst `geoimport` ergänzt (`docs/mosidi-integration.md`).

Häufige Befehle:

```bash
docker compose run --rm geoimport run                             # alle Quellen sofort
docker compose run --rm geoimport run --source inkar_gemeinden    # nur eine Quelle
docker compose run --rm geoimport check-config                    # Konfiguration prüfen
docker compose logs -f geoimport                                  # Protokoll
docker compose exec brandenburg_db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Nach Änderungen an `config/` genügt `docker compose restart geoimport`.

## Installation ohne Docker

```bash
python3.12 -m venv /opt/geoimport/venv
/opt/geoimport/venv/bin/pip install /pfad/zu/geodaten-import
export GEOIMPORT_CONFIG=/etc/geoimport/pipeline.yml
export GEOIMPORT_DB_DSN="postgresql://geoimport@dbserver:5432/brandenburg"
export GEOIMPORT_DB_PASSWORD_FILE=/etc/geoimport/db-passwort   # Rechte 600
/opt/geoimport/venv/bin/geoimport init-db
```

Zeitsteuerung entweder über den eingebauten Scheduler als Dienst, z. B. als
systemd-Unit:

```ini
[Service]
User=geoimport
EnvironmentFile=/etc/geoimport/umgebung
ExecStart=/opt/geoimport/venv/bin/geoimport schedule
Restart=on-failure
```

oder über die vorhandene Zeitsteuerung des Servers (cron, systemd-Timer), die
`geoimport run --source <id>` aufruft. Beide Wege nutzen denselben Code.

## Umgebungsvariablen

| Variable | Bedeutung |
|---|---|
| `GEOIMPORT_CONFIG` | Pfad zu `pipeline.yml` (Standard `config/pipeline.yml`) |
| `GEOIMPORT_DB_DSN` | Verbindungs-URL, z. B. `postgresql://benutzer@host:5432/db` |
| `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` | libpq-Standardvariablen, alternativ oder ergänzend zur DSN (so im Compose-Setup) |
| `GEOIMPORT_DB_PASSWORD_FILE` | Datei mit dem Datenbankpasswort (z. B. Docker-Secret) |
| `GEOIMPORT_REPORT_DIR` | Verzeichnis der Berichte (in `pipeline.yml` referenziert) |
| `GEOIMPORT_CRON` | Standardtakt (in `pipeline.yml` referenziert), Standard `0 3 * * 1` |
| `GEOIMPORT_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

In allen YAML-Werten können `${VAR}` und `${VAR:-standard}` verwendet werden.
Zugangsdaten gehören nicht in die YAML-Dateien, sondern in Umgebungsvariablen
oder Secret-Dateien. Benutzer/Passwort in URLs und Parameter wie `apikey`,
`token` oder `password` werden in Bericht, Protokoll und Metadaten maskiert.

## Globale Konfiguration (`pipeline.yml`)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `database.target_srid` | `4326` | Ziel-KBS aller Geometrien; die Geometriespalten der Zieltabellen werden vor dem Schreiben darauf geprüft |
| `database.meta_schema` | `pipeline_meta` | eigenes Schema der Pipeline (Stand, Änderungsprotokoll) |
| `database.dsn_env` | `GEOIMPORT_DB_DSN` | Name der Variablen mit der Verbindungs-URL |
| `database.connect_timeout_seconds` | `10` | Zeitlimit für den Verbindungsaufbau |
| `schedule.cron` | `0 3 * * *` | Standardtakt für Quellen ohne eigenes `schedule` (mitgeliefert: `${GEOIMPORT_CRON:-0 3 * * 1}`) |
| `schedule.timezone` | `Europe/Berlin` | Zeitzone des Zeitplans und der Berichte |
| `schedule.run_on_start` | `false` | beim Start von `schedule` sofort einmal laufen (`--run-now`) |
| `report.directory` | `reports` | Berichtsverzeichnis (relativ zu `pipeline.yml`) |
| `report.write_latest` | `true` | zusätzlich `latest.json`/`latest.md` schreiben |
| `report.markdown` | `true` | neben der JSON-Datei eine lesbare Zusammenfassung schreiben |
| `http.timeout_seconds` | `60` | Zeitlimit je HTTP-Anfrage |
| `http.retries` / `http.backoff_seconds` | `3` / `2` | Wiederholungen bei Netzwerkfehlern, HTTP 429 und 5xx (exponentiell) |
| `http.verify_tls` | `true` | TLS-Zertifikate prüfen |
| `sources_dir` | `sources` | Verzeichnis der Quelldateien |

Die Quellkonfiguration ist in `docs/entwickler.md` beschrieben.

## Zeitplan

Jede Quelle kann einen eigenen Takt haben (`schedule` in ihrer YAML-Datei, z. B.
INKAR monatlich `0 4 2 * *`); ohne Angabe gilt `schedule.cron`. Quellen mit
gleichem Takt laufen gemeinsam. Die Schreibweise folgt crontab; Wochentage
zählen wie bei cron (`0` oder `7` = Sonntag, `1-5` = Montag bis Freitag):

| Ausdruck | Bedeutung |
|---|---|
| `0 3 * * 1` | montags 03:00 |
| `0 4 2 * *` | am 2. jedes Monats 04:00 |
| `30 2 * * 1-5` | montags bis freitags 02:30 |
| `0 */6 * * *` | alle sechs Stunden |

Der Scheduler führt Läufe nacheinander aus (ein Arbeitsthread), zwei Läufe
überschneiden sich also nie. Termine während eines Ausfalls werden innerhalb
einer Stunde nachgeholt, ältere entfallen; mit `run_on_start: true` bzw.
`geoimport schedule --run-now` läuft nach einem Neustart sofort ein Import.
Zusätzlich sperrt jede Quelle während ihres Laufs eine Advisory-Sperre, sodass
ein parallel gestarteter manueller Lauf dieselbe Quelle erst danach schreibt.

## Datenbank und Rechte

`geoimport init-db` legt das Schema `pipeline_meta` an (Migrationen aus
`src/geoimport/migrations/`, protokolliert in `pipeline_meta.schema_migrations`).
Dafür braucht die Rolle einmalig `CREATE` auf der Datenbank. Bestehende Tabellen
werden von Migrationen nie verändert.

Für den laufenden Betrieb genügt danach eine Rolle **ohne DDL-Rechte**: Sie
liest, löscht und schreibt Zeilen, ändert aber keine Tabellen. Abgesichert durch
den Test `test_runtime_role_without_ddl_rights`:

```sql
CREATE ROLE geoimport_betrieb LOGIN PASSWORD '...';
GRANT CONNECT, TEMPORARY ON DATABASE brandenburg TO geoimport_betrieb;
GRANT USAGE ON SCHEMA public, pipeline_meta TO geoimport_betrieb;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA pipeline_meta TO geoimport_betrieb;
-- Zieltabellen: nur Zeilen lesen, einfügen und löschen
GRANT SELECT, INSERT, DELETE ON dashboard_data_de, table_metadata TO geoimport_betrieb;
GRANT SELECT, INSERT, DELETE ON "Geometrien (Demo)", "Naturschutzgebiete" TO geoimport_betrieb;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO geoimport_betrieb;   -- SERIAL-IDs
```

`TEMPORARY` wird für die Staging-Tabelle je Quelle gebraucht. Lesende Nutzer
(Dashboard, GIS) brauchen keine Rechte am Schema `pipeline_meta`.

Sicherung: übliche PostgreSQL-Verfahren, z. B. `pg_dump -Fc`. Der Zustand der
Pipeline liegt vollständig im Schema `pipeline_meta`; Berichte sind optional.

## Überwachung

- **Rückgabewert** von `geoimport run`: `0` alles erfolgreich, `1` mindestens
  eine Quelle fehlgeschlagen, `2` Lauf nicht möglich (Konfiguration, Datenbank).
- **Bericht**: `reports/latest.json` enthält `status` (`success`, `partial`,
  `failed`, `empty`), `finished_at` und je Quelle Ziel, Kennzahlen (neu,
  geändert, entfernt, unverändert, übersprungen, geschrieben), Fehler und
  Warnungen. Eine einfache Prüfung für ein Monitoring-System:

  ```bash
  jq -e '.status == "success"' reports/latest.json >/dev/null || echo "Import prüfen"
  find reports/latest.json -mmin -11000 | grep -q . || echo "Kein Lauf in den letzten 7 Tagen"
  ```

- **Datenbank**: `pipeline_meta.source_state` (`last_status`, `last_error`,
  `last_success_at`, `last_counts`, `records`) zeigt den Stand jeder Quelle,
  `pipeline_meta.import_run` jeden Lauf und `pipeline_meta.change_summary` die
  Änderungen je Lauf und Quelle.
- **Protokoll**: Ausgabe auf stderr (im Container `docker compose logs`).

## Fehlerfälle und Verhalten

| Situation | Verhalten |
|---|---|
| Dienst nicht erreichbar, HTTP 5xx/429, Zeitüberschreitung | bis zu `http.retries` Wiederholungen, danach Quelle fehlgeschlagen; andere Quellen laufen weiter. CSV mit `fallback_path`: abgelegte Datei wird verwendet (Warnung) |
| WFS liefert ExceptionReport (z. B. falscher Layername) | Quelle fehlgeschlagen, Meldung des Dienstes im Bericht; bei unbekanntem Layer werden die verfügbaren Layer genannt |
| WFS-Antwort gekürzt (`CountDefault` bzw. `numberMatched` größer als geliefert) | Quelle fehlgeschlagen mit Hinweis auf `paging: always`; nichts wird ersetzt |
| Dienst ignoriert `STARTINDEX` (gleiche Seite wiederholt) | Quelle fehlgeschlagen statt Endlosschleife oder Dubletten |
| Abruf bewusst begrenzt (`max_features`) | Quelle fehlgeschlagen; fehlende Objekte gelten nicht als entfernt |
| Mehr unbrauchbare Datensätze als `max_skipped_ratio` (Standard 0) | Quelle fehlgeschlagen; Bestand unverändert, Beispiele in den Warnungen |
| Zieltabelle oder Spalte fehlt, Pflichtspalte nicht belegt, KBS passt nicht | Quelle fehlgeschlagen vor dem Schreiben (`TargetError`) |
| Fehler beim Schreiben (z. B. Wert zu lang für die Spalte, Typ passt nicht) | Transaktion der Quelle zurückgerollt: Zeilen, Änderungsprotokoll und Stand bleiben wie vorher; `last_status = failed` |
| Viele Datensätze fehlen plötzlich (mehr als `max_delete_ratio`, Standard 50 %) | Quelle zurückgerollt (Schutz vor leeren oder unvollständigen Lieferungen); bei gewollter Bereinigung `max_delete_ratio` erhöhen |
| Datensatz ohne Schlüssel, ohne oder mit unlesbarer Geometrie, Zahl nicht lesbar | Datensatz übersprungen, Warnung mit Beispielen (zählt zu `max_skipped_ratio`) |
| Ungültige Geometrie (z. B. Selbstüberschneidung) | übernommen mit Warnung, optional automatisch repariert (`repair_invalid_geometries`) |
| Doppelter Schlüssel | je nach `duplicate_keys`: Warnung (erster Datensatz), Zusammenführen der Teilgeometrien oder Fehler |
| Schema der Quelle geändert (Felder neu/entfallen, gemappte Felder fehlen) | Import läuft, Warnung im Bericht |
| Datenbank nicht erreichbar, Konfiguration ungültig | kein Import, Bericht mit Abschnitt „Lauf abgebrochen“, Rückgabewert 2 |

Einen fehlgeschlagenen Import einer einzelnen Quelle wiederholen:
`geoimport run --source <id>`. Wiederholungen sind unschädlich: Unveränderte
Datensätze erzeugen keine Einträge im Änderungsprotokoll.

## Wartung

- **Berichte** werden nicht automatisch gelöscht, z. B.
  `find reports -name 'import_*' -mtime +180 -delete`.
- **Änderungsprotokoll**: wächst nur um tatsächliche Änderungen. Bei Bedarf
  ausdünnen, z. B.
  `DELETE FROM pipeline_meta.change_log WHERE changed_at < now() - interval '2 years';`
- **Neue Quelle in einer geteilten Tabelle**: Eine Quelle darf nur ihre eigenen
  Zeilen ersetzen. `geoimport check-config` weist doppelte Quellenangaben und
  Konflikte mit exklusiven Tabellen ab.
- PostgreSQL-Autovacuum genügt im Normalfall; die Staging-Tabellen sind temporär
  und verschwinden mit jeder Transaktion.

## Sicherheit

- Keine Zugangsdaten in Code, Konfiguration oder Image; `.env` ist in
  `.gitignore` und `.dockerignore` ausgeschlossen.
- XML wird mit `defusedxml` gelesen (Schutz vor Entity-Expansion und externen
  Entitäten), TLS-Prüfung ist standardmäßig aktiv.
- WFS-Folgeanfragen gehen standardmäßig an die konfigurierte URL, nicht an
  URLs aus den Capabilities (`use_advertised_urls: false`).
- Alle SQL-Bezeichner (Tabellen, Spalten, Schema) werden über `psycopg.sql`
  sicher gequotet, Werte immer als Parameter übergeben.
- Der Container läuft ohne Root-Rechte; die Datenbank ist im Compose-Setup nur
  im internen Netz erreichbar.
