"""Scalar functions, NULL handling, null ordering and the wider joins: each
query runs in DuckDB as SQL and in q as its translation, on the same data,
which has nulls in a sort key and padded strings."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same

DATA = {
    "t": {
        "a": [1, 2, 3, 4, 5, 6],
        "b": ["x", "y", "x", "z", "y", "x"],
        "c": [1.25, -2.5, 3.5, -0.5, 5.75, 2.675],
        "d": [10, None, 30, None, 50, 60],
        # Symbols cannot hold edge spaces (`$ trims them), so TRIM is
        # checked on text that has none; string mode is where it matters.
        "s": ["ab", "cd", "e", "f", "gh", "ij"],
    },
    "u": {"k": [1, 2, 5, 9], "v": ["p", "q", "r", "s"]},
}

QUERIES = [
    # NULL ordering: DuckDB sorts NULLs last unless told otherwise; q first.
    ("SELECT a, d FROM t ORDER BY d", True),
    ("SELECT a, d FROM t ORDER BY d DESC", True),
    ("SELECT a, d FROM t ORDER BY d NULLS FIRST, a", True),
    ("SELECT a, d FROM t ORDER BY d DESC NULLS FIRST, a DESC", True),
    ("SELECT a FROM t ORDER BY d, a DESC", True),
    ("SELECT a, d FROM t ORDER BY d LIMIT 3", True),
    # CASE without ELSE and NULLIF give a null of the branch's type.
    ("SELECT a, CASE WHEN a > 3 THEN b END AS e FROM t", False),
    ("SELECT a, CASE WHEN a > 3 THEN 'big' END AS e FROM t", False),
    ("SELECT a, CASE WHEN a > 3 THEN c END AS e FROM t", False),
    ("SELECT a, CASE WHEN a > 3 THEN NULL ELSE b END AS e FROM t", False),
    ("SELECT a, NULLIF(b, 'x') AS e, NULLIF(a, 2) AS f FROM t", False),
    # GREATEST and LEAST skip a null, as SQL does.
    ("SELECT GREATEST(a, d) AS g, LEAST(a, d) AS l FROM t", False),
    ("SELECT GREATEST(a, 3, d) AS g, LEAST(c, a, 2) AS l FROM t", False),
    # Rounding half away from zero, powers, signs, truncating division.
    ("SELECT ROUND(c) AS r, ROUND(c, 1) AS s FROM t", False),
    ("SELECT POWER(a, 2) AS p, SIGN(c) AS g FROM t", False),
    ("SELECT a // 4 AS q, (a - 7) // 2 AS r FROM t", False),
    # Aggregates with FILTER.
    ("SELECT COUNT(*) FILTER (WHERE a > 2) AS n, SUM(c) FILTER (WHERE b = 'x') AS s FROM t", False),
    (
        "SELECT b, COUNT(*) FILTER (WHERE d IS NOT NULL) AS n, MAX(a) FILTER (WHERE a < 5) AS m "
        "FROM t GROUP BY b",
        False,
    ),
    # Text.
    ("SELECT TRIM(s) AS t1, LTRIM(s) AS t2, RTRIM(s) AS t3 FROM t", False),
    ("SELECT b || '-' || b AS c1, CONCAT('<', b, '>') AS c2 FROM t", False),
    ("SELECT CONCAT(b, 'z') AS c FROM t WHERE a < 3", False),
    # Joins on differently named keys, RIGHT JOIN, FROM t, u.
    ("SELECT a, v FROM t JOIN u ON t.a = u.k", False),
    ("SELECT a, v FROM t JOIN u ON u.k = t.a WHERE a > 1", False),
    ("SELECT a, v FROM t LEFT JOIN u ON t.a = u.k", False),
    ("SELECT k, v, b FROM t RIGHT JOIN u ON t.a = u.k", False),
    ("SELECT a, k FROM t, u WHERE a = 1", False),
    # SELECT * EXCLUDE.
    ("SELECT * EXCLUDE (s, c) FROM t", False),
    ("SELECT * EXCLUDE (s) FROM t WHERE a > 3 ORDER BY a DESC", True),
]


@pytest.fixture(scope="module")
def answers():
    qs = [sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql, _ in QUERIES]
    return dict(zip([s for s, _ in QUERIES], zip(qs, run_q(DATA, qs), strict=True), strict=True))


@pytest.mark.parametrize(("sql", "ordered"), QUERIES, ids=[s for s, _ in QUERIES])
def test_q_returns_what_duckdb_returns(answers, sql, ordered):
    q, got = answers[sql]
    expected = run_duckdb(DATA, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"


REFUSED = [
    "SELECT * FROM t FULL JOIN u ON t.a = u.k",
    "SELECT * FROM t JOIN u ON a = k",
    "SELECT TRIM('x' FROM s) FROM t",
    "SELECT COUNT(d) FILTER (WHERE a > 1) FROM t",
    "SELECT CONCAT(b, ' ') FROM t",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
