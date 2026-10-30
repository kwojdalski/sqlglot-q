"""The fourth sweep's queries: each runs in DuckDB as SQL and in q as its
translation, and reads back to the same q (tests/test_roundtrip.py)."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same
from tests.integration.test_sql_more import DATA
from tests.integration.test_temporal import TICKS

QUERIES = [
    # An item of a group's keys is one value a group: under by, a key is
    # the group's list of it.
    "SELECT g, UPPER(g) AS u, COUNT(*) AS c FROM t GROUP BY g",
    "SELECT g, o, o * 2 AS d, SUM(n) AS s FROM t GROUP BY g, o",
    "SELECT g, LENGTH(g) + COUNT(*) AS z FROM t GROUP BY g",
]

# On trades and quotes.
TICK_QUERIES = [
    # An ASOF join's time is a key: neither side leaves it out.
    "SELECT t.sym, t.time, t.px, q.bid FROM trades AS t ASOF LEFT JOIN quotes AS q "
    "ON t.sym = q.sym AND t.time >= q.time",
    # The epoch of an interval: its seconds.
    "SELECT sym, EXTRACT(epoch FROM time - LAG(time) OVER (PARTITION BY sym ORDER BY time)) "
    "AS secs FROM trades",
    "SELECT sym, EPOCH(time) AS e, EXTRACT(epoch FROM MAX(time) - MIN(time)) AS s FROM trades "
    "GROUP BY sym, time",
]

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
