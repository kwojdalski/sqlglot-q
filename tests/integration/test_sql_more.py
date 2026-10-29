"""More of SQL: functions q has no word for (scalars.py), DISTINCT inside
aggregates, set operations, month arithmetic, LIKE ... ESCAPE, quantified
subqueries, SELECT *, e, and the joins and subqueries whose q form once
answered something else: a LEFT JOIN with several matches on the right, a
correlated EXISTS, running and sliding frames over nulls. Each query runs in
DuckDB as SQL and in q as its translation, on data with nulls and duplicate
join keys, and reads back to the same q (tests/test_roundtrip.py)."""

from __future__ import annotations

import datetime as dt

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same

DATA = {
    "t": {
        "g": ["a", "b", "a", "a", "b", "c", "b"],
        "o": [3, 1, 1, 2, 2, 1, 5],
        "n": [1, None, 3, -4, 5, 6, 7],
        "x": [1.5, 2.0, None, -4.25, 8.0, 3.0, 0.0],
        "s": ["abc", "xbz", "a_x", "qqq", "Abq", "zz", "b"],
        "d": [
            dt.date(2024, 1, 31),
            dt.date(2024, 2, 29),
            None,
            dt.date(2023, 12, 1),
            dt.date(2024, 3, 15),
            dt.date(2024, 1, 1),
            dt.date(2024, 6, 30),
        ],
    },
    "u": {"g": ["a", "b", "d"], "v": [10, 20, 30]},
    "t2": {"a": [10, 20, 99, None], "w": [1, 2, 3, 4]},
}

