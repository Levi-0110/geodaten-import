# Lizenzen

## Dieses Projekt

`geoimport` steht unter der **Apache License 2.0** (`LICENSE`, Hinweise in
`NOTICE`). Copyright 2026 Levi Griesinger.

## Laufzeitabhängigkeiten (Python)

Ermittelt mit `pip-licenses` 5.5.5 aus der Testumgebung (Stand 31.10.2026);
enthalten sind alle Pakete, die `pip install geoimport` installiert.

| Paket | Version | Lizenz | Art |
|---|---|---|---|
| APScheduler | 3.11.3 | MIT | direkt |
| defusedxml | 0.7.1 | PSF-2.0 | direkt |
| httpx | 0.28.1 | BSD-3-Clause | direkt |
| psycopg | 3.3.6 | LGPL-3.0-only | direkt |
| psycopg-binary | 3.3.6 | LGPL-3.0-only | direkt (Extra `binary`) |
| pydantic | 2.14.2 | MIT | direkt |
| pyproj | 3.8.0 | MIT | direkt |
| PyYAML | 6.0.3 | MIT | direkt |
| shapely | 2.2.0 | BSD-3-Clause | direkt |
| tzdata | 2026.6 | Apache-2.0 | direkt |
| annotated-types | 0.8.0 | MIT | indirekt (pydantic) |
| anyio | 4.15.1 | MIT | indirekt (httpx) |
| certifi | 2026.10.23 | MPL-2.0 | indirekt (httpx) |
| h11 | 0.16.0 | MIT | indirekt (httpx) |
| httpcore | 1.0.9 | BSD-3-Clause | indirekt (httpx) |
| idna | 3.20 | BSD-3-Clause | indirekt (httpx) |
| numpy | 2.5.4 | BSD-3-Clause (und weitere freizügige, s. u.) | indirekt (shapely) |
| pydantic_core | 2.50.2 | MIT | indirekt (pydantic) |
| typing_extensions | 4.16.0 | PSF-2.0 | indirekt |
| typing-inspection | 0.4.4 | MIT | indirekt (pydantic) |
| tzlocal | 5.4.5 | MIT | indirekt (APScheduler) |

### In den Binärpaketen enthaltene Bibliotheken

| Paket | Enthaltene Bibliotheken (Lizenz) |
|---|---|
| shapely | GEOS (LGPL-2.1) |
| pyproj | PROJ (MIT), libcurl (curl-Lizenz, MIT-artig), nghttp2 (MIT), SQLite (gemeinfrei), libtiff (libtiff-Lizenz, BSD-artig) |
| psycopg-binary | libpq (PostgreSQL-Lizenz), OpenSSL 3 (Apache-2.0), MIT Kerberos (MIT), OpenLDAP-Client (OpenLDAP Public License), Cyrus SASL (BSD-artig), libxcrypt und keyutils (LGPL-2.1+), PCRE (BSD), libselinux (gemeinfrei) |
| numpy | OpenBLAS und LAPACK (BSD-3-Clause), libgfortran (GPL-3.0+ mit GCC Runtime Library Exception 3.1), libquadmath (LGPL-2.1+) |

### Bewertung

- Fast alle Abhängigkeiten stehen unter freizügigen Lizenzen (MIT, BSD,
  Apache-2.0, PSF, PostgreSQL).
- **LGPL** (psycopg, GEOS, einzelne Systembibliotheken): unverändert genutzt und
  dynamisch geladen bzw. als eigenständige Pakete installiert, die sich
  austauschen lassen. Das ist mit der Apache-2.0-Lizenz des Projekts vereinbar;
  bei Weitergabe eines Container-Images sind die Lizenztexte beizulegen (sie
  liegen in den `*.dist-info`-Verzeichnissen der Pakete).
- **certifi** (MPL-2.0, Zertifikatsliste von Mozilla) ist eine feste Abhängigkeit
  von httpx. Die MPL ist ein schwaches Copyleft auf Dateiebene, vergleichbar der
  LGPL: Pflichten entstehen nur bei Änderungen an den certifi-Dateien selbst,
  die hier nicht vorgenommen werden.
- **libgfortran** (in numpy-Wheels) steht unter der GPL mit GCC Runtime Library
  Exception, die die Nutzung in Programmen unter beliebiger Lizenz ausdrücklich
  erlaubt.
- Keine Abhängigkeit unter GPL ohne Ausnahme, AGPL oder kommerzieller Lizenz.

## Entwicklungswerkzeuge (nicht Teil der Auslieferung)

| Paket | Version | Lizenz |
|---|---|---|
| pytest | 9.2.0 | MIT |
| pluggy | 1.7.0 | MIT |
| iniconfig | 2.3.1 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| Pygments | 2.21.0 | BSD-2-Clause |
| ruff | 0.17.3 | MIT |
| pip-licenses | 5.5.5 | MIT |
| prettytable | 3.18.0 | BSD-3-Clause |
| wcwidth | 0.9.4 | MIT |

## Laufzeitumgebung und Dienste

| Komponente | Lizenz | Hinweis |
|---|---|---|
| Python 3.12 (Basis-Image `python:3.12-slim`) | PSF-2.0, Debian-Pakete unter ihren jeweiligen freien Lizenzen | |
| PostgreSQL (geprüft mit 16, 17 und 18) | PostgreSQL-Lizenz (freizügig) | eigenständiger Datenbankserver |
| PostGIS (geprüft mit 3.4, 3.5 und 3.6; Image `postgis/postgis`) | GPL-2.0-or-later | läuft als Erweiterung im eigenen Datenbankcontainer; `geoimport` spricht ihn nur über das PostgreSQL-Netzwerkprotokoll an und wird weder mit PostGIS gebunden noch zusammen verbreitet |

## Testdaten

Die aufgezeichneten Testdaten stehen unter den Lizenzen der Anbieter
(Herkunft, Abrufdatum und Änderungen: `tests/fixtures/QUELLEN.md`):

| Daten | Lizenz | Namensnennung |
|---|---|---|
| Ortsteile, WFS Verwaltungsgrenzen (LGB) | Datenlizenz Deutschland – Namensnennung – 2.0 | © GeoBasis-DE/LGB, dl-de/by-2-0 (Daten geändert) |
| Naturschutzgebiete, WFS Schutzgebiete (LfU) | Datenlizenz Deutschland – Namensnennung – 2.0 | © Landesamt für Umwelt Brandenburg |
| Ladesäulenregister (Bundesnetzagentur, über opendata.potsdam.de) | CC BY 4.0 | Bundesnetzagentur, Ladesäulenregister |

Die Datei `tests/fixtures/files/inkar_2025.zip` ist **künstlich**: Sie hat den
Spaltenaufbau der INKAR-Gesamtdatei, enthält aber ausgedachte Werte und keine
INKAR-Daten (Hinweis auch in `LIESMICH.txt` im Archiv).

Beim produktiven Import stehen Lizenz und Namensnennung je Quelle in der
Konfiguration (`metadata`); sie werden in `pipeline_meta.source_state` und,
falls konfiguriert, in die DCAT-AP-Metadatentabelle (`metadata_record`, z. B.
`table_metadata.dct_license`) übernommen.

## Liste neu erzeugen

```bash
pip install -e '.[dev]'
pip-licenses --from=mixed --with-urls --format=markdown \
  --packages APScheduler PyYAML annotated-types anyio certifi defusedxml h11 httpcore \
             httpx idna numpy psycopg psycopg-binary pydantic pydantic-core pyproj \
             shapely typing-extensions typing-inspection tzdata tzlocal
```
