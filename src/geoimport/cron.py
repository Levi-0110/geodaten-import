"""Cron-Ausdrücke in der üblichen crontab-Schreibweise.

APScheduler 3 zählt Wochentage ab Montag (0 = Montag), crontab ab Sonntag
(0 oder 7 = Sonntag, 1 = Montag). Damit ``30 2 * * 1-5`` wie gewohnt
"montags bis freitags" bedeutet, werden numerische Wochentage vor der Übergabe
in Namen (``mon``, ``tue`` ...) übersetzt.
"""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _convert_day_of_week(field: str) -> str:
    names: list[str] = []
    for item in field.split(","):
        base, _, step_text = item.partition("/")
        step = int(step_text) if step_text.isdigit() else 1
        if base == "*" and not step_text:
            return field
        if base == "*":
            start, end = 0, 6
        elif "-" in base and all(p.isdigit() for p in base.split("-", 1)):
            start, end = (int(p) for p in base.split("-", 1))
        elif base.isdigit():
            start = end = int(base)
            if step_text:
                end = 6
        else:  # Namen wie mon-fri werden von APScheduler direkt verstanden
            names.append(item)
            continue
        if not (0 <= start <= 7 and 0 <= end <= 7 and start <= end and step > 0):
            raise ValueError(f"ungültiger Wochentag {item!r}")
        names.extend(_DOW_NAMES[d] for d in range(start, end + 1, step))
    return ",".join(dict.fromkeys(names))


def crontab_trigger(expression: str, timezone: str) -> CronTrigger:
    """Erzeugt einen CronTrigger aus ``Minute Stunde Tag Monat Wochentag``."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("erwartet fünf Felder: Minute Stunde Tag Monat Wochentag")
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_convert_day_of_week(day_of_week),
        timezone=timezone,
    )
