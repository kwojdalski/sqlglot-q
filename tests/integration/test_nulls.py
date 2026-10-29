"""NULL, empty and one-row inputs: where SQL's three-valued logic and its
NULL aggregates part from q's. Every query runs in DuckDB as SQL and in q as
its translation, on data with nulls in every column type, null join keys, a
one-row table and negative numbers.

Each of these once came back different in q:
- a<3, a<=3, a<>3 and NOT (a>2) kept null rows (q's null is the smallest
  value, and has no unknown); so did a=k when both were null
- b NOT LIKE 'x%' lost its NOT (sqlglot's Like(negate=True))
- SUM over no values was 0, MIN and MAX were 0W and -0W, and MIN and MAX of
  text raised 'type
- joins matched null keys (0N=0N), and IN (SELECT ...) matched a null
- NOT IN with a null in the list or subquery kept rows SQL drops
- CASE WHEN a < 3 took the THEN branch for a null a
- LENGTH(NULL) was 0, CAST('5' AS BIGINT) was 'type, -7 % 3 was 2
"""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same

DATA = {
    "t": {
        "a": [1, None, 3, -4, 5, None],
        "b": ["x", None, "y", "x", None, "z"],
        "c": [1.5, None, -2.5, 0.0, 3.5, 2.5],
        "k": [1, 2, None, 4, 5, None],
    },
    "u": {"k": [1, None, 5, 2], "v": ["p", "q", "r", "s"]},
    "e": {"a": [7], "b": ["w"], "c": [7.5]},
}

QUERIES = [
    "SELECT a FROM t WHERE a < 3",
    "SELECT a FROM t WHERE NOT (a > 2)",
    "SELECT a FROM t WHERE b <> 'x'",
    "SELECT a FROM t WHERE a NOT IN (1, 3)",
    "SELECT a FROM t WHERE b NOT LIKE 'x%'",
    "SELECT a FROM t WHERE a > 2 OR a < 0",
    "SELECT SUM(a) AS s, MIN(a) AS lo, MAX(a) AS hi, AVG(a) AS m FROM t WHERE a > 100",
    "SELECT COUNT(*) AS n, COUNT(a) AS m FROM t WHERE a > 100",
    "SELECT SUM(a) AS s FROM t WHERE a IS NULL",
    "SELECT MAX(b) AS m, MIN(b) AS n FROM t",
    "SELECT b, SUM(a) AS s FROM t GROUP BY b",
    "SELECT a, v FROM t JOIN u ON t.k = u.k",
    "SELECT a FROM t WHERE k IN (SELECT k FROM u)",
    "SELECT CASE WHEN a < 3 THEN 'lo' ELSE 'hi' END AS e FROM t",
    "SELECT a, c FROM t WHERE c >= -2.5 AND c < 2.5",
    "SELECT LENGTH(b) AS n FROM t",
    "SELECT CAST('5' AS BIGINT) AS n FROM t",
    "SELECT a % 3 AS m, -7 % 3 AS n FROM t",
    "SELECT COUNT(DISTINCT b) AS n FROM t",
    "SELECT b, COUNT(*) AS n FROM t GROUP BY b HAVING SUM(a) < 5",
    "SELECT a FROM t ORDER BY b DESC, a",
    "SELECT a FROM t WHERE b IN ('x', 'y')",
    "SELECT SUM(c) / COUNT(c) AS m FROM t",
    "SELECT a * 2 AS d FROM t WHERE a * 2 < 4",
    "SELECT a, b FROM e",
    "SELECT a FROM t WHERE a <= 3",
    "SELECT a FROM t WHERE a <> 3",
    "SELECT a FROM t WHERE a = k",
    "SELECT a FROM t WHERE a <> k",
    "SELECT a FROM t WHERE b < 'y'",
    "SELECT a FROM t WHERE c < 1",
    "SELECT a FROM t WHERE a NOT BETWEEN 0 AND 3",
    "SELECT a FROM t WHERE NOT (a > 2 OR b = 'x')",
    "SELECT a FROM t WHERE NOT (a IS NULL) AND a < 3",
    "SELECT MIN(c) AS m, MAX(c) AS x FROM t WHERE c IS NULL",
    "SELECT b, MAX(c) AS m FROM t GROUP BY b",
    "SELECT a, v FROM t LEFT JOIN u ON t.k = u.k",
    "SELECT a FROM t WHERE a NOT IN (SELECT k FROM u)",
    "SELECT UPPER(b) AS u FROM t",
    "SELECT CAST(a AS VARCHAR) AS s FROM t",
    "SELECT CAST(c AS BIGINT) AS n FROM t",
    "SELECT ABS(a) AS x, -a AS y FROM t",
    "SELECT DISTINCT b FROM t",
    "SELECT COALESCE(b, 'none') AS b2 FROM t",
    "SELECT a FROM t WHERE c BETWEEN a AND 3",
    "SELECT a FROM t LIMIT 2 OFFSET 5",
    "SELECT GREATEST(a, k) AS g FROM t",
    "SELECT SUM(a) AS s, COUNT(*) AS n FROM e",
    "SELECT b, MAX(c) AS m FROM e GROUP BY b",
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
    assert same(got, expected, "ORDER BY" in sql), f"{q}\nq:      {got}\nduckdb: {expected}"


def test_not_like_keeps_its_not():
    q = sqlglot.transpile("SELECT a FROM t WHERE b NOT LIKE 'x%'", read="duckdb", write="q")[0]
    assert "not b like" in q
