"""Staging, Dubletten, Änderungserkennung und Änderungsprotokoll.

Alle Funktionen laufen innerhalb der Transaktion einer Quelle. Ablauf:

1. Parallele Läufe derselben Quelle verhindert eine Advisory-Sperre.
2. Die Datensätze werden während des Abrufs per ``COPY`` in eine temporäre
   Staging-Tabelle geschrieben (Streaming, geringer Speicherbedarf).
3. Doppelte Schlüssel werden nach ``duplicate_keys`` behandelt (Fehler, ersten
   Datensatz behalten oder Teilgeometrien zusammenfassen).
4. Je Datensatz wird der Inhalts-Hash mit dem Stand des letzten erfolgreichen
   Laufs (``record_state``) verglichen: neu, geändert, unverändert, gelöscht.
   Gelöscht ist, was bei diesem vollständigen und fehlerfreien Abruf fehlt.
5. Neue, geänderte und gelöschte Datensätze werden im Änderungsprotokoll
   (``change_log``) mit der Lauf-ID festgehalten; ``record_state`` übernimmt den
   neuen Stand. Frühere Fassungen der Daten werden nicht aufbewahrt.

Das eigentliche Ersetzen der Zeilen in der Zieltabelle übernimmt
:mod:`geoimport.targets` in derselben Transaktion.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from geoimport.config import SourceBase
from geoimport.geometry import to_wkb
from geoimport.hashing import content_hash, normalize_attributes
from geoimport.models import Feature, Warnings

STAGE_TABLE = "geoimport_stage"


class PlausibilityError(Exception):
    """Der Lauf einer Quelle wurde wegen unplausibler Ergebnisse abgebrochen."""


class IncompleteFetchError(Exception):
    """Der Abruf war nicht vollständig oder nicht fehlerfrei; der Bestand bleibt unverändert."""


class DuplicateKeyError(Exception):
    """Doppelter Schlüssel bei ``duplicate_keys: error``."""


class Meta:
    """SQL-Bezeichner der Tabellen im eigenen Schema der Pipeline."""

    def __init__(self, schema: str) -> None:
        self.schema = schema
        self.source_state = sql.Identifier(schema, "source_state")
        self.record_state = sql.Identifier(schema, "record_state")
        self.change_log = sql.Identifier(schema, "change_log")
        self.import_run = sql.Identifier(schema, "import_run")


def lock_source(conn: psycopg.Connection, source_id: str) -> None:
    """Sperrt die Quelle bis zum Ende der Transaktion (kein paralleler Lauf)."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 4711))", (source_id,))


# ------------------------------------------------------------------ Staging


def create_stage(conn: psycopg.Connection) -> None:
    conn.execute(
        sql.SQL(
            """
            CREATE TEMP TABLE {} (
                seq          bigint GENERATED ALWAYS AS IDENTITY,
                record_key   text NOT NULL,
                content_hash char(64) NOT NULL,
                geom_wkb     bytea,
                srid         integer,
                attrs        jsonb NOT NULL
            ) ON COMMIT DROP
            """
        ).format(sql.Identifier(STAGE_TABLE))
    )


def load_stage(conn: psycopg.Connection, features: Iterable[Feature]) -> int:
    """Schreibt die Features per COPY in die Staging-Tabelle. Liefert die Anzahl."""
    n = 0
    copy_sql = sql.SQL(
        "COPY {} (record_key, content_hash, geom_wkb, srid, attrs) FROM STDIN"
    ).format(sql.Identifier(STAGE_TABLE))
    with conn.cursor() as cur, cur.copy(copy_sql) as copy:
        for feature in features:
            wkb = to_wkb(feature.geometry) if feature.geometry is not None else None
            attrs = normalize_attributes(feature.attributes)
            copy.write_row(
                (
                    feature.key,
                    content_hash(attrs, wkb, feature.srid),
                    wkb,
                    feature.srid,
                    Jsonb(attrs),
                )
            )
            n += 1
    stage = sql.Identifier(STAGE_TABLE)
    conn.execute(sql.SQL("CREATE INDEX ON {} (record_key)").format(stage))
    conn.execute(sql.SQL("ANALYZE {}").format(stage))
    return n


