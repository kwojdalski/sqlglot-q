"""Features that come through sqlglot's own machinery: its rewrites
(QUALIFY, DISTINCT ON), GROUP BY ALL, the TRANSFORMS table (statistics,
logs, text), dialect settings, and the FUNCTIONS map for user functions.
Each query runs in DuckDB as SQL and in q as its translation."""

from __future__ import annotations

import pytest
import sqlglot

from sqlglot_q import Q
from tests.integration.harness import duckdb_connection, run_duckdb, run_q, same

DATA = {
    "t": {
        "g": ["a", "b", "a", "a", "b", "c"],
        "o": [3, 1, 1, 2, 2, 1],
        "x": [1.0, 2.0, 4.0, None, 8.0, 3.0],
        "y": [2.0, 1.0, 5.0, 3.0, 9.0, 4.0],
        "s": ["abc", "xbz", "a*x", "qqq", "abq", "zz"],
    }
}

QUERIES = [
    # sqlglot's eliminate_qualify and eliminate_distinct_on.
    ("SELECT g, o, x FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY g ORDER BY o) = 1", False),
    ("SELECT g, o FROM t QUALIFY RANK() OVER (ORDER BY o DESC) <= 2", False),
    ("SELECT DISTINCT ON (g) g, o, y FROM t ORDER BY g, o DESC", False),
    # GROUP BY ALL.
    ("SELECT g, SUM(y) AS s, COUNT(*) AS n FROM t GROUP BY ALL", False),
    # Statistics, through TRANSFORMS. MEDIAN skips the null q's med counts.
    (
        "SELECT MEDIAN(x) AS m, STDDEV(x) AS sd, STDDEV_POP(x) AS sp, VARIANCE(x) AS v, "
        "VAR_POP(x) AS vp FROM t",
        False,
    ),
    ("SELECT CORR(x, y) AS r, COVAR_POP(x, y) AS cp, COVAR_SAMP(x, y) AS cs FROM t", False),
    ("SELECT g, COUNT_IF(y > 2) AS n, ANY_VALUE(x) AS v FROM t WHERE g <> 'b' GROUP BY g", False),
    ("SELECT g, MEDIAN(y) AS m FROM t GROUP BY g", False),
    # Logs and text.
    ("SELECT LOG(2, y) AS l2, LOG10(y) AS l10, LOG2(y) AS lb FROM t", False),
    ("SELECT s FROM t WHERE STARTS_WITH(s, 'ab')", False),
    ("SELECT s FROM t WHERE ENDS_WITH(s, 'z') OR CONTAINS(s, '*')", False),
    ("SELECT LEFT(s, 2) AS l, RIGHT(s, 2) AS r, LEFT(s, 9) AS w FROM t", False),
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


# A script of statements: keyed tables, upsert, truncate. Both engines run
# it from nothing, and the table it leaves has to be the same.
SCRIPTS = [
    [
        "CREATE TABLE k (id BIGINT PRIMARY KEY, v DOUBLE)",
        "INSERT INTO k VALUES (1, 1.5)",
        "INSERT INTO k VALUES (2, 2.5)",
        "INSERT OR REPLACE INTO k VALUES (1, 9.5)",
        "INSERT INTO k VALUES (3, 3.5) ON CONFLICT DO UPDATE SET v = EXCLUDED.v",
        "INSERT INTO k VALUES (2, 7.5) ON CONFLICT DO UPDATE SET v = EXCLUDED.v",
        "SELECT id, v FROM k",
    ],
    [
        "CREATE TABLE p (a BIGINT, b BIGINT, v DOUBLE, PRIMARY KEY (a, b))",
        "INSERT INTO p VALUES (1, 1, 1.0), (1, 2, 2.0)",
        "INSERT OR REPLACE INTO p VALUES (1, 2, 5.0)",
        "SELECT a, b, v FROM p",
    ],
    [
        "CREATE TABLE z (a BIGINT, b VARCHAR)",
        "INSERT INTO z VALUES (1, 'x'), (2, 'y')",
        "TRUNCATE TABLE z",
        "INSERT INTO z VALUES (3, 'w')",
        "SELECT a, b FROM z",
    ],
]


@pytest.mark.parametrize("script", SCRIPTS, ids=[s[0] for s in SCRIPTS])
def test_a_script_leaves_the_table_duckdb_leaves(script):
    q = ";".join(sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql in script)
    (got,) = run_q({}, [q])
    con = duckdb_connection({})
    for sql in script[:-1]:
        con.execute(sql)
    expected = [tuple(r) for r in con.execute(script[-1]).fetchall()]
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, ordered=False), f"{q}\nq:      {got}\nduckdb: {expected}"


def test_string_literals_is_a_dialect_setting():
    sql = "SELECT a FROM t WHERE b = 'xy'"
    by_setting = sqlglot.transpile(sql, write="q, string_literals=string")[0]
    by_argument = sqlglot.transpile(sql, write="q", string_literals="string")[0]
    assert by_setting == by_argument == 'select a from t where b~\\:"xy"'


def test_a_dialect_can_map_its_own_functions():
    # Extending the dialect the way sqlglot dialects are extended.
    class MyQ(Q):
        class Generator(Q.Generator):
            FUNCTIONS = {**Q.Generator.FUNCTIONS, "VWAP": "wavg", "ZSCORE": ".stats.z"}

    sql = "SELECT VWAP(qty, px) AS w, ZSCORE(px) AS z FROM t"
    assert sqlglot.transpile(sql, write=MyQ)[0] == "select w:wavg[qty;px], z:.stats.z px from t"
    with pytest.raises(sqlglot.errors.UnsupportedError, match="FUNCTIONS"):
        sqlglot.transpile(sql, write="q")


def test_dialect_attributes():
    from sqlglot.dialects.dialect import NormalizationStrategy

    assert Q.NORMALIZATION_STRATEGY is NormalizationStrategy.CASE_SENSITIVE
    assert Q.NULL_ORDERING == "nulls_are_small"


REFUSED = [
    "SELECT LOG(x) FROM t",
    "INSERT INTO k VALUES (1, 2.0) ON CONFLICT DO NOTHING",
    "SELECT * FROM t WHERE CONTAINS(s, g)",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
