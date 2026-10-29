"""sqlglot-q: print SQL statements as q."""

from __future__ import annotations

import argparse
import sys

import sqlglot
from sqlglot.errors import SqlglotError

from sqlglot_q.dialect import Q


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sqlglot-q", description=__doc__)
    ap.add_argument("sql", nargs="*", help="SQL text; read from stdin when absent")
    ap.add_argument("--read", default=None, help="the SQL dialect to parse (default: sqlglot's)")
    ap.add_argument(
        "--strings", action="store_true", help="string literals are q strings, not symbols"
    )
    args = ap.parse_args(argv)
    text = " ".join(args.sql) if args.sql else sys.stdin.read()
    literals = "string" if args.strings else "symbol"
    try:
        for q in sqlglot.transpile(text, read=args.read, write=Q, string_literals=literals):
            print(q)
    except SqlglotError as e:
        print(f"sqlglot-q: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
