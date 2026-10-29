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
