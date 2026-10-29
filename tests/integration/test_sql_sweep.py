"""Queries a sweep found q answering wrongly, or refusing, each kept as the
case that fixed it. Each runs in DuckDB as SQL and in q as its translation,
and reads back to the same q (tests/test_roundtrip.py)."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same
from tests.integration.test_sql_more import DATA

QUERIES = [
    # GROUP BY with nothing computed: select by g of a table of only g is 'rank.
    "SELECT g FROM (SELECT g FROM t) GROUP BY g",
    "SELECT g FROM (SELECT g FROM t EXCEPT ALL SELECT g FROM u) GROUP BY g",
    "SELECT g, o FROM t WHERE n > 0 GROUP BY g, o",
    # HAVING took a subquery's aggregate for the group's.
    "SELECT g FROM t GROUP BY g HAVING SUM(n) > (SELECT MIN(v) FROM u) / 4",
    "SELECT g FROM t GROUP BY g HAVING SUM(n) > ANY (SELECT v FROM u)",
    # MIN and MAX of text over running, sliding and look-ahead frames.
    "SELECT g, MIN(s) OVER (ORDER BY o, g) AS r FROM t",
    "SELECT g, MAX(s) OVER (ORDER BY o, g ROWS 1 PRECEDING) AS r FROM t",
    "SELECT g, MIN(s) OVER (ORDER BY o, g ROWS 2 PRECEDING) AS r FROM t",
    "SELECT g, MAX(s) OVER (ORDER BY o, g ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) AS r FROM t",
    "SELECT g, MIN(d) OVER (ORDER BY o, g ROWS 1 PRECEDING) AS r, "
    "MAX(d) OVER (ORDER BY o, g) AS m FROM t",
    # LIKE '%%' was q's ** ('nyi), and so the regex ''.
    "SELECT g FROM t WHERE s LIKE '%%' OR s LIKE 'a%%b'",
    "SELECT g, s FROM t WHERE REGEXP_MATCHES(s, '')",
    # SPLIT_PART on '' splits into characters.
    "SELECT SPLIT_PART(s, '', 1) AS a, SPLIT_PART(s, '', -1) AS b, SPLIT_PART(s, '', 9) AS c "
    "FROM t",
    # Text functions of a literal: each string `abc walked its characters.
    "SELECT UPPER('ab') || 'c' AS z",
    "SELECT LENGTH('abc') AS a, REPLACE('abc', 'b', 'x') AS b, SUBSTRING('abcdef', 2, 3) AS c",
    "SELECT LPAD('ab', 4, '*') AS a, LTRIM('xxab', 'x') AS b, SPLIT_PART('a_b_c', '_', 2) AS c",
    "SELECT ASCII('A') AS a, REVERSE('abc') AS b, STRPOS('abcb', 'cb') AS c, LEFT('abc', 2) AS d",
    "SELECT g, REPLACE('abc', 'b', 'x') AS z FROM t",
    "SELECT g FROM t WHERE s = LOWER('ABC')",
    # A constant piece of a concatenation is joined to each row.
    "SELECT g, CONCAT(s, UPPER('x')) AS z FROM t",
    "SELECT CHR(65) || CHR(66) AS z",
    # DATE_TRUNC of a date: a timespan xbar of a date is no day or hour.
    "SELECT DATE_TRUNC('month', DATE '2024-02-10') AS a, DATE_TRUNC('day', DATE '2024-05-10') AS b",
    "SELECT d, DATE_TRUNC('day', d) AS a, DATE_TRUNC('hour', d) AS b FROM t",
    "SELECT ABS(-3) AS a, ROUND(2.567, 2) AS b, SIGN(-2) AS c",
    # An aggregate in an update with no by is a window over the whole table.
    "SELECT g, n, n * 100.0 / SUM(n) OVER () AS pct FROM t",
    "SELECT g, n, n - AVG(n) OVER () AS d, MAX(x) OVER () AS m FROM t",
    # t.* of the one table is *.
    "SELECT t.* FROM t WHERE n > 0",
    "SELECT a.*, n * 2 AS dbl FROM t AS a",
    # LIKE a pattern per row, made q's when the query runs.
    "SELECT g, n FROM t WHERE s LIKE '%' || g || '%'",
    "SELECT g, s FROM t WHERE s NOT LIKE g || '%'",
    "SELECT g, s LIKE '%' || g AS m FROM t",
    # IN (SELECT ...) before another condition: exec ... from took it in.
    "SELECT g, v FROM u WHERE g IN (SELECT g FROM t) AND v > 15",
    "SELECT g, v FROM u WHERE g NOT IN (SELECT g FROM t WHERE g IS NOT NULL) AND v > 5",
    "SELECT g, v FROM u WHERE EXISTS (SELECT 1 FROM t WHERE t.g = u.g) AND g LIKE CONCAT(g, '%')",
    # (a, b) IN (SELECT ...): rows matched whole, a null part matching nothing.
    "SELECT g, n FROM t WHERE (g, n) IN (SELECT g, n FROM t WHERE o > 1) AND n > 0",
    "SELECT a, w FROM t2 WHERE (a, w) IN (SELECT a, w FROM t2 WHERE w < 3)",
    # A set operation's constant side takes the first's names.
    "SELECT g FROM t INTERSECT SELECT 'a'",
    "SELECT g FROM t UNION SELECT 'zz'",
    "SELECT g, n FROM t WHERE g = 'a' UNION ALL SELECT 'q', 99",
    # A window over a join reads back.
    "SELECT g, n, v, ROW_NUMBER() OVER (PARTITION BY g ORDER BY n) AS r FROM t JOIN u USING (g)",
    "SELECT t.g, n, SUM(v) OVER (ORDER BY n) AS s FROM t JOIN u ON t.g = u.g",
    # SELECT * sorts its result, as the q that sorts one reads back.
    "SELECT g, o, COUNT(*) AS c FROM t GROUP BY g, o HAVING COUNT(*) >= 1 ORDER BY g, o",
    # IN (SELECT ...) of two number types: in raised 'type.
    "SELECT g, n FROM t WHERE n IN (SELECT v / 10 FROM u)",
    "SELECT g, n FROM t WHERE n NOT IN (SELECT v / 10 FROM u) AND n > 0",
    "SELECT g, x FROM t WHERE x IN (SELECT o FROM t)",
    # ARRAY_AGG: under by a group's values are its list; select l:n by g
    # read back as an ungrouped column.
    "SELECT g, LIST(n) AS l FROM t GROUP BY g",
    "SELECT g, ARRAY_AGG(n * 2) AS l, COUNT(*) AS c FROM t WHERE o > 1 GROUP BY g",
    "SELECT ARRAY_AGG(o) AS l FROM t",
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
    ordered = bool(sqlglot.parse_one(sql, read="duckdb").args.get("order"))
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"


READ = [
    # q's -3 after a space is a negative number: abs -3 is abs[-3].
    ("([] a:enlist abs -3)", "SELECT ABS(-3) AS a"),
    ("select a:n - 1 from t", "SELECT n - 1 AS a FROM t"),
]


@pytest.mark.parametrize(("q", "sql"), READ, ids=[q for q, _ in READ])
def test_q_reads_as(q, sql):
    assert sqlglot.transpile(q, read="q", write="duckdb")[0] == sql
