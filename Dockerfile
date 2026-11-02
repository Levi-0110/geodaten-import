# Geodaten-Import-Pipeline (WFS/CSV -> PostgreSQL/PostGIS)
# Build:  docker build -t geoimport:0.2.0 .
# Start:  siehe docker-compose.yml und docs/betrieb.md

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="geoimport" \
      org.opencontainers.image.description="Geodaten-Import-Pipeline: WFS/CSV nach PostgreSQL/PostGIS mit Änderungsprotokoll" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="0.2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEOIMPORT_CONFIG=/config/pipeline.yml \
    GEOIMPORT_REPORT_DIR=/data/reports

# Shapely, pyproj und psycopg bringen GEOS, PROJ und libpq als Wheels mit;
# zusätzliche Systempakete sind nicht nötig.
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl \
 && rm -rf /wheels \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin geoimport \
 && mkdir -p /config/sources /data/reports \
 && chown -R geoimport:geoimport /data

# Standardkonfiguration; im Betrieb wird ./config per Volume eingebunden
COPY config /config
USER geoimport
WORKDIR /data
VOLUME ["/data/reports"]

ENTRYPOINT ["geoimport"]
CMD ["schedule"]
