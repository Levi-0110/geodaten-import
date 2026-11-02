"""Zeitgesteuerte Ausführung über Cron-Ausdrücke (APScheduler).

Jede Quelle kann einen eigenen Rhythmus haben (``schedule`` in der
Quelldatei, z. B. monatlich für INKAR und das Amt für Statistik); ohne Angabe
gilt ``schedule.cron`` aus ``pipeline.yml``. Quellen mit gleichem Rhythmus
laufen gemeinsam. Die Läufe werden nacheinander ausgeführt (ein Arbeitsthread),
damit sich zwei Läufe nie überschneiden.
"""

from __future__ import annotations

import datetime as dt
import logging
import signal
from collections.abc import Callable
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from geoimport.config import ScheduleSettings
from geoimport.cron import crontab_trigger

log = logging.getLogger(__name__)


def build_trigger(cron: str, timezone: str) -> CronTrigger:
    return crontab_trigger(cron, timezone)


def next_run(cron: str, timezone: str, now: dt.datetime | None = None) -> dt.datetime | None:
    now = now or dt.datetime.now(ZoneInfo(timezone))
    return build_trigger(cron, timezone).get_next_fire_time(None, now)


def build_scheduler(
    settings: ScheduleSettings,
    groups: dict[str, list[str]],
    job: Callable[[list[str]], object],
) -> BlockingScheduler:
    scheduler = BlockingScheduler(
        timezone=settings.timezone, executors={"default": ThreadPoolExecutor(1)}
    )
    for i, (cron, ids) in enumerate(sorted(groups.items())):
        scheduler.add_job(
            job,
            args=[list(ids)],
            trigger=build_trigger(cron, settings.timezone),
            id=f"geoimport-{i}",
            name=f"Geodaten-Import {', '.join(ids)}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
    return scheduler


def run_scheduler(
    settings: ScheduleSettings,
    groups: dict[str, list[str]],
    job: Callable[[list[str]], object],
) -> None:
    """Startet den Scheduler im Vordergrund, bis SIGTERM oder SIGINT eintrifft."""
    scheduler = build_scheduler(settings, groups, job)

    def _stop(signum: int, _frame: object) -> None:
        log.info("Signal %s empfangen, Scheduler wird beendet", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if settings.run_on_start:
        log.info("Sofortiger Lauf aller Quellen vor dem ersten Termin (run_on_start)")
        job(sorted({i for ids in groups.values() for i in ids}))
    for cron, ids in sorted(groups.items()):
        log.info(
            "Takt %r (%s): %s – nächster Lauf %s",
            cron,
            settings.timezone,
            ", ".join(ids),
            next_run(cron, settings.timezone),
        )
    scheduler.start()
