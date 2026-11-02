"""Lokaler Testserver für aufgezeichnete WFS-Antworten und CSV-Dateien.

Die Antworten liegen unter ``tests/fixtures``. Der Server wählt sie anhand der
Anfrageparameter aus (nicht anhand der exakten URL):

* ``/wfs/<dienst>?REQUEST=GetCapabilities`` -> ``wfs/<dienst>/capabilities_2_0_0.xml``
  (bzw. ``capabilities_1_1_0.xml`` bei ``VERSION=1.1.0``)
* ``REQUEST=DescribeFeatureType&TYPENAMES=app:otl`` -> ``describefeaturetype_otl.xml``
* ``REQUEST=GetFeature&TYPENAMES=app:otl&STARTINDEX=3&COUNT=3`` ->
  ``getfeature_otl_start3_count3.xml`` (``.json`` bei GeoJSON-Ausgabeformat)
* ``REQUEST=GetFeature&VERSION=1.1.0&MAXFEATURES=2`` ->
  ``getfeature_otl_v110_maxfeatures2.xml``
* ``/csv/<datei>`` -> ``csv/<datei>``, ``/files/<datei>`` -> ``files/<datei>`` (z. B. ZIP)
* ``/status/<code>`` -> leere Antwort mit diesem HTTP-Status
* ``HEAD`` wird wie ``GET`` beantwortet, nur ohne Inhalt

Über :attr:`FixtureResponder.overrides` lassen sich einzelne Dateien für einen
Testlauf durch geänderte Inhalte ersetzen (z. B. für den zweiten E2E-Lauf).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

EXCEPTION = """<?xml version="1.0" encoding="UTF-8"?>
<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0">
  <ows:Exception exceptionCode="InvalidParameterValue">
    <ows:ExceptionText>{text}</ows:ExceptionText>
  </ows:Exception>
</ows:ExceptionReport>
"""


class FixtureResponder:
    def __init__(self, root: Path):
        self.root = root
        self.overrides: dict[str, bytes] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._lock = threading.Lock()

    def read(self, rel: str) -> bytes:
        if rel in self.overrides:
            return self.overrides[rel]
        return (self.root / rel).read_bytes()

    def exists(self, rel: str) -> bool:
        return rel in self.overrides or (self.root / rel).is_file()

    def requests_for(self, path: str) -> list[dict[str, str]]:
        return [q for p, q in self.requests if p == path]

    def respond(self, path: str, query: dict[str, str]) -> tuple[int, str, bytes]:
        with self._lock:
            self.requests.append((path, dict(query)))
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] == "wfs":
            return self._wfs(parts[1], query)
        if len(parts) == 2 and parts[0] in ("csv", "files"):
            rel = f"{parts[0]}/{parts[1]}"
            if self.exists(rel):
                ctype = "application/zip" if rel.endswith(".zip") else "text/csv; charset=utf-8"
                return 200, ctype, self.read(rel)
        if len(parts) == 2 and parts[0] == "status" and parts[1].isdigit():
            return int(parts[1]), "text/plain; charset=utf-8", b"Testfehler"
        return 404, "text/plain; charset=utf-8", b"nicht gefunden"

    def _wfs(self, service: str, query: dict[str, str]) -> tuple[int, str, bytes]:
        q = {k.upper(): v for k, v in query.items()}
        request = q.get("REQUEST", "").lower()
        type_local = (q.get("TYPENAMES") or q.get("TYPENAME") or "").split(":")[-1]
        name = None
        if request == "getcapabilities":
            v110 = q.get("VERSION") == "1.1.0" or q.get("ACCEPTVERSIONS", "").startswith("1.1")
            name = "capabilities_1_1_0.xml" if v110 else "capabilities_2_0_0.xml"
        elif request == "describefeaturetype":
            name = f"describefeaturetype_{type_local}.xml"
        elif request == "getfeature":
            if q.get("VERSION") == "1.1.0":
                name = f"getfeature_{type_local}_v110_maxfeatures{q.get('MAXFEATURES')}.xml"
            else:
                ext = "json" if "json" in q.get("OUTPUTFORMAT", "").lower() else "xml"
                start = q.get("STARTINDEX", "0")
                name = f"getfeature_{type_local}_start{start}_count{q.get('COUNT')}.{ext}"
        rel = f"wfs/{service}/{name}"
        if name is None or not self.exists(rel):
            text = f"Keine aufgezeichnete Antwort fuer {rel}"
            return 400, "application/xml", EXCEPTION.format(text=text).encode()
        if rel.endswith(".json"):
            ctype = "application/geo+json"
        elif "getfeature" in rel:
            ctype = "application/gml+xml; version=3.2"
        else:
            ctype = "text/xml; charset=UTF-8"
        return 200, ctype, self.read(rel)

    def transport(self) -> httpx.MockTransport:
        """httpx-Transport ohne Netzwerk für Unit-Tests."""

        def handler(request: httpx.Request) -> httpx.Response:
            status, ctype, body = self.respond(request.url.path, dict(request.url.params))
            if request.method == "HEAD":
                body = b""
            return httpx.Response(status, headers={"Content-Type": ctype}, content=body)

        return httpx.MockTransport(handler)


class FixtureServer:
    """Echter HTTP-Server (Thread) für den End-to-End-Test."""

    def __init__(self, responder: FixtureResponder):
        self.responder = responder
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, with_body: bool) -> None:
                parts = urlsplit(self.path)
                query = dict(parse_qsl(parts.query, keep_blank_values=True))
                status, ctype, body = outer.responder.respond(parts.path, query)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if with_body:
                    self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._answer(True)

            def do_HEAD(self) -> None:  # noqa: N802
                self._answer(False)

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def start(self) -> FixtureServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
