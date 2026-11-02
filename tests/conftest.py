"""Gemeinsame Test-Fixtures.

Datenbank-Tests benötigen eine PostgreSQL-Datenbank mit PostGIS, die über
``GEOIMPORT_TEST_DSN`` angegeben wird (z. B. vom Skript
``scripts/test-datenbank.sh``). Vor jedem Test werden das Schema der Pipeline
und die Beispiel-Zieltabellen gelöscht und aus ``demo/mosidi_zieltabellen.sql``
neu angelegt; der Datenbankname muss deshalb ``test`` enthalten. Ohne
``GEOIMPORT_TEST_DSN`` werden diese Tests übersprungen.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

sys.path.insert(0, str(Path(__file__).parent))

from fixture_server import FixtureResponder, FixtureServer  # noqa: E402
from geoimport.config import HttpSettings  # noqa: E402
from geoimport.http import HttpClient  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def responder() -> FixtureResponder:
    return FixtureResponder(FIXTURES)


@pytest.fixture
def http_client(responder: FixtureResponder):
    client = HttpClient(HttpSettings(retries=0, backoff_seconds=0), transport=responder.transport())
    yield client
    client.close()


@pytest.fixture
def fixture_server(responder: FixtureResponder):
    server = FixtureServer(responder).start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get("GEOIMPORT_TEST_DSN")
    if not dsn:
        pytest.skip("GEOIMPORT_TEST_DSN nicht gesetzt (PostGIS-Testdatenbank erforderlich)")
    with psycopg.connect(dsn, autocommit=True) as conn:
        name = conn.execute("SELECT current_database()").fetchone()[0]
        if "test" not in name and os.environ.get("GEOIMPORT_TEST_ALLOW_ANY_DB") != "1":
            pytest.exit(f"Testdatenbank {name!r} enthält nicht 'test' – Abbruch zum Schutz")
        has_postgis = conn.execute(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'postgis'"
        ).fetchone()[0]
        if not has_postgis:
            pytest.skip("PostGIS ist in der Testdatenbank nicht verfügbar")
    return dsn


DEMO_TABLES_SQL = PROJECT_ROOT / "demo" / "mosidi_zieltabellen.sql"
DEMO_TABLES = ("table_metadata", "dashboard_data_de", "Geometrien (Demo)", "Naturschutzgebiete")


def reset_database(dsn: str) -> None:
    """Pipeline-Schema löschen und Zieltabellen wie im MOSIDI-System neu anlegen."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS pipeline_meta CASCADE")
        for table in DEMO_TABLES:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table)))
        conn.execute(DEMO_TABLES_SQL.read_text("utf-8"))


@pytest.fixture
def clean_db(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    reset_database(pg_dsn)
    monkeypatch.setenv("GEOIMPORT_DB_DSN", pg_dsn)
    return pg_dsn
