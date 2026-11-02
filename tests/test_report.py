from __future__ import annotations

import datetime as dt
import json
import os
import uuid

from geoimport.config import ScheduleSettings
from geoimport.pipeline import RunResult, SourceResult
from geoimport.report import build_report, render_markdown, write_report
from geoimport.scheduler import build_scheduler, build_trigger, next_run

T0 = dt.datetime(2026, 10, 31, 2, 0, 0, 123456, tzinfo=dt.UTC)


def sample_run() -> RunResult:
    ok = SourceResult(
        source_id="wfs_a",
        source_type="wfs",
        title="A",
        location="https://example.org/wfs",
        layer="app:a",
        target="Geometrien (Demo) (source = 'A')",
        status="success",
        started_at=T0,
        duration_seconds=1.25,
        counts={
            "received": 5,
            "skipped": 1,
            "merged": 0,
            "new": 2,
            "changed": 1,
            "unchanged": 1,
            "deleted": 3,
            "records": 4,
            "target_removed": 6,
            "written": 4,
        },
        warnings=[
            {
                "code": "invalid_geometry",
                "message": "Objekte mit ungültiger Geometrie",
                "count": 2,
                "examples": ["k1: Self-intersection", "k2: Ring | offen"],
            }
        ],
    )
    failed = SourceResult(
        source_id="csv_b",
        source_type="csv",
        title="B",
        location="https://example.org/b.csv",
        layer=None,
        target="dashboard_data_de (source = 'B')",
        status="failed",
        started_at=T0,
        error={"type": "HttpError", "message": "HTTP 503 Service Unavailable"},
    )
    return RunResult(
        run_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        trigger="schedule",
        started_at=T0,
        finished_at=T0 + dt.timedelta(seconds=3.5),
        duration_seconds=3.5,
        target_srid=4326,
        sources=[ok, failed],
    )


def test_report_json_structure():
    report = build_report(sample_run())
    assert report["status"] == "partial"
    assert report["started_at"] == "2026-10-31T02:00:00.123456+00:00"
    assert report["summary"] == {
        "sources_total": 2,
        "sources_succeeded": 1,
        "sources_failed": 1,
        "new": 2,
        "changed": 1,
        "deleted": 3,
        "unchanged": 1,
        "warnings": 2,
    }
    failed = report["sources"][1]
    assert failed["status"] == "failed" and failed["error"]["type"] == "HttpError"
    json.dumps(report)  # vollständig serialisierbar


def test_markdown_summary():
    md = render_markdown(build_report(sample_run()), "Europe/Berlin")
    assert md.startswith("# Import-Bericht vom 31.10.2026 03:00:00")
    assert "(Auslöser: zeitgesteuert)" in md
    assert "**teilweise erfolgreich** – 1 von 2 Quellen erfolgreich" in md
    assert "| wfs_a | WFS | OK | 2 | 1 | 3 | 1 | 1 | 4 | 2 | 1,2 s |" in md
    assert "| csv_b | CSV | FEHLER | - | - | - | - | - | - | 0 | 0,0 s |" in md
    assert "Ziele: wfs_a → Geometrien (Demo) (source = 'A'); csv_b → dashboard_data_de" in md
    assert "- **csv_b** (CSV, https://example.org/b.csv): HttpError: HTTP 503" in md
    assert "bisherigen Zeilen in der Zieltabelle bleiben unverändert" in md
    assert "Ring \\| offen" in md  # Tabellen-/Listenzeichen maskiert
    assert "- Ziel-KBS: EPSG:4326" in md


def test_write_report_files(tmp_path):
    report = build_report(sample_run())
    json_path, md_path = write_report(report, tmp_path / "berichte")
    assert json_path.name == "import_20261031T020000Z_12345678.json"
    assert md_path.read_text("utf-8").startswith("# Import-Bericht")
    latest = json.loads((tmp_path / "berichte" / "latest.json").read_text("utf-8"))
    assert latest == report
    assert not list((tmp_path / "berichte").glob(".tmp-*"))
    mask = os.umask(0)
    os.umask(mask)
    assert json_path.stat().st_mode & 0o777 == 0o644 & ~mask  # nicht nur für den Eigentümer
    only_json = write_report(report, tmp_path / "nur_json", markdown=False, write_latest=False)
    assert only_json[1] is None
    assert [f.name for f in (tmp_path / "nur_json").iterdir()] == [only_json[0].name]


def test_run_status_rules():
    run = sample_run()
    run.sources = run.sources[:1]
    assert run.status == "success"
    run.sources = []
    assert run.status == "empty"
    run.error = {"type": "DatabaseError", "message": "x"}
    assert run.status == "failed"


def test_cron_trigger_next_run():
    tz = "Europe/Berlin"
    friday = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.UTC)
    nxt = next_run("30 2 * * 1-5", tz, friday)
    assert nxt.isoformat() == "2026-11-02T02:30:00+01:00"  # Montag, Winterzeit
    assert build_trigger("30 2 * * 1-5", tz).timezone.key == "Europe/Berlin"
    assert next_run("0 4 * * 0", tz, friday).isoformat() == "2026-11-01T04:00:00+01:00"
    every_other = next_run("0 4 * * */2", tz, friday)
    assert every_other.isoformat() == "2026-10-31T04:00:00+01:00"  # Sa (6)
    names = next_run("15 1 1 * mon-fri", tz, friday)
    assert names.isoformat() == "2026-12-01T01:15:00+01:00"
    monthly = next_run("0 4 2 * *", tz, friday)  # INKAR: monatlich am 2.
    assert monthly.isoformat() == "2026-11-02T04:00:00+01:00"


def test_scheduler_has_one_job_per_rhythm_and_runs_sequentially():
    settings = ScheduleSettings(cron="0 3 * * 1", timezone="Europe/Berlin")
    calls: list[list[str]] = []
    groups = {"0 3 * * 1": ["a", "b"], "0 4 2 * *": ["inkar"]}
    scheduler = build_scheduler(settings, groups, calls.append)
    jobs = sorted(scheduler.get_jobs(), key=lambda j: j.id)
    assert [j.args for j in jobs] == [(["a", "b"],), (["inkar"],)]
    assert all(j.max_instances == 1 and j.coalesce for j in jobs)
    assert scheduler._executors["default"]._pool._max_workers == 1  # nie zwei Läufe zugleich