def _examples(rows: list[tuple[Any, ...]], limit: int = 5) -> list[str]:
    return [str(r[0]) for r in rows[:limit]]


def resolve_duplicates(
    conn: psycopg.Connection, source: SourceBase, warnings: Warnings
) -> dict[str, int]:
    """Sorgt für eindeutige Schlüssel in der Staging-Tabelle.

    * ``error``: Abbruch der Quelle.
    * ``warn``: der erste Datensatz (Lieferreihenfolge) bleibt, weitere entfallen.
    * ``merge``: Teilgeometrien desselben Schlüssels werden zu einem Mehrteil-Objekt
      zusammengefasst (``ST_Collect``); die Attribute stammen vom ersten Datensatz.
      Der Hash ergibt sich aus den sortierten Hashes der Teile und ist damit
      unabhängig von der Lieferreihenfolge.
    """
    stage = sql.Identifier(STAGE_TABLE)
    dups = conn.execute(
        sql.SQL(
            "SELECT record_key, count(*), count(DISTINCT srid), count(DISTINCT attrs), "
            "       bool_and(geom_wkb IS NOT NULL) "
            "  FROM {} GROUP BY record_key HAVING count(*) > 1 ORDER BY record_key"
        ).format(stage)
    ).fetchall()
    if not dups:
        return {"merged": 0, "duplicates_dropped": 0}
    if source.duplicate_keys == "error":
        raise DuplicateKeyError(
            f"{len(dups)} Schlüssel kommen mehrfach vor (duplicate_keys: error), z. B. "
            + ", ".join(_examples(dups))
        )
    mergeable = [r for r in dups if r[2] == 1 and r[4]] if source.duplicate_keys == "merge" else []
    keep_first = [r for r in dups if r not in mergeable]
    dropped = 0
    if keep_first:
        for row in keep_first:
            warnings.add("duplicate_key", example=str(row[0]))
        dropped = conn.execute(
            sql.SQL(
                "DELETE FROM {stage} s USING (SELECT record_key, min(seq) AS keep FROM {stage} "
                "WHERE record_key = ANY(%s) GROUP BY record_key) d "
                "WHERE s.record_key = d.record_key AND s.seq <> d.keep"
            ).format(stage=stage),
            ([r[0] for r in keep_first],),
        ).rowcount
    merged = 0
    if mergeable:
        keys = [r[0] for r in mergeable]
        for row in mergeable:
            if row[3] > 1:
                warnings.add("duplicate_key_conflict", example=str(row[0]))
            warnings.add("duplicate_key_merged", example=str(row[0]))
        conn.execute(
            sql.SQL(
                """
                WITH parts AS (
                    SELECT s.record_key, s.seq, s.srid, (ST_Dump(ST_GeomFromWKB(s.geom_wkb))).geom
                      FROM {stage} s WHERE s.record_key = ANY(%(keys)s)
                ), merged AS (
                    SELECT record_key,
                           ST_AsBinary(ST_Collect(geom ORDER BY seq)) AS wkb
                      FROM parts GROUP BY record_key
                ), hashes AS (
                    SELECT record_key, min(seq) AS keep,
                           encode(sha256(convert_to(
                               string_agg(content_hash, '' ORDER BY content_hash), 'UTF8')),
                               'hex') AS hash
                      FROM {stage} WHERE record_key = ANY(%(keys)s) GROUP BY record_key
                )
                UPDATE {stage} s SET geom_wkb = m.wkb, content_hash = h.hash
                  FROM hashes h JOIN merged m USING (record_key)
                 WHERE s.seq = h.keep
                """
            ).format(stage=stage),
            {"keys": keys},
        )
        merged = conn.execute(
            sql.SQL(
                "DELETE FROM {stage} s USING (SELECT record_key, min(seq) AS keep FROM {stage} "
                "WHERE record_key = ANY(%s) GROUP BY record_key) d "
                "WHERE s.record_key = d.record_key AND s.seq <> d.keep"
            ).format(stage=stage),
            (keys,),
        ).rowcount
    return {"merged": merged, "duplicates_dropped": dropped}


