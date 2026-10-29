"""Conversions both ways have to settle: after one round trip, another
changes nothing, and the same input always gives the same output.

SQL -> q -> SQL -> q: the two q texts are equal, the two SQL texts are equal,
and the SQL read back from q means what the original SQL meant (DuckDB gives
the same rows for both).

q -> SQL -> q -> SQL: the same fixed point, over qSQL from q-lint's
examples/showcase.q and the public q corpus (tests/fixtures/q_examples.q),
and the q that comes back returns what the original did (run in q).

The first round may normalise (a keyed `select ... by` becomes `0!select ...
by`, `1000f` becomes `1000.0`); it is the second that must be the identity.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot.errors import ParseError

from tests.integration.harness import q_binary, run_duckdb, run_q, same
from tests.integration.test_calendar_text import DATA as CALENDAR_DATA
from tests.integration.test_calendar_text import QUERIES as CALENDAR
from tests.integration.test_functions import DATA as FUNCTIONED
from tests.integration.test_functions import QUERIES as FUNCTIONS
from tests.integration.test_nulls import DATA as NULL_DATA
from tests.integration.test_nulls import QUERIES as NULLS
from tests.integration.test_nulls_more import DATA as MORE_NULL_DATA
from tests.integration.test_nulls_more import QUERIES as MORE_NULLS
from tests.integration.test_nulls_more import ordered as more_ordered
from tests.integration.test_queries import QUERIES, STATEMENTS, TABLES
from tests.integration.test_sql_more import DATA as MORE_DATA
from tests.integration.test_sql_more import QUERIES as MORE
from tests.integration.test_sql_sweep import QUERIES as SWEEP
from tests.integration.test_sqlglot_native import DATA as NATIVE_DATA
from tests.integration.test_sqlglot_native import QUERIES as NATIVE
from tests.integration.test_sqlglot_native import SCRIPTS
from tests.integration.test_string_mode import QUERIES as STRING_MODE
from tests.integration.test_string_mode import WRITE as STRING_WRITE
from tests.integration.test_temporal import QUERIES as TEMPORAL
from tests.integration.test_temporal import TICKS
from tests.integration.test_windows import DATA as WINDOWED
from tests.integration.test_windows import QUERIES as WINDOWS

FIXTURES = Path(__file__).parent / "fixtures"


def statements(path: Path) -> list[str]:
    return [
        line for line in path.read_text().splitlines() if line.strip() and not line.startswith("/")
    ]


# SQL is DuckDB's throughout: it is the engine that checks it, and the one
# with TIME_BUCKET and ASOF JOIN.
def to_q(sql: str) -> str:
    return sqlglot.transpile(sql, read="duckdb", write="q")[0]


def to_sql(q: str) -> str:
    return sqlglot.parse_one(q, read="q").sql("duckdb")


SQL = (
    [sql for sql, _ in QUERIES]
    + [s for s, _ in STATEMENTS]
    + [
        "CREATE TABLE t (a INT, b VARCHAR, c DOUBLE)",
        "DROP TABLE t",
        "CREATE TABLE big AS SELECT a FROM t WHERE a > 3",
        'SELECT a AS "p.0", b AS c FROM t',
        "SELECT a FROM t WHERE b ILIKE 'X%'",
    ]
    + [sql for sql, _ in TEMPORAL]
    + WINDOWS
    + [sql for sql, _ in FUNCTIONS]
    + [sql for sql, _ in NATIVE]
    + [sql for script in SCRIPTS for sql in script]
    + NULLS
    + MORE_NULLS
    + CALENDAR
    + MORE
    + SWEEP
)
from tests.integration.test_reading import QUERIES as READING  # noqa: E402

Q_EXAMPLES = statements(FIXTURES / "q_examples.q") + READING
Q_REFUSED = statements(FIXTURES / "q_refused.q")


@pytest.mark.parametrize("sql", SQL, ids=SQL)
def test_sql_to_q_and_back_settles(sql):
    q1 = to_q(sql)
    sql1 = to_sql(q1)
    q2 = to_q(sql1)
    sql2 = to_sql(q2)
    assert q2 == q1, f"\nsql:  {sql}\nq1:   {q1}\nsql1: {sql1}\nq2:   {q2}"
    assert sql2 == sql1, f"\nq1:   {q1}\nsql1: {sql1}\nsql2: {sql2}"


SELECTS = [(sql, ordered) for sql, ordered in QUERIES if sql.startswith(("SELECT", "WITH"))]
SELECTS += TEMPORAL
SELECTS += [(sql, "LIMIT" in sql) for sql in WINDOWS]
SELECTS += FUNCTIONS
SELECTS += NATIVE
SELECTS += [(sql, "ORDER BY" in sql) for sql in NULLS]
SELECTS += [(sql, more_ordered(sql)) for sql in MORE_NULLS]
SELECTS += [(sql, False) for sql in CALENDAR]
SELECTS += [(sql, False) for sql in MORE]
SELECTS += [(sql, False) for sql in SWEEP]


@pytest.mark.parametrize(("sql", "ordered"), SELECTS, ids=[s for s, _ in SELECTS])
def test_sql_read_back_from_q_means_the_same(sql, ordered):
    back = to_sql(to_q(sql))
    tables = (
        TICKS
        if " trades" in sql
        else WINDOWED
        if sql in WINDOWS
        else FUNCTIONED
        if (sql, ordered) in FUNCTIONS
        else NATIVE_DATA
        if (sql, ordered) in NATIVE
        else NULL_DATA
        if sql in NULLS
        else MORE_NULL_DATA
        if sql in MORE_NULLS
        else CALENDAR_DATA
        if sql in CALENDAR
        else MORE_DATA
        if sql in MORE or sql in SWEEP
        else TABLES
    )
    assert same(run_duckdb(tables, back), run_duckdb(tables, sql), ordered), back


@pytest.mark.parametrize("q", Q_EXAMPLES, ids=Q_EXAMPLES)
def test_q_to_sql_and_back_settles(q):
    sql1 = to_sql(q)
    q1 = to_q(sql1)
    sql2 = to_sql(q1)
    q2 = to_q(sql2)
    assert sql2 == sql1, f"\nq:    {q}\nsql1: {sql1}\nq1:   {q1}\nsql2: {sql2}"
    assert q2 == q1, f"\nsql1: {sql1}\nq1:   {q1}\nq2:   {q2}"


@pytest.mark.parametrize("q", Q_REFUSED, ids=Q_REFUSED)
def test_q_without_a_sql_form_is_refused(q):
    with pytest.raises(ParseError):
        to_sql(q)


# The showcase's tables, so its examples can run before and after.
SHOWCASE = {
    "trades": {
        "sym": ["EUR", "USD", "EUR", "abX", "aab", "JPY"],
        "size": [1, 5, 200, 1, 3, 1],
        "px": [1.5, 2.5, 1.5, 4.0, 2.0, 9.5],
        "side": [0, 1, 0, 0, 1, 0],
    },
    "quotes": {"px": [1.0, 2.0, 1.0], "size": [50, 150, 300], "sym": ["EUR", "USD", "EUR"]},
}
RUNNABLE = [q for q in Q_EXAMPLES if " from trades" in q or " from quotes" in q]


@pytest.mark.skipif(not q_binary(), reason="q is not on PATH")
@pytest.mark.parametrize("q", RUNNABLE, ids=RUNNABLE)
def test_q_back_from_sql_returns_what_the_original_did(q):
    back = to_q(to_sql(q))
    # Tick queries run on the tick tables; the showcase's on its own.
    tables = TICKS if any(w in q for w in ("time", "day", "qty", "aj[")) else SHOWCASE
    before, after = run_q(tables, [q, back])
    if isinstance(before, str):
        pytest.skip(f"q refuses the original too: {before}")
    assert not isinstance(after, str), f"q refused {back}: {after}"
    assert same(after, before, ordered=False), f"\n{q}\n{back}"


SCRIPT = """
import json, sqlglot
sql, q = json.loads(input())
out = {s: sqlglot.transpile(s, read="duckdb", write="q")[0] for s in sql}
out.update({x: sqlglot.parse_one(x, read="q").sql("duckdb") for x in q})
print(json.dumps(out, sort_keys=True))
"""


def test_conversions_are_deterministic():
    # Fresh interpreters with different hash seeds: set and dict ordering
    # must never reach the output.
    payload = json.dumps([SQL, Q_EXAMPLES])
    runs = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", SCRIPT], input=payload, capture_output=True, text=True, env=env
        )
        assert out.returncode == 0, out.stderr
        runs.add(out.stdout)
    assert len(runs) == 1
    # And within one process, the same call twice.
    assert [to_q(s) for s in SQL] == [to_q(s) for s in SQL]


def test_a_dialects_own_functions_round_trip():
    # A subclass's FUNCTIONS are read back as well as written.
    from sqlglot_q import Q

    class MyQ(Q):
        class Generator(Q.Generator):
            FUNCTIONS = {**Q.Generator.FUNCTIONS, "VWAP": "wavg"}

    sql = "SELECT VWAP(qty, px) AS w FROM t"
    q = sqlglot.transpile(sql, write=MyQ)[0]
    assert q == "select w:wavg[qty;px] from t"
    assert sqlglot.parse_one(q, read=MyQ).sql() == sql


@pytest.mark.parametrize("sql", STRING_MODE, ids=STRING_MODE)
def test_string_mode_round_trips(sql):
    q1 = sqlglot.transpile(sql, read="duckdb", write=STRING_WRITE)[0]
    sql1 = to_sql(q1)
    q2 = sqlglot.transpile(sql1, read="duckdb", write=STRING_WRITE)[0]
    assert q2 == q1, f"\nsql:  {sql}\nq1:   {q1}\nsql1: {sql1}\nq2:   {q2}"
