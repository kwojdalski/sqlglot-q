"""The fifth sweep's queries: each runs in DuckDB as SQL and in q as its
translation, and reads back to the same q (tests/test_roundtrip.py)."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same
from tests.integration.test_sql_more import DATA
from tests.integration.test_temporal import TICKS

QUERIES = [
    # Over the whole partition any aggregate is its grouped value, spread.
    "SELECT g, n, MEDIAN(n) OVER (PARTITION BY g) AS m FROM t",
    "SELECT g, n, STDDEV(n) OVER (PARTITION BY g) AS m FROM t",
    "SELECT g, n, VARIANCE(x) OVER () AS m FROM t",
    "SELECT g, n, SUM(n) FILTER (WHERE o > 1) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, n, COUNT(*) FILTER (WHERE o > 1) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, n, BOOL_OR(n > 2) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, n, STRING_AGG(s, ',') OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, n, ANY_VALUE(s) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, n, ARG_MAX(s, n) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, n, CORR(n, x) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, n, MODE(o) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, n, n - MEDIAN(n) OVER (PARTITION BY g) AS s FROM t",
    "SELECT g, SUM(n) FILTER (WHERE o > 1) OVER () AS s FROM t",
    # ROUND of an infinity is itself: floor 0w is 0W, a long.
    "SELECT g, n, ROUND(n / SUM(n) OVER (PARTITION BY g), 2) AS p FROM t",
    "SELECT ROUND(CAST('inf' AS DOUBLE)) AS a, ROUND(CAST('-inf' AS DOUBLE), 2) AS b",
    "SELECT g, ROUND(x) AS a, ROUND(x, 1) AS b, ROUND(n) AS c, ROUND(n, 1) AS d FROM t",
    # RANGE n PRECEDING of a number, its key's nulls (n, x) sorted first for
    # the search: a null key's frame is its null peers.
    "SELECT g, o, n, SUM(n) OVER (ORDER BY o RANGE BETWEEN 1 PRECEDING AND CURRENT ROW) AS s "
    "FROM t",
    "SELECT g, o, n, COUNT(*) OVER (PARTITION BY g ORDER BY o "
    "RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) AS s FROM t",
    "SELECT g, n, AVG(x) OVER (ORDER BY n RANGE BETWEEN 3 PRECEDING AND CURRENT ROW) AS s FROM t",
    "SELECT g, x, MAX(n) OVER (ORDER BY x RANGE BETWEEN 2.5 PRECEDING AND CURRENT ROW) AS s FROM t",
    "SELECT g, n, MIN(s) OVER (ORDER BY n RANGE BETWEEN 0 PRECEDING AND CURRENT ROW) AS s FROM t",
    "SELECT g, n, COUNT(x) OVER (ORDER BY n RANGE 2 PRECEDING) AS s FROM t",
    # FIRST_VALUE and LAST_VALUE IGNORE NULLS.
    "SELECT g, n, FIRST_VALUE(n IGNORE NULLS) OVER (PARTITION BY g ORDER BY o) AS f FROM t",
    "SELECT g, n, LAST_VALUE(n IGNORE NULLS) OVER (ORDER BY o, g ROWS UNBOUNDED PRECEDING) AS f "
    "FROM t",
    "SELECT g, x, LAST_VALUE(x IGNORE NULLS) OVER (ORDER BY o) AS f FROM t",
    "SELECT g, n, FIRST_VALUE(n IGNORE NULLS) OVER (ORDER BY o "
    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS f FROM t",
    "SELECT g, n, LAST_VALUE(n IGNORE NULLS) OVER (PARTITION BY g ORDER BY o "
    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS f FROM t",
    "SELECT g, d, LAST_VALUE(d IGNORE NULLS) OVER (ORDER BY o, g) AS f FROM t",
]

# On trades and quotes.
TICK_QUERIES: list[str] = []

ONE_WAY: set[str] = set()


@pytest.fixture(scope="module")
def answers():
    out = {}
    for data, queries in ((DATA, QUERIES), (TICKS, TICK_QUERIES)):
        qs = [sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql in queries]
        out |= dict(zip(queries, zip(qs, run_q(data, qs), strict=True), strict=True))
    return out


@pytest.mark.parametrize("sql", QUERIES + TICK_QUERIES, ids=QUERIES + TICK_QUERIES)
def test_q_returns_what_duckdb_returns(answers, sql):
    q, got = answers[sql]
    expected = run_duckdb(TICKS if sql in TICK_QUERIES else DATA, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    ordered = bool(sqlglot.parse_one(sql, read="duckdb").args.get("order"))
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"
