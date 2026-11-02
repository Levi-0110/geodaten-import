"""HTTP-Zugriff mit Zeitlimit und Wiederholungen bei vorübergehenden Fehlern."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from geoimport import __version__
from geoimport.config import HttpSettings

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
_SECRET_PARAMS = {"apikey", "api_key", "key", "token", "access_token", "password", "passwd"}


class HttpError(Exception):
    """Eine Quelle war per HTTP nicht (fehlerfrei) erreichbar."""

    def __init__(self, message: str, status_code: int | None = None, body: bytes | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def redact_url(url: str) -> str:
    """Entfernt Werte von Parametern, die wie Zugangsdaten aussehen."""
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]  # Benutzer:Passwort@ entfernen
    if not parts.query and netloc == parts.netloc:
        return url
    query = [
        (k, "***" if k.lower() in _SECRET_PARAMS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    text = urlencode(query) if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, text, parts.fragment))


def _short(url: str, limit: int = 200) -> str:
    url = redact_url(url)
    return url if len(url) <= limit else url[: limit - 3] + "..."


class HttpClient:
    def __init__(self, settings: HttpSettings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._client = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds),
            headers={"User-Agent": settings.user_agent or f"geoimport/{__version__}"},
            follow_redirects=True,
            verify=settings.verify_tls,
            transport=transport,
        )

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        attempts = self.settings.retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = HttpError(f"Verbindungsfehler bei {_short(url)}: {exc}")
            else:
                if response.status_code < 400:
                    return response
                message = (
                    f"HTTP {response.status_code} {response.reason_phrase} "
                    f"von {_short(str(response.url))}"
                )
                last_error = HttpError(message, response.status_code, response.content[:20000])
                if response.status_code not in RETRY_STATUS:
                    raise last_error
            if attempt < attempts:
                delay = self.settings.backoff_seconds * (2 ** (attempt - 1))
                log.warning(
                    "%s – neuer Versuch in %.1f s (%d/%d)", last_error, delay, attempt, attempts - 1
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def download(self, url: str, dest: Path) -> int:
        """Lädt ``url`` in Teilen nach ``dest`` (geringer Speicherbedarf). Liefert die Bytes."""
        attempts = self.settings.retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with self._client.stream("GET", url) as response:
                    if response.status_code < 400:
                        size = 0
                        with dest.open("wb") as fh:
                            for chunk in response.iter_bytes(1 << 20):
                                fh.write(chunk)
                                size += len(chunk)
                        return size
                    body = response.read()[:20000]
                    message = (
                        f"HTTP {response.status_code} {response.reason_phrase} "
                        f"von {_short(str(response.url))}"
                    )
                    last_error = HttpError(message, response.status_code, body)
                    if response.status_code not in RETRY_STATUS:
                        raise last_error
            except httpx.TransportError as exc:
                last_error = HttpError(f"Verbindungsfehler bei {_short(url)}: {exc}")
            if attempt < attempts:
                delay = self.settings.backoff_seconds * (2 ** (attempt - 1))
                log.warning(
                    "%s – neuer Versuch in %.1f s (%d/%d)", last_error, delay, attempt, attempts - 1
                )
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def exists(self, url: str) -> bool:
        """True, wenn ``url`` abrufbar ist (HEAD, bei Bedarf GET ohne Inhalt)."""
        try:
            response = self._client.head(url)
            if response.status_code in (405, 501):
                with self._client.stream("GET", url) as streamed:
                    return streamed.status_code < 400
            return response.status_code < 400
        except httpx.TransportError as exc:
            raise HttpError(f"Verbindungsfehler bei {_short(url)}: {exc}") from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
