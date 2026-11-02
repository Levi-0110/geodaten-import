-- 001_pipeline_meta.sql
-- Eigenes Schema der Pipeline für Importsteuerung und Änderungsprotokoll.
-- Die Zieltabellen (Metadaten, Geometrien, Attribute) werden hier nicht angelegt
-- und nie verändert; die Pipeline schreibt nur Zeilen hinein (siehe target je Quelle).
-- Der Platzhalter {{meta_schema}} wird beim Ausführen durch database.meta_schema
-- (Standard pipeline_meta) ersetzt.

CREATE TABLE {{meta_schema}}.source_state (
    source_id        text PRIMARY KEY,
    source_type      text NOT NULL,
    title            text,
    location         text,
    layer            text,
    target_table     text NOT NULL,
    source_value     text,
    provider         text,
    licence          text,
    schema_fields    jsonb,
    first_success_at timestamptz,
    last_run_at      timestamptz,
    last_run_id      uuid,
    last_status      text CHECK (last_status IN ('success', 'failed')),
    last_error       text,
    last_success_at  timestamptz,
    last_counts      jsonb,
    records          integer
);

COMMENT ON TABLE {{meta_schema}}.source_state IS
    'Eine Zeile je Quelle: Ziel, Status und Kennzahlen des letzten Laufs';
COMMENT ON COLUMN {{meta_schema}}.source_state.source_value IS
    'Wert der Quellenspalte in der Zieltabelle (NULL bei exklusiver Tabelle)';
COMMENT ON COLUMN {{meta_schema}}.source_state.schema_fields IS
    'Felder der Quelle beim letzten erfolgreichen Lauf (Erkennung von Schemaänderungen)';
COMMENT ON COLUMN {{meta_schema}}.source_state.records IS
    'Anzahl der Datensätze der Quelle nach dem letzten erfolgreichen Lauf';

CREATE TABLE {{meta_schema}}.record_state (
    source_id          text NOT NULL REFERENCES {{meta_schema}}.source_state (source_id)
                            ON UPDATE CASCADE ON DELETE CASCADE,
    record_key         text NOT NULL,
    content_hash       char(64) NOT NULL,
    first_run_id       uuid NOT NULL,
    last_change_run_id uuid NOT NULL,
    PRIMARY KEY (source_id, record_key)
);

COMMENT ON TABLE {{meta_schema}}.record_state IS
    'Fingerabdruck (SHA-256) je Datensatz des aktuellen Stands; Grundlage der Änderungserkennung';
COMMENT ON COLUMN {{meta_schema}}.record_state.record_key IS
    'Fachlicher Schlüssel laut key_fields der Quelle (mehrere Felder mit | verbunden)';

CREATE TABLE {{meta_schema}}.import_run (
    run_id       uuid PRIMARY KEY,
    trigger      text NOT NULL,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz,
    status       text NOT NULL,
    summary      jsonb
);

COMMENT ON TABLE {{meta_schema}}.import_run IS
    'Ein Eintrag je Importlauf (Zusammenfassung; der vollständige Bericht liegt als JSON-Datei vor)';

CREATE TABLE {{meta_schema}}.change_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id       uuid NOT NULL,
    source_id    text NOT NULL,
    record_key   text NOT NULL,
    action       text NOT NULL CHECK (action IN ('new', 'changed', 'deleted')),
    old_hash     char(64),
    new_hash     char(64),
    changed_at   timestamptz NOT NULL
);

COMMENT ON TABLE {{meta_schema}}.change_log IS
    'Änderungsprotokoll je Importlauf: welche Datensätze einer Quelle neu, geändert oder entfernt sind';

CREATE INDEX change_log_run_idx ON {{meta_schema}}.change_log (run_id);
CREATE INDEX change_log_record_idx ON {{meta_schema}}.change_log (source_id, record_key, changed_at);

CREATE VIEW {{meta_schema}}.change_summary AS
SELECT run_id,
       source_id,
       min(changed_at) AS changed_at,
       count(*) FILTER (WHERE action = 'new')     AS new,
       count(*) FILTER (WHERE action = 'changed') AS changed,
       count(*) FILTER (WHERE action = 'deleted') AS deleted
  FROM {{meta_schema}}.change_log
 GROUP BY run_id, source_id;

COMMENT ON VIEW {{meta_schema}}.change_summary IS 'Anzahl der Änderungen je Lauf und Quelle';
