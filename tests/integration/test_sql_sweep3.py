"""The third sweep's queries: each runs in DuckDB as SQL and in q as its
translation, and reads back to the same q (tests/test_roundtrip.py)."""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import run_duckdb, run_q, same
from tests.integration.test_calendar_text import DATA as CALENDAR
from tests.integration.test_sql_more import DATA

QUERIES = [
    # A join of an unnamed query: its columns are not the other side's.
    "SELECT g, n FROM (SELECT g, n FROM t ORDER BY n LIMIT 3) AS q JOIN u USING (g)",
    # A constant ORDER BY term sorts nothing.
    "SELECT g, n FROM t ORDER BY (SELECT MAX(v) FROM u), n",
    # Row values, compared part by part.
    "SELECT * FROM t WHERE (g, n) = ('a', 3)",
    "SELECT * FROM t WHERE (g, n) <> ('a', 3)",
    "SELECT * FROM t WHERE (g, n) IN (('a', 3), ('b', 5))",
    # x // y of a float is DuckDB's plain division, of integers toward zero.
    "SELECT x // 1 AS a, x // 2 AS b, n // -2 AS c, 7 // 2 AS d FROM t",
    # Functions.
    "SELECT ISFINITE(x) AS a FROM t WHERE x IS NOT NULL",
    "SELECT ROUND_EVEN(x, 0) AS a, ROUND_EVEN(x, 1) AS b FROM t",
    "SELECT SIGNBIT(x) AS a FROM t WHERE x IS NOT NULL",
    "SELECT WEEKOFYEAR(d) AS a FROM t",
    "SELECT EPOCH_US(CAST(d AS TIMESTAMP)) AS a, EPOCH_NS(CAST(d AS TIMESTAMP)) AS b FROM t",
    "SELECT MAKE_TIME(10, o, 1.5) AS a FROM t",
    "SELECT PREFIX(s, 'a') AS a, SUFFIX(s, 'z') AS b FROM t",
    "SELECT UNICODE(s) AS a FROM t",
    "SELECT LEVENSHTEIN(s, 'abc') AS a FROM t",
    "SELECT BIT_LENGTH(s) AS a FROM t",
    "SELECT STRING_SPLIT(s, '_') AS a FROM t",
    "SELECT TRUNC(x, 1) AS a, TRUNC(n, -1) AS b FROM t",
    # A window in a CASE: a value per row, not a constant to lift.
    "SELECT g, n, CASE WHEN ROW_NUMBER() OVER (PARTITION BY g ORDER BY n) = 1 "
    "THEN 'first' ELSE 'rest' END AS k FROM t",
    # UNION ALL BY NAME: q's uj.
    "SELECT g, n FROM t UNION ALL BY NAME SELECT v, g FROM u",
    # WITH over a set operation: its CTEs written in, as a select's.
    "WITH a AS (SELECT g FROM t WHERE n > 0), b AS (SELECT g FROM u) "
    "SELECT g FROM a INTERSECT SELECT g FROM b",
    "WITH a AS (SELECT g, n, RANK() OVER (PARTITION BY g ORDER BY n DESC) AS r FROM t) "
    "SELECT g, n FROM a WHERE r = 1 UNION ALL SELECT g, v FROM u",
]

# On the calendar table, which has a boolean column f.
CALENDAR_QUERIES = [
    # Text as a boolean, as SQL reads it: q's "B"$ reads only t and 1.
    "SELECT a, CAST('true' AS BOOLEAN) AS z, CAST('No' AS BOOLEAN) AS w FROM t",
    # OR of a boolean column, q's or: f|x reads back as GREATEST.
    "SELECT a FROM t WHERE f OR n > 5",
    "SELECT a FROM t WHERE f AND x > 0",
    # TRY_CAST of text to a date: q's parse of it.
    "SELECT TRY_CAST(b AS DATE) AS d, TRY_CAST(CAST(d AS VARCHAR) AS DATE) AS e FROM t",
]

