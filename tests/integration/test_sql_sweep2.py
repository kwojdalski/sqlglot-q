"""The second sweep's queries: each runs in DuckDB as SQL and in q as its
translation, and reads back to the same q (tests/test_roundtrip.py)."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same
from tests.integration.test_sql_more import DATA

QUERIES = [
    # LEFT(s, -n) is all but the last n; RIGHT(s, -n) all but the first n.
    "SELECT LEFT(s, -1) AS a, RIGHT(s, -1) AS b, LEFT(s, 2) AS c, RIGHT(s, 2) AS d FROM t",
    # A rank of several order keys: (flip(o;n))?flip(o;n), parenthesised.
    "SELECT g, n, RANK() OVER (PARTITION BY g ORDER BY o DESC, n) AS r FROM t",
    "SELECT g, n, DENSE_RANK() OVER (ORDER BY g, o) AS r, PERCENT_RANK() OVER (ORDER BY g, o) AS p "
    "FROM t",
    # Math.
    "SELECT SIN(x) AS a, COS(x) AS b, TAN(x) AS c, ATAN(x) AS d FROM t",
    "SELECT ASIN(0.5) AS a, ACOS(0.5) AS b, ATAN2(x, o) AS d FROM t",
    "SELECT DEGREES(x) AS a, RADIANS(x) AS b, CBRT(x) AS c FROM t",
    "SELECT FACTORIAL(CAST(o AS INTEGER)) AS a FROM t",
    "SELECT GCD(o, 4) AS a, LCM(o, 4) AS b, EVEN(x) AS c, BIT_COUNT(o) AS d FROM t",
    "SELECT ISINF(x) AS b FROM t WHERE x IS NOT NULL",
    # Bits, through q's boolean vectors.
    "SELECT o & 3 AS a, o | 4 AS b, o << 1 AS c, o >> 1 AS d, xor(o, 1) AS e, ~o AS f FROM t",
    # Text.
    "SELECT TRY_CAST(s AS INTEGER) AS a FROM t",
    "SELECT CONCAT_WS('-', g, s) AS a FROM t",
    "SELECT TRANSLATE(s, 'ab', 'x') AS a FROM t",
    "SELECT MD5(s) AS a FROM t",
    # Aggregates.
    "SELECT g, KURTOSIS(x) AS a, SKEWNESS(x) AS b FROM t GROUP BY g",
    "SELECT KURTOSIS(x) AS a, SKEWNESS(n) AS b FROM t",
    "SELECT g, PRODUCT(n) AS a FROM t GROUP BY g",
    "SELECT g, BIT_AND(o) AS a, BIT_OR(o) AS b, BIT_XOR(o) AS c FROM t GROUP BY g",
    "SELECT g, APPROX_COUNT_DISTINCT(o) AS a FROM t GROUP BY g",
    "SELECT g, QUANTILE_DISC(x, 0.5) AS a, QUANTILE_DISC(n, 0.25) AS b FROM t GROUP BY g",
    "SELECT g, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) AS a FROM t GROUP BY g",
    "SELECT PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY n) AS a FROM t",
    # Windows: LAST_VALUE and NTH_VALUE to the current row, COUNT(DISTINCT).
    "SELECT g, n, LAST_VALUE(n) OVER (PARTITION BY g ORDER BY o) AS r FROM t",
    "SELECT g, n, LAST_VALUE(n) OVER (PARTITION BY g ORDER BY o, n "
    "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS r FROM t",
    "SELECT g, n, NTH_VALUE(n, 2) OVER (PARTITION BY g ORDER BY o, n) AS r FROM t",
    "SELECT g, n, NTH_VALUE(n, 2) OVER (PARTITION BY g ORDER BY o) AS r FROM t",
    "SELECT g, n, NTH_VALUE(s, 1) OVER (PARTITION BY g ORDER BY o, n "
    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS r FROM t",
    "SELECT g, n, COUNT(DISTINCT o) OVER (PARTITION BY g) AS r FROM t",
    # Named windows.
    "SELECT g, n, ROW_NUMBER() OVER w AS r, SUM(n) OVER w AS s FROM t "
    "WINDOW w AS (PARTITION BY g ORDER BY o, n)",
    "SELECT g, n, SUM(n) OVER (w ROWS 1 PRECEDING) AS s FROM t "
    "WINDOW w AS (PARTITION BY g ORDER BY o, n)",
    # Windows over a grouped result.
    "SELECT g, SUM(n) AS s, SUM(SUM(n)) OVER () AS total FROM t GROUP BY g",
    "SELECT g, COUNT(*) AS c, RANK() OVER (ORDER BY COUNT(*) DESC) AS r FROM t GROUP BY g",
    "SELECT g, SUM(n) AS s, SUM(n) * 100.0 / SUM(SUM(n)) OVER () AS pct FROM t GROUP BY g",
    "SELECT g, o, MAX(n) AS m, ROW_NUMBER() OVER (PARTITION BY g ORDER BY MAX(n) DESC) AS r "
    "FROM t GROUP BY g, o",
    # ORDER BY after INTERSECT and EXCEPT.
    "SELECT g FROM t INTERSECT SELECT g FROM u ORDER BY g DESC",
    "SELECT g FROM t EXCEPT ALL SELECT g FROM u ORDER BY 1",
    # Grouping sets, as a UNION ALL of groupings.
    "SELECT g, COUNT(*) AS c FROM t GROUP BY ROLLUP (g)",
    "SELECT g, o, SUM(n) AS s FROM t GROUP BY ROLLUP (g, o)",
    "SELECT g, o, COUNT(*) AS c FROM t GROUP BY GROUPING SETS ((g), (o))",
    "SELECT g, o, COUNT(*) AS c FROM t GROUP BY GROUPING SETS ((g, o), ())",
    "SELECT g, o, MAX(x) AS m FROM t GROUP BY CUBE (g, o)",
    # VALUES as a table.
    "SELECT k, s FROM (VALUES (1, 'a'), (2, 'b')) AS v(k, s) WHERE k > 1",
    "SELECT t.g, n, k FROM t JOIN (VALUES ('a', 1), ('b', 2)) AS v(g, k) ON t.g = v.g",
    "SELECT k FROM (VALUES (5)) AS v(k)",
    # Quoted names: `$(...) xcol, parenthesised; a keyword as a name.
    'SELECT g AS "Group", n AS "my col" FROM t',
    'SELECT g, n AS "select" FROM t',
    'SELECT g AS "a b", COUNT(*) AS "n rows" FROM t GROUP BY g',
    # A joined q table keeps the right side's column of a shared name: each
    # side first leaves out what the query reads from another.
    "SELECT t.g, u.v FROM t CROSS JOIN u",
    "SELECT t.g, u.v FROM t, u WHERE u.v > 15",
    "SELECT t.n, u.v FROM t JOIN u ON t.g = u.g",
    "SELECT t.o, v FROM t JOIN u USING (g)",
    "SELECT u.g, t.n FROM u LEFT JOIN t ON u.g = t.g",
    "SELECT a, w, u.g FROM t2 CROSS JOIN u",
]

# Translated, but read back from q only as far as SQL can: one way.
ONE_WAY = {
    "SELECT CONCAT_WS('-', g, s) AS a FROM t",
    "SELECT g, COUNT(*) AS c FROM t GROUP BY ROLLUP (g)",
    "SELECT g, o, SUM(n) AS s FROM t GROUP BY ROLLUP (g, o)",
    "SELECT g, o, COUNT(*) AS c FROM t GROUP BY GROUPING SETS ((g), (o))",
    "SELECT g, o, COUNT(*) AS c FROM t GROUP BY GROUPING SETS ((g, o), ())",
    "SELECT g, o, MAX(x) AS m FROM t GROUP BY CUBE (g, o)",
}


@pytest.fixture(scope="module")
def answers():
    qs = [sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql in QUERIES]
    return dict(zip(QUERIES, zip(qs, run_q(DATA, qs), strict=True), strict=True))


@pytest.mark.parametrize("sql", QUERIES, ids=QUERIES)
def test_q_returns_what_duckdb_returns(answers, sql):
    q, got = answers[sql]
    expected = run_duckdb(DATA, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    ordered = bool(sqlglot.parse_one(sql, read="duckdb").args.get("order"))
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"
