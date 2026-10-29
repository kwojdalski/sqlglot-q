"""Window functions: each query runs in DuckDB as SQL and in q as its chain
of updates, on data with ties in the order key (where RANGE and ROWS frames
differ) and a null."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same

DATA = {
    "w": {
        "g": ["a", "b", "a", "a", "b", "a", "b"],
        "o": [1, 1, 2, 2, 3, 5, 4],
        "x": [10, 20, 30, 40, 50, 60, None],
        "f": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5],
    }
}

QUERIES = [
    "SELECT g, o, SUM(x) OVER (PARTITION BY g) AS s FROM w",
    "SELECT g, AVG(f) OVER (PARTITION BY g) AS m, COUNT(*) OVER (PARTITION BY g) AS n FROM w",
    "SELECT g, MAX(x) OVER () AS m, MIN(f) OVER () AS lo FROM w",
    "SELECT g, COUNT(x) OVER (PARTITION BY g) AS n FROM w",
    # RANGE, the default with ORDER BY: tied rows share the value.
    "SELECT g, o, SUM(x) OVER (PARTITION BY g ORDER BY o) AS s FROM w",
    "SELECT g, o, COUNT(*) OVER (PARTITION BY g ORDER BY o) AS n FROM w",
    "SELECT g, o, AVG(f) OVER (PARTITION BY g ORDER BY o) AS m FROM w",
    "SELECT g, o, MAX(f) OVER (ORDER BY o) AS m FROM w",
    "SELECT g, o, SUM(f) OVER (PARTITION BY g ORDER BY o DESC) AS s FROM w",
    # ROWS: a running value per row.
    "SELECT g, o, f, SUM(f) OVER (PARTITION BY g ORDER BY o, f "
    "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM w",
    "SELECT g, o, f, AVG(f) OVER (PARTITION BY g ORDER BY o, f "
    "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS m FROM w",
    "SELECT g, o, f, MAX(f) OVER (ORDER BY o, f ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS m "
    "FROM w",
    "SELECT g, o, SUM(f) OVER (PARTITION BY g ORDER BY o "
    "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS s FROM w",
    # Ranking and offsets.
    "SELECT g, o, RANK() OVER (PARTITION BY g ORDER BY o) AS r FROM w",
    "SELECT g, o, DENSE_RANK() OVER (PARTITION BY g ORDER BY o) AS r FROM w",
    "SELECT g, o, RANK() OVER (ORDER BY o DESC) AS r FROM w",
    "SELECT g, o, f, ROW_NUMBER() OVER (PARTITION BY g ORDER BY o, f) AS rn FROM w",
    "SELECT g, o, f, LAG(f) OVER (PARTITION BY g ORDER BY o, f) AS p FROM w",
    "SELECT g, o, f, LEAD(f, 2) OVER (PARTITION BY g ORDER BY o, f) AS nx FROM w",
    "SELECT g, f - LAG(f) OVER (PARTITION BY g ORDER BY o, f) AS d FROM w",
    # Around the windows: WHERE runs first, ORDER BY and LIMIT after.
    "SELECT g, o, SUM(x) OVER (PARTITION BY g ORDER BY o) AS s FROM w WHERE o > 1",
    "SELECT *, ROW_NUMBER() OVER (PARTITION BY g ORDER BY o, f) AS rn FROM w",
    "SELECT g, o, f, SUM(f) OVER (PARTITION BY g) AS s FROM w ORDER BY f DESC LIMIT 3",
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
    ordered = "LIMIT" in sql
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"


REFUSED = [
    "SELECT LAG(x, 1, 0) OVER (ORDER BY o) FROM w",
    "SELECT SUM(x) OVER (ORDER BY o ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING) FROM w",
    "SELECT NTH_VALUE(x, 2) OVER (ORDER BY o) FROM w",
    "SELECT g, SUM(SUM(x)) OVER () FROM w GROUP BY g",
    "SELECT RANK() OVER (PARTITION BY g) FROM w",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
