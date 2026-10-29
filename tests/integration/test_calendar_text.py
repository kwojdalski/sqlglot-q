"""Calendar parts and truncations, durations, text functions, EXISTS, IN
over mixed numbers, and the window functions q has no word for. Each query
runs in DuckDB as SQL and in q as its translation."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same

T = dt.datetime
DATA = {
    "t": {
        "a": [1, 2, 3, 4, 5, 6],
        "f": [True, False, True, True, False, True],
        "x": [1.5, -2.25, 0.0, 9.0, -0.5, 4.0],
        "n": [-3, 7, 0, 12, 7, -8],
        "d": [
            dt.date(2024, 1, 15),
            dt.date(2024, 2, 29),
            dt.date(2023, 12, 31),
            dt.date(2024, 3, 1),
            dt.date(2024, 1, 1),
            dt.date(2024, 5, 26),
        ],
        "ts": [
            T(2024, 1, 15, 9, 30),
            T(2024, 1, 15, 23, 59, 59),
            T(2024, 1, 16, 0, 0, 1),
            T(2024, 2, 1, 12),
            T(2023, 12, 31, 23, 0),
            T(2024, 1, 15, 9, 30, 1, 250000),
        ],
        "b": ["x", "yy", "xyz", "zzz", "yy", "w"],
    }
}

QUERIES = [
    "SELECT a FROM t WHERE f",
    "SELECT a FROM t WHERE NOT f",
    "SELECT COUNT_IF(f) AS c, SUM(CAST(f AS INT)) AS s FROM t",
    "SELECT a FROM t WHERE a IN (1, 2.5, 3)",
    "SELECT a FROM t WHERE x IN (1.5, 4, 9)",
    "SELECT a, d - DATE '2024-01-01' AS days FROM t",
    "SELECT a, ts - TIMESTAMP '2024-01-15 00:00:00' AS dur FROM t",
    "SELECT a, CAST(ts AS DATE) AS d2, CAST(d AS TIMESTAMP) AS t2 FROM t",
    "SELECT a FROM t WHERE ts >= DATE '2024-01-16'",
    "SELECT a, EXTRACT(dow FROM d) AS w, DAYOFWEEK(d) AS w2 FROM t",
    "SELECT a, EXTRACT(doy FROM d) AS y, EXTRACT(quarter FROM d) AS q FROM t",
    "SELECT a, EXTRACT(millisecond FROM ts) AS ms FROM t",
    "SELECT a, DATE_DIFF('day', d, DATE '2024-03-01') AS dd FROM t",
    "SELECT a, DATE_DIFF('day', ts, TIMESTAMP '2024-01-17 00:00:00') AS dd FROM t",
    "SELECT a, DATE_TRUNC('month', d) AS m, DATE_TRUNC('year', d) AS y FROM t",
    "SELECT a, DATE_TRUNC('quarter', ts) AS q FROM t",
    "SELECT a, REPLACE(b, 'y', 'Y') AS r FROM t",
    "SELECT a, SUBSTRING(b, 2, 2) AS s, SUBSTRING(b, 2) AS s2 FROM t",
    "SELECT a, REVERSE(b) AS r FROM t",
    "SELECT a FROM t WHERE EXISTS (SELECT 1 FROM t WHERE a > 5)",
    "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM t WHERE a > 50)",
    "SELECT a, FIRST_VALUE(n) OVER (ORDER BY a) AS fv FROM t",
    "SELECT a, FIRST_VALUE(n) OVER (PARTITION BY f ORDER BY a DESC) AS fv FROM t",
    "SELECT a, LAST_VALUE(n) OVER (PARTITION BY f ORDER BY a "
    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS lv FROM t",
    "SELECT a, NTILE(2) OVER (ORDER BY a) AS t2, NTILE(4) OVER (ORDER BY a) AS t4 FROM t",
    "SELECT a, PERCENT_RANK() OVER (ORDER BY n) AS p FROM t",
    "SELECT a, CUME_DIST() OVER (ORDER BY n) AS c FROM t",
    "SELECT a, COUNT(*) OVER () AS c, ROW_NUMBER() OVER () AS r FROM t",
    # Months on a timestamp keep its time of day.
    "SELECT a, ts + INTERVAL 1 MONTH AS b, ts - INTERVAL 1 YEAR AS c FROM t",
    # A date is its own bucket: xbar of a date by a timespan is no day.
    "SELECT a, TIME_BUCKET(INTERVAL '1 day', d) AS b, TIME_BUCKET(INTERVAL '1 hour', ts) AS c "
    "FROM t",
    # Parts and differences of timestamps.
    "SELECT a, HOUR(ts) AS h, MINUTE(ts) AS m, SECOND(ts) AS s FROM t",
    "SELECT a, EXTRACT(epoch FROM ts) AS e, EPOCH(d) AS g FROM t",
    "SELECT a, EXTRACT(week FROM d) AS w FROM t",
    "SELECT a, DATE_DIFF('hour', ts, TIMESTAMP '2024-01-16 00:00:00') AS h FROM t",
    "SELECT a, DATE_DIFF('minute', TIMESTAMP '2024-01-15 09:00:00', ts) AS m, "
    "DATE_DIFF('second', TIMESTAMP '2024-01-15 09:00:00', ts) AS s FROM t",
    "SELECT a, DATE_DIFF('quarter', d, DATE '2025-01-01') AS q FROM t",
    # Text as SQL writes a timestamp and a whole float.
    "SELECT a, CAST(x AS VARCHAR) AS z, CAST(ts AS VARCHAR) AS w FROM t",
    # AND of a boolean column as a value.
    "SELECT a, f AND a > 2 AS z, f OR x < 0 AS w FROM t",
]


@pytest.fixture(scope="module")
def answers():
    qs = [sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql in QUERIES]
    return dict(zip(QUERIES, zip(qs, run_q(DATA, qs), strict=True), strict=True))


@pytest.mark.parametrize("sql", QUERIES, ids=QUERIES)
def test_q_returns_what_duckdb_returns(answers, sql):
    q, got = answers[sql]
    expected = run_duckdb(DATA, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, False), f"{q}\nq:      {got}\nduckdb: {expected}"


REFUSED = [
    "SELECT DATE_DIFF('week', d, DATE '2024-03-01') FROM t",
    "SELECT LAST_VALUE(n) OVER (ORDER BY a) FROM t",
    "SELECT REPLACE(b, b, 'x') FROM t",
    "SELECT NTILE(2) OVER () FROM t",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