# ------------------------------------------------------------------ Vergleich


def compare_with_state(conn: psycopg.Connection, meta: Meta, source: SourceBase) -> dict[str, int]:
    """Zählt neue, geänderte, unveränderte und gelöschte Datensätze."""
    p = {"sid": source.id}
    stage = sql.Identifier(STAGE_TABLE)
    new, changed, unchanged = conn.execute(
        sql.SQL(
            """
            SELECT count(*) FILTER (WHERE r.record_key IS NULL),
                   count(*) FILTER (WHERE r.content_hash <> s.content_hash),
                   count(*) FILTER (WHERE r.content_hash = s.content_hash)
              FROM {stage} s
              LEFT JOIN {state} r ON r.source_id = %(sid)s AND r.record_key = s.record_key
            """
        ).format(stage=stage, state=meta.record_state),
        p,
    ).fetchone()
    previous, deleted = conn.execute(
        sql.SQL(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE NOT EXISTS (
                       SELECT 1 FROM {stage} s WHERE s.record_key = r.record_key))
              FROM {state} r WHERE r.source_id = %(sid)s
            """
        ).format(stage=stage, state=meta.record_state),
        p,
    ).fetchone()
    return {
        "previous": int(previous),
        "new": int(new),
        "changed": int(changed),
        "unchanged": int(unchanged),
        "deleted": int(deleted),
    }


def check_plausibility(source: SourceBase, counts: dict[str, int]) -> None:
    previous, deleted = counts["previous"], counts["deleted"]
    if previous and deleted / previous > source.max_delete_ratio:
        raise PlausibilityError(
            f"Plausibilitätsprüfung: {deleted} von {previous} Datensätzen würden entfernt "
            f"({deleted / previous:.0%} > max_delete_ratio {source.max_delete_ratio:.0%}). "
            f"Quelle zurückgerollt, Bestand unverändert."
        )


def write_change_log(
    conn: psycopg.Connection,
    meta: Meta,
    source: SourceBase,
    run_ts: dt.datetime,
    run_id: uuid.UUID,
) -> None:
    """Protokolliert neue, geänderte und gelöschte Datensätze und übernimmt den neuen Stand."""
    p = {"sid": source.id, "run": run_id, "ts": run_ts}
    fmt = {"stage": sql.Identifier(STAGE_TABLE), "state": meta.record_state, "log": meta.change_log}
    conn.execute(
        sql.SQL(
            """
            INSERT INTO {log} (run_id, source_id, record_key, action, old_hash, new_hash,
                               changed_at)
            SELECT %(run)s, %(sid)s, s.record_key,
                   CASE WHEN r.record_key IS NULL THEN 'new' ELSE 'changed' END,
                   r.content_hash, s.content_hash, %(ts)s
              FROM {stage} s
              LEFT JOIN {state} r ON r.source_id = %(sid)s AND r.record_key = s.record_key
             WHERE r.record_key IS NULL OR r.content_hash <> s.content_hash
            UNION ALL
            SELECT %(run)s, %(sid)s, r.record_key, 'deleted', r.content_hash, NULL, %(ts)s
              FROM {state} r
             WHERE r.source_id = %(sid)s
               AND NOT EXISTS (SELECT 1 FROM {stage} s WHERE s.record_key = r.record_key)
            """
        ).format(**fmt),
        p,
    )
    conn.execute(
        sql.SQL(
            """
            DELETE FROM {state} r
             WHERE r.source_id = %(sid)s
               AND NOT EXISTS (SELECT 1 FROM {stage} s WHERE s.record_key = r.record_key)
            """
        ).format(**fmt),
        p,
    )
    conn.execute(
        sql.SQL(
            """
            INSERT INTO {state} AS r (source_id, record_key, content_hash, first_run_id,
                                      last_change_run_id)
            SELECT %(sid)s, s.record_key, s.content_hash, %(run)s, %(run)s FROM {stage} s
            ON CONFLICT (source_id, record_key) DO UPDATE
               SET content_hash = EXCLUDED.content_hash,
                   last_change_run_id = EXCLUDED.last_change_run_id
             WHERE r.content_hash <> EXCLUDED.content_hash
            """
        ).format(**fmt),
        p,
    )


# ------------------------------------------------------------------ Status je Quelle


def ensure_source_state(conn: psycopg.Connection, meta: Meta, source: SourceBase) -> None:
    """Legt die Statuszeile der Quelle an bzw. aktualisiert ihre Stammdaten."""
    conn.execute(
        sql.SQL(
            """
            INSERT INTO {} (source_id, source_type, title, location, layer, target_table,
                            source_value, provider, licence)
            VALUES (%(sid)s, %(type)s, %(title)s, %(location)s, %(layer)s, %(table)s,
                    %(value)s, %(provider)s, %(licence)s)
            ON CONFLICT (source_id) DO UPDATE SET
                source_type = EXCLUDED.source_type, title = EXCLUDED.title,
                location = EXCLUDED.location, layer = EXCLUDED.layer,
                target_table = EXCLUDED.target_table, source_value = EXCLUDED.source_value,
                provider = EXCLUDED.provider, licence = EXCLUDED.licence
            """
        ).format(meta.source_state),
        {
            "sid": source.id,
            "type": source.type,
            "title": source.title,
            "location": source.location,
            "layer": source.layer,
            "table": source.target.table,
            "value": source.source_value,
            "provider": source.metadata.provider,
            "licence": source.metadata.licence,
        },
    )


def previous_schema(conn: psycopg.Connection, meta: Meta, source_id: str) -> list[str] | None:
    row = conn.execute(
        sql.SQL("SELECT schema_fields FROM {} WHERE source_id = %s").format(meta.source_state),
        (source_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return list(row[0])


def mark_success(
    conn: psycopg.Connection,
    meta: Meta,
    source_id: str,
    run_ts: dt.datetime,
    run_id: uuid.UUID,
    schema_fields: list[str],
    counts: dict[str, int],
) -> None:
    conn.execute(
        sql.SQL(
            """
            UPDATE {}
               SET schema_fields = %(schema)s,
                   first_success_at = COALESCE(first_success_at, %(ts)s),
                   last_run_at = %(ts)s, last_success_at = %(ts)s, last_run_id = %(run)s,
                   last_status = 'success', last_error = NULL, last_counts = %(counts)s,
                   records = %(records)s
             WHERE source_id = %(sid)s
            """
        ).format(meta.source_state),
        {
            "schema": Jsonb(schema_fields),
            "ts": run_ts,
            "run": run_id,
            "counts": Jsonb(counts),
            "records": counts.get("records"),
            "sid": source_id,
        },
    )


def mark_failed(
    conn: psycopg.Connection,
    meta: Meta,
    source: SourceBase,
    run_ts: dt.datetime,
    run_id: uuid.UUID,
    message: str,
) -> None:
    ensure_source_state(conn, meta, source)
    conn.execute(
        sql.SQL(
            """
            UPDATE {}
               SET last_run_at = %(ts)s, last_run_id = %(run)s, last_status = 'failed',
                   last_error = %(msg)s
             WHERE source_id = %(sid)s
            """
        ).format(meta.source_state),
        {"ts": run_ts, "run": run_id, "msg": message[:4000], "sid": source.id},
    )


def record_run(
    conn: psycopg.Connection,
    meta: Meta,
    run_id: uuid.UUID,
    trigger: str,
    started_at: dt.datetime,
    finished_at: dt.datetime | None,
    status: str,
    summary: dict[str, Any],
) -> None:
    conn.execute(
        sql.SQL(
            """
            INSERT INTO {} (run_id, trigger, started_at, finished_at, status, summary)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET finished_at = EXCLUDED.finished_at,
                status = EXCLUDED.status, summary = EXCLUDED.summary
            """
        ).format(meta.import_run),
        (run_id, trigger, started_at, finished_at, status, Jsonb(summary)),
    )
