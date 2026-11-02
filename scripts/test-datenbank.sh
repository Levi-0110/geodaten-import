#!/usr/bin/env bash
# Wegwerf-PostgreSQL/PostGIS-Cluster für die End-to-End-Tests.
#
# Voraussetzung: PostgreSQL-Server und PostGIS lokal installiert, z. B. unter
# Debian/Ubuntu:  apt-get install postgresql-16 postgresql-16-postgis-3
#
#   scripts/test-datenbank.sh start    # Cluster anlegen (falls nötig) und starten
#   scripts/test-datenbank.sh dsn      # Verbindungs-URL ausgeben (enthält das Passwort)
#   scripts/test-datenbank.sh stop     # Cluster anhalten
#   scripts/test-datenbank.sh remove   # Cluster anhalten und Verzeichnis löschen
#
# Tests dann z. B. mit:
#   export GEOIMPORT_TEST_DSN="$(scripts/test-datenbank.sh dsn)"
#   pytest
#
# Umgebungsvariablen: PGTEST_DIR (Standard: .pgtest im Projektverzeichnis),
# PGTEST_PORT (55433), PG_BINDIR (Verzeichnis mit initdb/pg_ctl).
# Der Cluster lauscht nur auf 127.0.0.1; das Passwort wird zufällig erzeugt und
# in "$PGTEST_DIR/.pw" (Rechte 600) abgelegt.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="${PGTEST_DIR:-$PROJECT_DIR/.pgtest}"
PORT="${PGTEST_PORT:-55433}"
DB="geoimport_test"
DB_USER="geoimport"

find_bindir() {
    if [[ -n "${PG_BINDIR:-}" ]]; then
        echo "$PG_BINDIR"
    elif command -v pg_config >/dev/null && [[ -x "$(pg_config --bindir)/initdb" ]]; then
        pg_config --bindir
    else
        ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -n 1
    fi
}

BINDIR="$(find_bindir)"
if [[ -z "$BINDIR" || ! -x "$BINDIR/initdb" ]]; then
    echo "initdb nicht gefunden – PostgreSQL-Server installieren oder PG_BINDIR setzen" >&2
    exit 2
fi

# initdb verweigert den Start als root; dann als Benutzer postgres ausführen.
# Das Verzeichnis muss für diesen Benutzer erreichbar sein.
if [[ "$(id -u)" == "0" ]]; then
    AS_OWNER=(runuser -u postgres --)
else
    AS_OWNER=()
fi

run_owner() { "${AS_OWNER[@]}" "$@"; }

dsn() {
    echo "postgresql://$DB_USER:$(cat "$DIR/.pw")@127.0.0.1:$PORT/$DB"
}

start() {
    if [[ ! -d "$DIR/data" ]]; then
        mkdir -p "$DIR"
        chmod 700 "$DIR"
        (umask 077 && head -c 18 /dev/urandom | base64 | tr '+/' '-_' > "$DIR/.pw")
        if [[ ${#AS_OWNER[@]} -gt 0 ]]; then
            chown -R postgres:postgres "$DIR"
        fi
        run_owner "$BINDIR/initdb" -D "$DIR/data" -U "$DB_USER" --pwfile="$DIR/.pw" \
            --auth-local=scram-sha-256 --auth-host=scram-sha-256 -E UTF8 --locale=C.UTF-8 \
            >"$DIR/initdb.log"
    fi
    if ! run_owner "$BINDIR/pg_ctl" -D "$DIR/data" status >/dev/null 2>&1; then
        run_owner "$BINDIR/pg_ctl" -D "$DIR/data" -l "$DIR/server.log" -w \
            -o "-p $PORT -k $DIR -c listen_addresses=127.0.0.1" start >/dev/null
    fi
    export PGPASSWORD PGOPTIONS="--client-min-messages=warning"
    PGPASSWORD="$(cat "$DIR/.pw")"
    if ! "$BINDIR/psql" -h 127.0.0.1 -p "$PORT" -U "$DB_USER" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '$DB'" | grep -q 1; then
        "$BINDIR/psql" -h 127.0.0.1 -p "$PORT" -U "$DB_USER" -d postgres -q \
            -c "CREATE DATABASE $DB"
    fi
    "$BINDIR/psql" -h 127.0.0.1 -p "$PORT" -U "$DB_USER" -d "$DB" -q \
        -c "CREATE EXTENSION IF NOT EXISTS postgis"
    echo "Testdatenbank läuft auf 127.0.0.1:$PORT (Datenbank $DB)."
    echo "Verbindung für die Tests:"
    echo "  export GEOIMPORT_TEST_DSN=\"\$($0 dsn)\""
}

stop() {
    if [[ -d "$DIR/data" ]]; then
        run_owner "$BINDIR/pg_ctl" -D "$DIR/data" -m fast stop >/dev/null 2>&1 || true
    fi
}

case "${1:-start}" in
    start) start ;;
    dsn) dsn ;;
    stop) stop ;;
    remove) stop; rm -rf "$DIR" ;;
    *) echo "Aufruf: $0 {start|dsn|stop|remove}" >&2; exit 2 ;;
esac
