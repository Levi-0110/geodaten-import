-- mosidi_zieltabellen.sql
-- NUR FÜR DEMO UND TESTS: legt Zieltabellen an, wie sie im MOSIDI-System bereits
-- bestehen. Im Betrieb sind diese Tabellen vorhanden und werden von der Pipeline
-- nie angelegt oder verändert; die Pipeline schreibt nur Zeilen hinein.
--
-- Vorlagen (öffentliches Repository MOSIDI-ORG/MOSIDI, Stand 02.11.2026):
--   dashboard_data_de  services/backend/src/db_table_schema/schema.sqlalchemy
--   table_metadata     services/backend/src/db_table_metadata/script.py
-- Die Geometrietabellen sind Beispiele im Stil der MOSIDI-Tabellen; ihre
-- endgültige Struktur ergibt sich aus den Schemas, die die Hochschule nach dem
-- Zuschlag übergibt. Die Abbildung erfolgt dann nur in der YAML-Konfiguration.
--
-- Der Docker-Compose-Aufbau führt diese Datei beim ersten Start der Datenbank aus
-- (docker-entrypoint-initdb.d); die End-to-End-Tests ebenso.

CREATE EXTENSION IF NOT EXISTS postgis;

-- (1) Metadatentabelle: DCAT-AP-Beschreibung je Datensatz
CREATE TABLE IF NOT EXISTS public.table_metadata (
    dct_title TEXT,
    dct_description TEXT,
    dct_catalog_title TEXT,
    dct_catalog_description TEXT,
    dct_catalog_publisher TEXT,
    dct_accessurl TEXT,
    dct_license TEXT,
    dct_identifier TEXT,
    dcatde_contributorid TEXT,
    dct_distribution TEXT,
    dct_language TEXT,
    dct_bbox GEOMETRY,
    dct_centroid GEOMETRY,
    geometry_type TEXT,
    dcatde_politicalGeocodingLevelURI TEXT,
    dcatde_politicalGeocodingURI TEXT,
    dcatde_geocodingText TEXT,
    dct_modified TIMESTAMP,
    dct_issued TIMESTAMP,
    dct_accrualperiodicity TEXT,
    dct_temporal_startdata TIMESTAMP,
    dct_temporal_enddate TIMESTAMP,
    table_name TEXT,
    details JSONB,
    imported TIMESTAMP,
    dct_type TEXT,
    legend_url TEXT,
    dcat_ap_id TEXT,
    dcat_ap_title TEXT,
    CONSTRAINT table_metadata_pkey PRIMARY KEY (table_name, dcatde_politicalGeocodingLevelURI)
);

-- (2) Attributtabelle: Geo-Referenz (kennziffer = AGS), Attribut-Typ (indikator),
--     Wert, Zeit-Referenz (zeitbezug) und Quellenangabe (source)
CREATE TABLE IF NOT EXISTS public.dashboard_data_de (
    id SERIAL NOT NULL,
    indikator TEXT,
    kennziffer TEXT,
    wert NUMERIC,
    zeitbezug INTEGER,
    granularity VARCHAR(50),
    source VARCHAR(200),
    data_type VARCHAR(50),
    CONSTRAINT dashboard_data_de_expanded_pkey PRIMARY KEY (id)
);

-- (3a) Beispiel-Geometrietabelle, die sich mehrere Quellen teilen (Spalte source)
CREATE TABLE IF NOT EXISTS public."Geometrien (Demo)" (
    id SERIAL NOT NULL,
    geo_ref VARCHAR(64) NOT NULL,
    name TEXT,
    geom geometry(GEOMETRY, 4326) NOT NULL,
    source VARCHAR(200) NOT NULL,
    CONSTRAINT geometrien_demo_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS geometrien_demo_geom ON public."Geometrien (Demo)" USING gist (geom);

-- (3b) Beispiel einer themenbezogenen Geometrietabelle ohne Quellenspalte,
--      die einer einzigen Quelle gehört (in der Konfiguration: exclusive: true)
CREATE TABLE IF NOT EXISTS public."Naturschutzgebiete" (
    id SERIAL NOT NULL,
    geom geometry(MULTIPOLYGON, 4326),
    gebietsnummer VARCHAR(20),
    name VARCHAR(254),
    typ VARCHAR(50),
    schutzstatus VARCHAR(50),
    inkrafttreten VARCHAR(30),
    CONSTRAINT naturschutzgebiete_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS naturschutzgebiete_geom ON public."Naturschutzgebiete" USING gist (geom);
