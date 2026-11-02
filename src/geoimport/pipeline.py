"""Ablaufsteuerung eines Importlaufs.

Jede Quelle wird für sich gelesen, abgebildet und in einer eigenen
Datenbanktransaktion geschrieben. Schlägt eine Quelle fehl (nicht erreichbar,
fehlerhafte oder unvollständige Antwort, Zieltabelle passt nicht,
Datenbankfehler, Plausibilitätsprüfung), wird nur ihre Transaktion
zurückgerollt: ihre bisherigen Zeilen in der Zieltabelle bleiben unverändert,
der Fehler landet im Bericht und die übrigen Quellen laufen weiter.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg

from geoimport import db, store
from geoimport.config import CsvSource, PipelineConfig, WfsSource
from geoimport.http import HttpClient, redact_url
from geoimport.mapping import FeatureStream, check_schema
from geoimport.models import Warnings
from geoimport.sources import create_adapter
from geoimport.store import IncompleteFetchError, Meta
from geoimport.targets import TargetPlan, metadata_values, write_metadata_record

log = logging.getLogger(__name__)

Connector = Callable[[], psycopg.Connection]


@dataclass
class SourceResult:
    source_id: str
    source_type: str
    title: str | None
    location: str | None
    layer: str | None
    target: str | None = None
    status: str = "failed"
    started_at: dt.datetime | None = None
    duration_seconds: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, str] | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    run_id: uuid.UUID
    trigger: str
    started_at: dt.datetime
    target_srid: int
    finished_at: dt.datetime | None = None
    duration_seconds: float = 0.0
    sources: list[SourceResult] = field(default_factory=list)
    error: dict[str, str] | None = None

    @property
    def status(self) -> str:
        if self.error is not None:
            return "failed"
        if not self.sources:
            return "empty"
        failed = sum(1 for s in self.sources if s.status != "success")
        if failed == 0:
            return "success"
        return "failed" if failed == len(self.sources) else "partial"


def _error_dict(exc: BaseException) -> dict[str, str]:
    message = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, psycopg.Error):
        error = {"type": "DatabaseError", "message": message.splitlines()[0]}
        if exc.sqlstate:
            error["sqlstate"] = exc.sqlstate
        return error
    return {"type": exc.__class__.__name__, "message": message}


def _target_label(source: WfsSource | CsvSource) -> str:
    target = source.target
    if target.exclusive:
        return f"{target.table} (exklusiv)"
    return f"{target.table} ({target.source_column} = {source.source_value!r})"


def run_source(
    source: WfsSource | CsvSource,
    connector: Connector,
    http: HttpClient,
    run_ts: dt.datetime,
    run_id: uuid.UUID,
    target_srid: int,
    meta: Meta,
) -> SourceResult:
    result = SourceResult(
        source_id=source.id,
        source_type=source.type,
        title=source.title,
        location=redact_url(source.location) if source.location else None,
        layer=source.layer,
        target=_target_label(source),
        started_at=dt.datetime.now(dt.UTC),
    )
    warnings = Warnings()
    t0 = time.monotonic()
    log.info("Quelle %s (%s) wird verarbeitet", source.id, source.type)
    conn: psycopg.Connection | None = None
    try:
        adapter = create_adapter(source, http, warnings)
        conn = connector()
        with conn.transaction():
            store.lock_source(conn, source.id)
            plan = TargetPlan.prepare(conn, source, target_srid)
            store.ensure_source_state(conn, meta, source)
            store.create_stage(conn)
            stream = FeatureStream(adapter.read(), source, warnings)
            try:
                store.load_stage(conn, stream)
            finally:
                result.details = dict(adapter.details)
            if not adapter.complete:
                raise IncompleteFetchError(
                    "Abruf bewusst begrenzt (max_features); fehlende Objekte dürfen nicht als "
                    "gelöscht gelten. Bestand unverändert."
                )
            if stream.received and stream.skipped / stream.received > source.max_skipped_ratio:
                raise IncompleteFetchError(
                    f"Abruf nicht fehlerfrei: {stream.skipped} von {stream.received} Datensätzen "
                    f"nicht verwendbar (> max_skipped_ratio {source.max_skipped_ratio:.0%}). "
                    f"Bestand unverändert; Details unter Warnungen."
                )
            dup = store.resolve_duplicates(conn, source, warnings)
            previous_fields = store.previous_schema(conn, meta, source.id)
            schema = check_schema(
                source,
                adapter.declared_fields(),
                stream.observed_fields,
                previous_fields,
                warnings,
            )
            diff = store.compare_with_state(conn, meta, source)
            store.check_plausibility(source, diff)
            store.write_change_log(conn, meta, source, run_ts, run_id)
            written = plan.replace_rows(conn)
            records = diff["new"] + diff["changed"] + diff["unchanged"]
            if source.metadata_record is not None:
                write_metadata_record(
                    conn,
                    source,
                    source.metadata_record,
                    metadata_values(source, plan, run_ts, run_id, records, result.details),
                )
            counts = {
                "received": stream.received,
                "skipped": stream.skipped + dup["duplicates_dropped"],
                "merged": dup["merged"],
                "new": diff["new"],
                "changed": diff["changed"],
                "unchanged": diff["unchanged"],
                "deleted": diff["deleted"],
                "records": records,
                **written,
            }
            store.mark_success(conn, meta, source.id, run_ts, run_id, schema, counts)
        result.counts = counts
        result.status = "success"
    except Exception as exc:  # Fehler je Quelle abfangen, Lauf geht weiter
        log.error("Quelle %s fehlgeschlagen: %s", source.id, exc)
        log.debug("Details", exc_info=True)
        result.status = "failed"
        result.error = _error_dict(exc)
        try:
            if conn is None or conn.closed or conn.broken:
                if conn is not None:
                    conn.close()
                conn = connector()
            with conn.transaction():
                store.mark_failed(conn, meta, source, run_ts, run_id, result.error["message"])
        except Exception as inner:  # z. B. Datenbank nicht erreichbar
            log.warning(
                "Status der Quelle %s konnte nicht gespeichert werden: %s", source.id, inner
            )
    finally:
        if conn is not None:
            conn.close()
    result.warnings = warnings.as_list()
    result.duration_seconds = round(time.monotonic() - t0, 3)
    log.info(
        "Quelle %s: %s %s",
        source.id,
        result.status,
        ", ".join(f"{k}={v}" for k, v in result.counts.items()) or result.error,
    )
    return result


def run_summary(run: RunResult) -> dict[str, Any]:
    succeeded = sum(1 for s in run.sources if s.status == "success")
    totals = dict.fromkeys(("new", "changed", "deleted", "unchanged"), 0)
    for s in run.sources:
        for key in totals:
            totals[key] += int(s.counts.get(key, 0))
    return {
        "sources_total": len(run.sources),
        "sources_succeeded": succeeded,
        "sources_failed": len(run.sources) - succeeded,
        **totals,
    }


def run_pipeline(
    cfg: PipelineConfig,
    source_ids: list[str] | None = None,
    trigger: str = "manual",
    connector: Connector | None = None,
    http: HttpClient | None = None,
) -> RunResult:
    """Führt einen Importlauf über alle (bzw. die gewählten) Quellen aus."""
    target_srid = cfg.database.target_srid
    meta = Meta(cfg.database.meta_schema)
    run = RunResult(
        run_id=uuid.uuid4(),
        trigger=trigger,
        started_at=dt.datetime.now(dt.UTC),
        target_srid=target_srid,
    )
    t0 = time.monotonic()
    connect = connector or (lambda: db.connect(cfg.database))
    own_http = http is None
    client = http or HttpClient(cfg.http)
    try:
        sources = cfg.select_sources(source_ids)
        with connect() as conn:
            db.migrate(conn, meta.schema)
            db.check_schema(conn, meta.schema)
        for source in sources:
            run.sources.append(
                run_source(source, connect, client, run.started_at, run.run_id, target_srid, meta)
            )
    except Exception as exc:  # Konfigurations- oder Datenbankfehler: Lauf nicht möglich
        log.error("Importlauf nicht möglich: %s", exc)
        run.error = _error_dict(exc)
    finally:
        if own_http:
            client.close()
    run.finished_at = dt.datetime.now(dt.UTC)
    run.duration_seconds = round(time.monotonic() - t0, 3)
    if run.error is None:
        try:
            with connect() as conn, conn.transaction():
                store.record_run(
                    conn,
                    meta,
                    run.run_id,
                    run.trigger,
                    run.started_at,
                    run.finished_at,
                    run.status,
                    run_summary(run),
                )
        except Exception as exc:
            log.warning("Lauf konnte nicht in %s.import_run vermerkt werden: %s", meta.schema, exc)
    return run