QUERIES = [
    # Functions as q lambdas.
    "SELECT TRUNC(x) AS r FROM t",
    "SELECT LPAD(s, 5, '*') AS l, RPAD(s, 2, '*') AS r FROM t",
    "SELECT REPEAT(s, 2) AS p FROM t",
    "SELECT STRPOS(s, 'b') AS p, POSITION('bq' IN s) AS q FROM t",
    "SELECT LAST_DAY(d) AS e, DAYNAME(d) AS n, WEEK(d) AS w FROM t",
    "SELECT MAKE_DATE(2024, 1, o) AS e FROM t",
    "SELECT DATE_DIFF('month', d, DATE '2024-12-31') AS m FROM t",
    "SELECT DATE_DIFF('year', DATE '2020-12-31', d) AS y FROM t",
    "SELECT PI() * 2 AS p FROM t",
    "SELECT CAST(d AS VARCHAR) AS z, CAST(n AS VARCHAR) AS w FROM t",
    "SELECT CAST(n > 2 AS VARCHAR) AS z FROM t WHERE n IS NOT NULL",
    # Aggregates as q lambdas, and DISTINCT inside them.
    "SELECT g, ARG_MAX(n, o) AS a FROM t WHERE o <> 1 GROUP BY g",
    "SELECT g, ARG_MIN(n, x) AS a FROM t GROUP BY g",
    "SELECT g, QUANTILE_CONT(x, 0.5) AS p, QUANTILE_CONT(n, 0.25) AS q FROM t GROUP BY g",
    "SELECT MODE(g) AS m FROM t",
    "SELECT SUM(DISTINCT o) AS s, AVG(DISTINCT o) AS a, MIN(DISTINCT o) AS lo FROM t",
    "SELECT g, SUM(DISTINCT o) AS s, AVG(DISTINCT x) AS a FROM t GROUP BY g",
    "SELECT SUM(DISTINCT n) AS s FROM t WHERE n > 100",
    # Months, quarters and years are no fixed timespan.
    "SELECT d, d + INTERVAL 1 MONTH AS e, d - INTERVAL 1 MONTH AS f FROM t",
    "SELECT d, d + INTERVAL 1 YEAR AS e, d - INTERVAL 2 QUARTER AS f FROM t",
    "SELECT d FROM t WHERE d > DATE '2024-06-30' - INTERVAL 3 MONTH",
    "SELECT DATE_TRUNC('week', d) AS e FROM t",
    # Frames: the n PRECEDING shorthand, and nulls in running and sliding ones.
    "SELECT g, n, MAX(n) OVER (PARTITION BY g ORDER BY o, n ROWS UNBOUNDED PRECEDING) AS r FROM t",
    "SELECT g, n, MIN(n) OVER (PARTITION BY g ORDER BY o, n ROWS 2 PRECEDING) AS r FROM t",
    "SELECT g, n, MIN(n) OVER (PARTITION BY g ORDER BY o, n) AS r FROM t",
    "SELECT g, n, SUM(n) OVER (ORDER BY o, g ROWS 1 PRECEDING) AS r FROM t",
    "SELECT g, n, MAX(n) OVER (ORDER BY o, g ROWS 1 PRECEDING) AS r FROM t",
    # Set operations, and UNION of differently named columns.
    "SELECT g FROM t INTERSECT SELECT g FROM u",
    "SELECT g FROM u EXCEPT SELECT g FROM t WHERE n > 0",
    "SELECT g, o FROM t INTERSECT SELECT g, v FROM u",
    "SELECT g, n FROM t WHERE g = 'a' UNION ALL SELECT g, v FROM u",
    # LIKE ... ESCAPE: q's like has no escape; % and _ there are themselves.
    "SELECT g, s FROM t WHERE s LIKE '%\\_%' ESCAPE '\\'",
    "SELECT g, s FROM t WHERE s NOT LIKE '%!_%' ESCAPE '!'",
    "SELECT g, s FROM t WHERE s ILIKE 'A\\_%' ESCAPE '\\'",
    # = ANY and <> ALL are IN and NOT IN.
    "SELECT g, n FROM t WHERE g = ANY (SELECT g FROM u)",
    "SELECT g, n FROM t WHERE n <> ALL (SELECT n FROM t WHERE g = 'b')",
    # SELECT *, e: q's update, over the rows WHERE keeps.
    "SELECT *, n * 2 AS dbl FROM t",
    "SELECT *, n * 2 AS dbl, x + 1 AS y FROM t WHERE n > 0",
    # Correlated EXISTS: a semi-join, and NOT EXISTS keeps a null key.
    "SELECT g, n FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.g = t.g)",
    "SELECT g, n FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.g = t.g)",
    "SELECT g, n FROM t WHERE n > 0 AND NOT EXISTS (SELECT * FROM u WHERE u.g = t.g AND v < 15)",
    "SELECT g, v FROM u WHERE EXISTS (SELECT 1 FROM t WHERE t.g = u.g AND t.n > 4)",
    # A LEFT JOIN keeps every match: lj would keep the first.
    "SELECT u.g, COUNT(n) AS c FROM u LEFT JOIN t ON u.g = t.g GROUP BY u.g",
    "SELECT u.g, v, n FROM u LEFT JOIN t ON u.g = t.g",
    "SELECT a, v FROM t2 LEFT JOIN u ON t2.a = u.v",
    "SELECT g, n, v FROM t LEFT JOIN u USING (g)",
    "SELECT t.g, n, u.g AS h FROM t JOIN u ON t.g = u.g",
    # More of an inner join's ON is a filter; FROM t, u WHERE t.k = u.k a join.
    "SELECT t.g, n, v FROM t JOIN u ON t.g = u.g AND t.n > 2",
    "SELECT t.g, n, v FROM t JOIN u ON u.g = t.g AND t.o < u.v WHERE n > 0",
    "SELECT t.g, u.v FROM t, u WHERE t.g = u.g AND n > 2",
    "SELECT a, w, v FROM t2, u WHERE t2.a = u.v",
    # > ANY and > ALL: above the least, or above every value with no NULL.
    "SELECT g, n FROM t WHERE n > ANY (SELECT v / 4 FROM u)",
    "SELECT g, n FROM t WHERE n < ANY (SELECT o FROM t WHERE g = 'a')",
    "SELECT g, n FROM t WHERE n >= ALL (SELECT o FROM t)",
    "SELECT g, n FROM t WHERE n > ALL (SELECT n FROM t WHERE g = 'b')",
    "SELECT g, n FROM t WHERE n > ALL (SELECT v FROM u WHERE v > 100)",
    "SELECT g, n FROM t WHERE n <= ALL (SELECT o FROM t WHERE o > 1) OR g = 'c'",
    # A NULL adds nothing to CONCAT; 3 xbar of a null month is 2000.01m.
    "SELECT CONCAT(s, NULL) AS u, CONCAT(NULL, s, '-', g) AS w FROM t",
    "SELECT DATE_TRUNC('quarter', d) AS c FROM t",
    # 2.5 is a DECIMAL, cast half away from zero; a DOUBLE column half even.
    "SELECT CAST(-2.5 AS INTEGER) AS a, CAST(2.5 AS INTEGER) AS b, CAST(x AS INTEGER) AS c FROM t",
    "SELECT LTRIM(s, 'ab') AS a, RTRIM(s, 'zq') AS b, TRIM(s, 'a') AS c FROM t",
    "SELECT TRIM(BOTH 'xz' FROM s) AS c FROM t",
    "SELECT SPLIT_PART(s, '_', 1) AS a, SPLIT_PART(s, '_', -1) AS b, "
    "SPLIT_PART(s, 'b', 3) AS c FROM t",
    "SELECT ASCII(s) AS a FROM t",
    "SELECT g, n FROM t WHERE d < CURRENT_DATE AND CURRENT_TIMESTAMP > d",
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
    # q evaluates a subquery once, as a table of its own: b.g = a.g is g=g.
    "SELECT g, n FROM t AS a WHERE n > (SELECT AVG(n) FROM t AS b WHERE b.g = a.g)",
    "SELECT g, n FROM t WHERE g IN (SELECT g FROM u WHERE u.v > t.n)",
    "SELECT g FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.v > t.n)",
    # A joined q table has one column per name.
    "SELECT a.g, b.g AS h FROM t AS a CROSS JOIN u AS b",
    "SELECT a.n, b.n AS m FROM t AS a JOIN t AS b ON a.o = b.o",
    # A LEFT JOIN's right key is NULL where nothing matches.
    "SELECT t.g, n, u.g AS h FROM t LEFT JOIN u ON t.g = u.g",
    "SELECT t.g, n, v FROM t RIGHT JOIN u ON t.g = u.g",
    "SELECT g FROM t INTERSECT ALL SELECT g FROM u",
    "SELECT g, n FROM t WHERE NOT (n > ALL (SELECT v FROM u))",
    "SELECT s || NULL AS u FROM t",
    "SELECT g, o FROM t ORDER BY ABS(n)",
    "SELECT TRUNC(x, 1) FROM t",
    "SELECT LPAD(s, 5) FROM t",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
