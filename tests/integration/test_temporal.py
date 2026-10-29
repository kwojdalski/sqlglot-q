"""Dates, timestamps, time buckets and as-of joins: each query runs in DuckDB
as SQL and in q as its translation, on the same tick data."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same

T = dt.datetime
TICKS = {
    "trades": {
        "sym": ["a", "b", "a", "b", "a", "a"],
        "time": [
            T(2024, 1, 15, 9, 30, 0),
            T(2024, 1, 15, 9, 31, 30),
            T(2024, 1, 15, 9, 37, 12, 500000),
            T(2024, 1, 15, 10, 2, 0),
            T(2024, 1, 16, 9, 30, 0),
            T(2024, 1, 16, 11, 59, 59),
        ],
        "day": [
            dt.date(2024, 1, 15),
            dt.date(2024, 1, 15),
            dt.date(2024, 1, 15),
            dt.date(2024, 1, 15),
            dt.date(2024, 1, 16),
            dt.date(2024, 1, 16),
        ],
        "px": [1.5, 20.0, 1.7, 21.0, 1.6, 1.9],
        "qty": [100, 5, 250, 7, 300, 40],
    },
    "quotes": {
        "sym": ["a", "a", "b", "a", "b", "a"],
        "time": [
            T(2024, 1, 15, 9, 29, 0),
            T(2024, 1, 15, 9, 35, 0),
            T(2024, 1, 15, 9, 31, 0),
            T(2024, 1, 15, 9, 37, 0),
            T(2024, 1, 15, 10, 5, 0),
            T(2024, 1, 16, 9, 0, 0),
        ],
        "bid": [1.4, 1.6, 19.5, 1.65, 20.5, 1.55],
    },
}

QUERIES = [
    ("SELECT sym, px FROM trades WHERE day = DATE '2024-01-15'", False),
    ("SELECT sym FROM trades WHERE time > TIMESTAMP '2024-01-15 09:35:00'", False),
    ("SELECT sym FROM trades WHERE time >= TIMESTAMP '2024-01-15 09:37:12.5'", False),
    ("SELECT sym FROM trades WHERE day BETWEEN DATE '2024-01-14' AND DATE '2024-01-15'", False),
    ("SELECT DATE '2024-01-15' AS d, sym FROM trades", False),
    ("SELECT CAST(time AS DATE) AS d, px FROM trades", False),
    (
        "SELECT EXTRACT(year FROM time) AS y, EXTRACT(month FROM time) AS m, "
        "EXTRACT(day FROM time) AS d FROM trades",
        False,
    ),
    (
        "SELECT EXTRACT(hour FROM time) AS h, EXTRACT(minute FROM time) AS u, "
        "EXTRACT(second FROM time) AS s FROM trades",
        False,
    ),
    ("SELECT YEAR(day) AS y, MONTH(day) AS m, DAY(day) AS d FROM trades", False),
    ("SELECT time + INTERVAL 1 HOUR AS later FROM trades", False),
    ("SELECT time + INTERVAL '90' SECOND AS later FROM trades", False),
    ("SELECT day + INTERVAL 1 DAY AS next FROM trades", False),
    ("SELECT TIME_BUCKET(INTERVAL '5 minutes', time) AS b, sym FROM trades", False),
    (
        "SELECT TIME_BUCKET(INTERVAL '1 hour', time) AS b, COUNT(*) AS n, SUM(qty) AS v "
        "FROM trades GROUP BY b",
        False,
    ),
    ("SELECT DATE_TRUNC('day', time) AS d, px FROM trades", False),
    ("SELECT DATE_TRUNC('minute', time) AS m FROM trades", False),
    ("SELECT FLOOR(qty / 100) * 100 AS lot, COUNT(*) AS n FROM trades GROUP BY lot", False),
    ("SELECT FLOOR(px / 0.5) * 0.5 AS band FROM trades", False),
    ("SELECT sym, MAX(time) AS last FROM trades GROUP BY sym", False),
    ("SELECT sym, px, bid FROM trades ASOF LEFT JOIN quotes USING (sym, time)", False),
    (
        "SELECT trades.sym, px, bid FROM trades ASOF LEFT JOIN quotes "
        "ON trades.sym = quotes.sym AND trades.time >= quotes.time",
        False,
    ),
    (
        "SELECT sym, AVG(px - bid) AS spread FROM trades ASOF LEFT JOIN quotes "
        "USING (sym, time) GROUP BY sym",
        False,
    ),
    # The README's example.
    (
        "SELECT sym, TIME_BUCKET(INTERVAL '5 minutes', time) AS bar, AVG(px - bid) AS spread "
        "FROM trades ASOF LEFT JOIN quotes USING (sym, time) "
        "WHERE time >= TIMESTAMP '2024-01-15 09:30:00' GROUP BY sym, bar",
        False,
    ),
]


@pytest.fixture(scope="module")
def answers():
    qs = [sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql, _ in QUERIES]
    return dict(zip([s for s, _ in QUERIES], zip(qs, run_q(TICKS, qs), strict=True), strict=True))


@pytest.mark.parametrize(("sql", "ordered"), QUERIES, ids=[sql for sql, _ in QUERIES])
def test_q_returns_what_duckdb_returns(answers, sql, ordered):
    q, got = answers[sql]
    expected = run_duckdb(TICKS, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"


REFUSED = [
    "SELECT d + INTERVAL '1.5' MONTH FROM t",
    "SELECT TIME_BUCKET(INTERVAL '7 minutes', ts) FROM t",
    "SELECT * FROM t ASOF JOIN q USING (sym, time)",
    "SELECT DATE_TRUNC('decade', ts) FROM t",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
