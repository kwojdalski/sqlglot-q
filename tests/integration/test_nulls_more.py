"""More NULL ground: windows over nulls, null dates and timestamps, set
operations, grouping and ordering on a column with nulls, IS [NOT] DISTINCT
FROM (q's own = and <>, which are null-safe) and STRING_AGG. Each query runs
in DuckDB as SQL and in q as its translation."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same

T = dt.datetime
DATA = {
    "t": {
        "g": ["a", "b", "a", "a", "b", None],
        "o": [1, 2, None, 2, 1, 3],
        "x": [10.0, None, 30.0, 40.0, None, 60.0],
        "n": [1, 2, 3, None, 5, 6],
        "d": [
            dt.date(2024, 1, 15),
            None,
            dt.date(2024, 2, 1),
            dt.date(2024, 1, 15),
            dt.date(2023, 12, 31),
            dt.date(2024, 3, 1),
        ],
        "ts": [
            T(2024, 1, 15, 9, 30),
            T(2024, 1, 15, 9, 31),
            None,
            T(2024, 1, 15, 10, 0),
            T(2024, 1, 16, 9, 0),
            T(2024, 1, 15, 9, 59, 59),
        ],
    },
    "u": {"g": ["a", "c", None], "y": [1, 2, 3]},
}

QUERIES = [
    "SELECT n, SIGN(n) AS s, SIGN(x - 30) AS t FROM t",
    "SELECT g, SUM(x) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, o, ROW_NUMBER() OVER (PARTITION BY g ORDER BY o) AS r FROM t",
    "SELECT g, o, RANK() OVER (ORDER BY o) AS r FROM t",
    "SELECT g, o, SUM(n) OVER (ORDER BY o) AS s FROM t",
    "SELECT g, n, LAG(n) OVER (ORDER BY n) AS p FROM t",
    "SELECT g, MIN(x) OVER (PARTITION BY g) AS m, MAX(x) OVER (PARTITION BY g) AS h FROM t",
    "SELECT g, COUNT(x) OVER (PARTITION BY g) AS c, AVG(x) OVER (PARTITION BY g) AS a FROM t",
    "SELECT n FROM t WHERE d < DATE '2024-02-01'",
    "SELECT n FROM t WHERE d <> DATE '2024-01-15'",
    "SELECT n FROM t WHERE ts BETWEEN TIMESTAMP '2024-01-15 09:30:00' "
    "AND TIMESTAMP '2024-01-15 10:00:00'",
    "SELECT EXTRACT(year FROM d) AS y FROM t",
    "SELECT MIN(d) AS lo, MAX(ts) AS hi FROM t",
    "SELECT g, MAX(d) AS m FROM t GROUP BY g",
    "SELECT g, COUNT(*) AS c FROM t GROUP BY g ORDER BY g",
    "SELECT g FROM t UNION SELECT g FROM u",
    "SELECT g FROM t UNION ALL SELECT g FROM u",
    "SELECT DISTINCT g FROM t ORDER BY g DESC",
    "SELECT g, y FROM t JOIN u USING (g)",
    "SELECT n FROM t WHERE g IS DISTINCT FROM 'a'",
    "SELECT n, COALESCE(x, n) AS c FROM t",
    "SELECT n FROM t WHERE x IS NULL OR n > 4",
    "SELECT CASE WHEN x IS NULL THEN 'none' WHEN x > 20 THEN 'big' ELSE 'small' END AS k FROM t",
    "SELECT n, x / n AS r FROM t",
    "SELECT SUM(n) FILTER (WHERE x IS NULL) AS s FROM t",
    "SELECT g, AVG(n) AS a FROM t WHERE n < 6 GROUP BY g HAVING COUNT(*) > 1",
    "SELECT n FROM t ORDER BY x DESC LIMIT 3",
    "SELECT n FROM t ORDER BY x NULLS FIRST",
    "SELECT ROUND(x / 3, 2) AS r FROM t",
    "SELECT n, n * 1.0 / 4 AS f FROM t WHERE n BETWEEN 2 AND 5",
    "SELECT g, STRING_AGG(g, ',') AS s FROM t GROUP BY g",
    "SELECT COUNT(*) AS c FROM t WHERE ts > TIMESTAMP '2024-01-15 09:45:00'",
    "SELECT d + INTERVAL 1 DAY AS e FROM t",
    "SELECT n FROM t WHERE NOT (g = 'a')",
    "SELECT n FROM t WHERE g NOT IN ('a')",
    "SELECT GREATEST(n, o) AS g1, LEAST(n, o) AS l1 FROM t",
    "SELECT n FROM t WHERE g IS NOT DISTINCT FROM 'a'",
    "SELECT n FROM t WHERE n IS DISTINCT FROM o",
    "SELECT STRING_AGG(g, '-') AS s FROM t",
]


@pytest.fixture(scope="module")
def answers():
    qs = [sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql in QUERIES]
    return dict(zip(QUERIES, zip(qs, run_q(DATA, qs), strict=True), strict=True))


def ordered(sql: str) -> bool:
    return "ORDER BY" in sql and "OVER" not in sql


@pytest.mark.parametrize("sql", QUERIES, ids=QUERIES)
def test_q_returns_what_duckdb_returns(answers, sql):
    q, got = answers[sql]
    expected = run_duckdb(DATA, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, ordered(sql)), f"{q}\nq:      {got}\nduckdb: {expected}"


REFUSED = [
    "SELECT NTH_VALUE(n, a) OVER (ORDER BY n) FROM t",
    "SELECT STRING_AGG(g, sep) FROM t",
    "SELECT STRING_AGG(DISTINCT g, ',') FROM t",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
