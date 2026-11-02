"""Datenbankverbindung und SQL-Migrationen.

Verbindungsdaten kommen ausschließlich aus der Umgebung:

* ``GEOIMPORT_DB_DSN`` (z. B. ``postgresql://geoimport@db:5432/geodaten``) und/oder
  die libpq-Standardvariablen ``PGHOST``, ``PGPORT``, ``PGDATABASE``, ``PGUSER``,
  ``PGPASSWORD``;
* optional ``GEOIMPORT_DB_PASSWORD_FILE`` (Pfad zu einer Datei mit dem Passwort,
  z. B. ein Docker-Secret).

Migrationen sind SQL-Dateien in ``geoimport/migrations`` (``NNN_name.sql``). Sie
legen ausschließlich das eigene Schema der Pipeline an (Standard ``pipeline_meta``),
werden in Namensreihenfolge genau einmal ausgeführt und in
``<meta_schema>.schema_migrations`` protokolliert. Bestehende Tabellen der
Datenbank werden von Migrationen nie verändert.
"""

from __future__ import annotations

import hashlib
import logging
import os
from importlib import resources
from pathlib import Path

import psycopg
from psycopg import sql

from geoimport.config import DatabaseSettings

log = logging.getLogger(__name__)

MIGRATIONS_PACKAGE = "geoimport.migrations"
_LOCK_ID = 4_711_253  # Advisory-Lock gegen parallele Migrationen


class DatabaseError(Exception):
    """Datenbank nicht erreichbar oder nicht passend initialisiert."""


def connect(settings: DatabaseSettings) -> psycopg.Connection:
    dsn = os.environ.get(settings.dsn_env, "")
    kwargs: dict[str, object] = {
        "autocommit": True,
        "application_name": "geoimport",
        "connect_timeout": settings.connect_timeout_seconds,
    }
    pw_file = os.environ.get(settings.password_file_env)
    if pw_file:
        try:
            kwargs["password"] = Path(pw_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DatabaseError(f"Passwortdatei nicht lesbar: {pw_file} ({exc})") from exc
    try:
        return psycopg.connect(dsn, **kwargs)
    except psycopg.Error as exc:
        raise DatabaseError(f"Datenbank nicht erreichbar: {exc}".strip()) from exc


def available_migrations() -> list[tuple[str, str]]:
    """Liste (Version, SQL) aller mitgelieferten Migrationen, sortiert."""
    files = [
        f
        for f in resources.files(MIGRATIONS_PACKAGE).iterdir()
        if f.name.endswith(".sql") and f.name[:3].isdigit()
    ]
    return [
        (f.name.removesuffix(".sql"), f.read_text(encoding="utf-8"))
        for f in sorted(files, key=lambda f: f.name)
    ]


def _meta(meta_schema: str) -> sql.Identifier:
    return sql.Identifier(meta_schema)


def _ensure_table(conn: psycopg.Connection, meta_schema: str) -> None:
    conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(_meta(meta_schema)))
    conn.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {}.schema_migrations (
                version     text PRIMARY KEY,
                checksum    text NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        ).format(_meta(meta_schema))
    )


def applied_migrations(conn: psycopg.Connection, meta_schema: str) -> dict[str, str]:
    exists = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL",
        (sql.Identifier(meta_schema, "schema_migrations").as_string(conn),),
    ).fetchone()[0]
    if not exists:
        return {}
    rows = conn.execute(
        sql.SQL("SELECT version, checksum FROM {}.schema_migrations").format(_meta(meta_schema))
    ).fetchall()
    return {v: c for v, c in rows}


def _checksum(sql_text: str) -> str:
    return hashlib.sha256(sql_text.encode("utf-8")).hexdigest()


def migrate(conn: psycopg.Connection, meta_schema: str) -> list[str]:
    """Führt ausstehende Migrationen aus. Liefert die Liste der neu angewendeten.

    Ist nichts ausstehend, wird nur gelesen; der laufende Betrieb kommt dann mit
    einer Rolle ohne DDL-Rechte aus (siehe docs/betrieb.md).
    """
    available = available_migrations()
    done = applied_migrations(conn, meta_schema)
    for version, text in available:
        if version in done and done[version] != _checksum(text):
            log.warning("Migration %s wurde nach dem Anwenden geändert", version)
    if all(version in done for version, _ in available):
        return []
    applied: list[str] = []
    quoted = sql.Identifier(meta_schema).as_string(conn)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
        _ensure_table(conn, meta_schema)
        done = applied_migrations(conn, meta_schema)
        for version, text in available:
            if version in done:
                continue
            log.info("Wende Migration %s an (Schema %s)", version, meta_schema)
            conn.execute(text.replace("{{meta_schema}}", quoted))
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.schema_migrations (version, checksum) VALUES (%s, %s)"
                ).format(_meta(meta_schema)),
                (version, _checksum(text)),
            )
            applied.append(version)
    return applied


def check_schema(conn: psycopg.Connection, meta_schema: str) -> None:
    """Prüft, ob PostGIS vorhanden und alle Migrationen der Pipeline angewendet sind."""
    has_postgis = conn.execute(
        "SELECT count(*) FROM pg_extension WHERE extname = 'postgis'"
    ).fetchone()[0]
    if not has_postgis:
        raise DatabaseError("PostGIS ist in der Zieldatenbank nicht installiert")
    done = applied_migrations(conn, meta_schema)
    pending = [v for v, _ in available_migrations() if v not in done]
    if pending:
        raise DatabaseError(
            "Datenbank nicht initialisiert bzw. veraltet (ausstehend: "
            + ", ".join(pending)
            + "). Bitte 'geoimport init-db' ausführen."
        )
