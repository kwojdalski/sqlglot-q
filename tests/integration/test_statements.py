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
    ("ALTER TABLE t DROP COLUMN s", "SELECT * FROM t"),
    ("ALTER TABLE u RENAME TO uu", "SELECT g, v FROM uu"),
    # Already right, kept so they stay so.
    ("UPDATE u SET v = v + 1, g = 'q' WHERE v > 15", "SELECT g, v FROM u"),
    ("DELETE FROM t WHERE g IN (SELECT g FROM u)", "SELECT g, n FROM t"),
    (
        "CREATE TABLE w (k INTEGER PRIMARY KEY, b VARCHAR); INSERT INTO w VALUES (1, 'x'), "
        "(2, 'z'); INSERT INTO w VALUES (1, 'y') ON CONFLICT (k) DO UPDATE SET b = EXCLUDED.b",
        "SELECT k, b FROM w",
    ),
]


def run_in_q(statements: list[str], check: str) -> list[tuple] | str:
    setup = "".join(f"{name}:{q_table(cols)};\n" for name, cols in DATA.items())
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


REFUSED = [
    # Written as a plain update, t.g = u.g would read g=g: every row.
    "UPDATE u SET v = t.n FROM t WHERE t.g = u.g",
]


@pytest.mark.parametrize("sql", REFUSED, ids=REFUSED)
def test_what_q_cannot_match_is_refused(sql):
    with pytest.raises(sqlglot.errors.UnsupportedError):
        sqlglot.transpile(sql, read="duckdb", write="q")
