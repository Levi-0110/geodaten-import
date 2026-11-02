"""Kommandozeile: ``geoimport run | schedule | init-db | check-config``.

Rückgabewerte von ``geoimport run``:

* 0 – alle Quellen erfolgreich
* 1 – mindestens eine Quelle fehlgeschlagen (Bericht enthält die Ursache)
* 2 – Lauf nicht möglich (Konfiguration oder Datenbank)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from geoimport import __version__
from geoimport.config import ConfigError, PipelineConfig, load_config

log = logging.getLogger("geoimport")

EXIT_OK = 0
EXIT_SOURCE_FAILED = 1
EXIT_ERROR = 2


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _run_once(cfg: PipelineConfig, source_ids: list[str] | None, trigger: str) -> int:
    from geoimport.pipeline import run_pipeline
    from geoimport.report import build_report, write_report

    run = run_pipeline(cfg, source_ids=source_ids, trigger=trigger)
    report = build_report(run)
    json_path, md_path = write_report(
        report,
        cfg.report_dir(),
        cfg.schedule.timezone,
        cfg.report.write_latest,
        cfg.report.markdown,
    )
    summary = report["summary"]
    print(
        f"Importlauf {report['run_id']}: {report['status']} – "
        f"{summary['sources_succeeded']}/{summary['sources_total']} Quellen, "
        f"{summary['new']} neu, {summary['changed']} geändert, {summary['deleted']} gelöscht, "
        f"{summary['warnings']} Warnungen"
    )
    print(f"Bericht: {json_path}")
    if md_path is not None:
        print(f"         {md_path}")
    if run.error is not None:
        return EXIT_ERROR
    return EXIT_OK if run.status in ("success", "empty") else EXIT_SOURCE_FAILED


def cmd_run(cfg: PipelineConfig, args: argparse.Namespace) -> int:
    try:
        cfg.select_sources(args.source)
    except ConfigError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return _run_once(cfg, args.source, "manual")


def cmd_schedule(cfg: PipelineConfig, args: argparse.Namespace) -> int:
    from geoimport.scheduler import run_scheduler

    if args.run_now:
        cfg.schedule.run_on_start = True
    groups = cfg.schedule_groups()
    if not groups:
        print("Keine aktiven Quellen konfiguriert.", file=sys.stderr)
        return EXIT_ERROR
    run_scheduler(cfg.schedule, groups, lambda ids: _run_once(cfg, ids, "schedule"))
    return EXIT_OK


def cmd_init_db(cfg: PipelineConfig, args: argparse.Namespace) -> int:
    from geoimport import db

    schema = cfg.database.meta_schema
    try:
        with db.connect(cfg.database) as conn:
            applied = db.migrate(conn, schema)
            db.check_schema(conn, schema)
    except Exception as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if applied:
        print(f"Migrationen angewendet (Schema {schema}): " + ", ".join(applied))
    else:
        print(f"Schema {schema} ist aktuell, keine Migration ausstehend.")
    return EXIT_OK


def cmd_check_config(cfg: PipelineConfig, args: argparse.Namespace) -> int:
    print(
        f"Konfiguration gültig: {len(cfg.sources)} Quelle(n), Ziel-KBS "
        f"EPSG:{cfg.database.target_srid}, Schema der Pipeline {cfg.database.meta_schema}, "
        f"Zeitplan '{cfg.schedule.cron}' ({cfg.schedule.timezone})"
    )
    for s in cfg.sources:
        state = "aktiv" if s.enabled else "deaktiviert"
        if s.target.exclusive:
            target = f"{s.target.table} (exklusiv)"
        else:
            target = f"{s.target.table} [{s.target.source_column} = {s.source_value!r}]"
        print(f"  - {s.id} [{s.type}, {state}] {s.title or ''}")
        print(f"      Quelle: {s.location}")
        print(f"      Ziel:   {target}")
        print(f"      Takt:   {s.schedule or cfg.schedule.cron}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geoimport",
        description="Geodaten-Import-Pipeline: WFS- und CSV-Quellen nach PostgreSQL/PostGIS",
    )
    parser.add_argument("--version", action="version", version=f"geoimport {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("GEOIMPORT_CONFIG", "config/pipeline.yml"),
        help="Pfad zu pipeline.yml (Standard: $GEOIMPORT_CONFIG oder config/pipeline.yml)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("GEOIMPORT_LOG_LEVEL", "INFO"),
        help="DEBUG, INFO, WARNING oder ERROR",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="Import jetzt ausführen (manueller Auslöser)")
    p_run.add_argument(
        "--source",
        action="append",
        metavar="ID",
        help="nur diese Quelle(n) importieren; mehrfach angebbar",
    )
    p_run.set_defaults(func=cmd_run)
    p_sched = sub.add_parser("schedule", help="Import zeitgesteuert nach Cron-Ausdruck ausführen")
    p_sched.add_argument("--run-now", action="store_true", help="zusätzlich sofort einmal laufen")
    p_sched.set_defaults(func=cmd_schedule)
    p_init = sub.add_parser("init-db", help="Datenbankschema anlegen bzw. migrieren")
    p_init.set_defaults(func=cmd_init_db)
    p_check = sub.add_parser("check-config", help="Konfiguration prüfen und Quellen auflisten")
    p_check.set_defaults(func=cmd_check_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Fehler in der Konfiguration:\n{exc}", file=sys.stderr)
        return EXIT_ERROR
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