ONE_WAY: set[str] = set()
if tuple(int(p) for p in sqlglot.__version__.split(".")[:2]) < (30, 1):
    # sqlglot 30.0 writes DuckDB's TRUNC without its places ("not supported").
    ONE_WAY.add("SELECT TRUNC(x, 1) AS a, TRUNC(n, -1) AS b FROM t")


@pytest.fixture(scope="module")
def answers():
    out = {}
    for data, queries in ((DATA, QUERIES), (CALENDAR, CALENDAR_QUERIES)):
        qs = [sqlglot.transpile(sql, read="duckdb", write="q")[0] for sql in queries]
        out |= dict(zip(queries, zip(qs, run_q(data, qs), strict=True), strict=True))
    return out


@pytest.mark.parametrize("sql", QUERIES + CALENDAR_QUERIES, ids=QUERIES + CALENDAR_QUERIES)
def test_q_returns_what_duckdb_returns(answers, sql):
    q, got = answers[sql]
    expected = run_duckdb(CALENDAR if sql in CALENDAR_QUERIES else DATA, sql)
    assert not isinstance(got, str), f"q refused {q}: {got}"
    ordered = bool(sqlglot.parse_one(sql, read="duckdb").args.get("order"))
    assert same(got, expected, ordered), f"{q}\nq:      {got}\nduckdb: {expected}"


# Hand-written q, read: run in q, and as the SQL it reads as in DuckDB.
from tests.integration.test_temporal import TICKS  # noqa: E402

READ_RUN = [
    # q's parse of text, "D"$: TRY_CAST, null where it is no date.
    'select sym, d:"D"$string sym, n:"J"$string sym from trades',
    # count each string s: each text's length.
    "select sym, c:count each string sym from trades",
    # Functional selects, ?[t;c;b;a].
    "?[trades;enlist (>;`qty;100);0b;()]",
    "?[trades;((>;`qty;10);(=;`sym;enlist `a));0b;()]",
    "?[trades;();(enlist `sym)!enlist `sym;(enlist `n)!enlist (count;`i)]",
    "?[trades;();(enlist `sym)!enlist `sym;`mx`tot!((max;`px);(sum;`qty))]",
    "?[trades;enlist (<;`px;2);0b;`sym`px!`sym`px]",
    "?[trades;();1b;(enlist `sym)!enlist `sym]",
    # lj and ij with a keyed grouping (its keys once each): LEFT and inner
    # joins; uj of two tables, UNION ALL BY NAME; a join on its own.
    "trades lj select b:max bid by sym from quotes",
    "select sym, px, b from trades lj select b:max bid by sym from quotes",
    "trades ij select b:max bid by sym from quotes",
    "select from (trades ij select b:max bid by sym from quotes) where b>2",
    "trades lj `sym xkey select b:max bid by sym from quotes",
    "(select sym, px from trades) uj select sym, bid from quotes",
    "aj[`sym`time;trades;quotes]",
    "ej[`sym;trades;select sym, bp:bid from quotes]",
]

REFUSED = [
    # `$ trims a symbol's ends: ' x' would be `x, and s = ' x' find x.
    "SELECT g, n FROM t WHERE s = ' x'",
    "SELECT LENGTH(' x ') AS c FROM t",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")


REFUSED_READ = [
    # `k xkey q and n!q may repeat a key, which lj matches once and SQL's
    # join every time.
    "trades lj `sym xkey select sym, b:bid from quotes",
    "trades lj 1!select sym, b:bid from quotes",
]


@pytest.mark.parametrize("q", REFUSED_READ, ids=REFUSED_READ)
def test_q_without_a_sql_form_is_refused(q):
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.transpile(q, read="q", write="duckdb")


@pytest.mark.parametrize("q", READ_RUN, ids=READ_RUN)
def test_q_read_back_runs_the_same(q):
    got = run_q(TICKS, [q])[0]
    assert not isinstance(got, str), got
    sql = sqlglot.transpile(q, read="q", write="duckdb")[0]
    assert same(got, run_duckdb(TICKS, sql), False), sql
