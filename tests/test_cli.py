"""The sqlglot-q command, both ways, and python -m sqlglot_q."""

from __future__ import annotations

import subprocess
import sys

from sqlglot_q.cli import main


def test_sql_to_q(capsys):
    assert main(["SELECT a FROM t WHERE b > 1; SELECT c FROM u"]) == 0
    assert capsys.readouterr().out.splitlines() == ["select a from t where b>1", "select c from u"]


def test_q_to_sql(capsys):
    assert main(["--from-q", "select avg px by sym from trades"]) == 0
    assert capsys.readouterr().out.strip() == "SELECT sym, AVG(px) FROM trades GROUP BY sym"
    assert main(["--from-q", "--write", "postgres", "select n:count i by g from t"]) == 0
    assert capsys.readouterr().out.strip() == "SELECT g, COUNT(*) AS n FROM t GROUP BY g"


def test_refusal_is_an_error(capsys):
    assert main(["--from-q", "select from t where i<3"]) == 1
    assert "row index" in capsys.readouterr().err


def test_python_m():
    out = subprocess.run(
        [sys.executable, "-m", "sqlglot_q", "SELECT a FROM t"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "select a from t"
