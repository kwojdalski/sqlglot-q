"""Run q, and the same data through DuckDB, so a translation is judged by
what q returns rather than by what its text looks like.

q is run on a script file, never on stdin (it waits for more input there),
and from a fresh directory, because q loads a q.q from the directory it
starts in.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

Rows = list[tuple]


def q_binary() -> str | None:
    return shutil.which("q")


class Strings(list):
    """A text column as q strings (char vectors), for string_literals="string";
    a plain list of str is a symbol column. q has no null string: None is ""."""


def q_column(values: list) -> str:
    """A Python column as a q list literal of one type. One value is a
    one-item list (enlist), not the atom q would read the bare literal as."""
    text = q_items(values)
    one = len(values) == 1 and not text.startswith(("`$(", "("))
    return f"enlist {text}" if one else text


def q_items(values: list) -> str:
    if isinstance(values, Strings):
        # (),"x" keeps a one-character string a string, not a char atom.
        return "(" + ";".join("()," + json.dumps(v or "") for v in values) + ")"
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, dt.datetime) for v in present):
        return " ".join(
            "0N" if v is None else v.strftime("%Y.%m.%dD%H:%M:%S.%f000") for v in values
        )
    if present and all(isinstance(v, dt.date) for v in present):
        return " ".join("0N" if v is None else v.strftime("%Y.%m.%d") for v in values)
    if present and all(isinstance(v, dt.time) for v in present):
        return " ".join("0N" if v is None else v.strftime("%H:%M:%S.%f")[:12] for v in values)
    if all(isinstance(v, bool) for v in present):
        return "".join("1" if v else "0" for v in values) + "b" if values else "`boolean$()"
    if all(isinstance(v, int) for v in present):
        return " ".join("0N" if v is None else str(v) for v in values)
    if all(isinstance(v, (int, float)) for v in present):
        return (
            "`float$" + "(" + ";".join("0n" if v is None else repr(float(v)) for v in values) + ")"
        )
    if all(isinstance(v, str) for v in present):
        # (),"x" keeps a one-character string a string, not a char atom.
        cells = ['(),""' if v is None else "()," + json.dumps(v) for v in values]
        return "`$(" + ";".join(cells) + ")"
    raise TypeError(f"no q column for {values!r}")


def q_table(columns: dict[str, list]) -> str:
    body = ";".join(f"{name}:{q_column(values)}" for name, values in columns.items())
    return f"([] {body})"


def q_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def run_q(tables: dict[str, dict[str, list]], queries: list[str]) -> list[Rows | str]:
    """A string column comes back from q's JSON as text, as a symbol does."""
    """Evaluate each query on fresh copies of the tables; a result is its rows,
    or the error q signalled, as a string."""
    binary = q_binary()
    assert binary, "q is not on PATH"
    setup = "".join(f"{name}:{q_table(cols)};\n" for name, cols in tables.items())
    lines = [
        'system "P 0";',
        "run:{[setup;q] value setup; r:value q;",
        "  $[98h=type r; r; 99h=type r; 0!r; ([] x:enlist r)]};",
        "show_:{-1 .j.j `ok`rows!(1b; flip value flip x)};",
        "fail:{-1 .j.j `ok`error!(0b; x)};",
    ]
    for q in queries:
        lines.append(f"@[{{show_ run[x;y]}}[{q_string(setup)}];{q_string(q)};fail];")
    lines.append("exit 0;")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "run.q"
        script.write_text("\n".join(lines) + "\n")
        out = subprocess.run(
            [binary, str(script), "-q"], cwd=tmp, capture_output=True, text=True, timeout=60
        )
    results: list[Rows | str] = []
    for line in out.stdout.splitlines():
        # q's JSON writes float infinities as inf, which JSON has no word for.
        answer = json.loads(re.sub(r"(?<![A-Za-z\"])(-?)inf(?![A-Za-z\"])", r"\1Infinity", line))
        if answer["ok"]:
            results.append([tuple(row) for row in answer["rows"]])
        else:
            results.append(f"'{answer['error']}")
    assert len(results) == len(queries), out.stderr or out.stdout
    return results


def duckdb_connection(tables: dict[str, dict[str, list]]):
    """An in-memory DuckDB holding the same tables q gets."""
    import duckdb

    con = duckdb.connect()
    for name, cols in tables.items():
        decl = ", ".join(f"{n} {duck_type(v)}" for n, v in cols.items())
        con.execute(f"CREATE TABLE {name} ({decl})")
        rows = list(zip(*cols.values(), strict=True))
        if rows:
            con.executemany(f"INSERT INTO {name} VALUES ({', '.join('?' for _ in cols)})", rows)
    return con


def run_duckdb(tables: dict[str, dict[str, list]], sql: str) -> Rows:
    return [tuple(r) for r in duckdb_connection(tables).execute(sql).fetchall()]


def duck_type(values: list) -> str:
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, dt.datetime) for v in present):
        return "TIMESTAMP"
    if present and all(isinstance(v, dt.date) for v in present):
        return "DATE"
    if present and all(isinstance(v, dt.time) for v in present):
        return "TIME"
    if all(isinstance(v, bool) for v in present):
        return "BOOLEAN"
    if all(isinstance(v, int) for v in present):
        return "BIGINT"
    if all(isinstance(v, (int, float)) for v in present):
        return "DOUBLE"
    return "VARCHAR"


def normal(rows: Rows) -> Rows:
    """Rows with q's nulls as None and floats rounded, so the two engines'
    answers compare."""

    def cell(v):
        if isinstance(v, bool):
            return v
        # A duration: DuckDB's timedelta and q's timespan 0D09:30:00.000000000.
        if isinstance(v, dt.timedelta):
            return round(v.total_seconds(), 6)
        if isinstance(v, str) and SPAN.match(v):
            sign, days, h, m, sec = SPAN.match(v).groups()
            total = int(days) * 86400 + int(h) * 3600 + int(m) * 60 + float(sec)
            return round(-total if sign else total, 6)
        if isinstance(v, (dt.date, dt.time)):
            v = v.isoformat()
        if isinstance(v, str) and TEMPORAL.match(v):
            # q's JSON writes 2024-01-15T09:30:00.000000000 and 09:30:00.000;
            # Python's isoformat drops the zero fraction.
            v = re.sub(r"\.?0+$", "", v) if "." in v else v
            return v.replace(" ", "T")
        if isinstance(v, (int, float)):
            # q's count is an int where DuckDB's is a bigint, and its avg of
            # longs a float: compare values, not types.
            return None if math.isnan(v) else round(float(v), 9)
        if v == "":
            return None
        return v

    return [tuple(cell(v) for v in row) for row in rows]


SPAN = re.compile(r"(-?)(\d+)D(\d\d):(\d\d):(\d\d(?:\.\d+)?)$")
TEMPORAL = re.compile(
    r"(\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(\.\d+)?)?|\d{2}:\d{2}:\d{2}(\.\d+)?)$"
)


def same(a: Rows, b: Rows, ordered: bool) -> bool:
    a, b = normal(a), normal(b)
    if not ordered:
        a, b = sorted(a, key=repr), sorted(b, key=repr)
    return a == b
