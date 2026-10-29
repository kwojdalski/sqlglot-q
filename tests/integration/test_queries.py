"""Each query runs in DuckDB as SQL and in q as its translation, on the same
tables, and the two answers have to agree. This is the test that matters:
the unit tests pin the text, this one pins what the text does."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import duckdb_connection, run_duckdb, run_q, same

TABLES = {
    "t": {
        "a": [1, 2, 3, 4, 5, 6],
        "b": ["x", "y", "x", "z", "y", "x"],
        "c": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
        "d": [10, None, 30, None, 50, 60],
    },
    "u": {
        "a": [1, 2, 5, 9],
        "v": ["p", "q", "r", "s"],
    },
}

# (sql, ordered): ordered compares row order too, and only queries with an
# ORDER BY that decides it have one.
QUERIES = [
    ("SELECT * FROM t", False),
    ("SELECT a, c FROM t WHERE a > 2", False),
    ("SELECT a FROM t WHERE a > 1 AND b = 'x'", False),
    ("SELECT a FROM t WHERE a > 4 OR b = 'y'", False),
    ("SELECT a FROM t WHERE NOT (a > 2)", False),
    ("SELECT a * 2 + c AS e FROM t", False),
    ("SELECT a - 1 - 1 AS e FROM t", False),
    ("SELECT a / 2 AS e FROM t", False),
    ("SELECT -a AS e, -1.5 AS f FROM t", False),
    ("SELECT a FROM t WHERE b IN ('x', 'z')", False),
    ("SELECT a FROM t WHERE b NOT IN ('x', 'z')", False),
    ("SELECT a FROM t WHERE a IN (1, 3, 9)", False),
    ("SELECT a FROM t WHERE a BETWEEN 2 AND 4", False),
    ("SELECT a FROM t WHERE b LIKE 'x%'", False),
    ("SELECT a FROM t WHERE d IS NULL", False),
    ("SELECT a FROM t WHERE d IS NOT NULL", False),
    ("SELECT COUNT(*) AS n FROM t", False),
    ("SELECT COUNT(d) AS n, COUNT(DISTINCT b) AS m FROM t", False),
    ("SELECT SUM(a) AS s, AVG(c) AS m, MIN(a) AS lo, MAX(a) AS hi FROM t", False),
    ("SELECT b, SUM(a) AS s FROM t GROUP BY b", False),
    ("SELECT SUM(a) AS s, b FROM t GROUP BY b", False),
    ("SELECT SUM(a) AS s FROM t GROUP BY b", False),
    ("SELECT b, COUNT(*) AS n FROM t GROUP BY b HAVING COUNT(*) > 1", False),
    ("SELECT b FROM t GROUP BY b HAVING SUM(a) > 6", False),
    ("SELECT b, AVG(d) AS m FROM t GROUP BY 1", False),
    ("SELECT a FROM t ORDER BY a DESC", True),
    ("SELECT a, b FROM t ORDER BY b, a DESC", True),
    ("SELECT a FROM t ORDER BY c DESC", True),
    ("SELECT a FROM t ORDER BY a LIMIT 2", True),
    ("SELECT a FROM t ORDER BY a LIMIT 2 OFFSET 3", True),
    ("SELECT a FROM t ORDER BY a LIMIT 50", True),
    ("SELECT b, SUM(a) AS s FROM t GROUP BY b ORDER BY s DESC", True),
    ("SELECT DISTINCT b FROM t", False),
    (
        "SELECT a, CASE WHEN a > 4 THEN 'big' WHEN a > 2 THEN 'mid' ELSE 'small' END AS k FROM t",
        False,
    ),
    ("SELECT CASE b WHEN 'x' THEN 1 ELSE 0 END AS k FROM t", False),
    ("SELECT COALESCE(d, 0) AS e FROM t", False),
    ("SELECT CAST(c AS BIGINT) AS e, CAST(a AS DOUBLE) AS f FROM t", False),
    ("SELECT ABS(a - 4) AS e, UPPER(b) AS f FROM t", False),
    ("SELECT EXP(c) AS e, LN(c) AS f, SQRT(c) AS g FROM t", False),
    ("SELECT t.a, v FROM t JOIN u ON t.a = u.a", False),
    ("SELECT t.a, v FROM t INNER JOIN u USING (a)", False),
    ("SELECT t.a, v FROM t LEFT JOIN u ON t.a = u.a", False),
    ("SELECT a FROM t WHERE a IN (SELECT a FROM u)", False),
    ("SELECT s FROM (SELECT a + 1 AS s FROM t) AS q WHERE s > 3", False),
    ("WITH w AS (SELECT a, c FROM t WHERE a > 2) SELECT a FROM w WHERE c < 6", False),
    ("SELECT a FROM t UNION ALL SELECT a FROM u", False),
    ("SELECT a FROM t UNION SELECT a FROM u", False),
    ("SELECT 1 + 2 AS x", False),
]


@pytest.fixture(scope="module")
def answers():
    translated = [sqlglot.transpile(sql, write="q")[0] for sql, _ in QUERIES]
    return dict(
        zip(
            [sql for sql, _ in QUERIES],
            zip(translated, run_q(TABLES, translated), strict=True),
            strict=True,
        )
    )


@pytest.mark.parametrize(("sql", "ordered"), QUERIES, ids=[sql for sql, _ in QUERIES])
def test_q_returns_what_duckdb_returns(answers, sql, ordered):
    q, got = answers[sql]
    expected = run_duckdb(TABLES, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"


STATEMENTS = [
    (
        "INSERT INTO t VALUES (7, 'w', 7.5, 70)",
        "SELECT a, b, c, d FROM t",
    ),
    (
        "INSERT INTO t (a, b, c, d) VALUES (7, 'w', 7.5, 70), (8, 'v', 8.5, NULL)",
        "SELECT a, b, d FROM t",
    ),
    ("UPDATE t SET c = 0, d = a WHERE a > 4", "SELECT a, c, d FROM t"),
    ("DELETE FROM t WHERE b = 'x'", "SELECT a FROM t"),
]


@pytest.mark.parametrize(("statement", "check"), STATEMENTS, ids=[s for s, _ in STATEMENTS])
def test_a_statement_leaves_the_table_duckdb_leaves(statement, check):
    q = sqlglot.transpile(statement, write="q")[0] + ";" + sqlglot.transpile(check, write="q")[0]
    (got,) = run_q(TABLES, [q])
    con = duckdb_connection(TABLES)
    con.execute(statement)
    expected = [tuple(r) for r in con.execute(check).fetchall()]
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, ordered=False), f"{q}\nq:      {got}\nduckdb: {expected}"
