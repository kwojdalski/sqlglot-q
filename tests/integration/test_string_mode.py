"""string_literals="string": text as q strings (char vectors), not symbols.
A list of strings is not a vector q's null, sort, comparison, ?[...] or ^
treat as text: null tests each character, sorting goes element by element,
"b"<"abc" compares characters, and ?[...] and ^ refuse strings. So text is
tested for emptiness, ordered and compared as symbols, and picked row by row.
q has no null string: the empty string stands for NULL.

Each query runs in DuckDB as SQL and in q as its translation, and reads
back to the same q (tests/test_roundtrip.py)."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import Strings, run_duckdb, run_q, same

WRITE = "q, string_literals=string"
DATA = {
    "s": {
        "a": [1, 2, 3, 4, 5],
        "w": Strings(["abc", "b", None, "abc", "xyz"]),
        "v": Strings(["p", "qq", "r", "s", "t"]),
    }
}

QUERIES = [
    "SELECT a FROM s WHERE w = 'abc'",
    "SELECT a FROM s WHERE w <> 'abc'",
    "SELECT a FROM s WHERE w = v",
    "SELECT a FROM s WHERE w LIKE 'a%'",
    "SELECT a FROM s WHERE w IN ('abc', 'b')",
    "SELECT a, LENGTH(w) AS l FROM s",
    "SELECT a, UPPER(w) AS u FROM s",
    "SELECT a, w || '-' || v AS c FROM s",
    "SELECT a, CONCAT(w, v) AS c FROM s",
    "SELECT w, COUNT(*) AS c FROM s GROUP BY w",
    "SELECT DISTINCT w FROM s",
    "SELECT a, w FROM s ORDER BY w, a",
    "SELECT MIN(w) AS lo, MAX(w) AS hi FROM s",
    "SELECT a, TRIM(w) AS t1 FROM s",
    "SELECT a FROM s WHERE w > 'b'",
    "SELECT a, LEFT(w, 2) AS l FROM s",
    "SELECT STRING_AGG(w, ',') AS g FROM s",
    "SELECT a, CASE WHEN w = 'b' THEN 'B' ELSE w END AS c FROM s",
    "SELECT a, COALESCE(NULLIF(w, 'b'), 'none') AS c FROM s",
    "SELECT a, COALESCE(w, 'none') AS c FROM s",
    "SELECT a FROM s WHERE STARTS_WITH(w, 'ab')",
    # The functions of scalars.py, in their string forms.
    "SELECT a, LPAD(w, 4, '*') AS l, RPAD(v, 3, '.') AS r FROM s",
    "SELECT a, REPEAT(w, 2) AS p, STRPOS(w, 'b') AS q FROM s",
    "SELECT MODE(w) AS m, ARG_MAX(w, a) AS x, ARG_MIN(a, w) AS n FROM s",
    "SELECT a, CAST(a AS VARCHAR) AS c, CAST(a > 2 AS VARCHAR) AS b FROM s",
    "SELECT a FROM s WHERE w LIKE 'a\\_%' ESCAPE '\\'",
    "SELECT a, LTRIM(w, 'a') AS l, RTRIM(w, 'cz') AS r, TRIM(w, 'ab') AS t FROM s",
    "SELECT a, SPLIT_PART(w, 'b', 1) AS p, SPLIT_PART(w, 'b', -1) AS q FROM s",
    "SELECT a, ASCII(w) AS c, CONCAT(w, NULL, v) AS d FROM s",
    # q reads right to left: a three-part join is parenthesised on the left.
    "SELECT a, CONCAT(w, '-', v) AS c, w || '-' || v AS d FROM s",
    "SELECT a, CHR(CAST(a + 64 AS INTEGER)) AS c FROM s WHERE REGEXP_MATCHES(w, 'b')",
    # Text of two columns, of unknown type when written: compared as symbols.
    "SELECT a FROM s WHERE w < v",
    "SELECT a FROM s WHERE w BETWEEN 'a' AND 'b'",
    "SELECT a, COALESCE(w, v) AS c FROM s",
    "SELECT a, w FROM s WHERE w = (SELECT MIN(w) FROM s)",
    # enlist "abc" is a list of one string.
    "SELECT a FROM s WHERE w NOT IN ('abc')",
    "SELECT w FROM s INTERSECT SELECT 'abc'",
    "SELECT REPLACE('abc', 'b', 'x') AS z",
    "SELECT a, UPPER('x') || w AS c FROM s",
    "SELECT a, CAST(a AS VARCHAR) || w AS c FROM s",
    # A window's one string per partition, over its rows.
    "SELECT a, w, FIRST_VALUE(w) OVER (ORDER BY a) AS p FROM s",
    "SELECT a, MAX(w) OVER () AS p FROM s",
    "SELECT a, MIN(w) OVER (ORDER BY a ROWS 1 PRECEDING) AS p FROM s",
    # A join on a text key: a string's null is the empty one.
    "SELECT s.a, s.w, t2.k FROM s JOIN (SELECT w, a AS k FROM s WHERE a > 3) AS t2 ON s.w = t2.w",
    "SELECT x.a, x.w FROM s AS x LEFT JOIN (SELECT v, a AS k FROM s) AS y ON x.w = y.v",
    # Windows of text: LAST_VALUE's peers, NTH_VALUE, COUNT(DISTINCT), FIRST.
    "SELECT a, w, LAST_VALUE(w) OVER (ORDER BY a) AS l FROM s",
    "SELECT a, w, NTH_VALUE(w, 2) OVER (ORDER BY a) AS l FROM s",
    "SELECT a, COUNT(DISTINCT w) OVER () AS c FROM s",
    "SELECT a, FIRST(w) OVER () AS f FROM s",
    # A constant text value, one per row.
    "SELECT a, 'x' AS c FROM s",
    "SELECT a, UPPER('x') AS c, CAST(5 AS VARCHAR) AS d FROM s",
    "SELECT a, STRFTIME(DATE '2024-01-05', '%Y') AS y FROM s",
    # Row values, GREATEST/LEAST and NULLIF of text columns.
    "SELECT a, w FROM s WHERE (w, a) IN (SELECT w, a FROM s WHERE a < 3)",
    "SELECT a, GREATEST(w, v) AS g, LEAST(w, v) AS l, GREATEST(w, v, 'q') AS h FROM s",
    "SELECT a, NULLIF(w, v) AS n, NULLIF(w, w) AS m FROM s",
]


@pytest.fixture(scope="module")
def answers():
    qs = [sqlglot.transpile(sql, read="duckdb", write=WRITE)[0] for sql in QUERIES]
    return dict(zip(QUERIES, zip(qs, run_q(DATA, qs), strict=True), strict=True))


@pytest.mark.parametrize("sql", QUERIES, ids=QUERIES)
def test_q_returns_what_duckdb_returns(answers, sql):
    q, got = answers[sql]
    expected = run_duckdb(DATA, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, "ORDER BY" in sql), f"{q}\nq:      {got}\nduckdb: {expected}"
