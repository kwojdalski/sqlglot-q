# sqlglot-q

A sqlglot dialect plugin that writes q/kdb+: `sqlglot.transpile(sql,
write="q")`. It is write-only; reading q is refused.

## The property the whole design rests on

**A translation is right when q returns what the SQL returns, and nothing
else counts.** q text that looks like the SQL is not evidence: q evaluates
right to left, has its own null and rounding rules, and refuses things SQL
converts silently. So:

- Every q question is settled by running q 5 (the `q` on this machine), not
  by reading the manual. `tests/integration` does this for every construct:
  the SQL runs in DuckDB, its translation runs in q, on the same tables, and
  the answers are compared.
- A construct with no faithful q form is refused (`UnsupportedError`, the
  default here), never emitted approximately. Under `ErrorLevel.IGNORE` the
  output still contains `'unsupported`, which q signals rather than runs.
- Where q's semantics differ from SQL's, the translation pays to match SQL
  and the code says why (half-even integer casts, `count x where not null x`,
  `sublist` not `#`, typed `update`).

## Verify with

```sh
uv run pytest                      # unit + integration (integration needs q on PATH)
uv run --group orbital pytest      # also scores orbital-exported models in q
uv run ruff check . && uv run ruff format --check .
```

CI has no q, so the integration tests skip there: run them locally before
calling a change done. A new construct wants a case in
`tests/integration/test_queries.py` as well as `tests/test_q.py`; the unit
test alone only proves the text didn't change.

Running q by hand: write a script file and run `q script.q -q`, ending it in
`exit 0;`. Never feed q on stdin (it waits for input), and never run it in a
directory holding a `q.q`, which it loads at startup. `harness.run_q` does
both right.

## Where things are

| | |
|---|---|
| `src/sqlglot_q/dialect.py` | `Q(Dialect)`: wires the generator, refuses parsing |
| `src/sqlglot_q/generator.py` | `QGenerator`: one `q_<node>` method per sqlglot node; anything else is refused |
| `src/sqlglot_q/query.py` | SELECT, which in q is several expressions composed (group, having, sort, limit) |
| `src/sqlglot_q/reserved.json` | q's reserved names, which can't name a column (shared with q-lint) |
| `src/sqlglot_q/cli.py` | the `sqlglot-q` command |
| `tests/test_q.py` | the q text each construct becomes |
| `tests/integration/` | q against DuckDB on the same data; `harness.py` runs both |
| `docs/design.md` | why the generator owns dispatch, and every SQL/q difference handled |

The plugin is registered in `pyproject.toml` under
`[project.entry-points."sqlglot.dialects"]`, as `q` and `kdb`.

## Releasing

Bump `version` in `pyproject.toml` and `src/sqlglot_q/version.py`, add the
CHANGELOG entry, run the full verify above (with q), then `uv build`.
