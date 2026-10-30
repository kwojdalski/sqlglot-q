"""Dates, times and intervals, between SQL's spelling and q's.

q writes a date 2024.01.15, a timestamp 2024.01.15D09:30:00.000000000, a time
09:30:00.000 and a fixed interval as a timespan, 0D00:05:00.000000000. SQL
writes the same values as typed strings (DATE '2024-01-15') and INTERVAL
'5' MINUTE. Months and years are not fixed lengths, so they have no timespan
and are refused.
"""

from __future__ import annotations

import re

from sqlglot import exp

NS = 1
UNITS = {
    "NANOSECOND": NS,
    "MICROSECOND": 1_000,
    "MILLISECOND": 1_000_000,
    "SECOND": 1_000_000_000,
    "MINUTE": 60 * 1_000_000_000,
    "HOUR": 3600 * 1_000_000_000,
    "DAY": 86400 * 1_000_000_000,
    "WEEK": 7 * 86400 * 1_000_000_000,
}
DAY = UNITS["DAY"]

# EXTRACT(part FROM x) as the q cast that takes the part.
PARTS = {
    "YEAR": "year",
    "MONTH": "mm",
    "DAY": "dd",
    "HOUR": "hh",
    "MINUTE": "uu",
    "SECOND": "ss",
}
CASTS = {q: sql for sql, q in PARTS.items()}

DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\Z")
TIMESTAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?\Z")
TIME = re.compile(r"(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?\Z")


def date_literal(text: str) -> str | None:
    m = DATE.match(text)
    return f"{m[1]}.{m[2]}.{m[3]}" if m else None


def timestamp_literal(text: str) -> str | None:
    m = TIMESTAMP.match(text)
    if not m:
        d = date_literal(text)
        return f"{d}D00:00:00.000000000" if d else None
    frac = (m[7] or "").ljust(9, "0")
    return f"{m[1]}.{m[2]}.{m[3]}D{m[4]}:{m[5]}:{m[6] or '00'}.{frac}"


def time_literal(text: str) -> str | None:
    m = TIME.match(text)
    if not m:
        return None
    frac = (m[4] or "")[:3].ljust(3, "0")
    return f"{m[1]}:{m[2]}:{m[3] or '00'}.{frac}"


def nanoseconds(node: exp.Interval) -> int | None:
    if not node.text("unit") and isinstance(node.this, exp.Literal):
        # INTERVAL '1 day 2 hours': the sum of its parts, each fixed.
        parts = re.findall(r"(-?\d+(?:\.\d+)?)\s*([A-Za-z]+)", node.this.name)
        if not parts or re.sub(r"(-?\d+(?:\.\d+)?)\s*([A-Za-z]+)", "", node.this.name).strip():
            return None
        total = 0
        for count, unit in parts:
            part = nanoseconds(exp.Interval(this=exp.Literal.string(count), unit=exp.var(unit)))
            if part is None:
                return None
            total += part
        return total
    unit = node.text("unit").upper()
    unit = unit[:-1] if unit.endswith("S") else unit
    if unit not in UNITS:
        return None
    try:
        count = float(node.this.name)
    except (AttributeError, ValueError):
        return None
    ns = count * UNITS[unit]
    return int(ns) if ns == int(ns) else None


def months_of(node: exp.Interval) -> int | None:
    """A whole number of months or years, as months: INTERVAL '3 months',
    INTERVAL 1 YEAR, '1 year 6 months'."""
    text = node.this.name if isinstance(node.this, exp.Literal) else None
    if text is None:
        return None
    unit = node.text("unit")
    spec = f"{text} {unit}" if unit else text
    parts = re.findall(r"(\d+)\s*([A-Za-z]+)", spec)
    if not parts or re.sub(r"(\d+)\s*([A-Za-z]+)", "", spec).strip():
        return None
    total = 0
    for count, name in parts:
        name = name.upper().rstrip("S")
        if name not in ("MONTH", "YEAR"):
            return None
        total += int(count) * (12 if name == "YEAR" else 1)
    return total or None


def span_text(ns: int) -> str:
    days, rest = divmod(ns, DAY)
    hours, rest = divmod(rest, UNITS["HOUR"])
    minutes, rest = divmod(rest, UNITS["MINUTE"])
    seconds, nanos = divmod(rest, UNITS["SECOND"])
    return f"{days}D{hours:02}:{minutes:02}:{seconds:02}.{nanos:09}"


def timespan(node: exp.Interval) -> str | None:
    ns = nanoseconds(node)
    return None if ns is None else span_text(ns)


# Reading q back: a timespan as the largest SQL unit it is a whole number of.
SPAN = re.compile(r"(\d+)D(?:(\d{2})(?::(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?)?\Z")


def span_nanoseconds(text: str) -> int | None:
    m = SPAN.match(text)
    if not m:
        return None
    frac = int((m[5] or "").ljust(9, "0") or 0)
    return (
        int(m[1]) * DAY
        + int(m[2] or 0) * UNITS["HOUR"]
        + int(m[3] or 0) * UNITS["MINUTE"]
        + int(m[4] or 0) * UNITS["SECOND"]
        + frac
    )


def interval(ns: int) -> exp.Interval:
    for unit in ("DAY", "HOUR", "MINUTE", "SECOND", "MILLISECOND", "MICROSECOND"):
        if ns % UNITS[unit] == 0:
            count = ns // UNITS[unit]
            return exp.Interval(this=exp.Literal.string(str(count)), unit=exp.var(unit))
    return exp.Interval(this=exp.Literal.string(str(ns)), unit=exp.var("NANOSECOND"))
