"""Hand-written q, read back as SQL: q runs the q, DuckDB runs the SQL the
reader gives for it, on the same data with nulls, and the rows must agree.

The writer's q carries guards that make reading it back exact; hand-written q
does not, and q's comparisons are two-valued (a null is below every value):
`where n<3` keeps the null rows, so it reads as `n < 3 OR n IS NULL`, and a
comparison as a value never reads as NULL. The q idioms with a SQL form
(select[n], count x, wavg, wsum, deltas, prev, next, signum, an aggregate in a
where phrase) read as that form."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same

DATA = {
    "t": {
        "g": ["a", "b", "a", "a", "b", "c"],
        "o": [1, 2, 3, 4, 5, 6],
        "x": [10.0, None, 30.0, 40.0, None, 60.0],
        "n": [1, None, 3, -4, 5, 6],
        "w": [2.0, 1.0, 0.5, 1.0, 3.0, None],
    }
}

QUERIES = [
    "select from t where n<3",
    "select from t where n<=3",
    "select from t where not n>2",
    "select from t where n<>3",
    "select from t where x=n",
    "select from t where (n<0)|n>4",
    "select from t where not null x",
    "select from t where n within 0 4",
    "select from t where not n within 0 4",
    "select f:n>2, h:n<3, e:n=3 from t",
    "select f:n in 1 3, k:not n<2 from t",
    "select s:sum n by g from t",
    "select a:avg x by g from t",
    "select c:count x by g from t",
    "select c:count i by g from t",
    "select[2] from t",
    "select[3] o, n from t",
    "select v:x wavg n by g from t",
    "select v:w wsum x by g from t",
    "select v:w wsum x from t where g=`b",
    "update d:deltas n by g from `o xasc t",
    "update p:prev x by g from `o xasc t",
    "update q:next n by g from `o xasc t",
    "update s:sums n by g from `o xasc t",
    "select h:abs n, s:signum n from t",
    "select o, k:n*2 from t where n>0",
    "select from t where g in `a`c",
    'select from t where g like "a*"',
    "select from t where x>avg x",
    "select from t where n>0, o<avg o",
    "select from t where o>max n",
    "select distinct g from t",
    "select e:?[n<3;`lo;`hi] from t",
]


@pytest.fixture(scope="module")
def answers():
    return dict(zip(QUERIES, run_q(DATA, QUERIES), strict=True))


@pytest.mark.parametrize("q", QUERIES, ids=QUERIES)
def test_the_read_sql_returns_what_q_returns(answers, q):
    sql = sqlglot.parse_one(q, read="q").sql("duckdb")
    got = answers[q]
    assert not isinstance(got, str), f"q refuses {q}: {got}"
    expected = run_duckdb(DATA, sql)
    assert same(expected, got, ordered=q.startswith("select[")), (
        f"{sql}\nq:      {got}\nduckdb: {expected}"
    )


REFUSED = [
    # q's row order: SQL's tables have none, and windows need an ORDER BY.
    "update s:sums n by g from t",
    "select[-2] from t",
    "select n:count distinct g from t",  # q counts a null as a value
]


@pytest.mark.parametrize("q", REFUSED, ids=REFUSED)
def test_q_without_a_faithful_sql_form_is_refused(q):
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one(q, read="q")
