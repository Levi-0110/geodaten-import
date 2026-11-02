"""Import-Bericht je Lauf: JSON (maschinenlesbar) und optional Markdown (Zusammenfassung).

Der Bericht weist je Quelle neu hinzugefügte, geänderte und gelöschte Datensätze
aus (LB 4.5), dazu Laufzeit, Ziel, Warnungen und Fehler. Dieselben Zahlen stehen
je Datensatz im Änderungsprotokoll ``<meta_schema>.change_log`` (Lauf-ID).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from geoimport import __version__
from geoimport.pipeline import RunResult

REPORT_VERSION = 2
STATUS_TEXT = {
    "success": "erfolgreich",
    "partial": "teilweise erfolgreich",
    "failed": "fehlgeschlagen",
    "empty": "keine Quellen ausgewählt",
}
COUNT_KEYS = ("new", "changed", "deleted", "unchanged")
TRIGGER_TEXT = {"manual": "manuell", "schedule": "zeitgesteuert"}


def _iso(value: dt.datetime | None) -> str | None:
    """ISO 8601 mit voller Genauigkeit; entspricht changed_at im Änderungsprotokoll."""
    return value.isoformat() if value else None


def build_report(run: RunResult) -> dict[str, Any]:
    sources = []
    totals = dict.fromkeys(COUNT_KEYS, 0)
    warnings_total = 0
    for s in run.sources:
        for key in COUNT_KEYS:
            totals[key] += int(s.counts.get(key, 0))
        warnings_total += sum(w.get("count", 1) for w in s.warnings)
        sources.append(
            {
                "id": s.source_id,
                "type": s.source_type,
                "title": s.title,
                "location": s.location,
                "layer": s.layer,
                "target": s.target,
                "status": s.status,
                "started_at": _iso(s.started_at),
                "duration_seconds": s.duration_seconds,
                "counts": s.counts,
                "warnings": s.warnings,
                "error": s.error,
                "details": s.details,
            }
        )
    succeeded = sum(1 for s in run.sources if s.status == "success")
    return {
        "report_version": REPORT_VERSION,
        "generator": f"geoimport {__version__}",
        "run_id": str(run.run_id),
        "trigger": run.trigger,
        "status": run.status,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_seconds": run.duration_seconds,
        "target_srid": run.target_srid,
        "error": run.error,
        "summary": {
            "sources_total": len(run.sources),
            "sources_succeeded": succeeded,
            "sources_failed": len(run.sources) - succeeded,
            **totals,
            "warnings": warnings_total,
        },
        "sources": sources,
    }


def _local(value: str | None, tz: ZoneInfo) -> str:
    if not value:
        return "-"
    return dt.datetime.fromisoformat(value).astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")


def _seconds(value: float) -> str:
    return f"{value:.1f} s".replace(".", ",")


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any], timezone: str = "Europe/Berlin") -> str:
    tz = ZoneInfo(timezone)
    s = report["summary"]
    lines = [
        f"# Import-Bericht vom {_local(report['started_at'], tz)}",
        "",
        f"- Lauf-ID: `{report['run_id']}` "
        f"(Auslöser: {TRIGGER_TEXT.get(report['trigger'], report['trigger'])})",
        f"- Status: **{STATUS_TEXT.get(report['status'], report['status'])}** – "
        f"{s['sources_succeeded']} von {s['sources_total']} Quellen erfolgreich",
        f"- Laufzeit: {_seconds(report['duration_seconds'])} "
        f"(Ende {_local(report['finished_at'], tz)}, Zeitzone {timezone})",
        f"- Summe: {s['new']} neu, {s['changed']} geändert, {s['deleted']} gelöscht, "
        f"{s['unchanged']} unverändert, {s['warnings']} Warnungen",
        f"- Ziel-KBS: EPSG:{report['target_srid']}",
        "",
    ]
    if report.get("error"):
        lines += [
            "## Lauf abgebrochen",
            "",
            f"{report['error']['type']}: {_cell(report['error']['message'])}",
            "",
        ]
    if report["sources"]:
        lines += [
            "## Quellen",
            "",
            "| Quelle | Typ | Status | neu | geändert | gelöscht | unverändert | "
            "übersprungen | geschrieben | Warnungen | Dauer |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for src in report["sources"]:
            c = src["counts"]
            ok = src["status"] == "success"
            lines.append(
                "| {id} | {typ} | {status} | {new} | {changed} | {deleted} | {unchanged} | "
                "{skipped} | {written} | {warn} | {dur} |".format(
                    id=_cell(src["id"]),
                    typ=src["type"].upper(),
                    status="OK" if ok else "FEHLER",
                    new=c.get("new", "-"),
                    changed=c.get("changed", "-"),
                    deleted=c.get("deleted", "-"),
                    unchanged=c.get("unchanged", "-"),
                    skipped=c.get("skipped", "-"),
                    written=c.get("written", "-"),
                    warn=sum(w.get("count", 1) for w in src["warnings"]),
                    dur=_seconds(src["duration_seconds"]),
                )
            )
        lines.append("")
        lines += [
            "Ziele: "
            + "; ".join(
                f"{_cell(s['id'])} → {_cell(s.get('target') or '-')}" for s in report["sources"]
            ),
            "",
        ]
    failed = [src for src in report["sources"] if src["status"] != "success"]
    if failed:
        lines += [
            "## Fehlgeschlagene Quellen",
            "",
            "Änderungen dieser Quellen wurden nicht übernommen (Transaktion zurückgerollt); "
            "ihre bisherigen Zeilen in der Zieltabelle bleiben unverändert.",
            "",
        ]
        for src in failed:
            err = src.get("error") or {}
            lines.append(
                f"- **{src['id']}** ({src['type'].upper()}, {src.get('location') or '-'}): "
                f"{err.get('type', 'Fehler')}: {_cell(err.get('message', '-'))}"
            )
        lines.append("")
    warned = [src for src in report["sources"] if src["warnings"]]
    if warned:
        lines += ["## Warnungen", ""]
        for src in warned:
            for w in src["warnings"]:
                examples = ", ".join(w.get("examples") or [])
                suffix = f" – z. B. {_cell(examples)}" if examples else ""
                lines.append(f"- {src['id']}: {w['message']} ({w['count']}×){suffix}")
        lines.append("")
    return "\n".join(lines)


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


_UMASK = _current_umask()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644 & ~_UMASK)  # mkstemp legt 0600 an; Berichte sind nicht geheim
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_report(
    report: dict[str, Any],
    directory: Path | str,
    timezone: str = "Europe/Berlin",
    write_latest: bool = True,
    markdown: bool = True,
) -> tuple[Path, Path | None]:
    """Schreibt ``import_<zeit>_<id>.json`` und optional ``.md``; optional ``latest.*``."""
    directory = Path(directory)
    started = dt.datetime.fromisoformat(report["started_at"]).astimezone(dt.UTC)
    stem = f"import_{started:%Y%m%dT%H%M%SZ}_{report['run_id'][:8]}"
    json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    json_path = directory / f"{stem}.json"
    _atomic_write(json_path, json_text)
    if write_latest:
        _atomic_write(directory / "latest.json", json_text)
    md_path = None
    if markdown:
        md_text = render_markdown(report, timezone) + "\n"
        md_path = directory / f"{stem}.md"
        _atomic_write(md_path, md_text)
        if write_latest:
            _atomic_write(directory / "latest.md", md_text)
    return json_path, md_path
