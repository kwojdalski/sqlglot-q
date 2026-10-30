"""Statements, run: each list of SQL statements is translated and run in q,
and run in DuckDB, and the table a final query reads must be the same in
both. (The other suites check a query's answer; these check what a statement
leaves behind.)"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
import sqlglot

from tests.integration.harness import duckdb_connection, q_binary, q_string, q_table, same
from tests.integration.test_sql_more import DATA

CASES = [
    # INSERT by position: q's insert matches names, so the query's are the
    # target's; a NULL is the column's own null, not a long 0N.
    ("INSERT INTO u SELECT g, o FROM t WHERE o > 2", "SELECT g, v FROM u"),
    ("INSERT INTO u (v, g) SELECT n, g FROM t WHERE n > 5", "SELECT g, v FROM u"),
    ("INSERT INTO u VALUES (NULL, 1)", "SELECT g, v FROM u"),
    ("INSERT INTO u VALUES ('e', NULL), (NULL, 9)", "SELECT g, v FROM u"),
    ("INSERT INTO u (g, v) VALUES (NULL, NULL), ('m', 3)", "SELECT g, v FROM u"),
    # UPDATE keeps a column's type, and a correlated value is a lookup.
    ("UPDATE u SET v = v / 3", "SELECT g, v FROM u"),
    ("UPDATE t SET x = n WHERE o = 1", "SELECT g, x FROM t"),
    ("UPDATE t SET s = NULL WHERE o = 1", "SELECT g, s FROM t"),
    ("UPDATE u SET v = NULL", "SELECT g, v FROM u"),
    ("UPDATE u SET v = (SELECT MAX(n) FROM t WHERE t.g = u.g)", "SELECT g, v FROM u"),
    ("UPDATE u SET v = (SELECT COUNT(*) FROM t WHERE t.g = u.g)", "SELECT g, v FROM u"),
    # One column of several rows.
    ("CREATE TABLE w (a INTEGER); INSERT INTO w VALUES (1), (2)", "SELECT a FROM w"),
    # ALTER TABLE.
    ("ALTER TABLE u ADD COLUMN z DOUBLE", "SELECT g, z FROM u"),
    (
        "ALTER TABLE u ADD COLUMN z VARCHAR; UPDATE u SET z = 'q' WHERE v > 15",
        "SELECT g, v, z FROM u",
    ),
    ("ALTER TABLE u RENAME COLUMN v TO w", "SELECT g, w FROM u"),
    # ADD COLUMN ... DEFAULT: the default on every row, of the column's type.
    ("ALTER TABLE u ADD COLUMN z INTEGER DEFAULT 7", "SELECT g, z FROM u"),
    ("ALTER TABLE u ADD COLUMN z DOUBLE DEFAULT 1", "SELECT g, z FROM u"),
    ("ALTER TABLE u ADD COLUMN z VARCHAR DEFAULT 'hi'", "SELECT g, z FROM u"),
    ("ALTER TABLE u ADD COLUMN z DATE DEFAULT DATE '2024-01-02'", "SELECT g, z FROM u"),
    (
        "ALTER TABLE u ADD COLUMN z BIGINT DEFAULT -3; UPDATE u SET z = z + v",
        "SELECT g, z FROM u",
    ),
    ("ALTER TABLE t DROP COLUMN s", "SELECT * FROM t"),
    ("ALTER TABLE u RENAME TO uu", "SELECT g, v FROM uu"),
    # INSERT casts its values to the columns' types, where q's raised 'type.
    ("INSERT INTO u VALUES ('x', 1.9)", "SELECT g, v FROM u"),
    ("INSERT INTO u VALUES ('x', 1.9), ('y', 2.5)", "SELECT g, v FROM u"),
    ("INSERT INTO u (v, g) VALUES (2.5, 'q')", "SELECT g, v FROM u"),
    (
        "CREATE TABLE w (a INTEGER NOT NULL, b DOUBLE); INSERT INTO w VALUES (1, 2)",
        "SELECT a, b FROM w",
    ),
    (
        "CREATE TABLE w (a DATE, b TIMESTAMP); "
        "INSERT INTO w VALUES (DATE '2024-01-02', DATE '2024-01-03')",
        "SELECT a, b FROM w",
    ),
    (
        "CREATE TABLE w (k BIGINT PRIMARY KEY, c DOUBLE); INSERT INTO w VALUES (1, NULL), "
        "(2.0, 3); INSERT OR REPLACE INTO w VALUES (1, 7)",
        "SELECT k, c FROM w",
    ),
    ("INSERT INTO u SELECT 'z', COUNT(*) FROM t", "SELECT g, v FROM u"),
    # ALTER COLUMN ... TYPE: each value cast.
    ("ALTER TABLE u ALTER COLUMN v TYPE DOUBLE", "SELECT g, v FROM u"),
    ("ALTER TABLE u ALTER COLUMN v SET DATA TYPE VARCHAR", "SELECT g, v FROM u"),
    ("ALTER TABLE t ALTER COLUMN x TYPE BIGINT", "SELECT g, x FROM t"),
    ("ALTER TABLE t ALTER COLUMN n TYPE DOUBLE; UPDATE t SET n = n / 4", "SELECT g, n FROM t"),
    ("ALTER TABLE t ALTER COLUMN d TYPE TIMESTAMP", "SELECT g, d FROM t"),
    # IF NOT EXISTS keeps an existing table; a view is q's view.
    ("CREATE TABLE IF NOT EXISTS u (g VARCHAR, v BIGINT)", "SELECT g, v FROM u"),
    ("CREATE TABLE IF NOT EXISTS w (a BIGINT); INSERT INTO w VALUES (1)", "SELECT a FROM w"),
    ("CREATE VIEW vw AS SELECT g, v FROM u WHERE v > 15", "SELECT g, v FROM vw"),
    ("CREATE OR REPLACE TABLE u AS SELECT g, n AS v FROM t WHERE n > 5", "SELECT g, v FROM u"),
    # A statement's own WITH, written in.
    (
        "WITH q AS (SELECT g, n FROM t WHERE n > 2) INSERT INTO u SELECT g, n FROM q",
        "SELECT g, v FROM u",
    ),
    (
        "WITH q AS (SELECT g FROM u WHERE v > 15) UPDATE t SET n = 0 WHERE g IN (SELECT g FROM q)",
        "SELECT g, n FROM t",
    ),
    (
        "WITH q AS (SELECT g FROM u) DELETE FROM t WHERE g IN (SELECT g FROM q)",
        "SELECT g, n FROM t",
    ),
    # Already right, kept so they stay so.
    ("UPDATE u SET v = v + 1, g = 'q' WHERE v > 15", "SELECT g, v FROM u"),
    ("DELETE FROM t WHERE g IN (SELECT g FROM u)", "SELECT g, n FROM t"),
    (
        "CREATE TABLE w (k INTEGER PRIMARY KEY, b VARCHAR); INSERT INTO w VALUES (1, 'x'), "
        "(2, 'z'); INSERT INTO w VALUES (1, 'y') ON CONFLICT (k) DO UPDATE SET b = EXCLUDED.b",
        "SELECT k, b FROM w",
    ),
]


def run_in_q(statements: list[str], check: str, data=DATA) -> list[tuple] | str:
    setup = "".join(f"{name}:{q_table(cols)};\n" for name, cols in data.items())
    lines = ['system "P 0";', setup]
    for q in statements:
        lines.append(f"@[value;{q_string(q)};{{-1 .j.j `ok`error!(0b;x);exit 0}}];")
    lines.append(f"r:value {q_string(check)}; r:$[99h=type r;0!r;r];")
    lines.append("-1 .j.j `ok`rows!(1b;flip value flip r);")
    lines.append("exit 0;")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "run.q"
        script.write_text("\n".join(lines) + "\n")
        out = subprocess.run(
            [q_binary(), str(script), "-q"], cwd=tmp, capture_output=True, text=True, timeout=60
        )
    answer = json.loads(out.stdout.strip().splitlines()[-1])
    return [tuple(r) for r in answer["rows"]] if answer["ok"] else f"'{answer['error']}"


@pytest.mark.parametrize(("sql", "check"), CASES, ids=[s for s, _ in CASES])
def test_statement_leaves_what_duckdb_leaves(sql, check):
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    qs = [sqlglot.transpile(s, read="duckdb", write="q")[0] for s in statements]
    got = run_in_q(qs, sqlglot.transpile(check, read="duckdb", write="q")[0])
    assert not isinstance(got, str), f"q refused {qs}: {got}"
    con = duckdb_connection(DATA)
    for s in statements:
        con.execute(s)
    expected = [tuple(r) for r in con.execute(check).fetchall()]
    assert same(got, expected, False), f"{qs}\nq:      {got}\nduckdb: {expected}"


# String mode: a string is a list of characters, one value per row there.
STRING_CASES = [
    ("INSERT INTO s VALUES (9, NULL, 'zz'), (10, 'q', NULL)", "SELECT a, w, v FROM s"),
    ("UPDATE s SET w = 'up' WHERE a > 3", "SELECT a, w FROM s"),
    ("UPDATE s SET w = NULL WHERE a = 1", "SELECT a, w FROM s"),
    ("UPDATE s SET w = UPPER(v)", "SELECT a, w FROM s"),
    (
        "ALTER TABLE s ADD COLUMN z VARCHAR; UPDATE s SET z = 'q' WHERE a > 3",
        "SELECT a, z FROM s",
    ),
    ("DELETE FROM s WHERE w IS NULL", "SELECT a, w FROM s"),
    ("ALTER TABLE s ALTER COLUMN a TYPE VARCHAR", "SELECT a, w FROM s"),
    ("INSERT INTO s (a, w) VALUES (1.6, 'q')", "SELECT a, w FROM s"),
    ("ALTER TABLE s ADD COLUMN z VARCHAR DEFAULT 'hi'", "SELECT a, z FROM s"),
    (
        "ALTER TABLE s ADD COLUMN z VARCHAR DEFAULT 'h'; UPDATE s SET z = w WHERE a > 3",
        "SELECT a, z FROM s",
    ),
]


@pytest.mark.parametrize(("sql", "check"), STRING_CASES, ids=[s for s, _ in STRING_CASES])
def test_string_mode_statement_leaves_what_duckdb_leaves(sql, check):
    from tests.integration.test_string_mode import DATA as STRINGS

    dialect = sqlglot.Dialect.get_or_raise("q, string_literals=string")
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    qs = [sqlglot.transpile(s, read="duckdb", write=dialect)[0] for s in statements]
    got = run_in_q(qs, sqlglot.transpile(check, read="duckdb", write=dialect)[0], STRINGS)
    assert not isinstance(got, str), f"q refused {qs}: {got}"
    con = duckdb_connection(STRINGS)
    for s in statements:
        con.execute(s)
    expected = [tuple(r) for r in con.execute(check).fetchall()]
    assert same(got, expected, False), f"{qs}\nq:      {got}\nduckdb: {expected}"


REFUSED = [
    # Written as a plain update (delete), t.g = u.g would read g=g: every row.
    "UPDATE u SET v = t.n FROM t WHERE t.g = u.g",
    "DELETE FROM t USING u WHERE t.g = u.g",
    # q's insert, update and delete give back no rows.
    "INSERT INTO u VALUES ('e', 50) RETURNING g",
    "UPDATE u SET v = 1 RETURNING v",
    # q has no column defaults: an insert of the other columns has none to use.
    "CREATE TABLE w (a INTEGER DEFAULT 1, b INTEGER)",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
