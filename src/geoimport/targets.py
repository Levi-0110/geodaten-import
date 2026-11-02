"""Schreiben in vorhandene Zieltabellen, ohne deren Struktur zu ändern.

Die Zieltabellen (z. B. die Attributtabelle ``dashboard_data_de``, eine
Geometrietabelle und die DCAT-AP-Metadatentabelle ``table_metadata`` des
MOSIDI-Systems) bestehen bereits und werden von der Plattform gelesen. Die
Pipeline führt deshalb nur ``DELETE`` und ``INSERT`` aus, nie ``CREATE``,
``ALTER``, ``DROP`` oder ``TRUNCATE``:

1. Vor dem Schreiben wird geprüft, ob Tabelle und alle konfigurierten Spalten
   existieren, ob Pflichtspalten (``NOT NULL`` ohne Standardwert) belegt sind und
   ob die Geometriespalte zum Ziel-KBS passt.
2. Die bisherigen Zeilen der Quelle werden gelöscht (``source_column =
   source_value``, bei exklusiven Tabellen alle Zeilen) und aus der
   Staging-Tabelle neu eingefügt. Alle Werte werden in den Datentyp der
   Zielspalte umgewandelt (``CAST``; ein Wert, der nicht passt oder zu lang ist,
   führt zum Fehler statt zu stiller Kürzung), Geometrien mit ``ST_Transform``
   ins Ziel-KBS gebracht.

Beides läuft in der Transaktion der Quelle: Lesende sehen bis zum ``COMMIT`` den
alten Stand, danach den neuen; bei einem Fehler bleibt der alte Stand erhalten.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql

from geoimport.config import KEY_REF, MetadataRecord, SourceBase, split_table_name
from geoimport.http import redact_url
from geoimport.store import STAGE_TABLE


class TargetError(Exception):
    """Zieltabelle fehlt oder passt nicht zur Konfiguration (Quelle wird nicht geschrieben)."""


@dataclass(frozen=True)
class Column:
    name: str
    type_sql: str  # vollständiger Typ, z. B. character varying(254)
    base_type: str  # ohne Längenangabe, z. B. character varying
    not_null: bool
    has_default: bool


@dataclass(frozen=True)
class TableInfo:
    schema: str
    name: str
    columns: dict[str, Column]

    @property
    def ident(self) -> sql.Composable:
        return sql.Identifier(self.schema, self.name)

    @property
    def label(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True)
class GeometryInfo:
    srid: int
    geometry_type: str  # z. B. MULTIPOLYGON, POINT, GEOMETRY

    @property
    def is_multi(self) -> bool:
        return self.geometry_type.upper().startswith("MULTI")


def inspect_table(conn: psycopg.Connection, table: str) -> TableInfo:
    """Liest Schema, Name und Spalten einer vorhandenen Tabelle aus dem Katalog."""
    schema, name = split_table_name(table)
    ident = sql.Identifier(schema, name) if schema else sql.Identifier(name)
    row = conn.execute(
        "SELECT n.nspname, c.relname, c.oid FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.oid = to_regclass(%s) AND c.relkind IN ('r', 'p')",
        (ident.as_string(conn),),
    ).fetchone()
    if row is None:
        raise TargetError(
            f"Zieltabelle {table!r} existiert nicht. Die Pipeline legt Zieltabellen nicht an; "
            f"Tabellenname und Schema in target.table prüfen."
        )
    nsp, rel, oid = row
    columns = {
        r[0]: Column(r[0], r[1], r[2], r[3], r[4])
        for r in conn.execute(
            "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), "
            "       pg_catalog.format_type(a.atttypid, NULL), a.attnotnull, "
            "       a.atthasdef OR a.attidentity <> '' OR a.attgenerated <> '' "
            "  FROM pg_attribute a WHERE a.attrelid = %s AND a.attnum > 0 "
            "   AND NOT a.attisdropped ORDER BY a.attnum",
            (oid,),
        )
    }
    return TableInfo(nsp, rel, columns)


def inspect_geometry(conn: psycopg.Connection, info: TableInfo, column: str) -> GeometryInfo:
    col = info.columns.get(column)
    if col is None or not col.type_sql.startswith("geometry"):
        raise TargetError(f"{info.label}: Spalte {column!r} ist keine PostGIS-Geometriespalte")
    row = conn.execute(
        "SELECT srid, type FROM geometry_columns "
        "WHERE f_table_schema = %s AND f_table_name = %s AND f_geometry_column = %s",
        (info.schema, info.name, column),
    ).fetchone()
    if row is None:
        return GeometryInfo(0, "GEOMETRY")
    return GeometryInfo(int(row[0] or 0), str(row[1] or "GEOMETRY"))


@dataclass
class TargetPlan:
    """Geprüfte Abbildung einer Quelle auf ihre Zieltabelle."""

    source: SourceBase
    table: TableInfo
    geometry: GeometryInfo | None
    target_srid: int

    @classmethod
    def prepare(cls, conn: psycopg.Connection, source: SourceBase, target_srid: int) -> TargetPlan:
        target = source.target
        info = inspect_table(conn, target.table)
        missing = [c for c in target.column_names() if c not in info.columns]
        if missing:
            raise TargetError(
                f"{info.label}: Spalte(n) {', '.join(missing)} fehlen. Die Pipeline ändert "
                f"Zieltabellen nicht; target.columns an das Schema anpassen."
            )
        uncovered = [
            c.name
            for c in info.columns.values()
            if c.not_null and not c.has_default and c.name not in target.column_names()
        ]
        if uncovered:
            raise TargetError(
                f"{info.label}: Pflichtspalte(n) {', '.join(uncovered)} (NOT NULL ohne "
                f"Standardwert) sind in target nicht belegt"
            )
        geometry = None
        if target.geometry_column:
            geometry = inspect_geometry(conn, info, target.geometry_column)
            if geometry.srid not in (0, target_srid):
                raise TargetError(
                    f"{info.label}.{target.geometry_column} hat EPSG:{geometry.srid}, "
                    f"Ziel-KBS der Pipeline ist EPSG:{target_srid} (database.target_srid)"
                )
        return cls(source, info, geometry, target_srid)

    # ------------------------------------------------------------ SQL-Bausteine

    def _cast(self, column: str, value: sql.Composable) -> sql.Composable:
        # Umwandlung in den Grundtyp ohne Längenangabe: Ein zu langer Text wird so beim
        # Einfügen abgelehnt (Fehler, Quelle zurückgerollt) statt stillschweigend gekürzt.
        return sql.SQL("CAST({} AS {})").format(
            value, sql.SQL(self.table.columns[column].base_type)
        )

    def _select_list(self) -> tuple[list[sql.Composable], list[sql.Composable], dict[str, Any]]:
        target = self.source.target
        names: list[sql.Composable] = []
        exprs: list[sql.Composable] = []
        params: dict[str, Any] = {}
        for i, (column, attr) in enumerate(target.columns.items()):
            names.append(sql.Identifier(column))
            if attr == KEY_REF:
                exprs.append(self._cast(column, sql.SQL("s.record_key")))
            else:
                params[f"a{i}"] = attr
                value = sql.SQL("(s.attrs ->> {})").format(sql.Placeholder(f"a{i}"))
                exprs.append(self._cast(column, value))
        for i, (column, constant) in enumerate(target.constants.items()):
            names.append(sql.Identifier(column))
            params[f"c{i}"] = None if constant is None else _constant_text(constant)
            exprs.append(self._cast(column, sql.Placeholder(f"c{i}")))
        if target.source_column:
            names.append(sql.Identifier(target.source_column))
            params["source_value"] = self.source.source_value
            exprs.append(self._cast(target.source_column, sql.Placeholder("source_value")))
        if target.geometry_column:
            assert self.geometry is not None
            names.append(sql.Identifier(target.geometry_column))
            geom = sql.SQL(
                "ST_Force2D(ST_Transform(ST_SetSRID(ST_GeomFromWKB(s.geom_wkb), s.srid), {}))"
            ).format(sql.Literal(int(self.target_srid)))
            if self.geometry.is_multi:
                geom = sql.SQL("ST_Multi({})").format(geom)
            exprs.append(geom)
        return names, exprs, params

    def _where_source(self) -> tuple[sql.Composable, dict[str, Any]]:
        target = self.source.target
        if target.exclusive:
            return sql.SQL(""), {}
        return (
            sql.SQL(" WHERE {} = {}").format(
                sql.Identifier(target.source_column),
                self._cast(target.source_column, sql.Placeholder("source_value")),
            ),
            {"source_value": self.source.source_value},
        )

    # ------------------------------------------------------------ Ausführung

    def count_rows(self, conn: psycopg.Connection) -> int:
        where, params = self._where_source()
        query = sql.SQL("SELECT count(*) FROM {}").format(self.table.ident) + where
        return int(conn.execute(query, params).fetchone()[0])

    def replace_rows(self, conn: psycopg.Connection) -> dict[str, int]:
        """Ersetzt die Zeilen der Quelle durch den Inhalt der Staging-Tabelle."""
        where, params = self._where_source()
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DELETE FROM {}").format(self.table.ident) + where, params)
            removed = cur.rowcount
            names, exprs, select_params = self._select_list()
            cur.execute(
                sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} s ORDER BY s.seq").format(
                    self.table.ident,
                    sql.SQL(", ").join(names),
                    sql.SQL(", ").join(exprs),
                    sql.Identifier(STAGE_TABLE),
                ),
                select_params,
            )
            written = cur.rowcount
        return {"target_removed": removed, "written": written}


def _constant_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_metadata_record(
    conn: psycopg.Connection,
    source: SourceBase,
    record: MetadataRecord,
    values: dict[str, Any],
) -> None:
    """Ersetzt die Metadatenzeile der Quelle (z. B. DCAT-AP in ``table_metadata``)."""
    info = inspect_table(conn, record.table)
    wanted = [*record.key_columns, *record.columns]
    missing = [c for c in wanted if c not in info.columns]
    if missing:
        raise TargetError(f"{info.label}: Spalte(n) {', '.join(missing)} fehlen")
    templates = {**record.key_columns, **record.columns}
    slot = {column: f"m{i}" for i, column in enumerate(wanted)}
    rendered = {slot[c]: _render(templates[c], values) for c in wanted}

    def cast(column: str) -> sql.Composable:
        return sql.SQL("CAST({} AS {})").format(
            sql.Placeholder(slot[column]), sql.SQL(info.columns[column].base_type)
        )

    where = sql.SQL(" AND ").join(
        sql.SQL("{} = {}").format(sql.Identifier(c), cast(c)) for c in record.key_columns
    )
    conn.execute(sql.SQL("DELETE FROM {} WHERE {}").format(info.ident, where), rendered)
    conn.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            info.ident,
            sql.SQL(", ").join(sql.Identifier(c) for c in wanted),
            sql.SQL(", ").join(cast(c) for c in wanted),
        ),
        rendered,
    )


def _render(template: str, values: dict[str, Any]) -> str | None:
    text = template.format_map({k: "" if v is None else v for k, v in values.items()})
    return text if text != "" else None


def metadata_values(
    source: SourceBase,
    plan: TargetPlan,
    run_ts: dt.datetime,
    run_id: uuid.UUID,
    records: int,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = source.metadata
    details = details or {}
    return {
        "id": source.id,
        "title": source.title or source.id,
        "description": meta.description,
        "provider": meta.provider,
        "licence": meta.licence,
        "licence_url": meta.licence_url,
        "attribution": meta.attribution,
        "update_cycle": meta.update_cycle,
        "url": details.get("url_used")
        or details.get("fallback_file")
        or (redact_url(source.location) if source.location else None),
        "layer": source.layer,
        "target_table": plan.table.label,
        "source_value": source.source_value,
        "records": records,
        "run_id": str(run_id),
        "run_time": run_ts.isoformat(),
    }
