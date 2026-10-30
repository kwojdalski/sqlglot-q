"""The third sweep's queries: each runs in DuckDB as SQL and in q as its
translation, and reads back to the same q (tests/test_roundtrip.py)."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same
from tests.integration.test_calendar_text import DATA as CALENDAR
from tests.integration.test_sql_more import DATA

QUERIES = [
    # A join of an unnamed query: its columns are not the other side's.
    "SELECT g, n FROM (SELECT g, n FROM t ORDER BY n LIMIT 3) AS q JOIN u USING (g)",
    # A constant ORDER BY term sorts nothing.
    "SELECT g, n FROM t ORDER BY (SELECT MAX(v) FROM u), n",
    # Row values, compared part by part.
    "SELECT * FROM t WHERE (g, n) = ('a', 3)",
    "SELECT * FROM t WHERE (g, n) <> ('a', 3)",
    "SELECT * FROM t WHERE (g, n) IN (('a', 3), ('b', 5))",
]

# On the calendar table, which has a boolean column f.
CALENDAR_QUERIES = [
    # Text as a boolean, as SQL reads it: q's "B"$ reads only t and 1.
    "SELECT a, CAST('true' AS BOOLEAN) AS z, CAST('No' AS BOOLEAN) AS w FROM t",
    # OR of a boolean column, q's or: f|x reads back as GREATEST.
    "SELECT a FROM t WHERE f OR n > 5",
    "SELECT a FROM t WHERE f AND x > 0",
]

ONE_WAY: set[str] = set()


@pytest.fixture(scope="module")
def answers():
    out = {}
    for data, queries in ((DATA, QUERIES), (CALENDAR, CALENDAR_QUERIES)):
        qs = [sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql in queries]
        out |= dict(zip(queries, zip(qs, run_q(data, qs), strict=True), strict=True))
    return out


@pytest.mark.parametrize("sql", QUERIES + CALENDAR_QUERIES, ids=QUERIES + CALENDAR_QUERIES)
def test_q_returns_what_duckdb_returns(answers, sql):
    q, got = answers[sql]
    expected = run_duckdb(CALENDAR if sql in CALENDAR_QUERIES else DATA, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    ordered = bool(sqlglot.parse_one(sql, read="duckdb").args.get("order"))
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"
